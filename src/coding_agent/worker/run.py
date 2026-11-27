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
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from coding_agent.inputs.pickup import PickupConfig

from coding_agent.inputs.errors import (
    CredentialFailure,
    HostFailure,
    PermissionRefusal,
    RateLimitExhausted,
    StepTrackerError,
)
from coding_agent.inputs.authorisation import IdentityMode
from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    sweep_and_claim,
)
from coding_agent.attempt_id import AttemptId
from coding_agent.inputs.gates import GateResult, check_gate
from coding_agent.inputs.pinning import PinnedInput
from coding_agent.inputs.port import InputType, StepTrackerPort, TrackedInput
from coding_agent.ledger.store import AttemptPin, RunLedger
from coding_agent.provider.observed_identity import PIN_VIOLATION_REASON
from coding_agent.workflow.effective import (
    PinnedConfigurationRefused,
    effective_base_branch,
    effective_end_labels_for_all,
    effective_input_type,
    effective_selection_label,
    require_full_effective_configuration,
)
from coding_agent.operator.events import EVENT_READY
from coding_agent.operator.live import LiveView
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.step_finalisation import (
    AttemptDecision,
    CredentialFailed,
    FinalisationTarget,
    GateStopped,
    InternalFailure,
    Interrupted,
    UnsettledEffectsError,
    finish_attempt,
)
from coding_agent.runtime.step_operations import ProfileUnavailable
from coding_agent.runtime.step_recovery import (
    GateCheck,
    RecoveredAttempt,
    decide_restart_outcome,
    recover_attempt,
    recover_unconfirmed_attempt,
)
from coding_agent.skillbundle.recovery import (
    ArchiveRecoveryRefused,
    ensure_archive_for_recovery,
)
from coding_agent.worker.halt_policy import (
    HALT_INFRASTRUCTURE_FAILURE,
    HALT_RECOVERY_WRITE_FAILURE,
    MAX_CONSECUTIVE_FAILURES,
    HaltPolicy,
)
from coding_agent.worker.volume import MIN_FREE_BYTES, volume_free_bytes
from coding_agent.workflow.schema import Workflow

CREDENTIAL_REASON = "credential-failure"
"""The Terminal Outcome reason that halts the Worker at once (§4.4, §16.3)."""

HALT_PIN_VIOLATION = "pin-violation"
"""Halt reason: a pin violation recorded by an Attempt (§4.4, §16.3)."""

INFRASTRUCTURE_PREFIX = "infrastructure-failure"
"""The Terminal Outcome reason prefix counted toward the halt-after-three (§4.4)."""

MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES = MAX_CONSECUTIVE_FAILURES
"""Halting bound: three consecutive infrastructure-class failures (§4.4).

Alias of the halt policy's bound: the loop delegates counting to
:class:`HaltPolicy` and keeps only classification, halt recording and I/O.
"""

HALT_STARTUP_CHECK_REFUSED = "startup-check-refused"
"""Halt reason: a refused startup check after the volume lock."""

HALT_RECOVERY_REFUSED = "recovery-refused"
"""Halt reason: an unfinished Attempt that cannot be recovered."""

HALT_CREDENTIAL_FAILURE = "credential-failure"
"""Halt reason: a Credential Failure in selection, an Attempt or recovery."""

# Halt reasons for the counted halt (re-exported from the halt policy):
# HALT_INFRASTRUCTURE_FAILURE is recorded where the shared counter reaches
# three on an Attempt outcome; HALT_RECOVERY_WRITE_FAILURE where it reaches
# three on a recovery write — the reason is whichever event fired last.

HALT_CLAIM_INTERRUPTED = "claim-interrupted"
"""Halt reason: a Claim interrupted after the Attempt opened."""

HALT_LEGACY_ATTEMPT = "legacy-attempt"
"""Halt reason: an unfinished MVP Attempt on a step-workflow volume (§16.4)."""

HALT_MALFORMED_ATTEMPT = "malformed-attempt"
"""Halt reason: an unfinished Attempt whose stored id is not an Attempt Id."""

