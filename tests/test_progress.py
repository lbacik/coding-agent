from __future__ import annotations

from pathlib import Path

from coding_agent.implement.ceilings import LoopCeilings, UsageTotals
from coding_agent.implement.progress import (
    GATE,
    IMPLEMENTED_BUT_UNVERIFIED,
    PROGRESS_DIFF,
    RESERVE,
    SAVED_PARTIAL_WORK,
    SOFT_STALL,
    VERIFIED_COMPLETION,
    AttemptPolicy,
    BudgetSnapshot,
    RunLedger,
    terminal_outcome,
)


def _snapshot(**values: float | int) -> BudgetSnapshot:
    return BudgetSnapshot(
        elapsed_seconds=float(values.get("elapsed", 0)),
        usage=UsageTotals(
            tokens=int(values.get("tokens", 0)),
            cost_usd=float(values.get("cost", 0)),
            tool_calls=int(values.get("tools", 0)),
        ),
    )


def test_policy_counts_only_qualifying_progress_and_stalls_after_two_responses() -> None:
    policy = AttemptPolicy("51/1", LoopCeilings(max_cost_usd=10))

    assert policy.observe_response(_snapshot()) == ()
    assert policy.observe_response(_snapshot()) == (SOFT_STALL,)
    policy.record_progress(PROGRESS_DIFF, _snapshot(), diff_ref="git-diff:1")
    assert policy.observe_response(_snapshot()) == ()
    assert policy.non_progress_responses == 1


def test_policy_enters_gate_then_reserve_on_the_first_meter_to_cross() -> None:
    policy = AttemptPolicy("51/1", LoopCeilings(max_cost_usd=10))

    assert policy.observe_budget(_snapshot(cost=7)) == (GATE,)
    assert policy.phase == "gate"
    assert policy.observe_budget(_snapshot(cost=8)) == (RESERVE,)
    assert str(policy.phase) == "verification_reserve"
    assert policy.allows("exploration") is False
    assert policy.allows("verification") is True


def test_run_ledger_persists_progress_stall_gate_reserve_and_outcome(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3", ceilings=LoopCeilings(max_cost_usd=10))
    snapshot = _snapshot(cost=2, tokens=20, tools=1)

    ledger.record_progress("#51/1", PROGRESS_DIFF, snapshot, diff_ref="git-diff:1")
    ledger.record("#51/1", SOFT_STALL, snapshot, detail="two responses")
    ledger.record("#51/1", GATE, snapshot, detail="run targeted test")
    ledger.record("#51/1", RESERVE, snapshot, detail="validation reserve")
    ledger.record_outcome("#51/1", SAVED_PARTIAL_WORK, snapshot, detail="hard limit")

    records = ledger.records("#51/1")
    assert [record.kind for record in records] == [
        "progress_event",
        SOFT_STALL,
        GATE,
        RESERVE,
        "terminal_outcome",
    ]
    assert records[0].payload["progress_kind"] == PROGRESS_DIFF
    assert records[0].diff_ref == "git-diff:1"
    assert records[-1].payload["outcome"] == SAVED_PARTIAL_WORK
    assert records[-1].remaining_budget["cost_usd"] == 8.0


def test_terminal_outcome_uses_one_category_and_only_clean_validation_is_verified() -> None:
    assert terminal_outcome(delivery_pushed=True, validation_clean=True, stopped_by=None) == VERIFIED_COMPLETION
    assert terminal_outcome(delivery_pushed=True, validation_clean=False, stopped_by=None) == IMPLEMENTED_BUT_UNVERIFIED
    assert terminal_outcome(delivery_pushed=True, validation_clean=True, stopped_by="wall_clock") == VERIFIED_COMPLETION
