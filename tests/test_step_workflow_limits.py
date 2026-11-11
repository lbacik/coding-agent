"""Step-workflow limits: meters, Attempt Ceiling, allowances and admission.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §11 (four
meters, the Attempt Ceiling and Step Allowances, Reserved Steps and the Work
Pool, worst-case admission with holds, the output cap, tool-call counting,
deadlines, operations still running after the ceiling, the
``limit-exceeded`` fact and its precedence).

Slice S5 of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: LIM-1 … LIM-17 (LIM-17 is L1 and stays skipped here).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
)
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2FieldType,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.scripted_model import ScriptedModel


def _prices() -> TokenPrices:
    """One dollar a megatoken in, two out; reads a tenth, writes at par."""
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


class FakeClock:
    """A deterministic wall clock for budget tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ceiling(
    *,
    cost: float = 100000.0,
    tokens: float = 100000.0,
    tools: float = 10000.0,
    wall: float = 600.0,
) -> V2Ceiling:
    return V2Ceiling(
        cost_usd=cost, tokens=tokens, tool_calls=tools, wall_clock_minutes=wall
    )


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, V2FieldType]],
    *,
    tools: tuple[str, ...] = ("workspace-read",),
    limits: dict[str, float] | None = None,
    reserved: bool = False,
    routes: dict[str, V2RouteTarget] | None = None,
    max_executions: int = 1,
) -> V2Step:
    targets = routes or {
        "done": V2RouteTarget(end="completed"),
        "limit-exceeded": V2RouteTarget(end="unfinished"),
        "error": V2RouteTarget(end="failed"),
    }
    return V2Step(
        id=step_id,
        agent=V2Agent(tools=tools, results=results),
        max_executions=max_executions,
        limits=dict(limits or {}),
        reserved=reserved,
        routes=dict(targets),
    )


def _op_step(
    step_id: str, operation: str, routes: dict[str, V2RouteTarget]
) -> V2Step:
    return V2Step(id=step_id, operation=operation, routes=dict(routes))


def _workflow(
    workflow_id: str, steps: dict[str, V2Step], start: str, ceiling: V2Ceiling
) -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type="issue", label="agent:test"),
        ceiling=ceiling,
        start=start,
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps=dict(steps),
    )


def _budgets(
    workflow: V2Workflow,
    clock: FakeClock,
    *,
    output_cap: int = 0,
    attempt_id: str = "implement#7/1",
) -> AttemptBudgets:
    return build_budgets(
        workflow,
        prices=_prices(),
        output_cap=output_cap,
        attempt_id=attempt_id,
        opened_at=clock.now,
        clock=clock,
    )


def _facts(attempt_id: str = "implement#7/1") -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id=attempt_id,
        workflow_id="implement",
        input_type="issue",
        input_number=7,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )


def _deps(
    attempt: StepAttemptFacts,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    budgets: AttemptBudgets,
    **overrides: Any,
) -> AgentStepDeps:
    params: dict[str, Any] = {
        "attempt": attempt,
        "model": model,
        "workspace": workspace,
        "tree_id": "tree-1",
        "archive_dir": tmp_path / "archive",
        "budgets": budgets,
        "prices": _prices(),
    }
    params.update(overrides)
    return AgentStepDeps(**params)


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _usage(
    input_tokens: int, output_tokens: int, *, cache_read: int = 0
) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {
            "cache_read": cache_read,
            "cache_creation": 0,
        },
    }


def _report_ai(
    result: str,
    payload: dict[str, object],
    usage: dict[str, object] | None = None,
    call_id: str = "report-1",
) -> AIMessage:
    return AIMessage(
        content="reporting",
        tool_calls=[
            _call(
                REPORT_TOOL_NAME,
                {"result": result, "payload_json": json.dumps(payload)},
                call_id,
            )
        ],
        usage_metadata=usage,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


# --- LIM-1: the four meters ----------------------------------------------------


def test_lim1_four_meters_metered_per_step_and_per_attempt() -> None:
    """LIM-1: cost, effective tokens, tool calls and wall clock are metered."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}})},
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock)
    clock.advance(120.0)
    budgets.add_spend(
        "process",
        UsageTotals(
            tokens=1000,
            effective_tokens=800,
            cost_usd=0.002,
            tool_calls=3,
            input_tokens=900,
            output_tokens=100,
            cache_read_tokens=200,
            cache_write_tokens=0,
        ),
    )
    budgets.add_step_seconds("process", 30.0)

    attempt = budgets.attempt_totals()
    assert attempt.cost_usd == pytest.approx(0.002)
    assert attempt.effective_tokens == 800
    assert attempt.tool_calls == 3
    assert budgets.elapsed_seconds() == pytest.approx(120.0)
    assert budgets.step_seconds("process") == pytest.approx(30.0)

    step = budgets.step_totals("process")
    assert step.cost_usd == pytest.approx(0.002)
    assert step.effective_tokens == 800
    assert step.tool_calls == 3


def test_lim1_cache_reads_are_excluded_from_effective_tokens(
    workspace: Path, tmp_path: Path
) -> None:
    """LIM-1: repeated context (cache reads) is not new model work."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}})},
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=64)
    model = ScriptedModel(
        [_report_ai("done", {}, _usage(1000, 100, cache_read=900))]
    )
    deps = _deps(_facts(), model, workspace, tmp_path, budgets)
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    assert outcome.status == "reported"
    spent = budgets.step_totals("process")
    assert spent.effective_tokens == 200
    assert spent.cost_usd == pytest.approx((100 / 1_000_000 * 1.0) + (100 / 1_000_000 * 2.0) + (900 / 1_000_000 * 0.1))


