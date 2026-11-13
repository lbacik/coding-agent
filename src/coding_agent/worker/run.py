"""The Worker Supervisor: startup, the poll loop, intake and halting.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (the
Worker's lifecycle: startup in order — validate the Workflow Configuration,
take the exclusive volume lock, open the operator store, verify the Skill
Bundle, run the non-mutating GitHub checks of §18, run the Provider
Capability Assertion, open the Run Ledger, then settle and finalise every
unfinished Attempt; the poll loop — sleep for the poll interval only when a
sweep claimed nothing, select again immediately after an Attempt ends;
SIGTERM stops intake at once and the process then exits; intake — while the
ledger holds an Attempt that is not ``finished`` or ``released`` the Worker
selects no new work; a recovery write that fails is retried on the next poll
cycle, and three in a row halt the Worker; halting — a Credential Failure
halts at once with a non-zero exit, three consecutive
infrastructure-class failures halt only after the third Attempt is
finalized, the counter resetting after any Attempt without such a failure).

Matrix row ids: CFG-38, CFG-41, CFG-42, REC-11, OPC-7; and the retained
lifecycle
rows (see the acceptance matrix).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from coding_agent.github.errors import (
    CredentialFailure,
    GitHubFailure,
    PermissionRefusal,
    RateLimitExhausted,
)
from coding_agent.inputs.authorisation import IdentityMode
from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    sweep_and_claim,
)
from coding_agent.inputs.gates import GateResult, check_gate
from coding_agent.inputs.github import StepTrackerError
from coding_agent.inputs.numbering import parse_attempt_id
from coding_agent.inputs.pinning import PinnedInput
from coding_agent.inputs.port import InputType, StepTrackerPort, TrackedInput
from coding_agent.ledger.store import AttemptPin, RunLedger
from coding_agent.provider.observed_identity import PIN_VIOLATION_REASON
from coding_agent.workflow.effective import (
    PinnedConfigurationRefused,
    effective_base_branch,
    effective_end_labels,
    effective_input_type,
    effective_selection_label,
    require_full_effective_configuration,
)
from coding_agent.operator.events import EVENT_READY
from coding_agent.operator.live import LiveView
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.step_recovery import (
    GateCheck,
    RecoveredAttempt,
    decide_restart_outcome,
    recover_attempt,
    recover_unconfirmed_attempt,
)
from coding_agent.worker.volume import MIN_FREE_BYTES, volume_free_bytes
from coding_agent.workflow.v2_schema import V2Workflow

CREDENTIAL_REASON = "credential-failure"
"""The Terminal Outcome reason that halts the Worker at once (§4.4, §16.3)."""

HALT_PIN_VIOLATION = "pin-violation"
"""Halt reason: a pin violation recorded by an Attempt (§4.4, §16.3)."""

INFRASTRUCTURE_PREFIX = "infrastructure-failure"
"""The Terminal Outcome reason prefix counted toward the halt-after-three (§4.4)."""

MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES = 3
"""Halting bound: three consecutive infrastructure-class failures (§4.4)."""

HALT_STARTUP_CHECK_REFUSED = "startup-check-refused"
"""Halt reason: a refused startup check after the volume lock."""

HALT_RECOVERY_REFUSED = "recovery-refused"
"""Halt reason: an unfinished Attempt that cannot be recovered."""

HALT_CREDENTIAL_FAILURE = "credential-failure"
"""Halt reason: a Credential Failure in selection, an Attempt or recovery."""

HALT_INFRASTRUCTURE_FAILURE = "infrastructure-failure"
"""Halt reason: three consecutive infrastructure-class failures."""

HALT_RECOVERY_WRITE_FAILURE = "recovery-write-failure"
"""Halt reason: three consecutive recovery write failures."""

HALT_CLAIM_INTERRUPTED = "claim-interrupted"
"""Halt reason: a Claim interrupted after the Attempt opened."""

HALT_LEGACY_ATTEMPT = "legacy-attempt"
"""Halt reason: an unfinished MVP Attempt on a step-workflow volume (§16.4)."""


def is_credential_reason(reason: str) -> bool:
    """Whether ``reason`` is the Credential Failure that halts at once (§4.4)."""
    return reason == CREDENTIAL_REASON or reason.startswith(CREDENTIAL_REASON + ":")


def is_pin_violation_reason(reason: str) -> bool:
    """Whether ``reason`` is the asserted pin violation that halts (§4.4, §16.3).

    A pin violation is an operator configuration fault that would repeat in
    every Attempt, so the Worker halts at once, as it does on a Credential
    Failure. The Workflow never routes it.
    """
    return reason == PIN_VIOLATION_REASON


def is_infrastructure_reason(reason: str) -> bool:
    """Whether ``reason`` counts toward the halt-after-three (§4.4).

    The infrastructure classes are setup, git, GitHub API and provider;
    every ``infrastructure-failure`` reason the Attempt records names its
    class after the colon, so the prefix decides.
    """
    return reason == INFRASTRUCTURE_PREFIX or reason.startswith(
        INFRASTRUCTURE_PREFIX + ":"
    )


def _halt_after_three(consecutive_infrastructure_failures: int) -> bool:
    """Whether the halt-after-three bound is reached (§4.4).

    One predicate for both counting sites — the escaped-exception path and
    the recorded-reason path — so the bound cannot drift between them.
    """
    return (
        consecutive_infrastructure_failures >= MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES
    )


def _failure_class(exc: BaseException) -> str:
    """The infrastructure class an escaped exception belongs to (§4.4)."""
    if isinstance(exc, GitFailure):
        return "git"
    if isinstance(
        exc, (GitHubFailure, RateLimitExhausted, PermissionRefusal, StepTrackerError)
    ):
        return "github"
    return "setup"


def _ensure_outcome(
    ledger: RunLedger, attempt_id: str, *, outcome: str, reason: str
) -> None:
    """Record the Terminal Outcome where the Attempt left none (§4.4, §15.2).

    The first record wins: ``record_terminal_outcome`` keeps the recorded
    row, so an outcome the graph already recorded stands and a failure that
    escaped it — a ``GitFailure`` from the mirror checkout, a
    ``CredentialFailure`` from the Claim — still ends recorded, and recovery
    finishes its writes after a restart with a working credential.
    """
    if ledger.get_outcome(attempt_id) is None:
        ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason=reason)


def _finalize(ledger: RunLedger, attempt_id: str) -> None:
    """Mark the Attempt ``finished`` so intake may select again (§4.2, §15.2)."""
    try:
        ledger.set_attempt_state(attempt_id, "finished")
    except Exception:
        pass


def _record_loop_halt(ledger: RunLedger, *, reason: str, detail: str) -> None:
    """Record the durable Halt and its ``halted`` event before exiting (§4.4).

    Every non-zero exit the poll loop decides on records here: the reason,
    the redacted detail (capped at 300 characters) and the time. A restart
    keeps the row, reporting it as ``instance.last_halt``. Best-effort: a
    halt write that itself fails never masks the halt it follows.
    """
    try:
        ledger.record_halt(reason=reason, detail=detail)
    except Exception as exc:
        print(f"run: could not record halt ({exc})", file=sys.stderr)


@dataclass(frozen=True)
class WorkerLoopConfig:
    """What one poll-loop run works under (contract §2, §4)."""

    claim_config: ClaimConfig
    """The Selection-and-Claim sweep parameters (§5, §6)."""
    workflow: V2Workflow
    """The running Workflow: input type, labels and end labels (§2.2)."""
    poll_interval_seconds: float
    live: LiveView | None = None
    """The prepared live view the control listener reads.

    The loop reports ``recovering`` while unfinished Attempts from an
    earlier process are being settled and marks readiness (plus the
    ``ready`` event) once none remains — readiness is reached only after
    recovery. ``None`` where no listener observes the loop (unit tests).
    """
    merge_base_resolver: Callable[[str, str], str] | None = None
    """The merge base of a pull-request head and the base tip (§7.2).

    Required for ``pull-request`` Claims; unused for ``issue`` Claims.
    Production resolves it through git; tests pass a stub or ``None``.
    """


@dataclass(frozen=True)
class StepRecoverySummary:
    """What one recovery pass settled and what it left pending (§4.2, §16.1)."""

    settled: tuple[str, ...]
    """Attempt ids this pass settled: ``released`` or ``finished``."""
    pending: tuple[str, ...]
    """Unfinished Attempt ids left for a later sweep (another Workflow's)."""


def _pin_to_input(pin: AttemptPin) -> PinnedInput:
    """Rebuild the Claim pin from its ledger row (§7, §16.1)."""
    from typing import cast

    return PinnedInput(
        workflow_id=pin.workflow_id,
        input_type=cast(InputType, pin.input_type),
        input_number=pin.input_number,
        title=pin.title,
        body=pin.body,
        fingerprint=pin.fingerprint,
        base_revision=pin.base_revision,
        base_tip=pin.base_tip,
        input_head=pin.input_head,
        head_branch=pin.head_branch,
        base_branch=pin.base_branch,
        selection_actor=pin.selection_actor,
        selection_actor_id=pin.selection_actor_id,
        selection_actor_type=pin.selection_actor_type,
        selection_actor_at=pin.selection_actor_at,
        identity_mode=cast(IdentityMode | None, pin.identity_mode),
    )


def _recovery_gate(
    tracker: StepTrackerPort,
    pin: PinnedInput,
    *,
    selection_label: str,
    base_branch: str,
) -> GateCheck:
    """The restart Gate over the live input and the Claim pin (§7.3, §16.1)."""

    def gate() -> GateResult:
        current: TrackedInput = tracker.fetch_input(pin.input_number, pin.input_type)
        try:
            base_tip_now: str | None = tracker.fetch_base_tip(base_branch)
        except Exception:
            base_tip_now = None
        return check_gate(
            current,
            pin,
            selection_label=selection_label,
            base_tip_now=base_tip_now,
        )

    return gate


def recover_unfinished_step_attempts(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    workflow: V2Workflow,
) -> StepRecoverySummary:
    """Settle every unfinished Attempt of the running Workflow (§4.1, §16.1).

    Never enters the Workflow graph and never calls the model: an ``open``
    Attempt (an unconfirmed Claim) is released, anything further along is
    settled and finalised through :func:`recover_attempt`, which finishes a
    recorded outcome as recorded and otherwise ends the Attempt
    ``interrupted`` / ``process-loss``, ``withdrawn`` or ``drift``. The Gate
    is read once per Attempt and the decided outcome selects the end labels,
    so settlement and finalisation agree.

    Recovery uses pinned values only (§2.10, CFG-41): the selection label,
    the end labels, the base branch and the input type come from the
    Attempt's pinned Effective Configuration, never from the currently
    loaded file. The Claim pin row (also pinned at Claim) decides the
    input type where present.

    Attempts of another Workflow are left pending: the running Worker does
    not know their labels. Intake stays closed while any remain (§4.2).

    Raises :class:`LegacyAttemptRefused` for an unfinished legacy Attempt
    (§16.4), and :class:`PinnedConfigurationRefused` (CFG-42) where an
    unfinished Attempt's pinned record is missing, unreadable or lacks the
    full shape; the caller turns either into the startup halt.
    """
    settled: list[str] = []
    pending: list[str] = []
    for record in ledger.unfinished_attempts():
        parsed = parse_attempt_id(record.attempt_id)
        if parsed is None:
            raise LegacyAttemptRefused(
                f"the state volume holds unfinished MVP Attempt(s): "
                f"{record.attempt_id}; drain them with the last pre-change "
                "release before upgrading (step-workflow contract §16.4)"
            )
        parsed_workflow_id, number, _ = parsed
        if parsed_workflow_id != workflow.id:
            pending.append(record.attempt_id)
            print(
                f"run: {record.attempt_id} belongs to workflow "
                f"{parsed_workflow_id!r}, not {workflow.id!r}; "
                "leaving it pending",
                file=sys.stderr,
            )
            continue
        effective = require_full_effective_configuration(
            record.effective_configuration, record.attempt_id
        )
        selection_label = effective_selection_label(effective)
        base_branch = effective_base_branch(effective)
        input_type: InputType = cast(InputType, effective_input_type(effective))
        pin_row = ledger.get_attempt_pin(record.attempt_id)
        pin = _pin_to_input(pin_row) if pin_row is not None else None
        if pin is not None:
            input_type = pin.input_type
        if record.state == "open":
            recover_unconfirmed_attempt(
                tracker=tracker,
                ledger=ledger,
                attempt_id=record.attempt_id,
                number=number,
                input_type=input_type,
            )
            settled.append(record.attempt_id)
            continue
        stored = ledger.get_outcome(record.attempt_id)
        if stored is not None:
            end_labels = effective_end_labels(effective, stored.outcome)
            gate: GateCheck | None = None
            if pin is not None:
                gate = _recovery_gate(
                    tracker,
                    pin,
                    selection_label=selection_label,
                    base_branch=base_branch,
                )
            recover_attempt(
                tracker=tracker,
                ledger=ledger,
                attempt_id=record.attempt_id,
                number=number,
                input_type=input_type,
                selection_label=selection_label,
                end_labels=end_labels,
                gate=gate,
            )
            settled.append(record.attempt_id)
            continue
        if pin is None:
            pre_gate: GateCheck | None = None
            gate_result: GateResult | None = None
        else:
            pre_gate = _recovery_gate(
                tracker,
                pin,
                selection_label=selection_label,
                base_branch=base_branch,
            )
            gate_result = pre_gate()
            pre_gate = (lambda _r=gate_result: _r)
        decision = decide_restart_outcome(gate_result)
        recover_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=record.attempt_id,
            number=number,
            input_type=input_type,
            selection_label=selection_label,
            end_labels=effective_end_labels(effective, decision.outcome),
            gate=pre_gate,
        )
        settled.append(record.attempt_id)
    return StepRecoverySummary(settled=tuple(settled), pending=tuple(pending))


