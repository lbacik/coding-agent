"""The Worker Supervisor: startup, the poll loop, intake and halting.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the Worker's
lifecycle: startup in order — validate the Workflow Configuration, verify
the Skill Bundle digest, run the non-mutating GitHub checks of §18, run the
Provider Capability Assertion, then open the Run Ledger; a failure at any
step exits non-zero with no Attempt opened and no GitHub write — the volume
lock and startup recovery are M8; the poll loop — sleep for the poll
interval only when a sweep claimed nothing, select again immediately after
an Attempt ends; intake — while the ledger holds an Attempt that is not
``finished`` or ``released`` the Worker selects no new work; halting — a
Credential Failure halts at once with a non-zero exit, three consecutive
infrastructure-class failures halt only after the third Attempt is
finalized, the counter resetting after any Attempt without such a failure).

MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8, W-CFG-13,
REC-CFG-2.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from coding_agent.github.errors import (
    CredentialFailure,
    GitHubFailure,
    RateLimitExhausted,
)
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.tracker.port import TrackerPort
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt, select_and_claim

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


def run_worker_loop(
    *,
    tracker: TrackerPort,
    ledger: RunLedger,
    loop_config: WorkerLoopConfig,
    run_one_attempt: Callable[[ClaimedAttempt], AttemptResult],
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
) -> int:
    """Run Selection → Claim → Attempt until a halt or a test bound (§3).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the Worker
    sleeps for the poll interval only when a sweep claimed nothing, and
    selects again immediately after an Attempt ends; while the ledger holds
    an Attempt that is not ``finished`` or ``released`` the Worker selects
    no new work — enforced inside ``select_and_claim``; a Credential
    Failure halts at once with a non-zero exit, writing no comment; three
    consecutive infrastructure-class failures halt only after the third
    Attempt is finalized, the counter resetting after any Attempt without
    such a failure, so a restart does not loop).

    MVP row ids: P-WRK-1, P-WRK-2, P-WRK-4.

    ``run_one_attempt`` runs the claimed Attempt to its recorded Terminal
    Outcome (the Attempt graph in production, a stub in unit tests) and
    returns its result. ``sleep`` is injected so tests script the clock
    instead of sleeping. ``max_sweeps`` and ``max_attempts`` bound the loop
    for tests; production passes neither and runs continuously. Returns the
    process exit code: ``0`` where a bound stopped the loop, ``1`` where a
    halt did.
    """
    consecutive_infrastructure_failures = 0
    attempts_finished = 0
    sweeps = 0
    poll_interval = loop_config.poll_interval_seconds
    while True:
        if max_sweeps is not None and sweeps >= max_sweeps:
            return 0
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
        except (GitHubFailure, RateLimitExhausted, GitFailure, OSError) as exc:
            print(f"run: selection failed ({exc}); retrying", file=sys.stderr)
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            sleep(poll_interval)
            continue
        sweeps += 1
        if claimed is None:
            # P-WRK-4: idle, the Worker sleeps for the poll interval.
            if max_sweeps is not None and sweeps >= max_sweeps:
                return 0
            sleep(poll_interval)
            continue
        try:
            result = run_one_attempt(claimed)
        except CredentialFailure as exc:
            print(
                f"run: credential failure in {claimed.attempt_id} ({exc}); halting",
                file=sys.stderr,
            )
            _ensure_outcome(
                ledger,
                claimed.attempt_id,
                outcome="failed",
                reason=CREDENTIAL_REASON,
            )
            _finalize(ledger, claimed.attempt_id)
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