def test_lim1_there_is_no_model_request_counter() -> None:
    """LIM-1: repetition is bounded by Execution Ceilings, not a counter."""
    names = {field.name for field in fields(AttemptBudgets)}
    assert "model_calls" not in names
    assert "request_count" not in names
    assert "requests" not in names


# --- LIM-2: Step Allowances accumulate -----------------------------------------


def test_lim2_allowance_accumulates_and_exhaustion_routes_to_handoff(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """LIM-2: every execution of a step draws on one allowance."""
    clock = FakeClock()
    process = _agent_step(
        "process",
        {"done": {}},
        limits={"tokens": 100.0},
        routes={
            "done": V2RouteTarget(end="completed"),
            "limit-exceeded": V2RouteTarget(step="ai-handoff"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    handoff = _agent_step(
        "ai-handoff",
        {"done": {}},
        limits={
            "cost-usd": 50.0,
            "tokens": 5000.0,
            "tool-calls": 50.0,
            "wall-clock-minutes": 60.0,
        },
        reserved=True,
    )
    workflow = _workflow(
        "implement", {"process": process, "ai-handoff": handoff}, "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=64)
    # Two earlier executions already spent 90 of the 100-token allowance.
    budgets.add_spend("process", UsageTotals(tokens=50, effective_tokens=50))
    budgets.add_spend("process", UsageTotals(tokens=40, effective_tokens=40))
    # The third entry's bound (a ~500-token opening plus the 64 cap) no
    # longer fits the remaining 10 tokens.
    model = ScriptedModel([_report_ai("done", {}, _usage(10, 5))])
    deps = _deps(_facts(), model, workspace, tmp_path, budgets)
    outcome = run_agent_step(workflow.steps["process"], 3, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["scope"] == "step"
    assert outcome.limit_fact["moment"] == "before-request"
    assert model.invocations == []

    # At the graph the over-budget step ends limit-exceeded on admission
    # (90 of 100 spent, the ~500-token opening plus the 64 cap no longer
    # fits) and routes to the Reserved handoff, which runs on its intact
    # reserve and completes the Attempt.
    model2 = ScriptedModel([_report_ai("done", {}, _usage(10, 5))])
    deps2 = _deps(_facts(), model2, workspace, tmp_path, budgets)
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        agent_deps=deps2,
        budgets=budgets,
    )
    assert result.outcome == "completed"
    executions = [
        (record.step_id, record.result)
        for record in ledger.list_step_executions("implement#7/1")
    ]
    assert executions == [("process", "limit-exceeded"), ("ai-handoff", "done")]


# --- LIM-3: the Work Pool ------------------------------------------------------


def test_lim3_pool_exhaustion_spares_the_reserve() -> None:
    """LIM-3: the pool is the ceiling minus reserves; reserves survive it."""
    clock = FakeClock()
    handoff = _agent_step(
        "ai-handoff",
        {"handoff": {}},
        limits={
            "cost-usd": 20.0,
            "tokens": 200.0,
            "tool-calls": 20.0,
            "wall-clock-minutes": 10.0,
        },
        reserved=True,
    )
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step("process", {"done": {}}),
            "ai-handoff": handoff,
        },
        "process",
        _ceiling(cost=100.0, tokens=1000.0, tools=100.0, wall=60.0),
    )
    budgets = _budgets(workflow, clock)
    assert budgets.pool["tokens"] == pytest.approx(800.0)
    budgets.add_spend(
        "process", UsageTotals(tokens=801, effective_tokens=801, cost_usd=0.001)
    )
    process_fact = budgets.check_entry("process")
    assert process_fact is not None
    assert process_fact.scope == "work-pool"
    assert budgets.check_entry("ai-handoff") is None


# --- LIM-4: reserve exhaustion -------------------------------------------------


def test_lim4_reserve_exhaustion_routes_past_the_handoff(
    ledger: RunLedger,
) -> None:
    """LIM-4: an exhausted reserve ends the handoff; the Hard Handoff runs."""
    clock = FakeClock()
    handoff = _agent_step(
        "ai-handoff",
        {"handoff": {}},
        limits={
            "cost-usd": 20.0,
            "tokens": 200.0,
            "tool-calls": 20.0,
            "wall-clock-minutes": 10.0,
        },
        reserved=True,
        routes={
            "handoff": V2RouteTarget(end="completed"),
            "limit-exceeded": V2RouteTarget(step="hard-handoff"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    hard = _op_step(
        "hard-handoff",
        "commit-and-push",
        {
            "pushed": V2RouteTarget(end="unfinished"),
            "no-changes": V2RouteTarget(end="unfinished"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process",
                {"done": {}},
                routes={
                    "done": V2RouteTarget(end="completed"),
                    "limit-exceeded": V2RouteTarget(step="ai-handoff"),
                    "error": V2RouteTarget(end="failed"),
                },
            ),
            "ai-handoff": handoff,
            "hard-handoff": hard,
        },
        "process",
        _ceiling(cost=100.0, tokens=1000.0, tools=100.0, wall=60.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.add_spend(
        "process", UsageTotals(tokens=801, effective_tokens=801, cost_usd=0.001)
    )
    # The reserve is strictly exhausted on cost (25 of 20 dollars) while
    # the Attempt Ceiling still holds (811 of 1000 tokens, ~25 of 100
    # dollars), so the reserve stays the primary reason.
    budgets.add_spend(
        "ai-handoff", UsageTotals(tokens=10, effective_tokens=10, cost_usd=25.0)
    )
    assert budgets.check_entry("process") is not None
    reserve_fact = budgets.check_entry("ai-handoff")
    assert reserve_fact is not None
    assert reserve_fact.scope == "reserve"

    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={"hard-handoff": ["pushed"]},
        budgets=budgets,
    )
    assert result.outcome == "unfinished"
    executions = [
        (record.step_id, record.result)
        for record in ledger.list_step_executions("implement#7/1")
    ]
    assert executions == [("hard-handoff", "pushed")]
    refused = [
        (record.to_step, record.reason)
        for record in ledger.list_step_transitions("implement#7/1")
        if record.kind == "refused"
    ]
    assert refused == [("process", "limit-exceeded"), ("ai-handoff", "limit-exceeded")]


# --- LIM-5: Reserved Steps draw only on their allowance ------------------------


def test_lim5_reserved_draws_only_on_its_allowance() -> None:
    """LIM-5: pool headroom is irrelevant to a Reserved Step; re-entry keeps the remainder."""
    clock = FakeClock()
    handoff = _agent_step(
        "ai-handoff",
        {"handoff": {}},
        limits={
            "cost-usd": 20.0,
            "tokens": 200.0,
            "tool-calls": 20.0,
            "wall-clock-minutes": 10.0,
        },
        reserved=True,
    )
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}}), "ai-handoff": handoff},
        "process",
        _ceiling(cost=100.0, tokens=1000.0, tools=100.0, wall=60.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.add_spend(
        "process", UsageTotals(tokens=700, effective_tokens=700)
    )
    # 700 of the 800-token pool is spent, yet the reserve is intact.
    assert budgets.check_entry("ai-handoff") is None
    fact, hold = budgets.admit_request("ai-handoff", 150)
    assert fact is None and hold is not None
    budgets.settle_hold(hold, "ai-handoff", UsageTotals(tokens=150, effective_tokens=150))
    # Re-entry gets only the remaining 50 tokens of the same allowance.
    assert budgets.check_entry("ai-handoff") is None
    refused, _ = budgets.admit_request("ai-handoff", 51)
    assert refused is not None
    assert refused.scope == "reserve"
    # And draining the pool afterwards still leaves the 50-token remainder:
    # 700 + 101 = 801 spent against the 800-token pool.
    budgets.add_spend("process", UsageTotals(tokens=101, effective_tokens=101))
    assert budgets.check_entry("process") is not None
    assert budgets.check_entry("ai-handoff") is None


# --- LIM-6: worst-case admission ------------------------------------------------


def test_lim6_request_beyond_remaining_is_not_sent(
    workspace: Path, tmp_path: Path
) -> None:
    """LIM-6: the bound is held before the request; over-bound is refused."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}}, limits={"tokens": 100.0})},
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=1000)
    model = ScriptedModel([_report_ai("done", {}, _usage(10, 5))])
    deps = _deps(_facts(), model, workspace, tmp_path, budgets)
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["moment"] == "before-request"
    assert outcome.limit_fact["scope"] == "step"
    assert model.invocations == []
    assert budgets.open_hold_ids() == ()


def test_lim6_admission_holds_the_bound_until_settled() -> None:
    """LIM-6: the admitted bound is held, then settled with actual usage."""
    clock = FakeClock()
    workflow = _workflow(
        "implement", {"process": _agent_step("process", {"done": {}})}, "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=64)
    fact, hold = budgets.admit_request("process", 500)
    assert fact is None and hold is not None
    assert budgets.open_hold_ids() == (hold,)
    # The held worst case is priced: 500 estimate tokens as uncached input
    # plus the 64-token output cap (1.0/2.0 dollars per megatoken).
    assert budgets.held_totals()[0] == 564
    assert budgets.held_totals()[1] == pytest.approx(0.000628)
    budgets.settle_hold(
        hold, "process", UsageTotals(tokens=120, effective_tokens=100, cost_usd=0.001)
    )
    assert budgets.open_hold_ids() == ()
    assert budgets.attempt_totals().effective_tokens == 100


# --- LIM-7: concurrent admission -------------------------------------------------


def test_lim7_second_concurrent_request_sees_the_first_hold() -> None:
    """LIM-7: two requests that fit alone do not both fit together."""
    clock = FakeClock()
    workflow = _workflow(
        "implement", {"process": _agent_step("process", {"done": {}})}, "process",
        _ceiling(tokens=100.0),
    )
    budgets = _budgets(workflow, clock)
    first_fact, first_hold = budgets.admit_request("process", 60)
    assert first_fact is None and first_hold is not None
    second_fact, second_hold = budgets.admit_request("process", 60)
    assert second_fact is not None
    assert second_fact.moment == "before-request"
    assert second_hold is None
    budgets.settle_hold(
        first_hold, "process", UsageTotals(tokens=10, effective_tokens=10)
    )
    third_fact, third_hold = budgets.admit_request("process", 60)
    assert third_fact is None and third_hold is not None


def test_lim7_spawn_group_holds_each_member(
    workspace: Path, tmp_path: Path
) -> None:
    """LIM-7: the group admits every member up front against the same headroom.

    Each 4000-character instruction estimates to 1000 tokens against a
    1500-token Step Allowance: the first fits alone, the second sees the
    first's hold in force and is refused without running.
    """
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process",
                {"done": {}},
                tools=("workspace-read", "subagents"),
                limits={"tokens": 1500.0},
            )
        },
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock)
    model = ScriptedModel(
        [
            AIMessage(
                content="spawning",
                tool_calls=[
                    _call(
                        "spawn_subagent",
                        {"description": "first", "instructions": "x" * 4000},
                        "spawn-1",
                    ),
                    _call(
                        "spawn_subagent",
                        {"description": "second", "instructions": "y" * 4000},
                        "spawn-2",
                    ),
                ],
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ]
    )
    calls: list[tuple[str, str, str]] = []

    def _runner(call_id: str, description: str, instructions: str) -> str:
        calls.append((call_id, description, instructions))
        return "subagent text"

    deps = _deps(
        _facts(), model, workspace, tmp_path, budgets, subagent_runner=_runner
    )
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    # Only the first spawn ran: the second was refused while the first hold
    # was in force. The refused spawn's tool result names the fact.
    assert [call[0] for call in calls] == ["spawn-1"]
    transcript = (tmp_path / "archive" / "transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert "limit-exceeded" in transcript
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["scope"] == "step"


def test_lim7_spawn_admissions_are_recorded_durably(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """LIM-7: the hold is in force in the ledger while the request runs."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process", {"done": {}}, tools=("workspace-read", "subagents")
            )
        },
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, attempt_id="implement#7/1")
    spawns = _call(
        "spawn_subagent", {"description": "s", "instructions": "read"}, "spawn-1"
    )
    model = ScriptedModel(
        [
            AIMessage(content="spawning", tool_calls=[spawns]),
            _report_ai("done", {}, _usage(10, 5)),
        ]
    )
    seen: list[str] = []

    def _runner(call_id: str, description: str, instructions: str) -> str:
        seen.append(call_id)
        open_holds = ledger.list_open_admission_holds("implement#7/1")
        assert len(open_holds) == 1
        assert open_holds[0].step_id == "process"
        return "subagent text"

    deps = _deps(
        _facts(),
        model,
        workspace,
        tmp_path,
        budgets,
        ledger=ledger,
        subagent_runner=_runner,
    )
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    assert outcome.status == "reported"
    assert seen == ["spawn-1"]
    assert ledger.list_open_admission_holds("implement#7/1") == ()


# --- LIM-8: charging the worst case ----------------------------------------------


def test_lim8_failed_request_without_usage_is_charged_its_worst_case() -> None:
    """LIM-8: a failure with no usage metadata charges the admitted bound."""
    clock = FakeClock()
    workflow = _workflow(
        "implement", {"process": _agent_step("process", {"done": {}})}, "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=64)
    fact, hold = budgets.admit_request("process", 500)
    assert fact is None and hold is not None
    budgets.charge_hold(hold)
    charged = budgets.attempt_totals()
    assert charged.tokens == 564
    assert charged.effective_tokens == 564
    assert budgets.step_totals("process").tokens == 564


def test_lim8_crash_with_an_open_hold_charges_every_open_hold() -> None:
    """LIM-8: recovery charges each hold still open after the crash."""
    clock = FakeClock()
    workflow = _workflow(
        "implement", {"process": _agent_step("process", {"done": {}})}, "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=64)
    _, first = budgets.admit_request("process", 100)
    _, second = budgets.admit_request("process", 200)
    assert first is not None and second is not None
    # Recovery replays the ledger's still-open holds and charges each one.
    for hold_id in budgets.open_hold_ids():
        budgets.charge_hold(hold_id)
    assert budgets.attempt_totals().tokens == (100 + 64) + (200 + 64)
    assert budgets.open_hold_ids() == ()


def test_lim8_charged_hold_is_recorded_as_charged(
    ledger: RunLedger,
) -> None:
    """LIM-8: the ledger settles a charged hold as charged, not settled."""
    record = ledger.record_admission_hold(
        "implement#7/1", "process", 1, "hold-crash",
        bound_tokens=564, bound_cost_usd=0.001,
    )
    assert record.status == "held"
    ledger.settle_admission_hold("hold-crash", status="charged")
    assert ledger.get_admission_hold("hold-crash") is not None
    assert ledger.get_admission_hold("hold-crash") is not None
    stored = ledger.get_admission_hold("hold-crash")
    assert stored is not None
    assert stored.status == "charged"


# --- LIM-9: tool-call counting -----------------------------------------------------


def test_lim9_tool_call_beyond_the_limit_is_not_executed(
    workspace: Path, tmp_path: Path
) -> None:
    """LIM-9: calls count when requested; over-limit calls never run."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process",
                {"done": {}},
                tools=("shell",),
                limits={"tool-calls": 1.0},
            )
        },
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock)
    ran: list[str] = []

    def _shell(command: str) -> str:
        ran.append(command)
        return "exit_code=0\n"

    model = ScriptedModel(
        [
            AIMessage(
                content="running",
                tool_calls=[
                    _call("run_command", {"command": "echo one"}, "c1"),
                    _call("run_command", {"command": "echo two"}, "c2"),
                ],
                usage_metadata=_usage(50, 10),
            )
        ]
    )
    deps = _deps(
        _facts(), model, workspace, tmp_path, budgets, shell_hook=_shell
    )
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["moment"] == "tool-call"
    assert ran == []
    # The requested calls still count toward the reported spend.
    assert budgets.step_totals("process").tool_calls == 2


# --- LIM-10: timeouts ---------------------------------------------------------------


def test_lim10_timeout_is_the_minimum_of_code_owned_and_remaining() -> None:
    """LIM-10: every command and model request races the remaining time."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}})},
        "process",
        _ceiling(wall=60.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.code_owned_command_timeout = 600.0
    budgets.code_owned_model_timeout = 300.0
    assert budgets.command_timeout_for("process") == pytest.approx(600.0)
    assert budgets.model_timeout_for("process") == pytest.approx(300.0)
    clock.advance(60.0 * 60.0 - 50.0)
    assert budgets.command_timeout_for("process") == pytest.approx(50.0)
    assert budgets.model_timeout_for("process") == pytest.approx(50.0)
    clock.advance(60.0)
    assert budgets.command_timeout_for("process") == pytest.approx(0.0)
    assert budgets.model_timeout_for("process") == pytest.approx(0.0)


def test_lim10_no_remaining_time_bars_the_tool_but_keeps_the_report(
    workspace: Path, tmp_path: Path
) -> None:
    """LIM-10: an expired command never starts; the step ends on deadline."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}}, tools=("shell",))},
        "process",
        _ceiling(wall=10.0),
    )
    budgets = _budgets(workflow, clock)
    clock.advance(10.0 * 60.0 + 1.0)
    ran: list[str] = []

    def _shell(command: str) -> str:
        ran.append(command)
        return "exit_code=0\n"

    model = ScriptedModel(
        [
            AIMessage(
                content="running",
                tool_calls=[_call("run_command", {"command": "echo hi"}, "c1")],
                usage_metadata=_usage(50, 10),
            )
        ]
    )
    deps = _deps(
        _facts(), model, workspace, tmp_path, budgets, shell_hook=_shell
    )
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["moment"] == "deadline"
    assert ran == []
    assert model.invocations == []


# --- LIM-11: after the Attempt Ceiling ----------------------------------------------


def test_lim11_agent_steps_refused_operations_still_run(
    ledger: RunLedger,
) -> None:
    """LIM-11: past the ceiling no model work starts, but operations run."""
    clock = FakeClock()
    process = _agent_step(
        "process",
        {"done": {}},
        routes={
            "done": V2RouteTarget(end="completed"),
            "limit-exceeded": V2RouteTarget(step="hard-handoff"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    hard = _op_step(
        "hard-handoff",
        "commit-and-push",
        {
            "pushed": V2RouteTarget(end="unfinished"),
            "no-changes": V2RouteTarget(end="unfinished"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    workflow = _workflow(
        "implement",
        {"process": process, "hard-handoff": hard},
        "process",
        _ceiling(cost=1.0, tokens=1000.0, tools=100.0, wall=60.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.add_spend(
        "process", UsageTotals(tokens=999, effective_tokens=999, cost_usd=1.5)
    )
    assert budgets.check_entry("process") is not None

    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={"hard-handoff": ["pushed"]},
        budgets=budgets,
    )
    assert result.outcome == "unfinished"
    executions = [
        (record.step_id, record.result)
        for record in ledger.list_step_executions("implement#7/1")
    ]
    assert executions == [("hard-handoff", "pushed")]


# --- LIM-12: the limit fact and precedence -------------------------------------------


def test_lim12_precedence_attempt_over_pool_over_step() -> None:
    """LIM-12: the primary reason follows Attempt > pool > Step Allowance."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process", {"done": {}}, limits={"cost-usd": 5.0}
            )
        },
        "process",
        _ceiling(cost=8.0, tokens=100000.0, tools=10000.0, wall=600.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.add_spend("process", UsageTotals(cost_usd=4.0))
    step_only = budgets.check_after_spend("process")
    assert step_only is None
    budgets.add_spend("process", UsageTotals(cost_usd=2.0))
    # 6 dollars: over the 5-dollar Step Allowance and the 8-dollar pool and
    # ceiling alike — the Attempt Ceiling is the primary reason.
    budgets.add_spend("process", UsageTotals(cost_usd=3.0))
    fact = budgets.check_after_spend("process")
    assert fact is not None
    assert fact.scope == "attempt-ceiling"
    assert fact.dimensions == ("cost-usd",)
    assert fact.limit["cost-usd"] == pytest.approx(8.0)
    assert fact.spend["cost-usd"] == pytest.approx(9.0)


def test_lim12_pool_beats_step_and_every_dimension_is_recorded() -> None:
    """LIM-12: the fact carries every crossed dimension with limit and spend."""
    clock = FakeClock()
    # The reserve separates the Work Pool from the Attempt Ceiling: the
    # 150-token, 6-dollar spend crosses the 50-token/5-dollar Step Allowance
    # and the 100-token Work Pool while the 200-token Attempt Ceiling holds,
    # so the pool is the primary reason.
    handoff = _agent_step(
        "ai-handoff",
        {"handoff": {}},
        limits={
            "cost-usd": 90.0,
            "tokens": 100.0,
            "tool-calls": 100.0,
            "wall-clock-minutes": 60.0,
        },
        reserved=True,
    )
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process",
                {"done": {}},
                limits={"cost-usd": 5.0, "tokens": 50.0},
            ),
            "ai-handoff": handoff,
        },
        "process",
        _ceiling(cost=100.0, tokens=200.0, tools=10000.0, wall=600.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.add_spend(
        "process",
        UsageTotals(cost_usd=6.0, tokens=150, effective_tokens=150),
    )
    fact = budgets.check_after_spend("process", moment="tool-call")
    assert fact is not None
    assert fact.scope == "work-pool"
    assert fact.dimensions == ("cost-usd", "tokens")
    assert fact.moment == "tool-call"
    assert fact.limit["cost-usd"] == pytest.approx(5.0)
    assert fact.limit["tokens"] == pytest.approx(100.0)
    assert fact.spend["cost-usd"] == pytest.approx(6.0)
    assert fact.spend["tokens"] == pytest.approx(150.0)
    stored = fact.to_dict()
    assert stored["scope"] == "work-pool"
    assert stored["moment"] == "tool-call"


# --- LIM-13: Sub-agent, handoff and retry spend ----------------------------------------


def test_lim13_spawn_tool_calls_count_against_step_and_attempt(
    workspace: Path, tmp_path: Path
) -> None:
    """LIM-13: Sub-agent requests count against the caller and the Attempt."""
    clock = FakeClock()
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process", {"done": {}}, tools=("workspace-read", "subagents")
            )
        },
        "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock)
    spawns = [
        _call(
            "spawn_subagent",
            {"description": "one", "instructions": "read"},
            "spawn-1",
        ),
        _call(
            "spawn_subagent",
            {"description": "two", "instructions": "read"},
            "spawn-2",
        ),
    ]
    model = ScriptedModel(
        [
            AIMessage(
                content="spawning",
                tool_calls=spawns,
                usage_metadata=_usage(50, 10),
            ),
            _report_ai("done", {}, _usage(50, 10)),
        ]
    )

    def _runner(call_id: str, description: str, instructions: str) -> str:
        return "subagent text"

    deps = _deps(
        _facts(), model, workspace, tmp_path, budgets, subagent_runner=_runner
    )
    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})
    assert outcome.status == "reported"
    assert budgets.step_totals("process").tool_calls == 2
    assert budgets.attempt_totals().tool_calls == 2