HALT_PROFILE_UNAVAILABLE = "profile-unavailable"
"""Halt reason: a validating Workflow's profile missing at Attempt start (§4.4)."""


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

    The infrastructure classes are setup, git, GitHub API, provider and
    ``unexpected`` (any other exception escaping the Attempt run);
    every ``infrastructure-failure`` reason the Attempt records names its
    class after the colon, so the prefix decides.
    """
    return reason == INFRASTRUCTURE_PREFIX or reason.startswith(
        INFRASTRUCTURE_PREFIX + ":"
    )


PROVIDER_FAILURE_FACT_KEY = "provider_failure"
"""The Execution Summary key marking a provider-failed Step Execution (§9.6)."""


def attempt_had_provider_failure(ledger: RunLedger, attempt_id: str) -> bool:
    """Whether any Step Execution of the Attempt ended on a provider failure (§4.4).

    Reads the Attempt's recorded Step Execution facts — the
    ``provider_failure`` marker the step loop writes into the Execution
    Summary of a Step Execution that ended ``error`` on a non-retryable or
    exhausted ``ModelError`` (§9.6) — so the answer holds both on the live
    path and after a restart, the same way recorded reasons do. A
    context-overflow ``error`` never counts: #563 decision 1 says a Context
    Overflow never halts the Worker. A failure inside a Sub-agent never
    counts by itself either: only the calling Step Execution's own marker
    counts (§9.8).
    """
    try:
        executions = ledger.list_step_executions(attempt_id)
    except Exception:
        return False
    for execution in executions:
        facts = execution.facts
        if not isinstance(facts, dict):
            continue
        marker = facts.get(PROVIDER_FAILURE_FACT_KEY)
        if not isinstance(marker, dict):
            continue
        if marker.get("context_overflow") is True:
            continue
        if marker.get("error"):
            return True
    return False


def _failure_class(exc: BaseException) -> str:
    """The infrastructure class an escaped exception belongs to (§4.4)."""
    if isinstance(exc, GitFailure):
        return "git"
    if isinstance(
        exc, (HostFailure, RateLimitExhausted, PermissionRefusal, StepTrackerError)
    ):
        return "github"
    return "setup"


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
    workflow: Workflow
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
    pickup_config: PickupConfig | None = None
    """The Round Handoff pickup parameters, tried ahead of selection (§8).

    ``None`` where the running Workflow leaves Round Handoff disabled or
    takes a pull-request input: the sweep keeps its ordinary Selection
    Label path.
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
    workflow: Workflow,
) -> StepRecoverySummary:
    """Settle every unfinished Attempt of the running Workflow (§4.1, §16.1).

    Never enters the Workflow graph and never calls the model: an ``open``
    Attempt (an unconfirmed Claim) is released, anything further along is
    settled and finalised through :func:`recover_attempt`, which finishes a
    recorded outcome as recorded and otherwise ends the Attempt
    ``interrupted`` / ``process-loss``, ``withdrawn`` or ``drift``. The Gate
    is read once per Attempt and the restart decision is handed to
    :func:`recover_attempt`; the finalisation entry point applies the end
    labels of the outcome it actually records or replays, so settlement and
    finalisation agree.

    Recovery uses pinned values only (§2.10, CFG-41): the selection label,
    the outcome → end-labels mapping, the base branch and the input type
    come from the Attempt's pinned Effective Configuration, never from the
    currently loaded file. The Claim pin row (also pinned at Claim) decides
    the input type where present.

    Attempts of another Workflow are left pending: the running Worker does
    not know their labels. Intake stays closed while any remain (§4.2).

    Recovery finishes a recorded outcome from the pinned identity and the
    archived blobs (SKV3-29): the archive is validated before any settlement
    for the Attempt, so a changed valid deployment with no former store
    still settles effects and finalisation from the archive, never from
    current files. Validation never repairs history, never restores secrets
    and never replays the graph or the model.

    Raises :class:`LegacyAttemptRefused` for an unfinished legacy Attempt
    (§16.4), :class:`PinnedConfigurationRefused` (CFG-42) where an
    unfinished Attempt's pinned record is missing, unreadable or lacks the
    full shape, and :class:`ArchiveRecoveryRefused` (SKV3-30/31) where the
    pinned identity or its archived blobs are missing, corrupt, escaping,
    symlinked or incompatible; the caller turns each into the startup halt.
    No archive is ever created or converted here (SKV3-31).
    """
    settled: list[str] = []
    pending: list[str] = []
    for record in ledger.unfinished_attempts():
        parsed = AttemptId.try_parse(record.attempt_id)
        if parsed is None:
            raise MalformedAttemptRefused(
                f"the state volume holds a row that is not an Attempt Id: "
                f"{record.attempt_id}; it identifies no Attempt"
            )
        if parsed.is_legacy:
            raise LegacyAttemptRefused(
                f"the state volume holds unfinished MVP Attempt(s): "
                f"{record.attempt_id}; drain them with the last pre-change "
                "release before upgrading (step-workflow contract §16.4)"
            )
        assert parsed.workflow is not None
        parsed_workflow_id, number = parsed.workflow, parsed.input_number
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
        # The integrity gate runs before any settlement for this Attempt
        # (SKV3-30): a refused archive settles nothing, writes nothing and
        # leaves the ledger row untouched (no partial success). The current
        # deployment is never consulted here.
        ensure_archive_for_recovery(ledger=ledger, attempt_id=record.attempt_id)
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
                end_labels_for=effective_end_labels_for_all(effective),
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
        restart = decide_restart_outcome(gate_result)
        if restart.outcome in ("withdrawn", "drift"):
            attempt_decision: AttemptDecision = GateStopped(
                outcome=restart.outcome, reason=restart.reason
            )
        else:
            attempt_decision = Interrupted(reason=restart.reason)
        recover_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=record.attempt_id,
            number=number,
            input_type=input_type,
            selection_label=selection_label,
            end_labels_for=effective_end_labels_for_all(effective),
            gate=pre_gate,
            decision=attempt_decision,
        )
        settled.append(record.attempt_id)
    return StepRecoverySummary(settled=tuple(settled), pending=tuple(pending))


