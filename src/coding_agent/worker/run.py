"""The Worker Supervisor: startup, the poll loop, intake and halting.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the Worker's
lifecycle: startup in order — validate the Workflow Configuration, take
the exclusive volume lock, verify the Skill Bundle digest, run the
non-mutating GitHub checks of §18, run the Provider Capability Assertion,
then open the Run Ledger; a failure at any step exits non-zero with no
Attempt opened and no GitHub write — startup recovery is M8; the poll loop — sleep for the poll
interval only when a sweep claimed nothing, select again immediately after
an Attempt ends; SIGTERM stops intake at once and the process then exits;
intake — while the ledger holds an Attempt that is not ``finished`` or
``released`` the Worker selects no new work; a recovery write that fails
is retried on the next poll cycle, and three in a row halt the Worker;
halting — a
Credential Failure halts at once with a non-zero exit, three consecutive
infrastructure-class failures halt only after the third Attempt is
finalized, the counter resetting after any Attempt without such a failure).

MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8, W-CFG-13,
W-CFG-14, L3-ERR-13, REC-CFG-2, REC-CLM-1, REC-CLM-2, REC-WRK-1, P-CAN-3,
REC-SIG-1, REC-INT-1, REC-INT-2, REC-INT-3, REC-INT-4, REC-INT-5, REC-INT-6,
P-REC-1, P-REC-3, P-REC-4.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coding_agent.github.errors import (
    CredentialFailure,
    GitHubFailure,
    RateLimitExhausted,
)
from coding_agent.github.issues import IssueFetchFailed
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.runtime.shutdown import is_shutdown_requested
from coding_agent.tracker.github import GitHubTrackerError
from coding_agent.tracker.port import TrackerPort
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt, select_and_claim
from coding_agent.worker.recovery import (
    InterruptedRecovery,
    RecoveryRefused,
    recover_unfinished_attempts,
)
from coding_agent.worker.volume import MIN_FREE_BYTES, volume_free_bytes

CREDENTIAL_REASON = "credential-failure"
"""The Terminal Outcome reason that halts the Worker at once (§13, §14)."""

INFRASTRUCTURE_PREFIX = "infrastructure-failure"
"""The Terminal Outcome reason prefix counted toward the halt-after-three (§14)."""

MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES = 3
"""Halting bound: three consecutive infrastructure-class failures (§3)."""


def is_credential_reason(reason: str) -> bool:
    """Whether ``reason`` is the Credential Failure that halts at once (§3, §13)."""
    return reason == CREDENTIAL_REASON or reason.startswith(CREDENTIAL_REASON + ":")


def is_infrastructure_reason(reason: str) -> bool:
    """Whether ``reason`` counts toward the halt-after-three (§3, §14).

    The infrastructure classes are setup, git, GitHub API and provider;
    every ``infrastructure-failure`` reason the Attempt records names its
    class after the colon, so the prefix decides.
    """
    return reason == INFRASTRUCTURE_PREFIX or reason.startswith(
        INFRASTRUCTURE_PREFIX + ":"
    )


def _halt_after_three(consecutive_infrastructure_failures: int) -> bool:
    """Whether the halt-after-three bound is reached (§3, §14).

    One predicate for both counting sites — the escaped-exception path and
    the recorded-reason path — so the bound cannot drift between them.
    """
    return (
        consecutive_infrastructure_failures >= MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES
    )


def _failure_class(exc: BaseException) -> str:
    """The infrastructure class an escaped exception belongs to (§3, §14)."""
    if isinstance(exc, GitFailure):
        return "git"
    if isinstance(exc, (GitHubFailure, RateLimitExhausted)):
        return "github"
    return "setup"


def _ensure_outcome(
    ledger: RunLedger, attempt_id: str, *, outcome: str, reason: str
) -> None:
    """Record the Terminal Outcome where the Attempt left none (§14).

    The graph records before publishing, so a failure that escaped it —
    a ``GitFailure`` from the mirror checkout, a ``CredentialFailure`` from
    the Claim assignment — still ends recorded, and recovery finishes its
    writes after a restart with a working credential.
    """
    if ledger.get_outcome(attempt_id) is None:
        ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason=reason)


def _finalize(ledger: RunLedger, attempt_id: str) -> None:
    """Mark the Attempt ``finished`` so intake may select again (§3, §17)."""
    try:
        ledger.set_attempt_state(attempt_id, "finished")
    except Exception:
        pass


@dataclass(frozen=True)
class WorkerLoopConfig:
    """What one poll-loop run works under (contract §2.5, §3)."""

    claim_config: ClaimConfig
    poll_interval_seconds: float
    interrupted_recovery: InterruptedRecovery | None = None
    """Where startup recovery settles an interrupted Attempt (§17).

    A ``claim-confirmed`` Attempt with no recorded outcome is finished
    through the shared claim-confirmed finalization (REC-INT-1 …
    REC-INT-6); without it such an Attempt reports as pending and intake
    stays closed. ``None`` in the tests that predate the interrupted slice.
    """


POLL_SLEEP_SLICE_SECONDS = 0.5
"""The interruptible poll sleep's chunk size (contract §3, §11, REC-SIG-1).

