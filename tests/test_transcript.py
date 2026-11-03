"""The per-Attempt model transcript: requests, responses, redaction.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (the transcript shows
which check returned a live claim) and §18 (registered secrets are replaced
by ``«redacted:<name>»`` before anything is written).

MVP row ids: L3-ERR-11, C-IMP-1.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from coding_agent.runtime.completion import (
    MAX_LISTED_RECORDED_IDS,
    ClaimFailure,
    format_claim_return,
)
from coding_agent.runtime.transcript import TranscriptModel, TranscriptWriter
from support.scripted_model import ScriptedModel


@tool
def probe(value: str) -> str:
    """A probe tool for binding."""
    return value


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_opening_messages_response_and_following_tool_results(tmp_path: Path) -> None:
    """§9: each request carries the messages new since the last assistant turn."""
    path = tmp_path / "transcript.jsonl"
    reply = AIMessage(
        content="calling",
        tool_calls=[{"name": "probe", "args": {"value": "x"}, "id": "call_1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    inner = ScriptedModel([reply, AIMessage(content="done")])
    model = TranscriptModel(inner, TranscriptWriter(path)).bind_tools([probe], tool_choice="probe")

    opening: list[BaseMessage] = [HumanMessage(content="brief")]
    first = model.invoke(opening)
    assert first is reply
    model.invoke([*opening, first, ToolMessage(content="x", tool_call_id="call_1")])

    records = _records(path)
    assert [r["kind"] for r in records] == ["request", "response", "request", "response"]
    assert [r["seq"] for r in records] == [1, 2, 3, 4]
    assert records[0]["tools"] == ["probe"]
    assert records[0]["tool_choice"] == "probe"
    assert records[0]["new_messages"] == [{"type": "human", "content": "brief"}]
    response = records[1]["message"]
    assert isinstance(response, dict)
    assert response["tool_calls"] == [{"name": "probe", "id": "call_1", "args": {"value": "x"}}]
    assert response["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert records[2]["message_count"] == 3
    assert records[2]["new_messages"] == [
        {"type": "tool", "content": "x", "tool_call_id": "call_1"}
    ]


def test_unforced_bind_passes_no_tool_choice(tmp_path: Path) -> None:
    """The wrapped model is bound exactly as the caller asked (ADR 0010)."""
    calls: list[tuple[list[str], str | None, bool]] = []

    class Recording(ScriptedModel):
        def bind_tools(
            self, tools: Sequence[BaseTool], tool_choice: str | None = None, **kwargs: object
        ) -> ScriptedModel:
            calls.append(([t.name for t in tools], tool_choice, "tool_choice" in kwargs))
            return super().bind_tools(tools, tool_choice)

    TranscriptModel(Recording([]), TranscriptWriter(tmp_path / "t.jsonl")).bind_tools([probe])
    assert calls == [(["probe"], None, False)]


def test_registered_secrets_never_reach_the_transcript(tmp_path: Path) -> None:
    """§18, L3-ERR-11: raw and JSON-escaped secret values are redacted."""
    path = tmp_path / "transcript.jsonl"
    quoted = 'pa"ss\\word'
    inner = ScriptedModel([AIMessage(content="token github_pat_secret123 seen")])
    model = TranscriptModel(
        inner,
        TranscriptWriter(path, {"GITHUB_TOKEN": "github_pat_secret123", "OTHER": quoted, "EMPTY": ""}),
    ).bind_tools([probe])

    model.invoke([HumanMessage(content=f"use github_pat_secret123 and {quoted}")])

    text = path.read_text(encoding="utf-8")
    assert "github_pat_secret123" not in text
    assert json.dumps(quoted)[1:-1] not in text
    assert "«redacted:GITHUB_TOKEN»" in text
    assert "«redacted:OTHER»" in text


def test_a_failed_request_is_recorded_and_raised(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"

    class Failing(ScriptedModel):
        def invoke(self, input: LanguageModelInput) -> BaseMessage:
            raise RuntimeError("provider down")

    model = TranscriptModel(Failing([]), TranscriptWriter(path)).bind_tools([probe])
    with pytest.raises(RuntimeError, match="provider down"):
        model.invoke([HumanMessage(content="brief")])
    records = _records(path)
    assert [r["kind"] for r in records] == ["request", "error"]
    assert records[1]["error"] == "RuntimeError: provider down"


def test_evidence_return_lists_recorded_ids_when_a_test_is_absent() -> None:
    """§9, C-IMP-1: an absent test is answered with the ids the evidence records."""
    failures = [
        ClaimFailure("AC-1", "tests/test_pricing.py::test_x", "absent from Criterion Evidence")
    ]
    text = format_claim_return(
        failures,
        returns_so_far=0,
        recorded_ids=frozenset({"tests.test_pricing::test_x", "tests.test_pricing::test_y"}),
    )
    assert "Recorded test ids:" in text
    assert "- tests.test_pricing::test_x" in text
    assert "- tests.test_pricing::test_y" in text


def test_evidence_return_lists_no_ids_when_no_test_is_absent() -> None:
    failures = [ClaimFailure("AC-1", "pkg::t", "latest recorded run did not pass")]
    text = format_claim_return(failures, returns_so_far=0, recorded_ids=frozenset({"pkg::t"}))
    assert "Recorded test ids" not in text


def test_evidence_return_bounds_the_listed_ids() -> None:
    failures = [ClaimFailure("AC-1", "missing", "absent from Criterion Evidence")]
    recorded = frozenset(f"pkg::t{index:03d}" for index in range(MAX_LISTED_RECORDED_IDS + 7))
    text = format_claim_return(failures, returns_so_far=0, recorded_ids=recorded)
    assert "(and 7 more)" in text
    assert sum(1 for line in text.splitlines() if line.startswith("- pkg::t")) == (
        MAX_LISTED_RECORDED_IDS
    )


def test_evidence_return_says_when_nothing_is_recorded() -> None:
    failures = [ClaimFailure("AC-1", "missing", "absent from Criterion Evidence")]
    text = format_claim_return(failures, returns_so_far=0)
    assert "Criterion Evidence has no recorded test yet." in text
