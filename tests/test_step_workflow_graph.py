"""Compile a validated Workflow into a `StateGraph` and run its control flow.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (the
Workflow graph: entry, Routes, Execution Ceilings, Workflow Ends and the
LangGraph mapping) and §13.2 (Step Executions and transitions).

Slice S3 of ``docs/plan/step-workflow-implementation-plan.md``. Operations
are stubbed: both executors answer from the run's scripted results, so every
assertion here is control flow — Step Execution numbering, transition facts,
the Terminal Outcome and the compiled shape — never operation behaviour.

Matrix row ids: CFG-40, ROUTE-1 … ROUTE-11, STEP-10, REC-2.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.ledger.store import (
    STEP_TRANSITION_END,
    STEP_TRANSITION_REFUSED,
    STEP_TRANSITION_ROUTE,
    RunLedger,
)
from coding_agent.runtime.graph import attempt as attempt_module
from coding_agent.runtime.graph.workflow_graph import (
    EXECUTOR_REGISTRY,
    WorkflowState,
    build_workflow_graph,
    describe_workflow_graph,
    effective_routes,
    end_node_name,
    run_workflow,
    workflow_recursion_limit,
)
from coding_agent.workflow import validate_v2_file
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2FieldType,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from coding_agent.runtime.graph import workflow_graph as workflow_graph_module

EXAMPLES = Path(__file__).parent.parent / "examples" / "workflows.yaml"


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, V2FieldType]],
    routes: dict[str, str | V2RouteTarget],
    *,
    max_executions: int = 1,
) -> V2Step:
    parsed_routes: dict[str, V2RouteTarget] = {}
    for name, target in routes.items():
        if isinstance(target, V2RouteTarget):
            parsed_routes[name] = target
        else:
            parsed_routes[name] = V2RouteTarget(step=target)
    return V2Step(
        id=step_id,
        agent=V2Agent(results=results),
        max_executions=max_executions,
        routes=parsed_routes,
    )


def _op_step(
    step_id: str,
    operation: str,
    routes: dict[str, str | V2RouteTarget],
    *,
    max_executions: int = 1,
) -> V2Step:
    parsed_routes: dict[str, V2RouteTarget] = {}
    for name, target in routes.items():
        if isinstance(target, V2RouteTarget):
            parsed_routes[name] = target
        else:
            parsed_routes[name] = V2RouteTarget(step=target)
    return V2Step(
        id=step_id,
        operation=operation,
        max_executions=max_executions,
        routes=parsed_routes,
    )


def _end(category: str) -> V2RouteTarget:
    return V2RouteTarget(end=category)


def _workflow(
    workflow_id: str,
    steps: dict[str, V2Step],
    start: str,
    *,
    input_type: str = "issue",
    defaults_routes: dict[str, V2RouteTarget] | None = None,
) -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type=input_type, label="agent:test"),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start=start,
        defaults_routes=defaults_routes or {},
        steps=steps,
    )


def _defaults() -> dict[str, V2RouteTarget]:
    return {"error": _end("failed"), "limit-exceeded": _end("unfinished")}


def _example_workflow(monkeypatch: pytest.MonkeyPatch, workflow_id: str) -> V2Workflow:
    _with_env(monkeypatch)
    config, faults = validate_v2_file(EXAMPLES)
    assert not faults
    assert config is not None
    return config.workflows[workflow_id]


def _executions(ledger: RunLedger, attempt_id: str) -> list[tuple[str, int, str | None]]:
    return [
        (record.step_id, record.seq, record.result)
        for record in ledger.list_step_executions(attempt_id)
    ]


# --- CFG-40: the compiled shape -------------------------------------------------


@pytest.mark.parametrize("workflow_id", ["implement", "review"])
def test_compiled_graph_shape_matches_the_validated_workflow(
    monkeypatch: pytest.MonkeyPatch, workflow_id: str
) -> None:
    """§8.5: one node per step, the only plain edge from START, one
    conditional edge per step keyed by its results after defaults, one end
    node per used category, no checkpointer and a derived backstop."""
    workflow = _example_workflow(monkeypatch, workflow_id)

    spec = describe_workflow_graph(workflow)

    assert spec.start == workflow.start
    assert spec.steps == tuple(workflow.steps)
    assert spec.plain_edges == (("__start__", workflow.start),)
    assert set(spec.conditional_edges) == set(workflow.steps)
    for step_id, step in workflow.steps.items():
        expected = {
            result: (
                target.step
                if target.step is not None
                else end_node_name(str(target.end))
            )
            for result, target in effective_routes(workflow, step).items()
        }
        assert spec.conditional_edges[step_id] == expected
    for end_node in spec.end_nodes:
        assert end_node.startswith("~end.")
        assert ":" not in end_node and "|" not in end_node
    assert spec.recursion_limit == workflow_recursion_limit(workflow)
    assert spec.recursion_limit == (
        sum(step.max_executions for step in workflow.steps.values())
        + len(workflow.steps)
        + 1
    )

    graph = build_workflow_graph(workflow)
    drawn = graph.get_graph()
    names = [name for name in drawn.nodes if not name.startswith("__")]
    assert names == list(workflow.steps) + [
        node for node in spec.end_nodes
    ]
    edges = {(edge.source, edge.target) for edge in drawn.edges}
    expected_edges = {("__start__", workflow.start)}
    for step_id, path_map in spec.conditional_edges.items():
        for target in path_map.values():
            expected_edges.add((step_id, target))
    for end_node in spec.end_nodes:
        expected_edges.add((end_node, "__end__"))
    assert edges == expected_edges
    assert graph.checkpointer is None


def test_compiled_graph_uses_no_send_command_or_interrupt() -> None:
    """§8.5: routing is one conditional edge per step, never Send/Command."""
    source = inspect.getsource(workflow_graph_module)
    assert re.search(r"\bSend\s*\(", source) is None
    assert re.search(r"\bCommand\s*\(", source) is None
    assert re.search(r"\bgoto\s*=", source) is None
    assert re.search(r"\binterrupt\s*\(", source) is None


def test_graph_state_carries_no_runtime_context() -> None:
    """The AttemptContext pattern: handles travel in config, never in state."""
    annotations = {name.lower() for name in WorkflowState.__annotations__}
    for forbidden in (
        "ledger",
        "model",
        "tracker",
        "script",
        "context",
        "meter",
        "budget",
        "evidence",
        "usage",
    ):
        assert all(forbidden not in name for name in annotations), forbidden


def test_nodes_require_a_workflow_context(ledger: RunLedger) -> None:
    workflow = _workflow(
        "w",
        {"only": _agent_step("only", {"done": {}}, {"done": _end("completed")})},
        "only",
        defaults_routes=_defaults(),
    )
    graph = build_workflow_graph(workflow)
    with pytest.raises(Exception):
        graph.invoke({"attempt_id": "w#1/1", "counts": {}})


def test_executor_registry_holds_both_kinds() -> None:
    assert set(EXECUTOR_REGISTRY) == {"agent", "operation"}


def test_mvp_graph_is_untouched_for_the_old_runtime() -> None:
    """S3 builds beside the MVP runtime: the fixed graph keeps its shape."""
    assert attempt_module.ATTEMPT_NODE_ORDER == (
        "claim",
        "readiness",
        "baseline",
        "run_step",
        "gate",
        "delivery_snapshot",
        "final_checks",
        "publication_gate",
        "delivery_intent",
        "publish",
        "terminal_writes",
    )


# --- ROUTE-1 … ROUTE-5: routing primitives ----------------------------------------


def test_yaml_order_is_meaningless_execution_begins_at_start(
    ledger: RunLedger,
) -> None:
    """ROUTE-1: the Workflow starts at `start` whatever the map order."""
    workflow = _workflow(
        "w",
        {
            "zzz": _agent_step("zzz", {"done": {}}, {"done": _end("completed")}),
            "aaa": _agent_step("aaa", {"done": {}}, {"done": "zzz"}),
        },
        "aaa",
        defaults_routes=_defaults(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"aaa": ["done"], "zzz": ["done"]},
    )
    assert result.outcome == "completed"
    assert _executions(ledger, "w#1/1") == [("aaa", 1, "done"), ("zzz", 1, "done")]


def test_a_step_own_route_wins_over_the_default(ledger: RunLedger) -> None:
    """ROUTE-2: the step's own `error` Route beats `defaults.routes`."""
    workflow = _workflow(
        "w",
        {
            "s": _agent_step(
                "s",
                {"done": {}},
                {"done": _end("completed"), "error": "own"},
            ),
            "own": _agent_step("own", {"done": {}}, {"done": _end("completed")}),
            "defaulted": _agent_step(
                "defaulted", {"done": {}}, {"done": _end("completed")}
            ),
        },
        "s",
        defaults_routes={
            "error": V2RouteTarget(step="defaulted"),
            "limit-exceeded": _end("unfinished"),
        },
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"s": ["error"], "own": ["done"]},
    )
    assert result.outcome == "completed"
    assert _executions(ledger, "w#1/1") == [("s", 1, "error"), ("own", 1, "done")]


