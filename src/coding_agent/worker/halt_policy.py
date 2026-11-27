"""The Worker's halt-after-three policy: one shared consecutive-failure counter.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4.4, §16.3.

Infrastructure-class Attempt outcomes and recovery write failures increment
the *same* consecutive-failure counter. The third consecutive firing halts
the Worker, and the recorded halt reason is whichever event fired last
(``infrastructure-failure`` vs ``recovery-write-failure``, with the
triggering detail). A recovery pass that settles without a write failure,
and any Attempt outcome that is not infrastructure-class, reset the counter
to zero. Sweeps that reach neither counting site (idle, low-volume,
shutdown) never touch the counter — the loop simply makes no policy call.

The policy takes already-classified events only: the loop keeps reason
classification (the infrastructure-reason predicate, the provider-failure
marker lookup, the unexpected-exception mapping) and all ledger I/O (the
durable halt recording) exactly where they are. It performs no I/O and
takes no ledger, tracker or clock dependencies, so it is unit-testable
without driving the poll loop.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_CONSECUTIVE_FAILURES = 3
"""Halting bound: three consecutive counted failures (§4.4)."""

HALT_INFRASTRUCTURE_FAILURE = "infrastructure-failure"
"""Halt reason: the shared counter reached three on an Attempt outcome."""

HALT_RECOVERY_WRITE_FAILURE = "recovery-write-failure"
"""Halt reason: the shared counter reached three on a recovery write."""


@dataclass(frozen=True)
class HaltVerdict:
    """The counted halt the policy decided on: its reason and detail."""

    reason: str
    """The halt reason to record (``infrastructure-failure`` or ``recovery-write-failure``)."""

    detail: str
    """The human-readable halt detail to record alongside the reason."""


class HaltPolicy:
    """Per-loop-run state machine holding the shared consecutive-failure count."""

    def __init__(self) -> None:
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """The current shared-counter value (for tests and diagnostics)."""
        return self._consecutive_failures

    def record_infrastructure_failure(
        self, halt_reason: str
    ) -> HaltVerdict | None:
        """Count one infrastructure-class Attempt outcome.

        Returns the halt verdict where this was the third consecutive
        counted failure, else ``None`` (continue polling).
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            return None
        return HaltVerdict(
            reason=HALT_INFRASTRUCTURE_FAILURE,
            detail=(
                "three consecutive infrastructure-class failures "
                f"({halt_reason}); halting"
            ),
        )

    def record_recovery_write_failure(self) -> HaltVerdict | None:
        """Count one failed recovery write.

        Returns the halt verdict where this was the third consecutive
        counted failure, else ``None`` (retry on the next poll cycle).
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            return None
        return HaltVerdict(
            reason=HALT_RECOVERY_WRITE_FAILURE,
            detail="three consecutive recovery write failures; halting",
        )

    def record_recovery_settled(self) -> None:
        """Reset the counter: a recovery pass settled with no write failure."""
        self._consecutive_failures = 0

    def record_non_infrastructure_outcome(self) -> None:
        """Reset the counter: an Attempt ended without an infrastructure class."""
        self._consecutive_failures = 0


__all__ = [
    "HALT_INFRASTRUCTURE_FAILURE",
    "HALT_RECOVERY_WRITE_FAILURE",
    "MAX_CONSECUTIVE_FAILURES",
    "HaltPolicy",
    "HaltVerdict",
]
