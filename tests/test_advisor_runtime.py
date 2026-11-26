"""One ``ask_advisor`` call opens a fresh read-only Advisor conversation (§9).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (an agent
step that declares the ``advisor`` tool group lets its model ask questions
with ``ask_advisor(question)``; each call opens a fresh conversation on the
pin the step's ``agent.advisor`` names — or the Pinned Model — with the
framing, the caller's Attempt facts, input and declared inputs, the
operator's ``advisor.prompt`` bytes and the question verbatim; the Advisor
binds exactly the Sub-agent read-only set plus its report tool; every call
is a barrier; every ending reaches the caller as the tool result while the
caller continues).

ADR: ``docs/adr/0036-an-advisor-answers-on-the-pin-its-step-names-in-a-fresh-read-only-conversation.md``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import origin_cell_from_readable
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.graph.advisor import (
    ADVISOR_ENDED_KIND,
    ADVISOR_STARTED_KIND,
    ADVISOR_TOOL_NAMES,
    ASK_ADVISOR_TOOL,
    REPORT_ADVICE_TOOL,
    AdvisorCallTally,
    advisor_question_preview,
    build_advisor_tools,
)
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_READ_TOOL_NAMES,
    build_subagent_tools,
)
from coding_agent.runtime.result_capping import InMemoryArtifactStore
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    LimitFact,
    build_budgets,
)
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.workflow.schema import (
    Advisor,
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import make_agent_step_deps, make_pin, make_prices

ATTEMPT_ID = "implement#7/1"
ADVISOR_PROMPT_LOCATOR = "advisor.md"
ADVISOR_PROMPT_TEXT = (
    "You are the triage advisor. Recommend the cheapest correct fix.\n"
)
ADVISOR_PIN_ID = "sage"
ADVISOR_MODEL_NAME = "sage-model"


def _advisor_pin() -> PinnedModel:
    """The Advisor's own pin: answered by ``sage-model``, not the Pinned Model."""
    return PinnedModel(
        provider="anthropic",
        model=ADVISOR_MODEL_NAME,
        endpoint="messages",
        effort="high",
        output_cap_tokens=8000,
        context_threshold_tokens=100000,
        context_window_tokens=200000,
    )


def _advisor_prices() -> TokenPrices:
    """Deliberately unlike the main prices, so mispriced spend is visible."""
    return TokenPrices(input=100.0, output=200.0, cache_read=10.0, cache_write=100.0)


def _ceiling() -> AttemptCeiling:
    return AttemptCeiling(
        cost_usd=100000.0,
        effective_tokens=100000.0,
        tool_calls=10000.0,
        wall_clock_minutes=600.0,
    )


def _advisor_step(
    step_id: str = "process",
    *,
    advisor_model: str | None = ADVISOR_PIN_ID,
    tools: tuple[str, ...] = ("workspace-read", "advisor"),
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(
            tools=tools,
            results={"done": {}},
            advisor=Advisor(prompt=ADVISOR_PROMPT_LOCATOR, model=advisor_model),
        ),
        max_executions=1,
        limits={},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )


def _workflow(step: WorkflowStep) -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=_ceiling(),
        start=step.id,
        steps={step.id: step},
    )


def _facts() -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id=ATTEMPT_ID,
        workflow_id="implement",
        input_type="issue",
        input_number=7,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )


def _budgets(step: WorkflowStep) -> AttemptBudgets:
    return build_budgets(
        _workflow(step), prices=make_prices(), output_cap=0, attempt_id=ATTEMPT_ID
    )


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _ai(
    calls: list[dict[str, object]] | None = None,
    *,
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls or []), usage_metadata=usage)


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ask_call(question: str, call_id: str) -> dict[str, object]:
    return _call(ASK_ADVISOR_TOOL, {"question": question}, call_id)


def _report_advice_call(answer: str, call_id: str = "adv-1") -> dict[str, object]:
    return _call(REPORT_ADVICE_TOOL, {"answer": answer}, call_id)