def test_route_into_a_step_at_its_ceiling_is_refused(ledger: RunLedger) -> None:
    """ROUTE-3: no Step Execution, no sequence advance, refusal fact, then
    the refused step's `limit-exceeded` Route."""
    workflow = _workflow(
        "w",
        {
            "a": _agent_step(
                "a",
                {"done": {}},
                {"done": "b", "limit-exceeded": _end("completed")},
                max_executions=2,
            ),
            "b": _agent_step(
                "b",
                {"done": {}},
                {"done": "a", "limit-exceeded": _end("unfinished")},
                max_executions=1,
            ),
        },
        "a",
        defaults_routes=_defaults(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"a": ["done", "done"], "b": ["done"]},
    )
    assert result.outcome == "unfinished"
    assert _executions(ledger, "w#1/1") == [
        ("a", 1, "done"),
        ("b", 1, "done"),
        ("a", 2, "done"),
    ]
    refused = [
        record
        for record in ledger.list_step_transitions("w#1/1")
        if record.kind == STEP_TRANSITION_REFUSED
    ]
    assert len(refused) == 1
    assert refused[0].to_step == "b"
    assert refused[0].reason == "execution-ceiling"


def test_workflow_end_records_the_fact_and_the_terminal_outcome(
    ledger: RunLedger,
) -> None:
    """ROUTE-4: "ended at end X after result R of step S", outcome X."""
    workflow = _workflow(
        "w",
        {"only": _agent_step("only", {"done": {}}, {"done": _end("blocked")})},
        "only",
        defaults_routes=_defaults(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"only": ["done"]},
    )
    assert result.outcome == "blocked"
    assert result.reason == "ended at end blocked after result done of step only"
    ends = [
        record
        for record in ledger.list_step_transitions("w#1/1")
        if record.kind == STEP_TRANSITION_END
    ]
    assert len(ends) == 1
    assert (ends[0].from_step, ends[0].result, ends[0].to_end) == (
        "only",
        "done",
        "blocked",
    )
    recorded = ledger.get_outcome("w#1/1")
    assert recorded is not None
    assert (recorded.outcome, recorded.reason) == (result.outcome, result.reason)


