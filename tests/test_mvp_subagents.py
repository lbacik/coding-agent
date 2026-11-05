"""Sub-agents: one read-only conversation per ``spawn_subagent`` call.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (a Workflow Step's
model starts one Sub-agent per ``spawn_subagent`` call; the fixed toolset;
the Worker-authored framing followed by ``instructions`` verbatim; input
validation; the code-owned round bound; the headed result to the caller;
the over-size report returned once; redaction and capping through the
caller's registry) and
[ADR 0020](../../docs/adr/0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)
(one call, one Sub-agent; consecutive calls in one turn run as one
concurrent group).

MVP row ids: C-SUB-5, C-SUB-6, C-SUB-8, C-SUB-9, C-SUB-10, C-SUB-22, C-SUB-25,
C-SUB-26, C-SUB-27, C-SUB-28, C-SUB-29, C-SUB-30, C-SUB-31, C-SUB-32, C-SUB-33.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from conftest import FakeChatModel, init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import LedgerRecord, RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.context_handoff import ContextWindowConfig
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    SUBAGENT_QUEUED_KIND,
    SUBAGENT_STARTED_KIND,
    SUBAGENT_FRAMING,
    SUBAGENT_TOOL_NAMES,
    build_read_change_tool,
    compose_subagent_opening,
    pinned_model_name,
    run_subagent,
    subagent_tool_result_text,
)
from coding_agent.runtime.step_progress import GATE, RESERVE, SOFT_STALL
from coding_agent.runtime.result_capping import InMemoryArtifactStore
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
    limit: int | None = None,
    latches: Mapping[tuple[str, int], threading.Event] | None = None,
    seed_started: Mapping[str, threading.Event] | None = None,
    ceilings: LoopCeilings | None = None,
    price: TokenPrices | None = None,
    context_window: ContextWindowConfig | None = None,
) -> tuple[AttemptResult, RunLedger, RoutedScriptedModel, Path | None]:
    """Drive a whole Attempt with the routing model; return outcome, ledger, model."""
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    model: RoutedScriptedModel = RoutedScriptedModel(caller, routes, latches=latches)
    if seed_started is not None:
        model.started_events.update(dict(seed_started))
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
        ceilings=ceilings if ceilings is not None else LoopCeilings(),
        price=price,
        context_window=context_window,
        max_parallel_subagents=limit,
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


def _subagent_change_texts(model: RoutedScriptedModel, route: str) -> list[str]:
    """Every ToolMessage content one Sub-agent conversation observed, in order."""
    texts: list[str] = []
    for routed, messages in model.invocations:
        if routed != route:
            continue
        for message in messages:
            if isinstance(message, ToolMessage):
                texts.append(_tool_text(message))
    return texts


def test_scenario_10_read_change_covers_uncommitted_and_untracked(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-10: ``read_change`` covers uncommitted and untracked changes."""
    caller = [
        _activate(),
        _ai(
            content="working",
            calls=[
                _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, "call_write_1"),
                _call("write_file", {"path": ".gitignore", "content": "*.log\n"}, "call_write_2"),
                _call("write_file", {"path": "debug.log", "content": "noise\n"}, "call_write_3"),
                _call(
                    "edit_file",
                    {"path": "README.md", "old_string": "hello\n", "new_string": "hello world\n"},
                    "call_edit_1",
                ),
                _call("test_targeted", {"path": "tests/test_greet.py"}, "call_test_1"),
            ],
        ),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("review the change", "[route:change] Review the change.", "call_spawn_change"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "change": [
            _ai(content="reading", calls=[_call("read_change", {}, "sub-change-1")]),
            _ai(content="done", calls=[_report_call("change findings", "sub-report-1")]),
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        assert result.outcome == "failed"
        change_texts = _subagent_change_texts(model, "change")
        assert change_texts != []
        change = change_texts[0]
        assert "(no commits between the Base Revision and HEAD)" in change
        assert "greeting.txt" in change
        assert "hello" in change
        assert "README.md" in change
        assert "hello world" in change
        assert ".gitignore" in change
        assert "debug.log" not in change
        texts = _caller_tool_texts(model)
        assert any("change findings" in text for text in texts)
    finally:
        ledger.close()


def test_scenario_10_read_change_bounds_large_change_with_pointer(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-10: a large change is bounded with a marker and a slice pointer."""
    big_content = "x" * 20_000
    caller = [
        _activate(),
        _ai(
            content="working",
            calls=[
                _call("write_file", {"path": "big.txt", "content": big_content}, "call_write_1"),
                _call("test_targeted", {"path": "tests/test_greet.py"}, "call_test_1"),
            ],
        ),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("review the big change", "[route:big] Review it.", "call_spawn_big"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "big": [
            _ai(content="reading", calls=[_call("read_change", {}, "sub-change-1")]),
            _ai(
                content="slicing",
                calls=[
                    _call(
                        "read_result_slice",
                        {
                            "artifact_id": "read-change-call_spawn_big",
                            "start": 0,
                            "end": 100,
                        },
                        "sub-slice-1",
                    )
                ],
            ),
            _ai(content="done", calls=[_report_call("big findings", "sub-report-1")]),
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        assert result.outcome == "failed"
        big_texts = _subagent_change_texts(model, "big")
        assert len(big_texts) == 3
        assert "characters total" in big_texts[0]
        assert "read_result_slice" in big_texts[0]
        assert "big.txt" in big_texts[1]
        texts = _caller_tool_texts(model)
        assert any("big findings" in text for text in texts)
    finally:
        ledger.close()


def test_scenario_10_read_change_states_an_unreadable_range(tmp_path: Path) -> None:
    """C-SUB-10: an unreadable range returns an explicit statement and no diff."""
    tool = build_read_change_tool(
        tmp_path,
        "0000000000000000000000000000000000000000",
        artifact_store=InMemoryArtifactStore(),
    )
    assert tool.name == "read_change"
    content = str(tool.invoke({}))
    assert "change unavailable" in content
    assert "diff --git" not in content


def _ledger_order(ledger: RunLedger) -> list[tuple[str, str]]:
    """Ledger event order as ``(kind, call_id)`` pairs, never wall-clock time."""
    return [
        (record.kind, str(record.payload.get("call_id")))
        for record in ledger.records("#7/1")
        if record.kind
        in (SUBAGENT_QUEUED_KIND, SUBAGENT_STARTED_KIND, SUBAGENT_ENDED_KIND)
    ]


def test_scenario_26_two_calls_at_default_limit_overlap_in_call_order(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-26: two calls in one turn at limit 2 overlap; results return in order."""
    beta_started = threading.Event()
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first parallel", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("second parallel", "[route:beta] Second.", "call_spawn_beta"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [_ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")])],
        "beta": [_ai(content="done", calls=[_report_call("beta findings", "sub-report-2")])],
    }
    result, ledger, model, transcript_path = _run(
        tmp_path,
        skills_dir,
        caller,
        routes,
        wrap_transcript=True,
        latches={("alpha", 0): beta_started},
        seed_started={"beta": beta_started},
    )
    try:
        order = _ledger_order(ledger)
        queued = [item for item in order if item[0] == SUBAGENT_QUEUED_KIND]
        assert queued == [
            (SUBAGENT_QUEUED_KIND, "call_spawn_alpha"),
            (SUBAGENT_QUEUED_KIND, "call_spawn_beta"),
        ]
        started_index = {
            call_id: position
            for position, (kind, call_id) in enumerate(order)
            if kind == SUBAGENT_STARTED_KIND
        }
        ended_index = {
            call_id: position
            for position, (kind, call_id) in enumerate(order)
            if kind == SUBAGENT_ENDED_KIND
        }
        assert set(started_index) == {"call_spawn_alpha", "call_spawn_beta"}
        assert set(ended_index) == {"call_spawn_alpha", "call_spawn_beta"}
        # Overlap is ledger event order only: the second start precedes the
        # first end. The latch holds alpha's first response until beta starts,
        # so this is deterministic, never timing-based.
        assert started_index["call_spawn_beta"] < ended_index["call_spawn_alpha"]
        assert started_index["call_spawn_alpha"] < started_index["call_spawn_beta"]
        queued_records = _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)
        assert [record.payload["group_ordinal"] for record in queued_records] == [0, 0]
        assert [record.payload["position_in_group"] for record in queued_records] == [0, 1]
        texts = _caller_tool_texts(model)
        positions = {
            "alpha findings": next(
                index for index, text in enumerate(texts) if "alpha findings" in text
            ),
            "beta findings": next(
                index for index, text in enumerate(texts) if "beta findings" in text
            ),
        }
        assert positions["alpha findings"] < positions["beta findings"]
        # Every concurrent record is intact: required payloads present, one
        # transcript line per record with monotonic unique sequence numbers.
        for record in queued_records:
            assert record.payload["description"] in ("first parallel", "second parallel")
            assert "instructions" in record.payload
        for record in _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND):
            assert record.payload["status"] == "reported"
            assert "report" in record.payload
            assert "usage" in record.payload
            assert "answering_model" in record.payload
        assert transcript_path is not None
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
        assert lines != []
        parsed = [json.loads(line) for line in lines]
        seqs = [int(record["seq"]) for record in parsed]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        for call_id in ("call_spawn_alpha", "call_spawn_beta"):
            own = [
                record
                for record in parsed
                if record.get("conversation") == f"subagent:{call_id}"
            ]
            assert own != []
    finally:
        ledger.close()


