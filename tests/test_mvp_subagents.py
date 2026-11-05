"""Sub-agents: one read-only conversation per ``spawn_subagent`` call.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (a Workflow Step's
model starts one Sub-agent per ``spawn_subagent`` call; the fixed toolset;
the Worker-authored framing followed by ``instructions`` verbatim; input
validation; the code-owned round bound; the headed result to the caller;
the over-size report returned once; redaction and capping through the
caller's registry) and
[ADR 0020](../../docs/adr/0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)
(one call, one Sub-agent; consecutive calls in a turn run one after
another — concurrent groups arrive in #215).

MVP row ids: C-SUB-5, C-SUB-6, C-SUB-8, C-SUB-9, C-SUB-22, C-SUB-25.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from conftest import FakeChatModel, init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import LedgerRecord, RunLedger
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    SUBAGENT_QUEUED_KIND,
    SUBAGENT_STARTED_KIND,
    SUBAGENT_FRAMING,
    SUBAGENT_TOOL_NAMES,
    compose_subagent_opening,
    pinned_model_name,
    run_subagent,
)
from coding_agent.runtime.transcript import TranscriptModel, TranscriptWriter
from support.fake_tracker import FakeTracker
from support.routing_model import CALLER_ROUTE, RoutedScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

JUNIT_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    *,
    content: str = "",
    calls: list[dict[str, object]] | None = None,
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=list(calls or []),
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def _spawn_call(
    description: str, instructions: str, call_id: str
) -> dict[str, object]:
    return _call(
        "spawn_subagent",
        {"description": description, "instructions": instructions},
        call_id,
    )


def _report_call(findings: str, call_id: str) -> dict[str, object]:
    return _call("report_subagent", {"report": findings}, call_id)


def _completion(call_id: str) -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"AC-1": ["pkg::test_ok"]},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _attempt_repository(tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote carrying the JUnit profile, and a workspace cloned from it."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(JUNIT_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    workspace = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workspace)], cwd=tmp_path)
    return bare, workspace


def _run(
    tmp_path: Path,
    skills_dir: Path,
    caller: Sequence[AIMessage],
    routes: Mapping[str, Sequence[AIMessage]] | None = None,
    *,
    wrap_transcript: bool = False,
) -> tuple[AttemptResult, RunLedger, RoutedScriptedModel, Path | None]:
    """Drive a whole Attempt with the routing model; return outcome, ledger, model."""
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    model: RoutedScriptedModel = RoutedScriptedModel(caller, routes)
    transcript_path: Path | None = None
    inner: Any = model
    if wrap_transcript:
        transcript_path = tmp_path / "transcript.jsonl"
        inner = TranscriptModel(model, TranscriptWriter(transcript_path))
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=FakeTracker(),
        ledger=ledger,
        model=inner,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
        remote_url=str(bare),
        agent_identity=IDENTITY,
        ceilings=LoopCeilings(),
    )
    return result, ledger, model, transcript_path


def _activate() -> AIMessage:
    return _ai(content="activating", calls=[_call("activate_implement", {}, "call_act_1")])


def _work() -> AIMessage:
    return _ai(
        content="working",
        calls=[
            _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, "call_write_1"),
            _call("test_targeted", {"path": "tests/test_greet.py"}, "call_test_1"),
        ],
    )


def _ledger_by_kind(ledger: RunLedger, kind: str) -> list[LedgerRecord]:
    return [record for record in ledger.records("#7/1") if record.kind == kind]


def _tool_text(message: ToolMessage) -> str:
    """A ToolMessage's text, unwrapping Anthropic block form where present.

    The runtime tags the trailing message of each request with a prompt-cache
    breakpoint, which converts its content from a plain string to a block
    list; both shapes carry the same text.
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _caller_tool_texts(model: RoutedScriptedModel) -> list[str]:
    """Every ToolMessage content the caller conversation observed, in order."""
    texts: list[str] = []
    for route, messages in model.invocations:
        if route != CALLER_ROUTE:
            continue
        for message in messages:
            if isinstance(message, ToolMessage):
                texts.append(_tool_text(message))
    return texts