def _report_ai(
    result: str, payload: dict[str, object], usage: dict[str, object]
) -> AIMessage:
    return AIMessage(
        content="reporting",
        tool_calls=[
            _call(
                REPORT_TOOL_NAME,
                {"result": result, "payload_json": json.dumps(payload)},
                "report-1",
            )
        ],
        usage_metadata=usage,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _run_with_advisor(
    step: WorkflowStep,
    main_script: list[AIMessage],
    advisor_script: list[AIMessage],
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    budgets: AttemptBudgets,
    *,
    main_identity: str = "test-model",
    advisor_identity: str = ADVISOR_MODEL_NAME,
    advisor_pin: PinnedModel | None = None,
    advisor_model_records_transcript: bool = False,
) -> tuple[Any, Any, Any]:
    """Run one declaring step; return ``(outcome, main_model, advisor_model)``."""
    from support.scripted_model import ScriptedModel

    workflow = _workflow(step)
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass
    main_model = ScriptedModel(main_script, response_models=[main_identity] * len(main_script))
    advisor_model = ScriptedModel(
        advisor_script, response_models=[advisor_identity] * len(advisor_script)
    )
    deps = make_agent_step_deps(
        model=main_model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=make_prices(),
        pin=make_pin(),
    )
    resolved_pin = advisor_pin if advisor_pin is not None else _advisor_pin()
    deps.prompt_texts = {ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT}
    deps.model_id = "sonnet"
    deps.pins = {"sonnet": make_pin(), ADVISOR_PIN_ID: resolved_pin}
    deps.pin_prices = {"sonnet": make_prices(), ADVISOR_PIN_ID: _advisor_prices()}
    advisor_inner: Any = advisor_model
    if advisor_model_records_transcript:
        assert deps.archive_dir is not None
        advisor_inner = TranscriptModel(
            advisor_model,
            TranscriptWriter(deps.archive_dir / TRANSCRIPT_FILE_NAME, None),
        )
    deps.pin_models = {"sonnet": deps.model, ADVISOR_PIN_ID: advisor_inner}
    outcome = run_agent_step(workflow.steps[step.id], 1, deps, {})
    return outcome, main_model, advisor_model


def _events(
    outcome: Any, kind: str, call_id: str | None = None
) -> list[dict[str, object]]:
    found = [
        event
        for event in outcome.events
        if event["kind"] == kind
        and isinstance(event.get("payload"), dict)
        and (call_id is None or event["payload"].get("call_id") == call_id)
    ]
    return [dict(event["payload"]) for event in found]


def _advisor_first_request_text(advisor_model: Any) -> str:
    """The concatenated text of the Advisor's first request messages."""
    first_call = advisor_model.invocations[0]
    parts: list[str] = []
    for message in first_call:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.append(str(content))
    return "\n".join(parts)


def test_reported_flows_back_to_caller_on_the_advisor_pin(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The Advisor answers on its pin; the caller receives the report."""
    step = _advisor_step()
    budgets = _budgets(step)
    question = "Which file holds the crash?"
    outcome, main_model, advisor_model = _run_with_advisor(
        step,
        [
            _ai([_ask_call(question, "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [
            _ai([_report_advice_call("README.md holds it.")], usage=_usage(100, 10)),
        ],
        workspace,
        tmp_path,
        ledger,
        budgets,
        advisor_model_records_transcript=True,
    )

    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    # The caller continued in the same conversation after the consultation.
    assert len(main_model.invocations) == 2

    started = _events(outcome, ADVISOR_STARTED_KIND, "advisor-0")
    assert len(started) == 1
    assert started[0]["pin_id"] == ADVISOR_PIN_ID
    assert started[0]["question_bytes"] == len(question.encode("utf-8"))
    assert started[0]["question_sha256"] == hashlib.sha256(
        question.encode("utf-8")
    ).hexdigest()

    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert len(ended) == 1
    assert ended[0]["pin_id"] == ADVISOR_PIN_ID
    assert ended[0]["ending"] == "reported"
    assert ended[0]["reported"] is True
    assert ended[0]["answering_model"] == ADVISOR_MODEL_NAME
    assert ended[0]["response_models"] == [ADVISOR_MODEL_NAME]
    assert float(ended[0]["duration_seconds"]) >= 0.0  # type: ignore[arg-type]
    assert isinstance(ended[0]["usage"], dict)

    # The opening order: framing, Attempt facts, input, declared inputs
    # (never the caller's Step Result contract), prompt bytes, question.
    opening = _advisor_first_request_text(advisor_model)
    framing_at = opening.find("You are an Advisor")
    facts_at = opening.find("Attempt: implement#7/1")
    input_at = opening.find("Fix the crash")
    prompt_at = opening.find(ADVISOR_PROMPT_TEXT.strip())
    question_at = opening.find(question)
    assert 0 <= framing_at < facts_at < input_at < prompt_at < question_at
    assert "Step Result" not in opening

    # The Execution Summary lists the consultation with its pin.
    consultations = outcome.summary.get("advisor_consultations")
    assert consultations == [
        {
            "question_preview": advisor_question_preview(question),
            "ending": "reported",
            "pin_id": ADVISOR_PIN_ID,
        }
    ]

    # The Transcript records the whole Advisor conversation.
    transcript_text = (tmp_path / "archive" / TRANSCRIPT_FILE_NAME).read_text(
        encoding="utf-8"
    )
    assert '"advisor:advisor-0"' in transcript_text
    assert question in transcript_text
    assert "README.md holds it." in transcript_text


def test_two_calls_in_one_turn_run_one_after_another_in_order(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Two ``ask_advisor`` calls in one turn run sequentially, results in order."""
    step = _advisor_step()
    budgets = _budgets(step)
    outcome, _, _ = _run_with_advisor(
        step,
        [
            _ai(
                [
                    _ask_call("first question", "c1"),
                    _ask_call("second question", "c2"),
                ],
                usage=_usage(10, 5),
            ),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [
            _ai([_report_advice_call("first answer")], usage=_usage(20, 4)),
            _ai([_report_advice_call("second answer")], usage=_usage(30, 6)),
        ],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )

    assert outcome.status == "reported"
    started_first = _events(outcome, ADVISOR_STARTED_KIND, "advisor-0")
    started_second = _events(outcome, ADVISOR_STARTED_KIND, "advisor-1")
    assert len(started_first) == 1
    assert len(started_second) == 1
    ended_first = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    ended_second = _events(outcome, ADVISOR_ENDED_KIND, "advisor-1")
    assert ended_first[0]["ending"] == "reported"
    assert ended_second[0]["ending"] == "reported"
    consultations = outcome.summary.get("advisor_consultations")
    assert isinstance(consultations, list) and len(consultations) == 2
    assert consultations[0]["question_preview"] == "first question"
    assert consultations[1]["question_preview"] == "second question"


def test_advisor_bound_set_is_the_read_only_set_plus_report(
    workspace: Path, tmp_path: Path
) -> None:
    """An Advisor binds exactly the Sub-agent read-only set plus its report."""
    from coding_agent.runtime.graph.subagent import SubagentRecord

    tools = build_advisor_tools(
        workspace,
        __import__(
            "coding_agent.runtime.graph.subagent", fromlist=["SubagentRecord"]
        ).SubagentRecord(description="probe"),
        artifact_store=InMemoryArtifactStore(),
        base_revision="abc123",
        change_artifact_id="read-change-probe",
    )
    assert {tool.name for tool in tools} == set(ADVISOR_TOOL_NAMES)
    assert REPORT_ADVICE_TOOL in {tool.name for tool in tools}
    assert ASK_ADVISOR_TOOL not in {tool.name for tool in tools}
    assert "spawn_subagent" not in {tool.name for tool in tools}
    read_only = set(SUBAGENT_READ_TOOL_NAMES) | {
        "read_change",
        "read_result_slice",
        REPORT_ADVICE_TOOL,
    }
    assert set(ADVISOR_TOOL_NAMES) == read_only


def test_subagent_tools_bind_no_ask_advisor(
    workspace: Path, tmp_path: Path
) -> None:
    """Sub-agents never bind ``ask_advisor``."""
    from coding_agent.runtime.graph.subagent import SubagentRecord

    tools = build_subagent_tools(
        workspace,
        SubagentRecord(description="probe"),
        artifact_store=InMemoryArtifactStore(),
        base_revision="abc123",
        change_artifact_id="read-change-probe",
    )
    assert ASK_ADVISOR_TOOL not in {tool.name for tool in tools}


def test_declaring_step_binds_ask_advisor_where_declared(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """The declaring step binds ``ask_advisor``; the Advisor does not rebind it."""
    step = _advisor_step()
    budgets = _budgets(step)
    outcome, main_model, _ = _run_with_advisor(
        step,
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [_ai([_report_advice_call("an answer")], usage=_usage(10, 5))],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    assert ASK_ADVISOR_TOOL in main_model.bound_tool_names


def test_invalid_question_is_an_error_and_starts_nothing(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A blank question is a tool error: no Advisor starts, the caller continues."""
    step = _advisor_step()
    budgets = _budgets(step)
    outcome, _, advisor_model = _run_with_advisor(
        step,
        [
            _ai([_ask_call("   ", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    assert _events(outcome, ADVISOR_STARTED_KIND) == []
    assert _events(outcome, ADVISOR_ENDED_KIND) == []
    assert advisor_model.invocations == []


def test_silent_ends_the_advisor_and_the_caller_continues(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A text-only Advisor ends ``silent``; the caller continues with the ending."""
    step = _advisor_step()
    budgets = _budgets(step)
    outcome, _, _ = _run_with_advisor(
        step,
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [_ai(None, usage=_usage(40, 6))],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert len(ended) == 1
    assert ended[0]["ending"] == "silent"
    assert ended[0]["reported"] is False
    assert outcome.summary["advisor_consultations"][0]["ending"] == "silent"


def test_round_limit_ends_the_advisor_and_the_caller_continues(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Eight tool rounds without a report end ``round-limit``."""
    step = _advisor_step()
    budgets = _budgets(step)
    advisor_script = [
        _ai(
            [_call("read_file", {"path": "README.md"}, f"r{index}")],
            usage=_usage(20 + index, 4),
        )
        for index in range(8)
    ]
    outcome, _, _ = _run_with_advisor(
        step,
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        advisor_script,
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert len(ended) == 1
    assert ended[0]["ending"] == "round-limit"
    assert ended[0]["reported"] is False


def test_admission_refusal_ends_the_advisor_ceiling_and_caller_continues(
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Admission refusal on the Advisor pin ends ``ceiling``, never the caller."""
    step = _advisor_step()
    budgets = _budgets(step)
    real_admit = budgets.admit_request
    calls = {"count": 0}

    def _admit(step_id: str, estimate_tokens: int, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            return (
                LimitFact(
                    scope="attempt-ceiling",
                    dimensions=("cost_usd",),
                    limit={"cost_usd": 1.0},
                    spend={"cost_usd": 2.0},
                    moment="before-request",
                ),
                None,
            )
        return real_admit(step_id, estimate_tokens, **kwargs)

    monkeypatch.setattr(budgets, "admit_request", _admit)
    outcome, _, advisor_model = _run_with_advisor(
        step,
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [_ai([_report_advice_call("unused")], usage=_usage(10, 5))],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    assert advisor_model.invocations == []
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert len(ended) == 1
    assert ended[0]["ending"] == "ceiling"
    assert ended[0]["reported"] is False
    assert ended[0]["duration_seconds"] == 0.0


def test_context_window_ends_the_advisor_and_the_caller_continues(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """An Advisor opening over its pin's usable threshold ends ``context-window``."""
    tiny_pin = PinnedModel(
        provider="anthropic",
        model=ADVISOR_MODEL_NAME,
        endpoint="messages",
        effort="high",
        output_cap_tokens=8000,
        context_threshold_tokens=3000,
        context_window_tokens=200000,
    )
    step = _advisor_step()
    budgets = _budgets(step)
    # A long question guarantees the Advisor opening (framing, caller facts,
    # prompt, question, tool schemas) exceeds the pin's usable threshold of
    # 1000 tokens, so the guard ends the Advisor before its first request.
    long_question = "Explain the crash in detail. " * 500
    outcome, _, advisor_model = _run_with_advisor(
        step,
        [
            _ai([_ask_call(long_question, "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [_ai([_report_advice_call("unused")], usage=_usage(10, 5))],
        workspace,
        tmp_path,
        ledger,
        budgets,
        advisor_pin=tiny_pin,
    )
    assert outcome.status == "reported"
    assert advisor_model.invocations == []
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert len(ended) == 1
    assert ended[0]["ending"] == "context-window"
    assert ended[0]["reported"] is False
    assert ended[0]["usable_threshold"] == 1000


def test_provider_failure_ends_the_advisor_and_the_caller_continues(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A model failure ends the Advisor ``provider-failure``, never the caller."""
    from support.scripted_model import ScriptedModel

    step = _advisor_step()
    budgets = _budgets(step)
    workflow = _workflow(step)
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass

    class _RaiseThenReport:
        def __init__(self, script: list[Any]) -> None:
            self._script = list(script)

        def bind_tools(self, tools: Any, tool_choice: Any = None) -> _RaiseThenReport:
            _ = (tools, tool_choice)
            return self

        def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
            _ = kwargs
            action = self._script.pop(0)
            if isinstance(action, Exception):
                raise action
            assert isinstance(action, AIMessage)
            metadata = dict(action.response_metadata or {})
            metadata.setdefault("model", ADVISOR_MODEL_NAME)
            return action.model_copy(update={"response_metadata": metadata})

    main_model = ScriptedModel(
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 2,
    )
    deps = make_agent_step_deps(
        model=main_model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=make_prices(),
        pin=make_pin(),
    )
    deps.prompt_texts = {ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT}
    deps.model_id = "sonnet"
    deps.pins = {"sonnet": make_pin(), ADVISOR_PIN_ID: _advisor_pin()}
    deps.pin_prices = {"sonnet": make_prices(), ADVISOR_PIN_ID: _advisor_prices()}
    deps.pin_models = {
        "sonnet": deps.model,
        ADVISOR_PIN_ID: _RaiseThenReport(
            [ModelInvalidRequestError("bad request for the advisor")]
        ),
    }
    outcome = run_agent_step(workflow.steps[step.id], 1, deps, {})

    assert outcome.status == "reported"
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert len(ended) == 1
    assert ended[0]["ending"] == "provider-failure"
    assert ended[0]["reported"] is False
    assert "ModelInvalidRequestError" in str(ended[0].get("provider_error"))


def test_stopped_discards_the_advisor_request(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A stopped Advisor ends ``stopped`` with its in-flight request discarded."""
    from coding_agent.runtime.graph.agent_step import _run_advisor_call

    step = _advisor_step()
    budgets = _budgets(step)
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass
    deps = make_agent_step_deps(
        model=__import__("support.scripted_model", fromlist=["ScriptedModel"]).ScriptedModel(
            [], response_models=[]
        ),
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=make_prices(),
        pin=make_pin(),
        is_stopped=lambda: True,
    )
    deps.prompt_texts = {ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT}
    deps.model_id = "sonnet"
    deps.pins = {"sonnet": make_pin(), ADVISOR_PIN_ID: _advisor_pin()}
    deps.pin_prices = {"sonnet": make_prices(), ADVISOR_PIN_ID: _advisor_prices()}
    deps.pin_models = {"sonnet": deps.model, ADVISOR_PIN_ID: deps.model}
    from coding_agent.runtime.graph.agent_step import compose_step_opening

    opening = list(compose_step_opening(step, deps, {}))
    events: list[dict[str, object]] = []

    def record_event(kind: str, detail: str, payload: dict[str, object]) -> None:
        events.append({"kind": kind, "detail": detail, "payload": payload})

    tally = AdvisorCallTally()
    call_id = tally.next_call_id()
    text, consultation, billable, fact = _run_advisor_call(
        deps, step, 1, opening, call_id, "a question", record_event
    )
    assert consultation.ending == "stopped"
    assert billable == 0
    assert fact is None
    assert "stopped" in text
    kinds = [event["kind"] for event in events]
    assert kinds == [ADVISOR_STARTED_KIND, ADVISOR_ENDED_KIND]
    ended_payload = events[-1]["payload"]
    assert isinstance(ended_payload, dict)
    assert ended_payload["ending"] == "stopped"
    assert ended_payload["reported"] is False


def test_pin_violation_on_the_advisor_pin_halts_the_worker(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """A pin violation on the Advisor pin propagates after its ``advisor_ended``."""
    step = _advisor_step()
    budgets = _budgets(step)
    workflow = _workflow(step)
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass
    from support.scripted_model import ScriptedModel

    main_model = ScriptedModel(
        [_ai([_ask_call("a question", "c1")], usage=_usage(10, 5))],
        response_models=["test-model"],
    )
    deps = make_agent_step_deps(
        model=main_model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=make_prices(),
        pin=make_pin(),
    )
    deps.prompt_texts = {ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT}
    deps.model_id = "sonnet"
    deps.pins = {"sonnet": make_pin(), ADVISOR_PIN_ID: _advisor_pin()}
    deps.pin_prices = {"sonnet": make_prices(), ADVISOR_PIN_ID: _advisor_prices()}
    deps.pin_models = {
        "sonnet": deps.model,
        ADVISOR_PIN_ID: ScriptedModel(
            [_ai([_report_advice_call("wrong pin")], usage=_usage(10, 5))],
            response_models=["model-other"],
        ),
    }
    with pytest.raises(PinViolation):
        run_agent_step(workflow.steps[step.id], 1, deps, {})

    stored = [
        record
        for record in ledger.records(ATTEMPT_ID)
        if record.kind == ADVISOR_ENDED_KIND
    ]
    assert len(stored) == 1
    payload = dict(stored[0].payload)
    assert payload["ending"] == "pin-violation"
    assert payload["reported"] is False
    assert payload["pin_id"] == ADVISOR_PIN_ID


def test_advisor_spend_is_priced_at_the_advisor_pin(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Advisor responses settle at the Advisor pin's prices, under ``subagent``."""
    step = _advisor_step()
    budgets = _budgets(step)
    outcome, _, _ = _run_with_advisor(
        step,
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [_ai([_report_advice_call("an answer")], usage=_usage(100, 10))],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    cell = origin_cell_from_readable(ended[0]["usage"])
    assert cell.responses == 1
    assert cell.input_tokens == 100
    assert cell.output_tokens == 10
    expected = 100 / 1_000_000 * 100.0 + 10 / 1_000_000 * 200.0
    assert cell.total_cost_usd == pytest.approx(expected)
    member_total = budgets.step_by_origin(step.id)["subagent"]
    assert member_total.total_cost_usd == pytest.approx(expected)
    # The report exit is excluded from the member's billable calls, so the
    # subagent origin carries no tool calls here; the caller's own
    # ``ask_advisor`` call counts on the calling step instead.
    assert member_total.tool_calls == 0
    assert budgets.step_totals(step.id).tool_calls >= 1


def test_ask_advisor_counts_as_one_tool_call_of_the_caller(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Each ``ask_advisor`` call counts as one tool call of the calling step."""
    step = _advisor_step()
    budgets = _budgets(step)
    before = budgets.step_totals(step.id).tool_calls
    outcome, _, _ = _run_with_advisor(
        step,
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        [_ai([_report_advice_call("an answer")], usage=_usage(10, 5))],
        workspace,
        tmp_path,
        ledger,
        budgets,
    )
    assert outcome.status == "reported"
    after = budgets.step_totals(step.id).tool_calls
    assert after - before >= 1


def test_advisor_without_model_uses_the_pinned_model(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """Without ``agent.advisor.model`` the Advisor answers on the Pinned Model."""
    step = _advisor_step(advisor_model=None)
    budgets = _budgets(step)
    workflow = _workflow(step)
    try:
        ledger.open_attempt(ATTEMPT_ID, effective_configuration={})
    except sqlite3.IntegrityError:
        pass
    from support.scripted_model import ScriptedModel

    model = ScriptedModel(
        [
            _ai([_ask_call("a question", "c1")], usage=_usage(10, 5)),
            _ai([_report_advice_call("an answer")], usage=_usage(20, 4)),
            _report_ai("done", {}, _usage(10, 5)),
        ],
        response_models=["test-model"] * 3,
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        attempt=_facts(),
        ledger=ledger,
        budgets=budgets,
        prices=make_prices(),
        pin=make_pin(),
    )
    deps.prompt_texts = {ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT}
    deps.model_id = "sonnet"
    outcome = run_agent_step(workflow.steps[step.id], 1, deps, {})

    assert outcome.status == "reported"
    started = _events(outcome, ADVISOR_STARTED_KIND, "advisor-0")
    assert len(started) == 1
    assert started[0]["pin_id"] == "sonnet"
    ended = _events(outcome, ADVISOR_ENDED_KIND, "advisor-0")
    assert ended[0]["ending"] == "reported"
