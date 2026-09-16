from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from coding_agent.implement.context_handoff import (
    CONTINUATION_SNAPSHOT_VERSION,
    ContextWindowConfig,
    ContinuationSnapshot,
    HandoffFailure,
    snapshot_artifact_key,
)
from coding_agent.implement.ceilings import InMemoryUsageLedger, LoopCeilings, UsageTotals
from coding_agent.implement.loop import ToolLoopResult, run_tool_loop
from coding_agent.implement.progress import AttemptPolicy, BudgetSnapshot, RunLedger
from coding_agent.implement.result_capping import InMemoryArtifactStore
from coding_agent.provider.price_table import TokenPrices
from conftest import FakeChatModel


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _response(*, content: str = "", tool_calls: list[dict[str, object]] | None = None, output: int = 0) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata={"input_tokens": 4, "output_tokens": output, "total_tokens": 4 + output},
    )


@tool
def write_file(path: str, content: str) -> str:
    """Write a test result."""
    return f"wrote {path}: {content}" + ("x" * 800 if path == "a" else "")


def _snapshot_payload() -> dict[str, object]:
    return {
        "version": CONTINUATION_SNAPSHOT_VERSION,
        "attempt_id": "#47/1",
        "base_revision": "base-sha",
        "branch": "agent/47/1-handoff",
        "commit": "head-sha",
        "changed_files": ["src/example.py"],
        "commands": [{"command": "pytest", "result": "1 passed"}],
        "validation_evidence": {"executed": 1, "failures": []},
        "unresolved_questions": [],
        "next_intended_action": "Run the targeted test.",
    }


def _run(
    model: FakeChatModel,
    store: InMemoryArtifactStore,
    *,
    ledger: RunLedger | None = None,
    context_window: ContextWindowConfig,
    redactions: dict[str, str] | None = None,
) -> ToolLoopResult:
    policy = AttemptPolicy("#47/1", LoopCeilings(), ledger=ledger)
    return run_tool_loop(
        model,
        [write_file],
        [SystemMessage(content="pinned prefix")],
        price=TokenPrices(input=1, output=1, cache_read=1, cache_write=1),
        context_window=context_window,
        result_cap_limit=10_000,
        result_store=store,
        ceilings=LoopCeilings(),
        usage_ledger=InMemoryUsageLedger(),
        policy=policy,
        redactions=redactions or {},
    )


def test_context_window_configuration_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        ContextWindowConfig(threshold_tokens=0)
    with pytest.raises(ValueError):
        ContextWindowConfig(threshold_tokens=100, handoff_token_cap=0)
    with pytest.raises(ValueError):
        ContextWindowConfig(threshold_tokens=100, safety_margin_tokens=100, request_overhead_tokens=1)


def test_continuation_snapshot_is_versioned_redacted_and_digest_addressed() -> None:
    snapshot = ContinuationSnapshot.from_payload(_snapshot_payload(), redactions={"secret": "top-secret"})
    encoded = snapshot.to_json()

    assert snapshot.version == CONTINUATION_SNAPSHOT_VERSION
    assert snapshot.digest == snapshot.digest_for(encoded)
    assert snapshot_artifact_key(snapshot.digest).endswith(f"{snapshot.digest}.json")
    assert "top-secret" not in encoded