def test_lim13_retry_and_handoff_spend_fold_into_step_and_attempt() -> None:
    """LIM-13: a charged retry and a handoff both stay on the step's bill."""
    clock = FakeClock()
    workflow = _workflow(
        "implement", {"process": _agent_step("process", {"done": {}})}, "process",
        _ceiling(),
    )
    budgets = _budgets(workflow, clock, output_cap=64)
    _, hold = budgets.admit_request("process", 500)
    assert hold is not None
    budgets.charge_hold(hold)
    _, retry = budgets.admit_request("process", 500)
    assert retry is not None
    budgets.settle_hold(
        retry, "process", UsageTotals(tokens=120, effective_tokens=100)
    )
    budgets.add_spend(
        "process", UsageTotals(tokens=40, effective_tokens=40, cost_usd=0.001)
    )
    step = budgets.step_totals("process")
    assert step.tokens == 564 + 120 + 40
    assert budgets.attempt_totals().tokens == step.tokens


# --- LIM-14: implement / Process over budget --------------------------------------------


def test_lim14_process_over_budget_trace(
    ledger: RunLedger,
) -> None:
    """LIM-14: ``process#1=limit-exceeded, ai-handoff#1=limit-exceeded, hard-handoff#1=pushed``."""
    clock = FakeClock()
    handoff = _agent_step(
        "ai-handoff",
        {"handoff": {}},
        limits={
            "cost-usd": 20.0,
            "tokens": 200.0,
            "tool-calls": 20.0,
            "wall-clock-minutes": 10.0,
        },
        reserved=True,
        routes={
            "handoff": V2RouteTarget(step="hard-handoff"),
            "limit-exceeded": V2RouteTarget(step="hard-handoff"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    workflow = _workflow(
        "implement",
        {
            "process": _agent_step(
                "process",
                {"done": {}},
                limits={"tokens": 100.0},
                routes={
                    "done": V2RouteTarget(step="validate"),
                    "limit-exceeded": V2RouteTarget(step="ai-handoff"),
                    "error": V2RouteTarget(end="failed"),
                },
            ),
            "validate": _op_step(
                "validate",
                "run-validation",
                {
                    "passed": V2RouteTarget(end="completed"),
                    "failed": V2RouteTarget(end="unfinished"),
                    "limit-exceeded": V2RouteTarget(end="unfinished"),
                    "error": V2RouteTarget(end="failed"),
                },
            ),
            "ai-handoff": handoff,
            "hard-handoff": _op_step(
                "hard-handoff",
                "commit-and-push",
                {
                    "pushed": V2RouteTarget(end="unfinished"),
                    "no-changes": V2RouteTarget(end="unfinished"),
                    "limit-exceeded": V2RouteTarget(end="unfinished"),
                    "error": V2RouteTarget(end="failed"),
                },
            ),
        },
        "process",
        _ceiling(cost=100.0, tokens=1000.0, tools=100.0, wall=60.0),
    )
    budgets = _budgets(workflow, clock)
    budgets.add_spend("process", UsageTotals(tokens=101, effective_tokens=101))
    # The reserve is strictly exhausted (201 of 200), so the handoff is
    # refused at entry and only the Hard Handoff executes.
    budgets.add_spend(
        "ai-handoff", UsageTotals(tokens=201, effective_tokens=201)
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={"hard-handoff": ["pushed"]},
        budgets=budgets,
    )
    assert result.outcome == "unfinished"
    assert result.reason == (
        "ended at end unfinished after result pushed of step hard-handoff"
    )
    executions = [
        (record.step_id, record.result)
        for record in ledger.list_step_executions("implement#7/1")
    ]
    assert executions == [("hard-handoff", "pushed")]
    refused = [
        (record.to_step, record.reason)
        for record in ledger.list_step_transitions("implement#7/1")
        if record.kind == "refused"
    ]
    assert refused == [("process", "limit-exceeded"), ("ai-handoff", "limit-exceeded")]


# --- LIM-15: review / Review over budget --------------------------------------------------


def test_lim15_review_over_budget_trace(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """LIM-15: ``review#1=limit-exceeded, ai-handoff#1=partial, handoff-comment#1=posted``."""
    clock = FakeClock()
    handoff = _agent_step(
        "ai-handoff",
        {"partial": {}},
        limits={
            "cost-usd": 50.0,
            "tokens": 5000.0,
            "tool-calls": 50.0,
            "wall-clock-minutes": 30.0,
        },
        reserved=True,
        routes={
            "partial": V2RouteTarget(step="handoff-comment"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    workflow = _workflow(
        "review",
        {
            "review": _agent_step(
                "review",
                {"reviewed": {}},
                routes={
                    "reviewed": V2RouteTarget(end="completed"),
                    "limit-exceeded": V2RouteTarget(step="ai-handoff"),
                    "error": V2RouteTarget(end="failed"),
                },
            ),
            "ai-handoff": handoff,
            "handoff-comment": _op_step(
                "handoff-comment",
                "post-comment",
                {
                    "posted": V2RouteTarget(end="unfinished"),
                    "limit-exceeded": V2RouteTarget(end="unfinished"),
                    "error": V2RouteTarget(end="failed"),
                },
            ),
        },
        "review",
        # The reserve (5000 tokens) stays carved out of the ceiling, so the
        # 1000-token Work Pool is exhausted by the 1001-token review spend
        # while the Attempt Ceiling still admits the handoff's bound.
        _ceiling(cost=100.0, tokens=6000.0, tools=100.0, wall=60.0),
    )
    budgets = _budgets(workflow, clock, output_cap=64, attempt_id="review#9/1")
    budgets.add_spend(
        "review", UsageTotals(tokens=1001, effective_tokens=1001, cost_usd=0.001)
    )
    assert budgets.check_entry("review") is not None
    assert budgets.check_entry("ai-handoff") is None
    model = ScriptedModel([_report_ai("partial", {}, _usage(100, 20))])
    deps = _deps(
        _facts("review#9/1"), model, workspace, tmp_path, budgets
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="review#9/1",
        ledger=ledger,
        script={"handoff-comment": ["posted"]},
        agent_deps=deps,
        budgets=budgets,
    )
    assert result.outcome == "unfinished"
    executions = [
        (record.step_id, record.result)
        for record in ledger.list_step_executions("review#9/1")
    ]
    assert executions == [("ai-handoff", "partial"), ("handoff-comment", "posted")]
    refused = [
        (record.to_step, record.reason)
        for record in ledger.list_step_transitions("review#9/1")
        if record.kind == "refused"
    ]
    assert refused == [("review", "limit-exceeded")]


# --- LIM-16: wall clock with a Reserved Step -------------------------------------------------


def test_lim16_non_reserved_stops_at_deadline_minus_reserves() -> None:
    """LIM-16: model work stops at the Attempt deadline minus reserves."""
    clock = FakeClock()
    handoff = _agent_step(
        "ai-handoff",
        {"handoff": {}},
        limits={
            "cost-usd": 20.0,
            "tokens": 200.0,
            "tool-calls": 20.0,
            "wall-clock-minutes": 4.0,
        },
        reserved=True,
    )
    workflow = _workflow(
        "implement",
        {"process": _agent_step("process", {"done": {}}), "ai-handoff": handoff},
        "process",
        _ceiling(wall=10.0),
    )
    budgets = _budgets(workflow, clock)
    assert budgets.remaining_wall_seconds("process") == pytest.approx(6.0 * 60.0)
    assert budgets.remaining_wall_seconds("ai-handoff") == pytest.approx(4.0 * 60.0)
    clock.advance(6.0 * 60.0 + 1.0)
    # The Attempt clock runs from opening, so the non-reserved step is spent
    # while the reserve — measured on its own executions — still has room.
    assert budgets.remaining_wall_seconds("process") == pytest.approx(0.0)
    process_fact = budgets.check_entry("process")
    assert process_fact is not None
    assert process_fact.scope == "work-pool"
    assert budgets.check_entry("ai-handoff") is None


def test_lim16_attempt_clock_includes_operation_time() -> None:
    """LIM-16: wall clock runs from opening and bars model work, not cleanup."""
    clock = FakeClock()
    workflow = _workflow(
        "implement", {"process": _agent_step("process", {"done": {}})}, "process",
        _ceiling(wall=10.0),
    )
    budgets = _budgets(workflow, clock)
    # Operations take time but spend no budget: the clock still advances past
    # the 10-minute Attempt deadline.
    clock.advance(10.0 * 60.0 + 1.0)
    assert budgets.remaining_wall_seconds("process") == pytest.approx(0.0)
    assert budgets.check_entry("process") is not None
    # …yet an operation step itself is never refused entry.
    assert budgets.check_entry("process", is_operation=True) is None


# --- LIM-17: the provider output cap (L1) ----------------------------------------------------


@pytest.mark.skip(reason="L1: needs the real provider contract; never in the default run")
def test_lim17_provider_honours_the_pinned_output_cap() -> None:
    """LIM-17: the request carries the code-owned cap and stops at it."""
    raise AssertionError("L1 only: run explicitly against the real provider")