def test_spawn_is_bound_in_every_step_binding(tmp_path: Path, skills_dir: Path) -> None:
    """Every caller binding offers ``spawn_subagent``; no Sub-agent one does."""
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("summarise", "[route:alpha] Summarise.", "call_spawn_alpha"),
            ],
        ),
        _ai(
            content="reporting",
            calls=[_report_call("alpha findings", "sub-report-1")],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [
            _ai(content="reading", calls=[_call("read_file", {"path": "README.md"}, "sub-read-1")]),
            _ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")]),
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        assert result.outcome == "failed"
        caller_bindings = [
            names
            for _choice, names in model.bind_history
            if "report_subagent" not in names
        ]
        subagent_bindings = [
            names
            for _choice, names in model.bind_history
            if "report_subagent" in names
        ]
        assert caller_bindings != []
        assert all("spawn_subagent" in names for names in caller_bindings)
        assert subagent_bindings != []
        assert all("spawn_subagent" not in names for names in subagent_bindings)
    finally:
        ledger.close()


def test_scenario_5_subagent_binds_exactly_the_fixed_set(tmp_path: Path, skills_dir: Path) -> None:
    """C-SUB-5: the bound set is exactly the fixed set; anything else is refused."""
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("probe the boundary", "[route:probe] Probe.", "call_spawn_probe"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "probe": [
            _ai(
                content="reaching",
                calls=[_call("write_file", {"path": "x.txt", "content": "x"}, "sub-w-1")],
            ),
            _ai(content="done", calls=[_report_call("probe findings", "sub-report-1")]),
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        assert result.outcome == "failed"
        assert set(model.bound_tool_names["probe"]) == set(SUBAGENT_TOOL_NAMES)
        assert "spawn_subagent" not in model.bound_tool_names["probe"]
        refused = [
            message.content
            for route, messages in model.invocations
            if route == "probe"
            for message in messages
            if isinstance(message, ToolMessage)
            and message.content == "Error: no such tool 'write_file'"
        ]
        assert refused != []
        texts = _caller_tool_texts(model)
        assert any("probe findings" in text for text in texts)
    finally:
        ledger.close()


def test_scenario_6_every_ended_event_names_the_pinned_model(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-6: every Sub-agent run records the Pinned Model as answering model."""
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first", "[route:one] First.", "call_spawn_one"),
                _spawn_call("second", "[route:two] Second.", "call_spawn_two"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "one": [_ai(content="done", calls=[_report_call("one findings", "sub-report-1")])],
        "two": [_ai(content="done", calls=[_report_call("two findings", "sub-report-2")])],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        ended = _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        assert len(ended) == 2
        expected = pinned_model_name(EFFECTIVE_CONFIGURATION)
        for record in ended:
            assert record.attempt_id == "#7/1"
            assert record.payload["answering_model"] == expected
    finally:
        ledger.close()


def test_scenario_8_silence_and_round_limit_yield_no_report(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-8: silence and the round bound each yield ``no-report`` with a reason."""
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("quiet one", "[route:quiet] Be quiet.", "call_spawn_quiet"),
                _spawn_call("busy one", "[route:busy] Keep reading.", "call_spawn_busy"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "quiet": [_ai(content="thinking, never reporting")],
        "busy": [
            _ai(
                content=f"reading {index}",
                calls=[_call("read_file", {"path": "README.md"}, f"sub-read-{index}")],
            )
            for index in range(8)
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        ended = {str(record.payload["call_id"]): record for record in _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)}
        assert ended["call_spawn_quiet"].payload["status"] == "no-report"
        assert ended["call_spawn_quiet"].payload["reason"] == "silent"
        assert "report" not in ended["call_spawn_quiet"].payload
        assert ended["call_spawn_busy"].payload["status"] == "no-report"
        assert ended["call_spawn_busy"].payload["reason"] == "round-limit"
        assert "report" not in ended["call_spawn_busy"].payload
        texts = _caller_tool_texts(model)
        quiet = next(text for text in texts if "'quiet one'" in text)
        busy = next(text for text in texts if "'busy one'" in text)
        assert "no-report (silent)" in quiet
        assert "no-report (round-limit)" in busy
        assert "reported" not in quiet and "reported" not in busy
    finally:
        ledger.close()


def test_scenario_9_invalid_calls_start_nothing(tmp_path: Path, skills_dir: Path) -> None:
    """C-SUB-9: an over-length description or over-size instructions starts nothing."""
    caller = [
        _activate(),
        _ai(
            content="spawning badly",
            calls=[
                _spawn_call("d" * 121, "[route:never] Too long a description.", "call_spawn_long"),
                _spawn_call("oversize", "[route:never] " + "i" * (64 * 1024), "call_spawn_big"),
            ],
        ),
        _ai(
            content="claiming uncovered",
            calls=[
                {
                    "name": "report_completion",
                    "args": {"AC-1": ["uncovered"]},
                    "id": "call_completion_1",
                    "type": "tool_call",
                }
            ],
        ),
    ]
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, None)
    try:
        texts = _caller_tool_texts(model)
        assert any("spawn_subagent 'description'" in text for text in texts)
        assert any("spawn_subagent 'instructions'" in text for text in texts)
        assert _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND) == []
        assert _ledger_by_kind(ledger, SUBAGENT_STARTED_KIND) == []
        assert _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND) == []
    finally:
        ledger.close()


def test_scenario_9_oversize_report_returned_once_then_accepted(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-9: an over-size report is returned once, then accepted when shortened."""
    oversize = "r" * (32 * 1024 + 1)
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("wordy", "[route:wordy] Report at length.", "call_spawn_wordy"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "wordy": [
            _ai(content="first draft", calls=[_report_call(oversize, "sub-report-1")]),
            _ai(content="shortened", calls=[_report_call("short findings", "sub-report-2")]),
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        ended = _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        assert len(ended) == 1
        assert ended[0].payload["status"] == "reported"
        assert ended[0].payload["report"] == "short findings"
        shorten_requests = [
            message.content
            for route, messages in model.invocations
            if route == "wordy"
            for message in messages
            if isinstance(message, ToolMessage) and "Shorten it" in str(message.content)
        ]
        assert len(shorten_requests) == 1
        texts = _caller_tool_texts(model)
        assert any("short findings" in text for text in texts)
        assert not any(oversize in text for text in texts)
    finally:
        ledger.close()


def test_oversize_report_accepted_on_second_oversize_call(tmp_path: Path) -> None:
    """The second over-size report is accepted: the return happens exactly once."""
    oversize = "r" * (32 * 1024 + 1)
    model = FakeChatModel(
        [
            _ai(content="first", calls=[_report_call(oversize, "call-1")]),
            _ai(content="second", calls=[_report_call(oversize, "call-2")]),
        ]
    )
    result = run_subagent(
        model,
        description="wordy",
        instructions="Report.",
        call_id="call_unit",
        workspace=tmp_path,
    )
    assert result.status == "reported"
    assert result.report == oversize
    tool_texts = [
        str(message.content) for message in result.messages if isinstance(message, ToolMessage)
    ]
    assert sum("Shorten it" in text for text in tool_texts) == 1


def test_scenario_22_opening_is_framing_then_instructions(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-22: a Sub-agent opens with the fixed framing, then ``instructions``."""
    instructions = "[route:open] Summarise README.md in one line."
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[_spawn_call("opener", instructions, "call_spawn_open")],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "open": [_ai(content="done", calls=[_report_call("a summary", "sub-report-1")])],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        first_invokes = [
            messages for route, messages in model.invocations if route == "open"
        ]
        assert first_invokes != []
        opening = first_invokes[0][0]
        assert isinstance(opening, HumanMessage)
        assert opening.content == compose_subagent_opening(instructions)
        assert str(opening.content).startswith(SUBAGENT_FRAMING)
        assert str(opening.content).endswith(instructions)
        assert len(first_invokes[0]) == 1
    finally:
        ledger.close()


def test_scenario_25_transcript_labels_each_subagent(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-25: the Transcript holds each Sub-agent under ``subagent:<call id>``."""
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first transcript", "[route:t1] First.", "call_spawn_t1"),
                _spawn_call("second transcript", "[route:t2] Second.", "call_spawn_t2"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "t1": [_ai(content="done", calls=[_report_call("t1 findings", "sub-report-1")])],
        "t2": [_ai(content="done", calls=[_report_call("t2 findings", "sub-report-2")])],
    }
    result, ledger, model, transcript_path = _run(
        tmp_path, skills_dir, caller, routes, wrap_transcript=True
    )
    try:
        assert transcript_path is not None
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
        assert lines != []
        records = [json.loads(line) for line in lines]
        seqs = [int(record["seq"]) for record in records]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        for call_id, description in (
            ("call_spawn_t1", "first transcript"),
            ("call_spawn_t2", "second transcript"),
        ):
            conversation = f"subagent:{call_id}"
            own = [record for record in records if record.get("conversation") == conversation]
            assert own != []
            requests = [record for record in own if record.get("kind") == "request"]
            assert requests != []
            assert requests[0].get("description") == description
    finally:
        ledger.close()


def test_subagent_ledger_events_carry_queueing_and_end_fields(
    tmp_path: Path, skills_dir: Path
) -> None:
    """Queued/started/ended events carry the Attempt id, call id and listed fields."""
    instructions_one = "[route:one] First."
    instructions_two = "[route:two] Second."
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first", instructions_one, "call_spawn_one"),
                _spawn_call("second", instructions_two, "call_spawn_two"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "one": [_ai(content="done", calls=[_report_call("one findings", "sub-report-1")])],
        "two": [_ai(content="done", calls=[_report_call("two findings", "sub-report-2")])],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        queued = _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)
        started = _ledger_by_kind(ledger, SUBAGENT_STARTED_KIND)
        ended = _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        assert [str(record.payload["call_id"]) for record in queued] == [
            "call_spawn_one",
            "call_spawn_two",
        ]
        assert [record.payload["group_ordinal"] for record in queued] == [0, 0]
        assert [record.payload["position_in_group"] for record in queued] == [0, 1]
        assert queued[0].payload["description"] == "first"
        assert queued[0].payload["instructions"] == instructions_one
        assert queued[1].payload["instructions"] == instructions_two
        assert [str(record.payload["call_id"]) for record in started] == [
            "call_spawn_one",
            "call_spawn_two",
        ]
        assert [str(record.payload["call_id"]) for record in ended] == [
            "call_spawn_one",
            "call_spawn_two",
        ]
        for record in queued + started + ended:
            assert record.attempt_id == "#7/1"
        for record in ended:
            assert record.payload["status"] == "reported"
            assert record.payload["reason"] == "reported"
            assert "usage" in record.payload
            assert "answering_model" in record.payload
        assert ended[0].payload["report"] == "one findings"
        assert ended[1].payload["report"] == "two findings"
    finally:
        ledger.close()


def test_blank_report_is_refused_and_records_nothing(tmp_path: Path) -> None:
    """A blank ``report_subagent`` records nothing; the run ends ``silent``."""
    model = FakeChatModel(
        [
            _ai(content="blank", calls=[_report_call("   ", "call-1")]),
            _ai(content="silent afterwards"),
        ]
    )
    result = run_subagent(
        model,
        description="blank",
        instructions="Report.",
        call_id="call_unit",
        workspace=tmp_path,
    )
    assert result.status == "no-report"
    assert result.reason == "silent"
    assert result.report is None