def test_scenario_27_two_calls_at_limit_one_do_not_overlap(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-27: at limit 1 the same turn runs sequentially in call order."""
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first sequential", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("second sequential", "[route:beta] Second.", "call_spawn_beta"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [_ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")])],
        "beta": [_ai(content="done", calls=[_report_call("beta findings", "sub-report-2")])],
    }
    result, ledger, model, _ = _run(
        tmp_path, skills_dir, caller, routes, limit=1
    )
    try:
        order = _ledger_order(ledger)
        assert order == [
            (SUBAGENT_QUEUED_KIND, "call_spawn_alpha"),
            (SUBAGENT_QUEUED_KIND, "call_spawn_beta"),
            (SUBAGENT_STARTED_KIND, "call_spawn_alpha"),
            (SUBAGENT_ENDED_KIND, "call_spawn_alpha"),
            (SUBAGENT_STARTED_KIND, "call_spawn_beta"),
            (SUBAGENT_ENDED_KIND, "call_spawn_beta"),
        ]
        texts = _caller_tool_texts(model)
        alpha_at = next(index for index, text in enumerate(texts) if "alpha findings" in text)
        beta_at = next(index for index, text in enumerate(texts) if "beta findings" in text)
        assert alpha_at < beta_at
    finally:
        ledger.close()


def test_scenario_28_third_call_queues_fifo_and_none_refused(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-28: three calls at limit 2 queue the third FIFO; none is refused."""
    beta_started = threading.Event()
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first queued", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("second queued", "[route:beta] Second.", "call_spawn_beta"),
                _spawn_call("third queued", "[route:gamma] Third.", "call_spawn_gamma"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [_ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")])],
        "beta": [_ai(content="done", calls=[_report_call("beta findings", "sub-report-2")])],
        "gamma": [_ai(content="done", calls=[_report_call("gamma findings", "sub-report-3")])],
    }
    result, ledger, model, _ = _run(
        tmp_path,
        skills_dir,
        caller,
        routes,
        limit=2,
        latches={("alpha", 0): beta_started},
        seed_started={"beta": beta_started},
    )
    try:
        order = _ledger_order(ledger)
        kinds = [kind for kind, _ in order]
        call_ids = [call_id for _, call_id in order]
        assert kinds[:3] == [SUBAGENT_QUEUED_KIND] * 3
        assert call_ids[:3] == ["call_spawn_alpha", "call_spawn_beta", "call_spawn_gamma"]
        started_at = {
            call_id: position
            for position, (kind, call_id) in enumerate(order)
            if kind == SUBAGENT_STARTED_KIND
        }
        ended_at = {
            call_id: position
            for position, (kind, call_id) in enumerate(order)
            if kind == SUBAGENT_ENDED_KIND
        }
        assert list(started_at) == ["call_spawn_alpha", "call_spawn_beta", "call_spawn_gamma"]
        # The third start follows a freed slot: at least one end precedes it.
        assert min(ended_at["call_spawn_alpha"], ended_at["call_spawn_beta"]) < started_at[
            "call_spawn_gamma"
        ]
        queued_records = _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)
        assert [record.payload["position_in_group"] for record in queued_records] == [0, 1, 2]
        assert {record.payload["group_ordinal"] for record in queued_records} == {0}
        texts = _caller_tool_texts(model)
        for findings in ("alpha findings", "beta findings", "gamma findings"):
            assert any(findings in text for text in texts)
        assert not any(text.startswith("Error:") for text in texts)
    finally:
        ledger.close()


def test_scenario_29_mixed_turn_barrier_and_second_group(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-29: ``[spawn, spawn, write, spawn]`` barriers the write mid-turn."""
    beta_started = threading.Event()
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning with a barrier",
            calls=[
                _spawn_call("first mixed", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("second mixed", "[route:beta] Second.", "call_spawn_beta"),
                _call(
                    "write_file",
                    {"path": "barrier-marker.txt", "content": "after-group-zero\n"},
                    "call_write_barrier",
                ),
                _spawn_call("third mixed", "[route:gamma] Third.", "call_spawn_gamma"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [_ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")])],
        "beta": [_ai(content="done", calls=[_report_call("beta findings", "sub-report-2")])],
        "gamma": [
            _ai(
                content="reading",
                calls=[_call("read_file", {"path": "barrier-marker.txt"}, "sub-read-1")],
            ),
            _ai(content="done", calls=[_report_call("gamma saw after-group-zero", "sub-report-2")]),
        ],
    }
    result, ledger, model, _ = _run(
        tmp_path,
        skills_dir,
        caller,
        routes,
        limit=2,
        latches={("alpha", 0): beta_started},
        seed_started={"beta": beta_started},
    )
    try:
        queued_records = _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)
        assert [str(record.payload["call_id"]) for record in queued_records] == [
            "call_spawn_alpha",
            "call_spawn_beta",
            "call_spawn_gamma",
        ]
        # Two groups: the first two spawns share an ordinal and the last
        # spawn forms its own group, the next ordinal.
        assert [record.payload["group_ordinal"] for record in queued_records] == [0, 0, 1]
        assert [record.payload["position_in_group"] for record in queued_records] == [0, 1, 0]
        order = _ledger_order(ledger)
        position_of = {item: position for position, item in enumerate(order)}
        for first_call in ("call_spawn_alpha", "call_spawn_beta"):
            assert position_of[(SUBAGENT_ENDED_KIND, first_call)] < position_of[
                (SUBAGENT_STARTED_KIND, "call_spawn_gamma")
            ]
        texts = _caller_tool_texts(model)
        alpha_at = next(index for index, text in enumerate(texts) if "alpha findings" in text)
        beta_at = next(index for index, text in enumerate(texts) if "beta findings" in text)
        gamma_at = next(
            index for index, text in enumerate(texts) if "gamma saw after-group-zero" in text
        )
        # Results join the Exchange Unit in emitted call order across the
        # barrier: both first-group results, then the write, then the last.
        assert alpha_at < beta_at < gamma_at
        assert any("barrier-marker.txt" in text for text in texts[beta_at + 1 : gamma_at])
        gamma_texts = [
            message.content
            for route, messages in model.invocations
            if route == "gamma"
            for message in messages
            if isinstance(message, ToolMessage)
        ]
        assert any("after-group-zero" in str(content) for content in gamma_texts)
    finally:
        ledger.close()


def _ai_tokens(
    content: str,
    *,
    input_tokens: int,
    output_tokens: int,
    calls: list[dict[str, object]] | None = None,
) -> AIMessage:
    """One scripted response with explicit provider usage for meter tests."""
    return AIMessage(
        content=content,
        tool_calls=list(calls or []),
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def test_scenario_7_ceiling_trip_stops_group_and_attempt(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-30: Sub-agent spend crossing a ceiling ends the group and the Attempt.

    At limit 1 the first Sub-agent's own spend crosses the tool-call ceiling
    mid-group: the running Sub-agent ends ``no-report`` / ``ceiling``, the
    queued one never starts yet still records its ``subagent_ended`` event
    with the same status, and the Attempt ends ``failed-limit`` naming the
    limit.
    """
    caller = [
        _activate(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first ceiled", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("second ceiled", "[route:beta] Second.", "call_spawn_beta"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [
            _ai(content="reading", calls=[_call("read_file", {"path": "README.md"}, "sub-read-1")]),
            _ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")]),
        ],
        "beta": [_ai(content="done", calls=[_report_call("beta findings", "sub-report-1")])],
    }
    result, ledger, model, _ = _run(
        tmp_path,
        skills_dir,
        caller,
        routes,
        limit=1,
        ceilings=LoopCeilings(max_tool_calls=2),
    )
    try:
        assert (result.outcome, result.reason) == ("failed-limit", "tool_calls")
        queued = _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)
        assert [str(record.payload["call_id"]) for record in queued] == [
            "call_spawn_alpha",
            "call_spawn_beta",
        ]
        started = _ledger_by_kind(ledger, SUBAGENT_STARTED_KIND)
        assert [str(record.payload["call_id"]) for record in started] == [
            "call_spawn_alpha"
        ]
        ended = {
            str(record.payload["call_id"]): record
            for record in _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        }
        assert set(ended) == {"call_spawn_alpha", "call_spawn_beta"}
        for record in ended.values():
            assert record.payload["status"] == "no-report"
            assert record.payload["reason"] == "ceiling"
            assert "report" not in record.payload
            assert "no-report / ceiling" in str(record.detail)
        # The caller loop exits on the ceiling with no further model call,
        # so the spawn results never reach another caller turn: the end
        # events above are the durable record, and the Attempt still ends
        # per the hard ceilings.
        stored = ledger.get_outcome("#7/1")
        assert stored is not None
        assert "limit crossed: tool_calls" in stored.terminal_comment_body
    finally:
        ledger.close()


def test_scenario_7_prespent_ceiling_starts_nothing(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-30: a ceiling crossed before the group starts no Sub-agent.

    The caller's own spawn turn crosses the token ceiling (tool calls only
    count as they run, after the group), so both queued Sub-agents never
    start — yet each still records its ``subagent_ended`` event with
    ``no-report`` / ``ceiling`` — and the Attempt ends ``failed-limit``
    naming the limit.
    """
    caller = [
        _activate(),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("first skipped", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("second skipped", "[route:beta] Second.", "call_spawn_beta"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "alpha": [_ai(content="done", calls=[_report_call("alpha findings", "sub-report-1")])],
        "beta": [_ai(content="done", calls=[_report_call("beta findings", "sub-report-2")])],
    }
    result, ledger, model, _ = _run(
        tmp_path,
        skills_dir,
        caller,
        routes,
        ceilings=LoopCeilings(max_effective_tokens=20),
    )
    try:
        assert (result.outcome, result.reason) == ("failed-limit", "tokens")
        assert len(_ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)) == 2
        assert _ledger_by_kind(ledger, SUBAGENT_STARTED_KIND) == []
        ended = _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        assert len(ended) == 2
        for record in ended:
            assert record.payload["status"] == "no-report"
            assert record.payload["reason"] == "ceiling"
            assert "report" not in record.payload
        assert model.started == ["caller"]
    finally:
        ledger.close()


def test_scenario_31_context_window_ends_no_report(tmp_path: Path) -> None:
    """C-SUB-31: a Sub-agent past the usable context ends ``no-report``.

    The opening alone already exceeds the usable threshold, so the run stops
    before any request — no model call, no content, no handoff.
    """
    model = FakeChatModel([])
    window = ContextWindowConfig(
        threshold_tokens=1200,
        safety_margin_tokens=1000,
        request_overhead_tokens=100,
    )
    assert window.usable_threshold == 100
    result = run_subagent(
        model,
        description="context-bound",
        instructions="[route:ctx] Summarise the tree.",
        call_id="call_ctx",
        workspace=tmp_path,
        context_window=window,
    )
    assert result.status == "no-report"
    assert result.reason == "context-window"
    assert result.report is None
    assert model.invocations == []
    text = subagent_tool_result_text(result)
    assert "no-report (context-window)" in text
    assert "reported" not in text


def test_scenario_32_subagent_spend_flushed_per_response(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-32: each Sub-agent response lands in the Attempt-wide meters at once.

    The caller spends 45 tokens before the group (its in-flight spawn call
    counts as it runs, after the group); the one Sub-agent answers twice.
    Both ``usage`` flushes — 60 tokens / 4 calls, then 75 / 5 — are durable
    between its ``subagent_started`` and ``subagent_ended`` events, before
    the group ends, with per-bucket spend.
    """
    caller = [
        _activate(),
        _work(),
        _ai(
            content="spawning",
            calls=[_spawn_call("solo flush", "[route:solo] Flush twice.", "call_spawn_solo")],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        "solo": [
            _ai(content="reading", calls=[_call("read_file", {"path": "README.md"}, "sub-read-1")]),
            _ai(content="done", calls=[_report_call("solo findings", "sub-report-1")]),
        ],
    }
    result, ledger, model, _ = _run(tmp_path, skills_dir, caller, routes)
    try:
        records = ledger.records("#7/1")
        started_at = next(
            index
            for index, record in enumerate(records)
            if record.kind == SUBAGENT_STARTED_KIND
        )
        ended_at = next(
            index
            for index, record in enumerate(records)
            if record.kind == SUBAGENT_ENDED_KIND
        )
        assert started_at < ended_at
        in_window: list[dict[str, Any]] = []
        for record in records[started_at + 1 : ended_at]:
            if record.kind != "usage":
                continue
            window_usage = record.payload["usage"]
            assert isinstance(window_usage, dict)
            in_window.append(window_usage)
        assert [usage["tokens"] for usage in in_window] == [60, 75]
        assert [usage["tool_calls"] for usage in in_window] == [4, 5]
        ended = _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        assert len(ended) == 1
        assert ended[0].payload["status"] == "reported"
        usage = ended[0].payload["usage"]
        assert isinstance(usage, dict)
        # The end event carries the Sub-agent's own spend (two responses);
        # the Attempt-wide cumulative (75 tokens / 5 calls) is already
        # durable in the last per-response ``usage`` flush above.
        assert usage["tokens"] == 30
        assert usage["tool_calls"] == 2
        for key in (
            "effective_tokens",
            "cost_usd",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_input_usd",
            "cost_output_usd",
            "cost_cache_read_usd",
            "cost_cache_write_usd",
        ):
            assert key in usage
        texts = _caller_tool_texts(model)
        assert any("solo findings" in text for text in texts)
    finally:
        ledger.close()


def test_scenario_33_reserve_spawn_runs_without_gate_feed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-SUB-33: spawning runs in the verification reserve and feeds no Gate.

    Caller spend enters the reserve before the spawn turn; all three queued
    Sub-agents still run to reports (no count cap), the second claim is
    still accepted past the reserve with the review gate unmet, and the six
    Sub-agent responses record no ``soft_stall``.
    """
    caller = [
        _ai_tokens(
            "activating",
            input_tokens=1200,
            output_tokens=200,
            calls=[_call("activate_implement", {}, "call_act_1")],
        ),
        _ai_tokens(
            "working",
            input_tokens=200,
            output_tokens=200,
            calls=[
                _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, "call_write_1"),
                _call("test_targeted", {"path": "tests/test_greet.py"}, "call_test_1"),
            ],
        ),
        _ai(
            content="spawning",
            calls=[
                _spawn_call("reserve one", "[route:alpha] First.", "call_spawn_alpha"),
                _spawn_call("reserve two", "[route:beta] Second.", "call_spawn_beta"),
                _spawn_call("reserve three", "[route:gamma] Third.", "call_spawn_gamma"),
            ],
        ),
        _completion("call_completion_1"),
        _completion("call_completion_2"),
    ]
    routes = {
        key: [
            _ai(content="reading", calls=[_call("read_file", {"path": "README.md"}, f"sub-read-{key}")]),
            _ai(content="done", calls=[_report_call(f"{key} findings", f"sub-report-{key}")]),
        ]
        for key in ("alpha", "beta", "gamma")
    }
    result, ledger, model, _ = _run(
        tmp_path,
        skills_dir,
        caller,
        routes,
        ceilings=LoopCeilings(max_effective_tokens=2000),
    )
    try:
        assert (result.outcome, result.reason) == ("failed", "review-missing")
        records = ledger.records("#7/1")
        kinds = [record.kind for record in records]
        assert GATE in kinds
        assert RESERVE in kinds
        reserve_at = kinds.index(RESERVE)
        first_started_at = next(
            index for index, record in enumerate(records) if record.kind == SUBAGENT_STARTED_KIND
        )
        assert reserve_at < first_started_at
        queued = _ledger_by_kind(ledger, SUBAGENT_QUEUED_KIND)
        assert [record.payload["position_in_group"] for record in queued] == [0, 1, 2]
        ended = _ledger_by_kind(ledger, SUBAGENT_ENDED_KIND)
        assert len(ended) == 3
        for record in ended:
            assert record.payload["status"] == "reported"
            assert "report" in record.payload
        texts = _caller_tool_texts(model)
        for key in ("alpha", "beta", "gamma"):
            assert any(f"{key} findings" in text for text in texts)
        assert _ledger_by_kind(ledger, SOFT_STALL) == []
        # The six Sub-agent responses neither advance nor reset gate state:
        # no progress_event is durable while any Sub-agent runs (the caller
        # stays suspended for the whole group, so any in-window event could
        # only come from a Sub-agent response).
        last_ended_at = max(
            index
            for index, record in enumerate(records)
            if record.kind == SUBAGENT_ENDED_KIND
        )
        assert first_started_at < last_ended_at
        assert [
            record.kind
            for record in records[first_started_at : last_ended_at + 1]
            if record.kind == "progress_event"
        ] == []
    finally:
        ledger.close()