def test_handoff_persists_once_and_starts_a_fresh_node_with_only_prefix_and_snapshot(tmp_path: Path) -> None:
    payload = _snapshot_payload()
    model = FakeChatModel(
        [
            _response(tool_calls=[_call("write_file", {"path": "a", "content": "x"}, "old-call")]),
            _response(content=json.dumps(payload)),
            _response(tool_calls=[_call("write_file", {"path": "b", "content": "y"}, "new-call")]),
            _response(content="done"),
        ]
    )
    store = InMemoryArtifactStore()
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    try:
        result = _run(
            model,
            store,
            ledger=ledger,
            context_window=ContextWindowConfig(threshold_tokens=210, safety_margin_tokens=0, request_overhead_tokens=0),
        )

        assert result.stopped_by is None
        assert result.handoff_count == 1
        assert len(result.handoff_snapshot_digests) == 1
        assert len(result.provider_usage) == 4
        assert result.provider_usage[1].output_tokens == 0
        continuation_request = model.invocations[2]
        assert continuation_request[0] == SystemMessage(content="pinned prefix")
        assert any(isinstance(message, HumanMessage) and "base_revision" in str(message.content) for message in continuation_request)
        assert not any(isinstance(message, ToolMessage) and message.tool_call_id == "old-call" for message in continuation_request)
        assert not any(isinstance(message, AIMessage) for message in continuation_request)
        handoffs = [record for record in ledger.records("#47/1") if record.kind == "handoff"]
        assert len(handoffs) == 1
        assert handoffs[0].payload["snapshot_digest"] == result.handoff_snapshot_digests[0]
    finally:
        ledger.close()


def test_recorded_snapshot_is_reused_without_a_second_handoff(tmp_path: Path) -> None:
    payload = _snapshot_payload()
    snapshot = ContinuationSnapshot.from_payload(payload)
    store = InMemoryArtifactStore()
    store.store(snapshot_artifact_key(snapshot.digest), snapshot.to_json())
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.record(
        "#47/1",
        "handoff",
        BudgetSnapshot(0, UsageTotals()),
        artifact_ref=snapshot_artifact_key(snapshot.digest),
        payload={"snapshot_digest": snapshot.digest, "snapshot_version": snapshot.version},
    )
    model = FakeChatModel([_response(content="done")])
    try:
        result = _run(
            model,
            store,
            ledger=ledger,
            context_window=ContextWindowConfig(threshold_tokens=1000, safety_margin_tokens=0, request_overhead_tokens=0),
        )
        assert result.handoff_count == 0
        assert len(model.invocations) == 1
        assert any("next_intended_action" in str(message.content) for message in model.invocations[0])
    finally:
        ledger.close()


class UnpersistableArtifactStore(InMemoryArtifactStore):
    """Reject snapshot writes while retaining failure artifacts."""

    def store(self, artifact_id: str, content: str) -> None:
        if artifact_id.endswith(".json") and "handoff-failure" not in artifact_id:
            raise OSError("artifact storage unavailable: secret-token")
        super().store(artifact_id, content)


def test_unpersistable_snapshot_stops_before_continuation_and_redacts_failure() -> None:
    model = FakeChatModel(
        [
            _response(tool_calls=[_call("write_file", {"path": "a", "content": "x"}, "old-call")]),
            _response(content=json.dumps(_snapshot_payload())),
            _response(content="must not run"),
        ]
    )
    store = UnpersistableArtifactStore()

    result = _run(
        model,
        store,
        context_window=ContextWindowConfig(threshold_tokens=210, safety_margin_tokens=0, request_overhead_tokens=0),
        redactions={"GITHUB_TOKEN": "secret-token"},
    )

    assert result.stopped_by == HandoffFailure.classification
    assert len(model.invocations) == 2
    assert result.handoff_failure_ref is not None
    failure = store.read(result.handoff_failure_ref)
    assert "secret-token" not in failure


@pytest.mark.parametrize(
    "handoff_response",
    [
        _response(content="not json"),
        _response(content=json.dumps(_snapshot_payload()), output=101),
    ],
)
def test_malformed_or_over_limit_handoff_stops_without_continuation(
    handoff_response: AIMessage,
) -> None:
    model = FakeChatModel(
        [
            _response(tool_calls=[_call("write_file", {"path": "a", "content": "x"}, "old-call")]),
            handoff_response,
            _response(content="must not run"),
        ]
    )
    result = _run(
        model,
        InMemoryArtifactStore(),
        context_window=ContextWindowConfig(threshold_tokens=200, safety_margin_tokens=0, request_overhead_tokens=0, handoff_token_cap=100),
        redactions={"GITHUB_TOKEN": "secret-token"},
    )

    assert result.stopped_by == HandoffFailure.classification
    assert result.handoff_failure_ref is not None
    assert len(model.invocations) == 2
