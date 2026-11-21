"""The Context-Window Handoff through the Worker's real wiring (#552).

Contract ``docs/contract/step-workflow-runtime-contract.md`` §9.9 (one
bounded handoff turn on the Pinned Model with no tools and its own output
cap) and §11.4 (every model request — provider retries included — passes
Admission under its own worst-case hold and settles at its
provider-reported usage).

Every test below builds its ``AgentStepDeps`` through
``worker.attempt_run.build_agent_step_deps`` — the one production wiring —
never by injecting a producer. The handoff turn therefore runs the
production path: conversation plus instruction, no tool schemas, the cap
sent as max output tokens, each try admitted and settled like any other
request, while the main-conversation context figures and cache-hit
counters stay untouched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.context_handoff import (
    ContextWindowConfig,
    ContinuationSnapshot,
    estimate_context_tokens,
)
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    build_agent_step_tools,
    compose_step_opening,
    run_agent_step,
)
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    LimitFact,
    worst_case_bound,
)
from coding_agent.runtime.result_capping import InMemoryArtifactStore
from coding_agent.runtime.transcript import TRANSCRIPT_FILE_NAME, TranscriptWriter
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.worker.attempt_run import build_agent_step_deps
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import (
    FakeCommandRunner,
    make_agent_step_deps,
    make_attempt,
    make_pin,
    make_prices,
    make_profile,
    make_workflow,
)
from support.scripted_model import ScriptedModel

ATTEMPT_ID = "implement#7/1"


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _usage_dict(
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


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content=content, tool_calls=list(calls or []), usage_metadata=usage
    )


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
        call_id,
    )


def _agent_step(
    step_id: str, *, tools: tuple[str, ...] = ("workspace-read",)
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(tools=tools, results={"done": {}}),
        routes={"done": RouteTarget(end="completed")},
    )


def _handoff_snapshot() -> ContinuationSnapshot:
    return ContinuationSnapshot(
        version=1,
        attempt_id=ATTEMPT_ID,
        base_revision="abc123",
        branch="agent/implement/7/1-x",
        commit="abc123",
        changed_files=("x.py",),
        commands=({"command": "true", "result": "ok"},),
        validation_evidence=None,
        unresolved_questions=(),
        next_intended_action="continue",
    )


def _handoff_response(usage: dict[str, object] | None = None) -> AIMessage:
    """One queued handoff-turn response carrying the snapshot JSON (§9.9)."""
    return _ai(None, _handoff_snapshot().to_json(), usage)


_BIG = "p" * 4000


def _big_tool_call(call_id: str = "big-1") -> dict[str, object]:
    return _call("read_file", {"path": "README.md"}, call_id)


def _wire(
    *,
    model: Any,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
    window: ContextWindowConfig | None,
    output_cap: int = 32,
) -> Any:
    """The Worker's real deps wiring: no injected producer, ever."""
    archive_dir = tmp_path / "archive"
    transcript = TranscriptWriter(archive_dir / TRANSCRIPT_FILE_NAME, None)
    return build_agent_step_deps(
        attempt=make_attempt(),
        model=model,
        workspace_path=workspace,
        tree_id="tree-1",
        activation_registry=ActivationRegistry(bodies={}),
        prompt_texts={},
        ledger=ledger,
        budgets=budgets,
        prices=make_prices(),
        output_cap=output_cap,
        context_window=window,
        pin=make_pin(),
        redactions=None,
        attempt_branch=None,
        profile=make_profile(),
        command_runner=FakeCommandRunner(),
        evidence_dir=tmp_path / "evidence",
        transcript=transcript,
        archive_dir=archive_dir,
        on_activity=None,
    )


def _budgets(
    workflow: Workflow, attempt_id: str = ATTEMPT_ID, output_cap: int = 32
) -> AttemptBudgets:
    from coding_agent.runtime.step_limits import build_budgets

    return build_budgets(
        workflow,
        prices=make_prices(),
        output_cap=output_cap,
        attempt_id=attempt_id,
    )


def _calibrated_window(
    step: WorkflowStep, probe: Any, anchor: int | None = None
) -> ContextWindowConfig:
    """A window the opening fits but the opening plus one padded turn exceeds.

    With ``anchor=None`` (no response reports usage) the usable value is the
    old calibrated threshold: the whole-request estimate plus a slack turn.
    With ``anchor`` (the first response reports ``anchor`` input tokens) the
    usable value sits one slack turn above the anchor: the anchored
    Conversation Context that follows (≈ anchor plus the appended turn,
    larger than the slack) crosses it, while the headroom above usable still
    fits the handoff turn itself, so the admitted handoff request is not an
    overflow.
    """
    tools = build_agent_step_tools(step, probe)
    opening = compose_step_opening(step, probe, {})
    if anchor is None:
        usable = estimate_context_tokens(list(opening), [], list(tools)) + 400
    else:
        usable = anchor + 500
    window = ContextWindowConfig(
        threshold_tokens=usable + 1_000 + 2_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=2_000,
        handoff_token_cap=200,
    )
    assert window.usable_threshold == usable
    return window