def test_result_outside_the_path_map_ends_internal_failure(
    ledger: RunLedger,
) -> None:
    """ROUTE-5: a runtime defect is never routed through `error`."""
    workflow = _workflow(
        "w",
        {"s": _agent_step("s", {"done": {}}, {"done": _end("completed")})},
        "s",
        defaults_routes=_defaults(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"s": ["bogus"]},
    )
    assert result.outcome == "internal-failure"
    assert "bogus" in result.reason
    assert "'s'" in result.reason or '"s"' in result.reason or "s" in result.reason
    kinds = {record.kind for record in ledger.list_step_transitions("w#1/1")}
    assert STEP_TRANSITION_ROUTE not in kinds
    assert STEP_TRANSITION_END not in kinds
    recorded = ledger.get_outcome("w#1/1")
    assert recorded is not None
    assert recorded.outcome == "internal-failure"


def test_recursion_backstop_ends_internal_failure(ledger: RunLedger) -> None:
    """ROUTE-5: a GraphRecursionError ends `internal-failure`."""
    workflow = _workflow(
        "w",
        {
            "a": _agent_step("a", {"done": {}}, {"done": "b"}),
            "b": _agent_step("b", {"done": {}}, {"done": _end("completed")}),
        },
        "a",
        defaults_routes=_defaults(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"a": ["done"], "b": ["done"]},
        recursion_limit=1,
    )
    assert result.outcome == "internal-failure"
    assert "GraphRecursionError" in result.reason
    recorded = ledger.get_outcome("w#1/1")
    assert recorded is not None
    assert recorded.outcome == "internal-failure"