class LegacyAttemptRefused(Exception):
    """An unfinished legacy Attempt blocks startup (§16.4, REC-11)."""


POLL_SLEEP_SLICE_SECONDS = 0.5
"""The interruptible poll sleep's chunk size (contract §4.2, §12).

The idle and retry sleeps keep their configured totals but yield every half
second so a SIGTERM observed mid-sleep stops intake at once instead of
sleeping through the grace period.
"""


def _sleep_or_shutdown(
    sleep: Callable[[float], None], total_seconds: float
) -> bool:
    """Sleep ``total_seconds`` in slices; True where SIGTERM arrived (§12).

    SIGTERM: intake stops at once; no new Claim is made.
    """
    from coding_agent.runtime.shutdown import is_shutdown_requested

    remaining = total_seconds
    while remaining > 0:
        if is_shutdown_requested():
            return True
        chunk = min(POLL_SLEEP_SLICE_SECONDS, remaining)
        sleep(chunk)
        remaining -= chunk
    return is_shutdown_requested()


def _volume_free_or_zero(volume: Path, free_bytes: Callable[[Path], int]) -> int:
    """Free bytes on the state volume, or ``0`` where it cannot be read (§17).

    A volume whose free space cannot be measured is treated as full, so the
    Attempt does not start.
    """
    try:
        return free_bytes(volume)
    except OSError as exc:
        print(
            f"run: could not read free space ({exc}); treating volume as full",
            file=sys.stderr,
        )
        return 0