def _open(ledger: RunLedger) -> None:
    import sqlite3 as _sqlite3

    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except _sqlite3.IntegrityError:
        pass


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        yield handle
    finally:
        handle.close()


def _probe(step: WorkflowStep, workspace: Path, tmp_path: Path) -> Any:
    return make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=make_attempt(),
    )


def test_worker_wiring_handoff_continues_with_persisted_snapshot(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The Worker wiring crosses the threshold into a fresh continuation."""
    step = _agent_step("process")
    store = InMemoryArtifactStore()
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG, usage=_usage_dict(4000, 200)),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai([_report_call("done", {})], usage=_usage_dict(3000, 200)),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, _probe(step, workspace, tmp_path), anchor=4000)
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=_budgets(make_workflow()),
        window=window,
    )
    # The wiring binds the production producer: the test injects none.
    assert deps.handoff_producer is not None
    deps.artifact_store = store
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    digest = outcome.summary["snapshot_digest"]
    assert digest == _handoff_snapshot().digest
    persisted = store.read(f"continuation-snapshots/{digest}.json")
    assert json.loads(persisted)["next_intended_action"] == "continue"
    fresh_invocation = " ".join(
        str(message.content) for message in model.invocations[2]
    )
    assert "Continuation Snapshot" in fresh_invocation


def test_handoff_request_carries_conversation_without_tools_and_cap(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The handoff turn sends messages plus the instruction, capped, unbound."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG, usage=_usage_dict(4000, 200)),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai([_report_call("done", {})], usage=_usage_dict(3000, 200)),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, _probe(step, workspace, tmp_path), anchor=4000)
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=_budgets(make_workflow()),
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert len(model.invocations) == 3
    handoff_request = model.invocations[1]
    assert len(handoff_request) > len(model.invocations[0])
    assert "Continuation Snapshot" in str(handoff_request[-1].content)
    assert model.invoke_kwargs[1] == {"max_tokens": window.handoff_token_cap}
    assert [] in [names for _, names in model.bind_history]


def test_handoff_spend_settles_provider_usage_without_fabricated_charge(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Step and Attempt meters carry the handoff turn's real usage only."""
    step = _agent_step("process")
    big_in, big_out = 40000, 5000
    hand_in, hand_out = 4500, 100
    final_in, final_out = 3000, 200
    model = ScriptedModel(
        [
            _ai(
                [_big_tool_call()],
                content=_BIG,
                usage=_usage_dict(big_in, big_out),
            ),
            _handoff_response(usage=_usage_dict(hand_in, hand_out)),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(final_in, final_out),
            ),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, _probe(step, workspace, tmp_path), anchor=40000)
    budgets = _budgets(make_workflow())
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=budgets,
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    # The fabricated charge this replaces priced len(snapshot)/4 as input
    # tokens; the real handoff input is the whole conversation, far larger.
    fabricated = len(_handoff_snapshot().to_json()) // 4
    assert fabricated != hand_in
    for totals in (budgets.step_totals("process"), budgets.attempt_totals()):
        assert totals.input_tokens == big_in + hand_in + final_in
        assert totals.output_tokens == big_out + hand_out + final_out
        assert totals.cost_usd == pytest.approx(
            (big_in + hand_in + final_in) / 1_000_000 * 1.0
            + (big_out + hand_out + final_out) / 1_000_000 * 2.0
        )


def test_handoff_refused_by_admission_ends_limit_exceeded(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A handoff bound that fits no allowance ends `limit-exceeded`."""
    step = _agent_step("process")
    probe = make_agent_step_deps(
        model=ScriptedModel([]),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=make_attempt(),
    )
    tools = build_agent_step_tools(step, probe)
    opening = compose_step_opening(step, probe, {})
    estimate = estimate_context_tokens(list(opening), [], list(tools))
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            tokens=float(estimate + 500),
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
    )
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG, usage=_usage_dict(3000, 500)),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai([_report_call("done", {})], usage=_usage_dict(3000, 200)),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, probe)
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=_budgets(workflow),
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.limit_fact is not None
    assert outcome.limit_fact["moment"] == "before-request"
    assert outcome.handoffs == 0
    # The refused turn is never sent: only the opening request went out.
    assert len(model.invocations) == 1


def test_handoff_response_without_usage_settles_at_hold_worst_case(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handoff response without usage charges its Admission hold (§11.4)."""
    step = _agent_step("process")
    big_in, big_out = 4000, 200
    final_in, final_out = 3000, 200
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG, usage=_usage_dict(big_in, big_out)),
            _handoff_response(usage=None),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(final_in, final_out),
            ),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, _probe(step, workspace, tmp_path), anchor=4000)
    budgets = _budgets(make_workflow())
    admitted: list[int] = []
    real_admit = budgets.admit_request

    def _admit(
        step_id: str, estimate_tokens: int, **kwargs: object
    ) -> tuple[LimitFact | None, str | None]:
        admitted.append(estimate_tokens)
        return real_admit(step_id, estimate_tokens, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(budgets, "admit_request", _admit)
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=budgets,
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    # The handoff hold is admitted at the anchored Conversation Context the
    # turn compares — the anchor plus the appended turn — never at a fresh
    # whole-request estimate of the handoff messages.
    assert len(admitted) == 3
    assert admitted[1] > estimate_context_tokens(list(model.invocations[1]), [], [])
    bound_tokens, _ = worst_case_bound(
        admitted[1], window.handoff_token_cap, make_prices()
    )
    totals = budgets.attempt_totals()
    assert totals.tokens == (big_in + big_out) + bound_tokens + (final_in + final_out)


class _FlakyOnceModel:
    """A fake that fails the handoff turn once with a retryable error."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.invocations: list[tuple[BaseMessage, ...]] = []
        self.invoke_kwargs: list[dict[str, object]] = []
        self.bind_history: list[tuple[str | None, list[str]]] = []

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> _FlakyOnceModel:
        self.bind_history.append((tool_choice, [tool.name for tool in tools]))
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: object) -> BaseMessage:
        assert isinstance(input, Sequence)
        self.invocations.append(tuple(input))  # type: ignore[arg-type]
        self.invoke_kwargs.append(dict(kwargs))
        next_outcome = self._outcomes.pop(0)
        if isinstance(next_outcome, Exception):
            raise next_outcome
        assert isinstance(next_outcome, BaseMessage)
        return next_outcome


def test_handoff_provider_retry_is_admitted_again(
    workspace: Path, tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each handoff try — retries included — holds its own Admission (§11.4)."""
    step = _agent_step("process")
    model = _FlakyOnceModel(
        [
            _ai([_big_tool_call()], content=_BIG, usage=_usage_dict(4000, 200)),
            ModelRateLimitError("slow down, retry me"),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai([_report_call("done", {})], usage=_usage_dict(3000, 200)),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, _probe(step, workspace, tmp_path), anchor=4000)
    budgets = _budgets(make_workflow())
    hold_ids: list[str] = []
    real_admit = budgets.admit_request

    def _admit(
        step_id: str, estimate_tokens: int, **kwargs: object
    ) -> tuple[LimitFact | None, str | None]:
        fact, hold_id = real_admit(step_id, estimate_tokens, **kwargs)  # type: ignore[arg-type]
        if hold_id is not None:
            hold_ids.append(hold_id)
        return fact, hold_id

    monkeypatch.setattr(budgets, "admit_request", _admit)
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=budgets,
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    # Opening, failed handoff try, handoff retry, continuation: four holds.
    assert len(hold_ids) == 4
    assert len(set(hold_ids)) == 4
    assert len(model.invocations) == 4


def test_oversized_handoff_request_is_a_failed_handoff(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A handoff request that fits no threshold ends `error`, unsent (§9.9)."""
    step = _agent_step("process")
    probe = _probe(step, workspace, tmp_path)
    tools = build_agent_step_tools(step, probe)
    opening = compose_step_opening(step, probe, {})
    usable = estimate_context_tokens(list(opening), [], list(tools)) + 400
    window = ContextWindowConfig(
        threshold_tokens=usable + 1_000 + 2_000,
        safety_margin_tokens=1_000,
        request_overhead_tokens=2_000,
        # A cap the handoff request itself cannot fit: estimate + cap
        # exceeds the threshold, so the handoff fails before any send.
        handoff_token_cap=5_000,
    )
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG, usage=_usage_dict(4000, 200)),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai([_report_call("done", {})], usage=_usage_dict(3000, 200)),
        ]
    )
    _open(ledger)
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=_budgets(make_workflow()),
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert outcome.detail.startswith("failed Context-Window Handoff:")
    assert outcome.handoffs == 0
    assert len(model.invocations) == 1


def test_handoff_leaves_context_figures_and_cache_counters(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The handoff turn never moves used, peak or the cache counters."""
    step = _agent_step("process")
    model = ScriptedModel(
        [
            _ai(
                [_big_tool_call()],
                content=_BIG,
                usage=_usage_dict(40000, 5000, cache_read=30000),
            ),
            _handoff_response(usage=_usage_dict(4500, 100)),
            _ai(
                [_report_call("done", {})],
                usage=_usage_dict(3000, 200, cache_read=1000),
            ),
        ]
    )
    _open(ledger)
    window = _calibrated_window(step, _probe(step, workspace, tmp_path), anchor=40000)
    budgets = _budgets(make_workflow())
    deps = _wire(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        ledger=ledger,
        budgets=budgets,
        window=window,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    context = budgets.main_context_snapshot()
    assert context["used"] == 3200
    assert context["peak"] == 45000
    assert context["conversation"] == "step/process/1/continuation/1"
    assert context["cache_read"] == 31000
    assert context["cache_input"] == 43000