# --- ROUTE-6 … ROUTE-11: the example traces at control-flow level -----------------


def test_implement_happy_path(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-6: ready, done, passed, approved, pushed, opened; completed."""
    workflow = _example_workflow(monkeypatch, "implement")
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={
            "preflight": ["ready"],
            "process": ["done"],
            "validate": ["passed"],
            "self-review": ["approved"],
            "push": ["pushed"],
            "open-pr": ["opened"],
        },
    )
    assert result.outcome == "completed"
    assert _executions(ledger, "implement#7/1") == [
        ("preflight", 1, "ready"),
        ("process", 1, "done"),
        ("validate", 1, "passed"),
        ("self-review", 1, "approved"),
        ("push", 1, "pushed"),
        ("open-pr", 1, "opened"),
    ]
    effects = {
        (record.step_id, record.status) for record in ledger.list_step_effects("implement#7/1")
    }
    assert ("push", "receipt") in effects
    assert ("open-pr", "receipt") in effects


def test_implement_validation_keeps_failing(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-7: process/validate alternate; the fourth process entry is
    refused; ai-handoff runs; unfinished."""
    workflow = _example_workflow(monkeypatch, "implement")
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={
            "preflight": ["ready"],
            "process": ["done", "done", "done"],
            "validate": ["failed", "failed", "failed"],
            "ai-handoff": ["handoff"],
            "handoff-push": ["pushed"],
            "handoff-comment": ["posted"],
        },
    )
    assert result.outcome == "unfinished"
    assert _executions(ledger, "implement#7/1") == [
        ("preflight", 1, "ready"),
        ("process", 1, "done"),
        ("validate", 1, "failed"),
        ("process", 2, "done"),
        ("validate", 2, "failed"),
        ("process", 3, "done"),
        ("validate", 3, "failed"),
        ("ai-handoff", 1, "handoff"),
        ("handoff-push", 1, "pushed"),
        ("handoff-comment", 1, "posted"),
    ]
    refused = [
        record
        for record in ledger.list_step_transitions("implement#7/1")
        if record.kind == STEP_TRANSITION_REFUSED
    ]
    assert [(record.to_step, record.reason) for record in refused] == [
        ("process", "execution-ceiling")
    ]


def test_implement_self_review_never_approves(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-8: the third self-review entry is refused; the default
    `limit-exceeded` Route runs hard-handoff; unfinished."""
    workflow = _example_workflow(monkeypatch, "implement")
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={
            "preflight": ["ready"],
            "process": ["done", "done", "done"],
            "validate": ["passed", "passed", "passed"],
            "self-review": ["changes-needed", "changes-needed"],
            "hard-handoff": ["pushed"],
        },
    )
    assert result.outcome == "unfinished"
    executions = _executions(ledger, "implement#7/1")
    assert [record for record in executions if record[0] == "self-review"] == [
        ("self-review", 1, "changes-needed"),
        ("self-review", 2, "changes-needed"),
    ]
    assert executions[-1] == ("hard-handoff", 1, "pushed")
    refused = [
        record
        for record in ledger.list_step_transitions("implement#7/1")
        if record.kind == STEP_TRANSITION_REFUSED
    ]
    assert [(record.to_step, record.reason) for record in refused] == [
        ("self-review", "execution-ceiling")
    ]


def test_implement_model_says_blocked(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-9: process blocked; one comment; blocked."""
    workflow = _example_workflow(monkeypatch, "implement")
    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={
            "preflight": ["ready"],
            "process": ["blocked"],
            "explain-blocked": ["posted"],
        },
    )
    assert result.outcome == "blocked"
    assert _executions(ledger, "implement#7/1") == [
        ("preflight", 1, "ready"),
        ("process", 1, "blocked"),
        ("explain-blocked", 1, "posted"),
    ]
    effects = {
        (record.step_id, record.status) for record in ledger.list_step_effects("implement#7/1")
    }
    assert ("explain-blocked", "receipt") in effects