The idle and retry sleeps keep their configured totals but yield every half
second so a SIGTERM observed mid-sleep stops intake at once instead of
sleeping through the grace period.
"""


def _sleep_or_shutdown(
    sleep: Callable[[float], None], total_seconds: float
) -> bool:
    """Sleep ``total_seconds`` in slices; True where SIGTERM arrived (§11).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (SIGTERM: intake
    stops at once; no new Claim is made).

    MVP row ids: P-CAN-3, REC-SIG-1.
    """
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

    Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (SQLite never
    meets a full disk: L3-ERR-13). A volume whose free space cannot be
    measured is treated as full, so the Attempt does not start.

    MVP row ids: L3-ERR-13.
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

    Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (one SQLite
    file in WAL mode). Every orderly Worker exit — the normal return, the
    SIGTERM shutdown, and later the Halt paths — checkpoints with
    ``TRUNCATE`` after its last write, so a restart or an Offline Read
    finds the committed state in the database file itself. Best-effort: a
    checkpoint failure (a full disk, a torn file, an incomplete checkpoint
    :meth:`RunLedger.checkpoint` reports) must never mask the exit it
    follows, mirroring :func:`_finalize`. Shared with the Supervisor,
    which covers the exits the poll loop never reaches.
    """
    try:
        ledger.checkpoint()
    except Exception:
        pass