class LegacyAttemptRefused(Exception):
    """An unfinished legacy Attempt blocks startup (§16.4, REC-11)."""


class MalformedAttemptRefused(Exception):
    """An unfinished Attempt whose stored id is not an Attempt Id blocks startup.

    The ledger holds a row that is not an Attempt Id, so it identifies no
    Attempt; this never uses the "unfinished MVP Attempt" message.
    """


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
    from coding_agent.operator.runlog import print_line
    from coding_agent.runtime.shutdown import is_shutdown_requested

    halt_policy = HaltPolicy()
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
    idle_announced = False

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
            except MalformedAttemptRefused as exc:
                print(f"run: recovery refused ({exc}); halting", file=sys.stderr)
                _record_loop_halt(
                    ledger,
                    reason=HALT_MALFORMED_ATTEMPT,
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
            except ArchiveRecoveryRefused as exc:
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
                HostFailure,
                RateLimitExhausted,
                PermissionRefusal,
                StepTrackerError,
                GitFailure,
                OSError,
            ) as exc:
                # A failed recovery write is retried on the next poll
                # cycle; three consecutive counted failures halt the
                # Worker with a non-zero exit, recovery writes sharing
                # the counter with infrastructure-class failures (§4.4).
                halt = halt_policy.record_recovery_write_failure()
                if halt is not None:
                    print(
                        "run: three consecutive recovery write failures; halting",
                        file=sys.stderr,
                    )
                    _record_loop_halt(
                        ledger,
                        reason=halt.reason,
                        detail=halt.detail,
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
            halt_policy.record_recovery_settled()
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
        # A pending ``next issue N`` priority is tried first at this Claim
        # boundary, under the same guards as normal selection, and settles
        # there (operator contract §8.3). Intake closed opens no Claim and
        # leaves the priority ``accepted`` for the next boundary.
        priority = ledger.pending_next_command()
        try:
            claimed = sweep_and_claim(
                tracker,
                ledger,
                loop_config.claim_config,
                priority=priority,
                merge_base_resolver=loop_config.merge_base_resolver,
                pickup=loop_config.pickup_config,
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
            HostFailure,
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
            # The Worker sleeps for the poll interval. The container log
            # notes the transition into idle once, not every poll.
            if not idle_announced:
                print_line("run: no ready issue")
                idle_announced = True
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            if _sleep_or_shutdown(sleep, poll_interval):
                return 0
            continue
        assert isinstance(claimed, ConfirmedClaim)
        print_line(f"run: selected issue {claimed.input_number}")
        idle_announced = False
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
            # The Attempt is recorded ``interrupted`` / ``credential-failure``
            # without terminal writes and left unfinished — not finalized —
            # so restart recovery finishes its terminal writes after the
            # credential is fixed (REC-17).
            finish_attempt(
                tracker=tracker,
                ledger=ledger,
                attempt_id=claimed.attempt_id,
                target=FinalisationTarget(
                    number=claimed.input_number,
                    input_type=loop_config.claim_config.input_type,
                    selection_label=loop_config.claim_config.selection_label,
                ),
                decision=CredentialFailed(),
                end_labels_for=dict(loop_config.workflow.outcomes),
            )
            _record_loop_halt(
                ledger,
                reason=HALT_CREDENTIAL_FAILURE,
                detail=(
                    f"credential failure in {claimed.attempt_id} ({exc}); halting"
                ),
            )
            return 1
        except (
            HostFailure,
            RateLimitExhausted,
            PermissionRefusal,
            StepTrackerError,
            UnsettledEffectsError,
            GitFailure,
            OSError,
        ) as exc:
            # An infrastructure-class failure that escaped the Attempt: git,
            # the GitHub API (including a failed live terminal write, whose
            # recorded outcome the next cycle's recovery finishes, §14.4),
            # or setup — the Attempt's workspace or transcript could not be
            # prepared (§4.4). It ends through the finalisation entry point
            # as ``internal-failure`` (§15.2), so the terminal writes are
            # performed best-effort; where finalisation itself fails the
            # Attempt stays recorded and unfinished for the next cycle's
            # recovery pass (§16.1).
            print(
                f"run: infrastructure failure in {claimed.attempt_id} ({exc})",
                file=sys.stderr,
            )
            infra_reason = f"{INFRASTRUCTURE_PREFIX}: {_failure_class(exc)} {exc}"
            try:
                finish_attempt(
                    tracker=tracker,
                    ledger=ledger,
                    attempt_id=claimed.attempt_id,
                    target=FinalisationTarget(
                        number=claimed.input_number,
                        input_type=loop_config.claim_config.input_type,
                        selection_label=loop_config.claim_config.selection_label,
                    ),
                    decision=InternalFailure(reason=infra_reason),
                    end_labels_for=dict(loop_config.workflow.outcomes),
                )
            except CredentialFailure as cred_exc:
                print(
                    f"run: credential failure during finalisation of "
                    f"{claimed.attempt_id} ({cred_exc}); halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=HALT_CREDENTIAL_FAILURE,
                    detail=(
                        "credential failure during finalisation of "
                        f"{claimed.attempt_id} ({cred_exc}); halting"
                    ),
                )
                return 1
            except (
                HostFailure,
                RateLimitExhausted,
                PermissionRefusal,
                StepTrackerError,
                GitFailure,
                OSError,
                UnsettledEffectsError,
            ) as fin_exc:
                # Best-effort finalisation failed: the Attempt stays recorded
                # and unfinished, and the next cycle's recovery pass finishes
                # it from the recorded row.
                print(
                    f"run: finalisation failed for {claimed.attempt_id} "
                    f"({fin_exc}); leaving it for recovery",
                    file=sys.stderr,
                )
            if live_view is not None:
                live_view.clear_attempt()
            reason = f"{INFRASTRUCTURE_PREFIX}: {_failure_class(exc)}"
        except ProfileUnavailable as exc:
            # A validating Workflow whose contract cannot be pinned at the
            # Attempt's start commit: the Attempt ends ``internal-failure``
            # through the normal finalisation path, and the Worker halts —
            # the cause belongs to the repository, so retrying would fail
            # every Attempt the same way (§4.4, ADR 0037). No exception
            # escapes: the Attempt is finished, never left open.
            print(
                f"run: profile unavailable in {claimed.attempt_id} "
                f"({exc.reason}); halting",
                file=sys.stderr,
            )
            try:
                finish_attempt(
                    tracker=tracker,
                    ledger=ledger,
                    attempt_id=claimed.attempt_id,
                    target=FinalisationTarget(
                        number=claimed.input_number,
                        input_type=loop_config.claim_config.input_type,
                        selection_label=loop_config.claim_config.selection_label,
                    ),
                    decision=InternalFailure(reason=exc.reason),
                    end_labels_for=dict(loop_config.workflow.outcomes),
                )
            except CredentialFailure as cred_exc:
                print(
                    f"run: credential failure during finalisation of "
                    f"{claimed.attempt_id} ({cred_exc}); halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=HALT_CREDENTIAL_FAILURE,
                    detail=(
                        "credential failure during finalisation of "
                        f"{claimed.attempt_id} ({cred_exc}); halting"
                    ),
                )
                return 1
            except (
                HostFailure,
                RateLimitExhausted,
                PermissionRefusal,
                StepTrackerError,
                GitFailure,
                OSError,
                UnsettledEffectsError,
            ) as fin_exc:
                # Best-effort finalisation failed: the Attempt stays recorded
                # and unfinished, and the next cycle's recovery pass finishes
                # it from the recorded row — the halt below still stops the
                # retry loop.
                print(
                    f"run: finalisation failed for {claimed.attempt_id} "
                    f"({fin_exc}); leaving it for recovery",
                    file=sys.stderr,
                )
            if live_view is not None:
                live_view.clear_attempt()
            _record_loop_halt(
                ledger,
                reason=HALT_PROFILE_UNAVAILABLE,
                detail=(
                    f"profile unavailable in {claimed.attempt_id} "
                    f"({exc.reason}); halting"
                ),
            )
            return 1
        except Exception as exc:
            # Any other escaped exception after the Claim: this Attempt ends
            # ``internal-failure`` through finalisation and the Worker keeps
            # polling. It counts toward the halt-after-three bound under the
            # ``unexpected`` failure class (§4.4). BaseExceptions
            # (KeyboardInterrupt, SystemExit) still propagate.
            print(
                f"run: unexpected failure in {claimed.attempt_id} "
                f"({exc!r}); ending internal-failure",
                file=sys.stderr,
            )
            try:
                finish_attempt(
                    tracker=tracker,
                    ledger=ledger,
                    attempt_id=claimed.attempt_id,
                    target=FinalisationTarget(
                        number=claimed.input_number,
                        input_type=loop_config.claim_config.input_type,
                        selection_label=loop_config.claim_config.selection_label,
                    ),
                    decision=InternalFailure(
                        reason=f"{INFRASTRUCTURE_PREFIX}: unexpected {exc}"
                    ),
                    end_labels_for=dict(loop_config.workflow.outcomes),
                )
            except CredentialFailure as cred_exc:
                print(
                    f"run: credential failure during finalisation of "
                    f"{claimed.attempt_id} ({cred_exc}); halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=HALT_CREDENTIAL_FAILURE,
                    detail=(
                        "credential failure during finalisation of "
                        f"{claimed.attempt_id} ({cred_exc}); halting"
                    ),
                )
                return 1
            except (
                HostFailure,
                RateLimitExhausted,
                PermissionRefusal,
                StepTrackerError,
                GitFailure,
                OSError,
                UnsettledEffectsError,
            ) as fin_exc:
                # Best-effort finalisation failed: the Attempt stays recorded
                # and unfinished, and the next cycle's recovery pass finishes
                # it from the recorded row.
                print(
                    f"run: finalisation failed for {claimed.attempt_id} "
                    f"({fin_exc}); leaving it for recovery",
                    file=sys.stderr,
                )
            if live_view is not None:
                live_view.clear_attempt()
            reason = f"{INFRASTRUCTURE_PREFIX}: unexpected"
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
        # A routed provider-failure ``error`` counts as an
        # infrastructure-class failure of class ``provider`` whatever End
        # the Workflow routed to (§4.4): the Terminal Outcome stays the
        # routed one, only the counter sees the provider class. The marker
        # lives in the Attempt's recorded Step Execution facts, so this
        # holds on the live path and after a restart.
        halt_reason = reason
        if not is_infrastructure_reason(
            reason
        ) and attempt_had_provider_failure(ledger, claimed.attempt_id):
            halt_reason = f"{INFRASTRUCTURE_PREFIX}: provider"
        if is_infrastructure_reason(halt_reason):
            halt = halt_policy.record_infrastructure_failure(halt_reason)
            if halt is not None:
                # Only after the third Attempt is finalised — the Attempt
                # above either finished through the finalisation entry
                # point or was left recorded and unfinished for recovery.
                print(
                    "run: three consecutive infrastructure-class failures; halting",
                    file=sys.stderr,
                )
                _record_loop_halt(
                    ledger,
                    reason=halt.reason,
                    detail=halt.detail,
                )
                return 1
        else:
            # Any other recorded reason resets the halt-after-three counter.
            halt_policy.record_non_infrastructure_outcome()
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
    "HALT_MALFORMED_ATTEMPT",
    "HALT_PIN_VIOLATION",
    "HALT_PROFILE_UNAVAILABLE",
    "HALT_RECOVERY_REFUSED",
    "HALT_RECOVERY_WRITE_FAILURE",
    "HALT_STARTUP_CHECK_REFUSED",
    "INFRASTRUCTURE_PREFIX",
    "ArchiveRecoveryRefused",
    "LegacyAttemptRefused",
    "MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES",
    "MalformedAttemptRefused",
    "PinnedConfigurationRefused",
    "StepRecoverySummary",
    "attempt_had_provider_failure",
    "WorkerLoopConfig",
    "checkpoint_or_ignore",
    "is_credential_reason",
    "is_infrastructure_reason",
    "is_pin_violation_reason",
    "recover_unfinished_step_attempts",
    "run_worker_loop",
]