def test_review_happy_path(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-10: reviewed, published; one review; completed."""
    workflow = _example_workflow(monkeypatch, "review")
    result = run_workflow(
        workflow=workflow,
        attempt_id="review#9/1",
        ledger=ledger,
        script={"review": ["reviewed"], "publish": ["published"]},
    )
    assert result.outcome == "completed"
    assert _executions(ledger, "review#9/1") == [
        ("review", 1, "reviewed"),
        ("publish", 1, "published"),
    ]
    effects = {
        (record.step_id, record.status) for record in ledger.list_step_effects("review#9/1")
    }
    assert ("publish", "receipt") in effects


def test_review_publish_retried(ledger: RunLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    """ROUTE-11: the retry is a Route bounded by max-executions; failed."""
    workflow = _example_workflow(monkeypatch, "review")
    result = run_workflow(
        workflow=workflow,
        attempt_id="review#9/1",
        ledger=ledger,
        script={"review": ["reviewed"], "publish": ["error", "error"]},
    )
    assert result.outcome == "failed"
    assert _executions(ledger, "review#9/1") == [
        ("review", 1, "reviewed"),
        ("publish", 1, "error"),
        ("publish", 2, "error"),
    ]


# --- STEP-10: execution numbering and immutability ---------------------------------


def test_step_entered_three_times_numbers_executions_immutably(
    ledger: RunLedger,
) -> None:
    """STEP-10: executions 1, 2, 3; each result and payload immutable."""
    workflow = _workflow(
        "w",
        {
            "s": _agent_step(
                "s",
                {"r1": {}, "r2": {}, "r3": {}},
                {"r1": "t", "r2": "t", "r3": "t", "limit-exceeded": _end("completed")},
                max_executions=3,
            ),
            "t": _op_step(
                "t",
                "check-readiness",
                {"ready": "s", "limit-exceeded": _end("completed")},
                max_executions=3,
            ),
        },
        "s",
        defaults_routes=_defaults(),
    )
    result = run_workflow(
        workflow=workflow,
        attempt_id="w#1/1",
        ledger=ledger,
        script={"s": ["r1", "r2", "r3"], "t": ["ready", "ready", "ready"]},
    )
    assert result.outcome == "completed"
    executions = ledger.list_step_executions("w#1/1")
    s_rows = [record for record in executions if record.step_id == "s"]
    assert [(record.seq, record.result) for record in s_rows] == [
        (1, "r1"),
        (2, "r2"),
        (3, "r3"),
    ]
    payloads = [record.payload for record in s_rows]
    assert payloads == [
        {"result": "r1"},
        {"result": "r2"},
        {"result": "r3"},
    ]
    assert all(record.state == "ended" for record in s_rows)
    assert [record.entry_result for record in s_rows[1:]] == ["ready", "ready"]


# --- REC-2: transition facts ---------------------------------------------------------


def test_routes_refusals_and_ends_all_record_as_transitions(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REC-2: every Route taken, refused entry and Workflow End records."""
    workflow = _example_workflow(monkeypatch, "implement")
    run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={
            "preflight": ["ready"],
            "process": ["done", "done", "done"],
            "validate": ["failed", "failed", "failed"],
            "ai-handoff": ["handoff"],
            "handoff-push": ["pushed"],
            "handoff-comment": ["posted"],
        },
    )
    kinds = [record.kind for record in ledger.list_step_transitions("implement#7/1")]
    assert STEP_TRANSITION_ROUTE in kinds
    assert STEP_TRANSITION_REFUSED in kinds
    assert kinds[-1] == STEP_TRANSITION_END
    routes = [
        (record.from_step, record.result, record.to_step)
        for record in ledger.list_step_transitions("implement#7/1")
        if record.kind == STEP_TRANSITION_ROUTE
    ]
    assert ("process", "done", "validate") in routes
    assert ("validate", "failed", "process") in routes
    ends = [
        record
        for record in ledger.list_step_transitions("implement#7/1")
        if record.kind == STEP_TRANSITION_END
    ]
    assert [(record.from_step, record.result, record.to_end) for record in ends] == [
        ("handoff-comment", "posted", "unfinished")
    ]