def _poll_loop(
    *,
    tracker: TrackerPort,
    ledger: RunLedger,
    loop_config: WorkerLoopConfig,
    run_one_attempt: Callable[[ClaimedAttempt], AttemptResult],
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
    free_bytes: Callable[[Path], int] = volume_free_bytes,
    minimum_free_bytes: int = MIN_FREE_BYTES,
    remote_branch_sha: Callable[[str], str | None] | None = None,
) -> int:
    """Run Selection → Claim → Attempt until a halt or a test bound (§3).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the Worker
    sleeps for the poll interval only when a sweep claimed nothing, and
    selects again immediately after an Attempt ends; while the ledger holds
    an Attempt that is not ``finished`` or ``released`` the Worker selects
    no new work — enforced inside ``select_and_claim``; a recovery write
    that fails is retried on the next poll cycle, and three consecutive
    recovery write failures halt the Worker with a non-zero exit; a
    Credential Failure halts at once with a non-zero exit, writing no
    comment; three consecutive infrastructure-class failures halt only
    after the third Attempt is finalized, the counter resetting after any
    Attempt without such a failure, so a restart does not loop), §11
    (SIGTERM: intake stops at once — the loop-top check makes no new Claim
    and exits 0, and the idle and retry sleeps yield every half second so
    a signal mid-sleep stops intake instead of sleeping through the grace
    period) and §17 (below the free-space threshold the Attempt does not
    start and nothing is written to the ledger file, so SQLite never meets
    a full disk; every sweep settles unfinished Attempts with their pinned
    values before any selection).

    MVP row ids: P-WRK-1, P-WRK-2, P-WRK-4, P-CAN-3, REC-SIG-1, L3-ERR-13,
    REC-CLM-1, REC-CLM-2, REC-WRK-1, REC-OUT-1, REC-OUT-2, REC-DEL-1,
    REC-DEL-2, REC-DEL-3, REC-DEL-4, P-REC-2, C-OUT-1, REC-INT-1, REC-INT-2,
    REC-INT-3, REC-INT-4, REC-INT-5, REC-INT-6, P-REC-1, P-REC-3, P-REC-4.

    ``run_one_attempt`` runs the claimed Attempt to its recorded Terminal
    Outcome (the Attempt graph in production, a stub in unit tests) and
    returns its result. ``sleep`` is injected so tests script the clock
    instead of sleeping. ``free_bytes`` is injected so tests set the free
    space instead of filling a disk. ``remote_branch_sha`` resolves the
    remote branch for recorded-outcome recovery (the Delivery Intent's
    branch check); ``None`` means recovery cannot check the remote and
    raises rather than guessing. ``max_sweeps`` and ``max_attempts``
    bound the loop for tests; production passes neither and runs
    continuously. Returns the process exit code: ``0`` where a bound
    stopped the loop, ``1`` where a halt did.
    """
    consecutive_infrastructure_failures = 0
    attempts_finished = 0
    sweeps = 0
    poll_interval = loop_config.poll_interval_seconds
    while True:
        if is_shutdown_requested():
            return 0
        if max_sweeps is not None and sweeps >= max_sweeps:
            return 0
        volume = ledger.path.parent
        if _volume_free_or_zero(volume, free_bytes) < minimum_free_bytes:
            # L3-ERR-13: the Attempt does not start. No Claim is opened, so
            # nothing is written to the ledger file; the sweep sleeps like an
            # idle one and selects again, in case space is freed.
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
            # unfinished (§3, §17, REC-WRK-1): settle unconfirmed Claims
            # and interrupted Attempts before any selection, with the
            # Attempt's pinned values. Selection below still selects
            # nothing while one remains. A restart retries from the
            # settled ledger state — settled Attempts stay ``released``
            # or ``finished`` — so the three-and-halt bound applies per
            # run and a restart does not loop.
            try:
                summary = recover_unfinished_attempts(
                    tracker,
                    ledger,
                    agent_login=loop_config.claim_config.agent_login,
                    remote_branch_sha=remote_branch_sha,
                    interrupted=loop_config.interrupted_recovery,
                )
            except CredentialFailure as exc:
                print(
                    f"run: credential failure during recovery ({exc}); halting",
                    file=sys.stderr,
                )
                return 1
            except RecoveryRefused as exc:
                print(
                    f"run: recovery refused ({exc}); halting",
                    file=sys.stderr,
                )
                return 1
            except (
                GitHubFailure,
                RateLimitExhausted,
                GitHubTrackerError,
                IssueFetchFailed,
                GitFailure,
                OSError,
            ) as exc:
                # A failed recovery write is retried on the next poll
                # cycle; three consecutive recovery write failures halt
                # the Worker with a non-zero exit (REC-WRK-1), counting
                # the same way infrastructure-class failures do (§3).
                consecutive_infrastructure_failures += 1
                if _halt_after_three(consecutive_infrastructure_failures):
                    print(
                        "run: three consecutive recovery write failures; halting",
                        file=sys.stderr,
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
                    f"run: {attempt_id} needs recorded-outcome recovery; "
                    "intake stays closed",
                    file=sys.stderr,
                )
        try:
            claimed = select_and_claim(
                tracker=tracker, ledger=ledger, config=loop_config.claim_config
            )
        except CredentialFailure:
            print(
                "run: credential failure during selection; halting",
                file=sys.stderr,
            )
            return 1
        except (
            GitHubFailure,
            RateLimitExhausted,
            GitHubTrackerError,
            IssueFetchFailed,
            GitFailure,
            OSError,
        ) as exc:
            if ledger.has_unfinished_attempt():
                # The failure came mid-Claim, after the Attempt opened (§5):
                # intake is closed until it is settled, so retrying would idle
                # forever with nothing said. Halt and leave it to startup
                # recovery (§17).
                print(
                    f"run: claim interrupted ({exc}); an open Attempt needs "
                    "recovery; halting",
                    file=sys.stderr,
                )
                return 1
            # The adapter's own errors (a 404, a 5xx) are a failed sweep,
            # not a crash: the Worker sleeps and selects again (§3).
            print(f"run: selection failed ({exc}); retrying", file=sys.stderr)
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            if _sleep_or_shutdown(sleep, poll_interval):
                return 0
            continue
        sweeps += 1
        if claimed is None:
            # P-WRK-4: idle, the Worker sleeps for the poll interval.
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            if _sleep_or_shutdown(sleep, poll_interval):
                return 0
            continue
        try:
            result = run_one_attempt(claimed)
        except CredentialFailure as exc:
            print(
                f"run: credential failure in {claimed.attempt_id} ({exc}); halting",
                file=sys.stderr,
            )
            # The graph already recorded the Terminal Outcome before the
            # 401 (REC-OUT-1): ``record_terminal_outcome`` keeps the first
            # row, so the recorded outcome stands. The Attempt is left
            # ``outcome-recorded`` — not finalized — so startup recovery
            # finishes its unfinished writes after a restart with a
            # working credential.
            _ensure_outcome(
                ledger,
                claimed.attempt_id,
                outcome="failed",
                reason=CREDENTIAL_REASON,
            )
            return 1
        except (GitHubFailure, RateLimitExhausted, GitFailure, OSError) as exc:
            # An infrastructure-class failure that escaped the graph: git,
            # the GitHub API, or setup — the Attempt's evidence directory or
            # transcript could not be prepared (§3, §14). It is recorded and
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
            reason = f"{INFRASTRUCTURE_PREFIX}: {_failure_class(exc)}"
        else:
            _finalize(ledger, claimed.attempt_id)
            stored = ledger.get_outcome(claimed.attempt_id)
            reason = stored.reason if stored is not None else result.reason
        attempts_finished += 1
        if is_credential_reason(reason):
            # P-WRK-1: halts at once with a non-zero exit and writes no
            # comment — the outcome is already recorded; finalization above
            # is what keeps a restart from looping.
            print(
                f"run: credential failure in {claimed.attempt_id}; halting",
                file=sys.stderr,
            )
            return 1
        if is_infrastructure_reason(reason):
            consecutive_infrastructure_failures += 1
            if _halt_after_three(consecutive_infrastructure_failures):
                # P-WRK-2: only after the third Attempt is finalized —
                # ``_finalize`` above ran before this return.
                print(
                    "run: three consecutive infrastructure-class failures; halting",
                    file=sys.stderr,
                )
                return 1
        else:
            consecutive_infrastructure_failures = 0
        # The test bound is checked only after the halt decision, so a
        # bounded test can never mask a halt.
        if max_attempts is not None and attempts_finished >= max_attempts:
            return 0
        # After an Attempt the Worker selects again immediately: no sleep
        # on this path (P-WRK-4).


def run_worker_loop(
    *,
    tracker: TrackerPort,
    ledger: RunLedger,
    loop_config: WorkerLoopConfig,
    run_one_attempt: Callable[[ClaimedAttempt], AttemptResult],
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
    free_bytes: Callable[[Path], int] = volume_free_bytes,
    minimum_free_bytes: int = MIN_FREE_BYTES,
    remote_branch_sha: Callable[[str], str | None] | None = None,
) -> int:
    """Run Selection → Claim → Attempt until a halt or a test bound (§3).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the Worker
    lifecycle) and §17 (one SQLite file in WAL mode). The poll itself is
    :func:`_poll_loop`; this wrapper only guarantees the second line of
    defence: every orderly exit — the normal return, the SIGTERM shutdown,
    and later the Halt paths — checkpoints the WAL with ``TRUNCATE`` after
    the loop's last write, so a restart or an Offline Read finds the
    committed state in the database file itself.
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
            remote_branch_sha=remote_branch_sha,
        )
    finally:
        checkpoint_or_ignore(ledger)