def checkpoint_or_ignore(ledger: RunLedger) -> None:
    """Checkpoint the WAL after the last write; never fail the exit (§17).

    Every orderly Worker exit — the normal return, the SIGTERM shutdown,
    and later the Halt paths — checkpoints with ``TRUNCATE`` after its last
    write, so a restart or an Offline Read finds the committed state in the
    database file itself. Best-effort: a checkpoint failure never masks the
    exit it follows. Shared with the Supervisor, which covers the exits the
    poll loop never reaches.
    """
    try:
        ledger.checkpoint()
    except Exception:
        pass


def _poll_loop(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    loop_config: WorkerLoopConfig,
    run_one_attempt: Callable[[ConfirmedClaim], WorkflowResult],
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
    free_bytes: Callable[[Path], int] = volume_free_bytes,
    minimum_free_bytes: int = MIN_FREE_BYTES,
) -> int:
    """Run Selection → Claim → Attempt until a halt or a test bound (§4.2).

    The Worker sleeps for the poll interval only when a sweep claimed
    nothing, and selects again immediately after an Attempt ends; while the
    ledger holds an Attempt that is not ``finished`` or ``released`` the
    Worker selects no new work. A recovery write that fails is retried on
    the next poll cycle, and three consecutive recovery write failures halt
    the Worker with a non-zero exit; a Credential Failure halts at once
    with a non-zero exit; three consecutive infrastructure-class failures
    halt only after the third Attempt is finalized, the counter resetting
    after any Attempt without such a failure, so a restart does not loop.
    SIGTERM stops intake at once.

    ``run_one_attempt`` runs the claimed Attempt to its recorded Terminal
    Outcome (the compiled Workflow graph in production, a stub in unit
    tests) and performs its terminal writes; it returns the recorded
    result. ``sleep`` is injected so tests script the clock instead of
    sleeping. ``free_bytes`` is injected so tests set the free space
    instead of filling a disk. ``max_sweeps`` and ``max_attempts`` bound
    the loop for tests; production passes neither and runs continuously.
    Returns the process exit code: ``0`` where a bound stopped the loop,
    ``1`` where a halt did.
    """
    from coding_agent.runtime.shutdown import is_shutdown_requested

    consecutive_infrastructure_failures = 0
    attempts_finished = 0
    sweeps = 0
    poll_interval = loop_config.poll_interval_seconds
    # Startup reconciliation: every ``accepted`` command an earlier process
    # left is resolved against the ledger before the first Claim boundary,
    # so nothing stays ``accepted`` forever across a restart.
    def _try_reconcile() -> bool:
        """Resolve leftover ``accepted`` commands; False where it failed."""
        try:
            ledger.reconcile_accepted_commands()
        except Exception as exc:
            print(
                f"run: command reconciliation failed ({exc}); retrying below",
                file=sys.stderr,
            )
            return False
        return True

    commands_reconciled = _try_reconcile()
    live_view = loop_config.live
    ready_signaled = live_view is None or live_view.is_ready()

    def _sync_readiness() -> None:
        """Report ``recovering`` or mark readiness on the live view (§4.1).

        Runs before any selection and again once a sweep settles unfinished
        Attempts, so the first Claim of this process never precedes the
        ``ready`` event. Readiness — a state other than ``starting`` — is
        reached only after recovery: while an unfinished Attempt remains
        the listener answers ``recovering``. After the one ``ready`` event
        this is a no-op and normal Attempts never touch it again.
        """
        nonlocal ready_signaled
        if live_view is None or ready_signaled:
            return
        if ledger.has_unfinished_attempt():
            live_view.set_recovering(True)
            return
        live_view.set_ready()
        try:
            ledger.emit_contract_event(
                process_started_at=live_view.process_started_at_iso(),
                event_type=EVENT_READY,
                text="ready",
            )
        except Exception as exc:
            print(f"run: could not emit ready ({exc})", file=sys.stderr)
        ready_signaled = True

    while True:
        if is_shutdown_requested():
            return 0
        if not commands_reconciled:
            commands_reconciled = _try_reconcile()
        if max_sweeps is not None and sweeps >= max_sweeps:
            return 0
        _sync_readiness()
        volume = ledger.path.parent
        if _volume_free_or_zero(volume, free_bytes) < minimum_free_bytes:
            # Below the free-space threshold the Attempt does not start. No
            # Claim is opened, so nothing is written to the ledger file; the
            # sweep sleeps like an idle one and selects again, in case space
            # is freed.
            print(
                f"run: volume {volume} is low on free space; "
                "not starting an Attempt",
                file=sys.stderr,
            )
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            if _sleep_or_shutdown(sleep, poll_interval):
                return 0
            continue
        if ledger.has_unfinished_attempt():
            # Startup recovery, retried every sweep until nothing is
            # unfinished (§4.1, §16.1): settle unconfirmed Claims and
            # unfinished Attempts before any selection. Selection below
            # still selects nothing while one remains. A restart retries
            # from the settled ledger state.
            try:
                summary = recover_unfinished_step_attempts(
                    tracker=tracker,
                    ledger=ledger,
                    workflow=loop_config.workflow,
                )
            except LegacyAttemptRefused as exc:
                print(f"run: recovery refused ({exc}); halting", file=sys.stderr)
                _record_loop_halt(
                    ledger,
                    reason=HALT_LEGACY_ATTEMPT,
                    detail=f"recovery refused ({exc}); halting",
                )
                return 1
            except PinnedConfigurationRefused as exc:
                print(f"run: recovery refused ({exc}); halting", file=sys.stderr)
                _record_loop_halt(
                    ledger,
                    reason=HALT_RECOVERY_REFUSED,
                    detail=f"recovery refused ({exc}); halting",
                )
                return 1
            except CredentialFailure as exc:
                print(
                    f"run: credential failure during recovery ({exc}); halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=HALT_CREDENTIAL_FAILURE,
                    detail=f"credential failure during recovery ({exc}); halting",
                )
                return 1
            except (
                GitHubFailure,
                RateLimitExhausted,
                PermissionRefusal,
                StepTrackerError,
                GitFailure,
                OSError,
            ) as exc:
                # A failed recovery write is retried on the next poll
                # cycle; three consecutive recovery write failures halt
                # the Worker with a non-zero exit, counting the same way
                # infrastructure-class failures do (§4.4).
                consecutive_infrastructure_failures += 1
                if _halt_after_three(consecutive_infrastructure_failures):
                    print(
                        "run: three consecutive recovery write failures; halting",
                        file=sys.stderr,
                    )
                    _record_loop_halt(
                        ledger,
                        reason=HALT_RECOVERY_WRITE_FAILURE,
                        detail="three consecutive recovery write failures; halting",
                    )
                    return 1
                print(
                    f"run: recovery write failed ({exc}); retrying on the "
                    "next poll cycle",
                    file=sys.stderr,
                )
                sweeps += 1
                if max_sweeps is not None and sweeps >= max_sweeps:
                    return 0
                if _sleep_or_shutdown(sleep, poll_interval):
                    return 0
                continue
            consecutive_infrastructure_failures = 0
            for attempt_id in summary.settled:
                print(f"run: recovery settled {attempt_id}", file=sys.stderr)
            for attempt_id in summary.pending:
                print(
                    f"run: {attempt_id} belongs to another Workflow; "
                    "intake stays closed",
                    file=sys.stderr,
                )
            # Recovery may have settled the last unfinished Attempt: sync
            # readiness before any selection, so the first Claim never
            # precedes the ``ready`` event.
            _sync_readiness()
        try:
            claimed = sweep_and_claim(
                tracker,
                ledger,
                loop_config.claim_config,
                merge_base_resolver=loop_config.merge_base_resolver,
            )
        except CredentialFailure as exc:
            print(
                "run: credential failure during selection; halting",
                file=sys.stderr,
            )
            _record_loop_halt(
                ledger,
                reason=HALT_CREDENTIAL_FAILURE,
                detail=f"credential failure during selection ({exc}); halting",
            )
            return 1
        except (
            GitHubFailure,
            RateLimitExhausted,
            PermissionRefusal,
            StepTrackerError,
            GitFailure,
            OSError,
        ) as exc:
            if ledger.has_unfinished_attempt():
                # The failure came mid-Claim, after the Attempt opened:
                # intake is closed until it is settled, so retrying would
                # idle forever with nothing said. Halt and leave it to
                # restart recovery (§16.1).
                print(
                    f"run: claim interrupted ({exc}); an open Attempt needs "
                    "recovery; halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=HALT_CLAIM_INTERRUPTED,
                    detail=(
                        f"claim interrupted ({exc}); an open Attempt needs "
                        "recovery; halting"
                    ),
                )
                return 1
            # Other adapter errors (a 404, a 5xx) are a failed sweep, not a
            # crash: the Worker sleeps and selects again (§4.2).
            print(f"run: selection failed ({exc}); retrying", file=sys.stderr)
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            if _sleep_or_shutdown(sleep, poll_interval):
                return 0
            continue
        sweeps += 1
        if claimed is None or isinstance(claimed, ReleasedClaim):
            # Idle: nothing selectable, or a lost Claim race that released.
            # The Worker sleeps for the poll interval.
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            if _sleep_or_shutdown(sleep, poll_interval):
                return 0
            continue
        assert isinstance(claimed, ConfirmedClaim)
        if live_view is not None:
            # The Claim boundary confirmed this Attempt: publish the
            # native id, input and title now, so ``status`` names the phase
            # and a fresh activity time from here until finalization.
            live_view.set_claim(
                claimed.attempt_id, claimed.input_number, claimed.pin.title
            )
        try:
            result = run_one_attempt(claimed)
        except CredentialFailure as exc:
            print(
                f"run: credential failure in {claimed.attempt_id} ({exc}); halting",
                file=sys.stderr,
            )
            # The graph already recorded the Terminal Outcome where it got
            # that far (REC-13: the first record wins), so the recorded
            # outcome stands. The Attempt is left unfinished — not
            # finalized — so restart recovery finishes its terminal writes
            # after the credential is fixed (REC-17).
            _ensure_outcome(
                ledger,
                claimed.attempt_id,
                outcome="failed",
                reason=CREDENTIAL_REASON,
            )
            _record_loop_halt(
                ledger,
                reason=HALT_CREDENTIAL_FAILURE,
                detail=(
                    f"credential failure in {claimed.attempt_id} ({exc}); halting"
                ),
            )
            return 1
        except (GitHubFailure, RateLimitExhausted, PermissionRefusal, GitFailure, OSError) as exc:
            # An infrastructure-class failure that escaped the Attempt: git,
            # the GitHub API, or setup — the Attempt's workspace or
            # transcript could not be prepared (§4.4). It is recorded and
            # finalized like any other, so intake never stays closed.
            print(
                f"run: infrastructure failure in {claimed.attempt_id} ({exc})",
                file=sys.stderr,
            )
            _ensure_outcome(
                ledger,
                claimed.attempt_id,
                outcome="failed",
                reason=f"{INFRASTRUCTURE_PREFIX}: {_failure_class(exc)} {exc}",
            )
            _finalize(ledger, claimed.attempt_id)
            if live_view is not None:
                live_view.clear_attempt()
            reason = f"{INFRASTRUCTURE_PREFIX}: {_failure_class(exc)}"
        else:
            _finalize(ledger, claimed.attempt_id)
            if live_view is not None:
                live_view.clear_attempt()
            stored = ledger.get_outcome(claimed.attempt_id)
            reason = stored.reason if stored is not None else result.reason
        attempts_finished += 1
        if is_credential_reason(reason):
            # Halts at once with a non-zero exit — the outcome is already
            # recorded and finalization above is what keeps a restart from
            # looping.
            print(
                f"run: credential failure in {claimed.attempt_id}; halting",
                file=sys.stderr,
            )
            _record_loop_halt(
                ledger,
                reason=HALT_CREDENTIAL_FAILURE,
                detail=(
                    f"credential failure in {claimed.attempt_id} "
                    f"({reason}); halting"
                ),
            )
            return 1
        if is_pin_violation_reason(reason):
            # A pin violation halts at once with a non-zero exit, as a
            # Credential Failure does (§16.3): the outcome is already
            # recorded and finalised above, and recovery finishes the
            # terminal writes after the operator re-pins. The Workflow
            # never routes it — it would repeat in every Attempt.
            print(
                f"run: pin violation in {claimed.attempt_id} ({reason}); halting",
                file=sys.stderr,
            )
            _record_loop_halt(
                ledger,
                reason=HALT_PIN_VIOLATION,
                detail=(
                    f"pin violation in {claimed.attempt_id} "
                    f"({reason}); halting"
                ),
            )
            return 1
        if is_infrastructure_reason(reason):
            consecutive_infrastructure_failures += 1
            if _halt_after_three(consecutive_infrastructure_failures):
                # Only after the third Attempt is finalized — ``_finalize``
                # above ran before this return.
                print(
                    "run: three consecutive infrastructure-class failures; halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=HALT_INFRASTRUCTURE_FAILURE,
                    detail=(
                        "three consecutive infrastructure-class failures "
                        f"({reason}); halting"
                    ),
                )
                return 1
        else:
            # Any other recorded reason resets the halt-after-three counter.
            consecutive_infrastructure_failures = 0
        # The test bound is checked only after the halt decision, so a
        # bounded test can never mask a halt.
        if max_attempts is not None and attempts_finished >= max_attempts:
            return 0
        # After an Attempt the Worker selects again immediately: no sleep
        # on this path.


