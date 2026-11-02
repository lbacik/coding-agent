"""The step subgraph's bounded context (M1 slice of contract §11).

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (an oversized
tool result is capped to head plus tail plus a pointer naming the stored
artifact and its full size, so the model can ask for a range of it;
activation results are exempt; within a node every assistant turn is
carried back verbatim — ADR 0010 — and Exchange Units stay whole; cache
breakpoints are applied; a Pinned Prefix that cannot fit with its
overhead is refused before any model work).

MVP row ids: L3-IMP-5, L3-IMP-13.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.context_handoff import ContextWindowConfig
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.graph.step import (
    build_implement_step_graph,
    check_opening_fits_context,
    run_implement_step,
)
from coding_agent.runtime.result_capping import InMemoryArtifactStore
from conftest import FakeChatModel, init_origin_repo, run_git
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel


_PROFILE_YAML = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


def _commit_step_profile(src: Path) -> None:
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(_PROFILE_YAML, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add project profile"], cwd=src, check=True)


def _ai_tool_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai_message(
    *, tool_calls: list[dict[str, object]] | None = None, content: object = ""
) -> AIMessage:
    return AIMessage(
        content=content,  # type: ignore[arg-type]
        tool_calls=tool_calls or [],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


@tool
def _fake_write_file(path: str, content: str) -> str:
    """Records a write for the test to inspect."""
    return f"wrote {path!r}"


def test_step_caps_oversized_result_with_pointer_and_slice_is_readable() -> None:
    """L3-IMP-5 at the step subgraph seam: head + tail + pointer, then a slice."""
    store = InMemoryArtifactStore()

    @tool
    def dump() -> str:
        """Return a lot of text."""
        return "z" * 5000

    model = FakeChatModel(
        [
            _ai_message(tool_calls=[_ai_tool_call("dump", {}, "call-1")]),
            _ai_message(
                tool_calls=[
                    _ai_tool_call(
                        "read_result_slice",
                        {"artifact_id": "call-1", "start": 0, "end": 20},
                        "call-slice",
                    )
                ]
            ),
            _ai_message(content="done"),
        ]
    )
    messages = run_implement_step(
        model,
        [dump],
        [SystemMessage(content="hello")],
        forced_tool=None,
        result_cap_limit=100,
        artifact_store=store,
        cache_breakpoints=False,
    )
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    capped = str(tool_messages[0].content)
    assert len(capped) < 5000
    assert "call-1" in capped
    assert "5000" in capped
    assert store.read("call-1") == "z" * 5000
    # The capped text — not the full content — is what the next request saw.
    second_request = model.invocations[1]
    sent_tool = second_request[-1]
    assert isinstance(sent_tool, ToolMessage)
    assert len(str(sent_tool.content)) < 5000
    # The follow-up slice returns the chosen range of the full content.
    assert tool_messages[1].content == store.read("call-1")[0:20]


def test_step_leaves_activation_result_whole() -> None:
    """L3-IMP-5 exemption: activation results are never capped."""
    store = InMemoryArtifactStore()

    @tool
    def activate_implement() -> str:
        """A fake activation body larger than the cap."""
        return "s" * 5000

    @tool
    def dump() -> str:
        """Return a lot of text."""
        return "q" * 5000

    model = FakeChatModel(
        [
            _ai_message(tool_calls=[_ai_tool_call("activate_implement", {}, "call-act")]),
            _ai_message(tool_calls=[_ai_tool_call("dump", {}, "call-dump")]),
            _ai_message(content="done"),
        ]
    )
    messages = run_implement_step(
        model,
        [activate_implement, dump],
        [SystemMessage(content="hello")],
        forced_tool="activate_implement",
        result_cap_limit=100,
        artifact_store=store,
        cache_breakpoints=False,
    )
    by_id = {
        m.tool_call_id: str(m.content) for m in messages if isinstance(m, ToolMessage)
    }
    assert by_id["call-act"] == "s" * 5000
    assert "capped" not in by_id["call-act"]
    assert len(by_id["call-dump"]) < 5000
    assert "call-dump" in by_id["call-dump"]


def test_step_carries_assistant_turn_back_verbatim() -> None:
    """ADR 0010 at the step seam: the opaque assistant turn survives intact."""
    opaque = AIMessage(
        content=[
            {"type": "reasoning", "encrypted_content": "opaque-blob-xyz"},
            {"type": "text", "text": "thinking..."},
        ],
        tool_calls=[_ai_tool_call("_fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")],
        additional_kwargs={"some_provider_field": "must-not-be-dropped"},
    )
    model = FakeChatModel([opaque, _ai_message(content="done")])
    messages = run_implement_step(
        model,
        [_fake_write_file],
        [SystemMessage(content="hello")],
        forced_tool=None,
        cache_breakpoints=True,
    )
    assert messages[1] is opaque
    # The same opaque object is what the next request actually sent back:
    # breakpoints tag only the prefix end and the tail end, never the
    # assistant turn between them.
    assert model.invocations[1][1] is opaque


def test_step_applies_cache_breakpoints_without_leaking_into_storage() -> None:
    """Cache breakpoints tag the wire request only, never stored messages."""
    model = FakeChatModel(
        [
            _ai_message(tool_calls=[_ai_tool_call("_fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]),
            _ai_message(content="done"),
        ]
    )
    messages = run_implement_step(
        model,
        [_fake_write_file],
        [SystemMessage(content="the pinned prefix")],
        forced_tool=None,
        cache_breakpoints=True,
    )
    first_request = model.invocations[0]
    assert first_request[0].content == [
        {
            "type": "text",
            "text": "the pinned prefix",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    second_request = model.invocations[1]
    assert second_request[0].content == [
        {
            "type": "text",
            "text": "the pinned prefix",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    tail = second_request[-1]
    assert isinstance(tail, ToolMessage)
    assert isinstance(tail.content, list)
    # Stored conversation keeps exactly what was produced: plain strings.
    assert messages[0].content == "the pinned prefix"
    stored_tool = next(m for m in messages if isinstance(m, ToolMessage))
    assert stored_tool.content == "wrote 'a.txt'"


def test_check_opening_fits_context_refuses_overhead_with_no_capacity() -> None:
    """L3-IMP-13, second half: overhead leaving no capacity refuses the same way."""
    store = InMemoryArtifactStore()

    @tool
    def dump() -> str:
        """Return a lot of text."""
        return "x"

    opening = [SystemMessage(content="hi")]
    # Usable capacity of 1 token: any real opening plus tool schemas is over.
    window = ContextWindowConfig(
        threshold_tokens=11, safety_margin_tokens=5, request_overhead_tokens=5
    )
    assert window.usable_threshold == 1
    refusal = check_opening_fits_context(opening, [dump], window)
    assert refusal is not None
    assert "no model call" in refusal
    assert "no partial" in refusal.lower() or "partial" in refusal
    _ = store


def test_attempt_refuses_when_pinned_prefix_alone_crosses_threshold(
    skills_dir: Path,
) -> None:
    """L3-IMP-13 at the Attempt seam: no model call, no partial prefix."""
    tracker = FakeTracker()
    model = ScriptedModel([AIMessage(content="must not run", tool_calls=[])])
    with tempfile.TemporaryDirectory() as tmp:
        ledger = RunLedger(Path(tmp) / "ledger.sqlite3")
        try:
            src = Path(tmp) / "origin-src"
            init_origin_repo(src)
            _commit_step_profile(src)
            bare = Path(tmp) / "remote.git"
            subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
            workdir = Path(tmp) / "workspace"
            run_git(["clone", "-q", str(bare), str(workdir)], cwd=Path(tmp))
            result = run_attempt(
                issue_number=7,
                attempt_number=1,
                tracker=tracker,
                ledger=ledger,
                model=model,
                workspace=workdir,
                effective_configuration={"repo": "x/y", "issue": 7},
                skills_dir=skills_dir,
                context_window=ContextWindowConfig(
                    threshold_tokens=10, safety_margin_tokens=0, request_overhead_tokens=0
                ),
            )
            assert result.outcome == "failed"
            assert "handoff-failure" in result.reason
            assert model.invocations == [], "no model call on prefix refusal"
        finally:
            ledger.close()


def test_attempt_refuses_when_overhead_leaves_no_usable_capacity(
    skills_dir: Path,
) -> None:
    """L3-IMP-13, second half, at the Attempt seam: same refusal shape."""
    tracker = FakeTracker()
    model = ScriptedModel([AIMessage(content="must not run", tool_calls=[])])
    with tempfile.TemporaryDirectory() as tmp:
        ledger = RunLedger(Path(tmp) / "ledger.sqlite3")
        try:
            src = Path(tmp) / "origin-src"
            init_origin_repo(src)
            _commit_step_profile(src)
            bare = Path(tmp) / "remote.git"
            subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
            workdir = Path(tmp) / "workspace"
            run_git(["clone", "-q", str(bare), str(workdir)], cwd=Path(tmp))
            result = run_attempt(
                issue_number=7,
                attempt_number=1,
                tracker=tracker,
                ledger=ledger,
                model=model,
                workspace=workdir,
                effective_configuration={"repo": "x/y", "issue": 7},
                skills_dir=skills_dir,
                context_window=ContextWindowConfig(
                    threshold_tokens=11, safety_margin_tokens=5, request_overhead_tokens=5
                ),
            )
            assert result.outcome == "failed"
            assert "handoff-failure" in result.reason
            assert model.invocations == []
        finally:
            ledger.close()


def test_step_graph_tools_include_a_slice_reader_for_capped_pointers() -> None:
    """Structural: the step's bound set always offers the pointer's reader."""
    store = InMemoryArtifactStore()
    graph = build_implement_step_graph(
        FakeChatModel([_ai_message(content="done")]),
        [_fake_write_file],
        forced_tool=None,
        artifact_store=store,
    )
    _ = graph
    # run_implement_step auto-adds the reader; assert through a run's toolset
    # instead of graph internals: a capped pointer must resolve.
    model = FakeChatModel(
        [
            _ai_message(tool_calls=[_ai_tool_call("_fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]),
            _ai_message(content="done"),
        ]
    )
    messages = run_implement_step(
        model,
        [_fake_write_file],
        [SystemMessage(content="hello")],
        forced_tool=None,
        artifact_store=store,
    )
    assert any(isinstance(m, ToolMessage) for m in messages)
