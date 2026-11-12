"""The Run Ledger records the observed answering model (direct pins).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §13 (every
model response records its observed response model from its own evidence,
never copied from the pin; ``subagent_ended`` carries the observed
answering model).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_REASON,
    check_direct_pin,
    last_reported_model,
    models_of_responses,
    observed_response_model,
)
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2FieldType,
    V2RouteTarget,
    V2Step,
)
from support.scripted_model import ScriptedModel


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
    *,
    metadata: dict[str, object] | None = None,
) -> AIMessage:
    message = AIMessage(content=content, tool_calls=list(calls or []))
    if metadata is not None:
        return message.model_copy(update={"response_metadata": metadata})
    return message


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
        call_id,
    )


def _event_payload(event: dict[str, object]) -> dict[str, object]:
    payload = event["payload"]
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, Any] = {
        "attempt_id": "implement#7/1",
        "workflow_id": "implement",
        "input_type": "issue",
        "input_number": 7,
        "title": "Fix the crash",
        "body": "The worker crashes on startup.",
        "base_revision": "abc123",
        "current_revision": "abc123",
    }
    base.update(overrides)
    return StepAttemptFacts(
        attempt_id=str(base["attempt_id"]),
        workflow_id=str(base["workflow_id"]),
        input_type=str(base["input_type"]),
        input_number=int(base["input_number"]),
        title=str(base["title"]),
        body=str(base["body"]),
        base_revision=str(base["base_revision"]),
        current_revision=str(base["current_revision"]),
    )


def _agent_step(step_id: str, results: dict[str, dict[str, V2FieldType]]) -> V2Step:
    return V2Step(
        id=step_id,
        agent=V2Agent(
            instructions=(),
            tools=("workspace-read", "subagents"),
            inputs=(),
            results=results,
        ),
        max_executions=1,
        routes={"done": V2RouteTarget(end="completed")},
    )


def _deps(
    attempt: StepAttemptFacts,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    **overrides: Any,
) -> AgentStepDeps:
    params: dict[str, Any] = {
        "attempt": attempt,
        "model": model,
        "workspace": workspace,
        "tree_id": "tree-1",
        "archive_dir": tmp_path / "archive",
    }
    params.update(overrides)
    return AgentStepDeps(**params)


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


def test_observed_response_model_reads_model_key() -> None:
    message = _ai(metadata={"model": "model-a"})
    assert observed_response_model(message) == "model-a"


def test_observed_response_model_prefers_model_over_model_name() -> None:
    message = _ai(metadata={"model": "model-a", "model_name": "model-b"})
    assert observed_response_model(message) == "model-a"


def test_observed_response_model_falls_back_to_model_name() -> None:
    message = _ai(metadata={"model_name": "model-b"})
    assert observed_response_model(message) == "model-b"


def test_observed_response_model_none_when_missing_or_blank() -> None:
    assert observed_response_model(_ai()) is None
    assert observed_response_model(_ai(metadata={})) is None
    assert observed_response_model(_ai(metadata={"model": ""})) is None
    assert observed_response_model(_ai(metadata={"model": "   "})) is None
    assert observed_response_model(_ai(metadata={"model": 42})) is None
    assert observed_response_model(_ai(metadata={"other": "model-a"})) is None


def test_observed_response_model_none_for_non_ai_messages() -> None:
    human = HumanMessage(content="hello", response_metadata={"model": "model-a"})
    assert observed_response_model(cast(BaseMessage, human)) is None
    tool = ToolMessage(content="ok", tool_call_id="t1")
    assert observed_response_model(cast(BaseMessage, tool)) is None


def test_observed_response_model_none_when_metadata_not_mapping() -> None:
    message = _ai(metadata={"model": "model-a"})

    class _Stub:
        response_metadata: object = "not-a-mapping"

    assert observed_response_model(cast(BaseMessage, _Stub())) is None
    assert observed_response_model(message) == "model-a"


def test_models_of_responses_skips_non_ai_and_preserves_order() -> None:
    messages: list[BaseMessage] = [
        _ai(metadata={"model": "model-a"}),
        HumanMessage(content="hello"),
        _ai(),
        _ai(metadata={"model_name": "model-b"}),
    ]
    assert models_of_responses(messages) == ["model-a", None, "model-b"]


def test_last_reported_model_picks_most_recent() -> None:
    assert last_reported_model([]) is None
    assert last_reported_model([None, None]) is None
    assert last_reported_model(["model-a", None, "model-b"]) == "model-b"
    assert last_reported_model(["model-a", None]) == "model-a"


def test_scripted_model_stamps_response_models() -> None:
    first = _ai()
    second = _ai(metadata={"model": "old"})
    model = ScriptedModel([first, second], response_models=["model-a", "model-b"])
    assert model._outcomes[0].response_metadata.get("model") == "model-a"
    assert model._outcomes[1].response_metadata.get("model") == "model-b"
    assert first.response_metadata.get("model") is None


def test_scripted_model_none_clears_model_keys() -> None:
    message = _ai(metadata={"model": "old", "model_name": "old-name", "other": "kept"})
    model = ScriptedModel([message], response_models=[None])
    stamped = model._outcomes[0]
    assert stamped.response_metadata.get("model") is None
    assert stamped.response_metadata.get("model_name") is None
    assert stamped.response_metadata.get("other") == "kept"


def test_scripted_model_mismatched_length_raises() -> None:
    with pytest.raises(ValueError, match="response_models"):
        ScriptedModel([_ai(), _ai()], response_models=["only-one"])


def test_scripted_model_without_response_models_leaves_messages() -> None:
    message = _ai(metadata={"model": "kept"})
    model = ScriptedModel([message])
    assert model._outcomes[0].response_metadata.get("model") == "kept"


def test_step_execution_records_per_response_observed_identity(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "README.md"}, "r1")]),
            _ai([_report_call("done", {})]),
        ],
        response_models=["step-model-a", "step-model-b"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    responses = [event for event in outcome.events if event["kind"] == "model_response"]
    assert [_event_payload(event)["response_model"] for event in responses] == [
        "step-model-a",
        "step-model-b",
    ]
    for index, event in enumerate(responses):
        payload = _event_payload(event)
        assert payload["attempt_id"] == attempt.attempt_id
        assert payload["step_id"] == "s"
        assert payload["seq"] == 1
        assert payload["response_index"] == index
    stored = [record for record in ledger.records(attempt.attempt_id) if record.kind == "model_response"]
    assert [record.payload.get("response_model") for record in stored] == [
        "step-model-a",
        "step-model-b",
    ]


def test_step_response_without_model_records_null_not_pin(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {})])],
        response_models=[None],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    responses = [event for event in outcome.events if event["kind"] == "model_response"]
    assert len(responses) == 1
    assert _event_payload(responses[0])["response_model"] is None
    stored = [record for record in ledger.records(attempt.attempt_id) if record.kind == "model_response"]
    assert len(stored) == 1
    assert stored[0].payload.get("response_model") is None


def test_real_subagent_records_per_response_and_answering_model(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {"description": "look", "instructions": "read files"},
                        "s1",
                    )
                ]
            ),
            _ai(
                [
                    _call(
                        "report_subagent",
                        {"report": "findings"},
                        "sub-report-1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ],
        response_models=["step-model-a", "sub-model-a", "step-model-b"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    kinds = [event["kind"] for event in outcome.events]
    assert kinds == [
        "model_response",
        "subagent_queued",
        "subagent_started",
        "model_response",
        "subagent_ended",
        "model_response",
    ]
    step_responses = [
        event
        for event in outcome.events
        if event["kind"] == "model_response" and "call_id" not in _event_payload(event)
    ]
    sub_responses = [
        event
        for event in outcome.events
        if event["kind"] == "model_response" and "call_id" in _event_payload(event)
    ]
    assert [_event_payload(event)["response_model"] for event in step_responses] == [
        "step-model-a",
        "step-model-b",
    ]
    assert len(sub_responses) == 1
    sub_payload = _event_payload(sub_responses[0])
    assert sub_payload["call_id"] == "s1"
    assert sub_payload["response_model"] == "sub-model-a"
    assert sub_payload["response_index"] == 0
    ended = [event for event in outcome.events if event["kind"] == "subagent_ended"]
    assert len(ended) == 1
    ended_payload = _event_payload(ended[0])
    assert ended_payload["answering_model"] == "sub-model-a"
    assert ended_payload["response_models"] == ["sub-model-a"]
    assert "pinned_model_name" not in ended_payload
    stored_models = [
        record.payload.get("response_model")
        for record in ledger.records(attempt.attempt_id)
        if record.kind == "model_response"
    ]
    assert stored_models == ["step-model-a", "sub-model-a", "step-model-b"]


def test_real_subagent_answering_model_null_when_nothing_reported(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {"description": "look", "instructions": "read files"},
                        "s1",
                    )
                ]
            ),
            _ai(
                [
                    _call(
                        "report_subagent",
                        {"report": "findings"},
                        "sub-report-1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ],
        response_models=[None, None, None],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    ended = [event for event in outcome.events if event["kind"] == "subagent_ended"]
    assert len(ended) == 1
    ended_payload = _event_payload(ended[0])
    assert ended_payload["answering_model"] is None
    assert ended_payload["response_models"] == [None]


def test_copied_pin_key_helper_is_retired() -> None:
    import coding_agent.runtime.graph.subagent as subagent_module

    assert not hasattr(subagent_module, "pinned_model_name")
    assert "pinned_model_name" not in dir(subagent_module)


def test_subagent_started_carries_no_answering_model(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {"description": "look", "instructions": "read files"},
                        "s1",
                    )
                ]
            ),
            _ai(
                [
                    _call(
                        "report_subagent",
                        {"report": "findings"},
                        "sub-report-1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ],
        response_models=["step-model-a", "sub-model-a", "step-model-b"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    for event in outcome.events:
        if event["kind"] in ("subagent_queued", "subagent_started"):
            payload = _event_payload(event)
            assert "answering_model" not in payload
            assert "pinned_model_name" not in payload


def _pin(model: str = "model-a") -> PinnedModel:
    return PinnedModel(
        provider="anthropic", model=model, endpoint="messages", effort="low"
    )


def test_check_direct_pin_matches_exact_model() -> None:
    check = check_direct_pin(_pin("model-a"), _ai(metadata={"model": "model-a"}))
    assert check == check_direct_pin(_pin("model-a"), _ai(metadata={"model": "model-a"}))
    assert check.observed == "model-a"
    assert check.expected == "model-a"
    assert check.violated is False


def test_check_direct_pin_violation_on_any_difference() -> None:
    assert (
        check_direct_pin(_pin("model-a"), _ai(metadata={"model": "model-b"})).violated
        is True
    )
    snapshot = check_direct_pin(
        _pin("model-a"), _ai(metadata={"model": "model-a-20260101"})
    )
    assert snapshot.observed == "model-a-20260101"
    assert snapshot.expected == "model-a"
    assert snapshot.violated is True


def test_check_direct_pin_reads_model_name_fallback() -> None:
    assert (
        check_direct_pin(
            _pin("model-a"), _ai(metadata={"model_name": "model-a"})
        ).violated
        is False
    )
    assert (
        check_direct_pin(
            _pin("model-a"), _ai(metadata={"model_name": "model-b"})
        ).violated
        is True
    )


def test_check_direct_pin_unreported_is_not_a_violation() -> None:
    check = check_direct_pin(_pin("model-a"), _ai())
    assert check.observed is None
    assert check.expected == "model-a"
    assert check.violated is False


def test_pin_violation_reason_names_the_new_reason() -> None:
    assert PIN_VIOLATION_REASON == "pin-violation"