def run_worker_loop(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    loop_config: WorkerLoopConfig,
    run_one_attempt: Callable[[ConfirmedClaim], WorkflowResult],
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
    free_bytes: Callable[[Path], int] = volume_free_bytes,
    minimum_free_bytes: int = MIN_FREE_BYTES,
) -> int:
    """Run Selection → Claim → Attempt until a halt or a test bound (§4.2).

    The poll itself is :func:`_poll_loop`; this wrapper only guarantees the
    second line of defence: every orderly exit — the normal return, the
    SIGTERM shutdown, and later the Halt paths — checkpoints the WAL with
    ``TRUNCATE`` after the loop's last write, so a restart or an Offline
    Read finds the committed state in the database file itself.
    """
    try:
        return _poll_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=loop_config,
            run_one_attempt=run_one_attempt,
            sleep=sleep,
            max_sweeps=max_sweeps,
            max_attempts=max_attempts,
            free_bytes=free_bytes,
            minimum_free_bytes=minimum_free_bytes,
        )
    finally:
        checkpoint_or_ignore(ledger)


__all__ = [
    "CREDENTIAL_REASON",
    "HALT_CLAIM_INTERRUPTED",
    "HALT_CREDENTIAL_FAILURE",
    "HALT_INFRASTRUCTURE_FAILURE",
    "HALT_LEGACY_ATTEMPT",
    "HALT_PIN_VIOLATION",
    "HALT_RECOVERY_REFUSED",
    "HALT_RECOVERY_WRITE_FAILURE",
    "HALT_STARTUP_CHECK_REFUSED",
    "INFRASTRUCTURE_PREFIX",
    "LegacyAttemptRefused",
    "MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES",
    "PinnedConfigurationRefused",
    "StepRecoverySummary",
    "WorkerLoopConfig",
    "checkpoint_or_ignore",
    "is_credential_reason",
    "is_infrastructure_reason",
    "is_pin_violation_reason",
    "recover_unfinished_step_attempts",
    "run_worker_loop",
]
