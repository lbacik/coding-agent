"""The Worker's halt-after-three policy as a unit (contract §4.4, §16.3).

The shared consecutive-failure counter lives in
``coding_agent.worker.halt_policy.HaltPolicy``; these tests drive it
directly, without starting the poll loop. Loop-level behaviour (halt
recording, classification, immediate halts) stays covered by
``test_worker_run.py`` and ``test_worker_recovery.py``.
"""

from __future__ import annotations

from coding_agent.worker.halt_policy import (
    HALT_INFRASTRUCTURE_FAILURE,
    HALT_RECOVERY_WRITE_FAILURE,
    MAX_CONSECUTIVE_FAILURES,
    HaltPolicy,
)


def test_the_threshold_is_three() -> None:
    """§4.4: the shared counter halts on the third consecutive failure."""
    assert MAX_CONSECUTIVE_FAILURES == 3


def test_fresh_policy_holds_an_empty_counter() -> None:
    """A sweep that never reaches either counting site leaves the counter at zero."""
    policy = HaltPolicy()

    assert policy.consecutive_failures == 0


def test_two_infrastructure_failures_do_not_halt() -> None:
    """Below the bound the policy votes continue and keeps counting."""
    policy = HaltPolicy()

    assert policy.record_infrastructure_failure("infrastructure-failure: git") is None
    assert policy.record_infrastructure_failure("infrastructure-failure: git") is None

    assert policy.consecutive_failures == 2


def test_three_infrastructure_failures_halt_with_the_attempt_reason() -> None:
    """The third Attempt outcome in a row halts with ``infrastructure-failure``."""
    policy = HaltPolicy()
    halt_reason = "infrastructure-failure: provider"

    assert policy.record_infrastructure_failure(halt_reason) is None
    assert policy.record_infrastructure_failure(halt_reason) is None
    halt = policy.record_infrastructure_failure(halt_reason)

    assert halt is not None
    assert halt.reason == HALT_INFRASTRUCTURE_FAILURE == "infrastructure-failure"
    assert halt_reason in halt.detail
    assert policy.consecutive_failures == 3


def test_three_recovery_write_failures_halt_with_the_recovery_reason() -> None:
    """The third failed recovery write in a row halts with ``recovery-write-failure``."""
    policy = HaltPolicy()

    assert policy.record_recovery_write_failure() is None
    assert policy.record_recovery_write_failure() is None
    halt = policy.record_recovery_write_failure()

    assert halt is not None
    assert halt.reason == HALT_RECOVERY_WRITE_FAILURE == "recovery-write-failure"
    assert "recovery write failures" in halt.detail
    assert policy.consecutive_failures == 3


def test_two_infrastructure_failures_then_a_recovery_write_halt_with_recovery() -> None:
    """A mixed run of three halts with the reason of whichever fired last."""
    policy = HaltPolicy()

    assert policy.record_infrastructure_failure("infrastructure-failure: git") is None
    assert policy.record_infrastructure_failure("infrastructure-failure: github") is None
    halt = policy.record_recovery_write_failure()

    assert halt is not None
    assert halt.reason == HALT_RECOVERY_WRITE_FAILURE


def test_two_recovery_writes_then_an_infrastructure_failure_halt_with_infrastructure() -> (
    None
):
    """The mirror mix halts with ``infrastructure-failure`` and names the Attempt reason."""
    policy = HaltPolicy()
    halt_reason = "infrastructure-failure: provider"

    assert policy.record_recovery_write_failure() is None
    assert policy.record_recovery_write_failure() is None
    halt = policy.record_infrastructure_failure(halt_reason)

    assert halt is not None
    assert halt.reason == HALT_INFRASTRUCTURE_FAILURE
    assert halt_reason in halt.detail


def test_a_non_infrastructure_outcome_resets_the_counter() -> None:
    """Any Attempt without an infrastructure class restarts the count from zero."""
    policy = HaltPolicy()

    assert policy.record_infrastructure_failure("infrastructure-failure: git") is None
    assert policy.record_recovery_write_failure() is None
    policy.record_non_infrastructure_outcome()

    assert policy.consecutive_failures == 0
    assert policy.record_infrastructure_failure("infrastructure-failure: git") is None
    assert policy.record_infrastructure_failure("infrastructure-failure: git") is None
    assert policy.record_infrastructure_failure("infrastructure-failure: git") is not None


def test_a_settled_recovery_resets_the_counter() -> None:
    """A recovery pass with no write failure restarts the count from zero."""
    policy = HaltPolicy()

    assert policy.record_recovery_write_failure() is None
    assert policy.record_recovery_write_failure() is None
    policy.record_recovery_settled()

    assert policy.consecutive_failures == 0
    assert policy.record_recovery_write_failure() is None
    assert policy.record_recovery_write_failure() is None
    assert policy.record_recovery_write_failure() is not None


def test_resets_keep_independent_counters_from_drifting() -> None:
    """Alternating failures and resets never accumulate toward a halt."""
    policy = HaltPolicy()

    for _ in range(5):
        assert (
            policy.record_infrastructure_failure("infrastructure-failure: setup")
            is None
        )
        policy.record_non_infrastructure_outcome()
        assert policy.record_recovery_write_failure() is None
        policy.record_recovery_settled()

    assert policy.consecutive_failures == 0
