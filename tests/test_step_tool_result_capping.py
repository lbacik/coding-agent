"""Capped tool results and unknown-argument refusal in the step loops.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (an
oversized tool result is capped to its head, its tail and a pointer to the
stored artifact and its full size; activation results are exempt).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    InheritedConversation,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import REPORT_SUBAGENT_TOOL
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    InMemoryArtifactStore,
    build_read_result_slice_tool,
)
from coding_agent.workflow.schema import Agent, RouteTarget, WorkflowStep
from support.agent_step_deps import FakeCommandRunner, make_agent_step_deps
from support.scripted_model import ScriptedModel


def _facts() -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id="implement#7/1",
        workflow_id="implement",
        input_type="issue",
        input_number=7,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )


def _agent_step(
    step_id: str,
    *,
    tools: tuple[str, ...] = ("workspace-read",),
) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        agent=Agent(
            instructions=(),
            tools=tools,
            inputs=(),
            results={"done": {}},
        ),
        max_executions=1,
        routes={"done": RouteTarget(end="completed")},
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
) -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls or []))


def _report_call(call_id: str = "report-1") -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": "done", "payload_json": json.dumps({})},
        call_id,
    )


def _tool_texts(model: ScriptedModel) -> list[str]:
    return [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]


def test_main_step_caps_oversized_read_file(tmp_path: Path) -> None:
    """A `read_file` over the cap adds head + tail + pointer, fully readable."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    big = "0123456789" * 600
    assert len(big) > DEFAULT_RESULT_CAP_LIMIT
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "big.txt"}, "r1")]),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path
    )
    outcome = run_agent_step(_agent_step("s"), 1, deps, {})
    assert outcome.status == "reported"
    texts = _tool_texts(model)
    assert len(texts) == 1
    assert len(texts[0]) < len(big)
    assert "capped:" in texts[0]
    assert "read_result_slice" in texts[0]
    assert "r1" in texts[0]
    assert deps.artifact_store.read("r1") == big
    reader = build_read_result_slice_tool(deps.artifact_store)
    assert str(reader.invoke({"artifact_id": "r1", "start": 0, "end": 10})) == big[0:10]
    assert (
        str(
            reader.invoke(
                {"artifact_id": "r1", "start": len(big) - 10, "end": len(big)}
            )
        )
        == big[len(big) - 10 :]
    )


def test_main_step_small_read_file_is_unchanged(tmp_path: Path) -> None:
    """A `read_file` at or under the cap returns the whole file verbatim."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "small.txt").write_text("hello\n", encoding="utf-8")
    model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "small.txt"}, "r1")]),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path
    )
    outcome = run_agent_step(_agent_step("s"), 1, deps, {})
    assert outcome.status == "reported"
    assert _tool_texts(model) == ["hello\n"]


def test_main_step_caps_oversized_run_command(tmp_path: Path) -> None:
    """A `run_command` result over the cap is capped with a resolving pointer."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    runner = FakeCommandRunner(stdout="y" * 5000)
    model = ScriptedModel(
        [
            _ai([_call("run_command", {"command": "echo hi"}, "c1")]),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        runner=runner,
    )
    outcome = run_agent_step(
        _agent_step("s", tools=("workspace-read", "shell")), 1, deps, {}
    )
    assert outcome.status == "reported"
    texts = _tool_texts(model)
    assert len(texts) == 1
    assert "capped:" in texts[0]
    assert "read_result_slice" in texts[0]
    full = deps.artifact_store.read("c1")
    assert "y" * 5000 in full
    assert "exit_code=0" in full


def test_main_step_small_run_command_is_byte_for_byte(tmp_path: Path) -> None:
    """A `run_command` result under the cap reaches the model exactly as built."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    runner = FakeCommandRunner(stdout="ok")
    model = ScriptedModel(
        [
            _ai([_call("run_command", {"command": "echo hi"}, "c1")]),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        runner=runner,
    )
    outcome = run_agent_step(
        _agent_step("s", tools=("workspace-read", "shell")), 1, deps, {}
    )
    assert outcome.status == "reported"
    assert _tool_texts(model) == [
        "run_command exit_code=0\n"
        "command: echo hi\n"
        "timed_out: false\n"
        "stdout:\n"
        "ok\n"
        "stderr:\n"
    ]


def test_main_step_two_capped_results_resolve_independently(
    tmp_path: Path,
) -> None:
    """Two capped results in one execution each keep their own full content."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    first = "FIRST-" + "a" * 5000
    second = "SECOND-" + "b" * 5000
    (workspace / "first.txt").write_text(first, encoding="utf-8")
    (workspace / "second.txt").write_text(second, encoding="utf-8")
    model = ScriptedModel(
        [
            _ai(
                [
                    _call("read_file", {"path": "first.txt"}, "r1"),
                    _call("read_file", {"path": "second.txt"}, "r2"),
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path
    )
    outcome = run_agent_step(_agent_step("s"), 1, deps, {})
    assert outcome.status == "reported"
    texts = _tool_texts(model)
    assert len(texts) == 2
    assert all("capped:" in text for text in texts)
    assert deps.artifact_store.read("r1") == first
    assert deps.artifact_store.read("r2") == second


def test_main_step_unknown_args_on_read_file_refused(tmp_path: Path) -> None:
    """`read_file` with `start_line`/`end_line` errors and points to `read_lines`."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "big.txt").write_text("z" * 5000, encoding="utf-8")
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "read_file",
                        {"path": "big.txt", "start_line": 1, "end_line": 30},
                        "r1",
                    )
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path
    )
    outcome = run_agent_step(_agent_step("s"), 1, deps, {})
    assert outcome.status == "reported"
    texts = _tool_texts(model)
    assert len(texts) == 1
    assert texts[0].startswith("Error:")
    assert "start_line" in texts[0]
    assert "end_line" in texts[0]
    assert "path" in texts[0]
    assert "read_lines" in texts[0]
    assert "z" * 100 not in texts[0]


def test_main_step_unknown_args_missing_file_proves_no_run(
    tmp_path: Path,
) -> None:
    """The refused call never runs: no `could not read`, no stored artifact."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "read_file",
                        {"path": "missing.txt", "start_line": 1},
                        "r1",
                    )
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    store = InMemoryArtifactStore()
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, artifact_store=store
    )
    outcome = run_agent_step(_agent_step("s"), 1, deps, {})
    assert outcome.status == "reported"
    texts = _tool_texts(model)
    assert len(texts) == 1
    assert texts[0].startswith("Error:")
    assert "start_line" in texts[0]
    assert "could not read" not in texts[0]
    with pytest.raises(Exception):
        store.read("r1")


def test_main_step_unknown_args_on_run_command_runs_nothing(
    tmp_path: Path,
) -> None:
    """An unknown argument on `run_command` errors and executes no command."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    runner = FakeCommandRunner(stdout="ok")
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "run_command",
                        {"command": "echo hi", "bogus_flag": True},
                        "c1",
                    )
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        runner=runner,
    )
    outcome = run_agent_step(
        _agent_step("s", tools=("workspace-read", "shell")), 1, deps, {}
    )
    assert outcome.status == "reported"
    texts = _tool_texts(model)
    assert len(texts) == 1
    assert texts[0].startswith("Error:")
    assert "bogus_flag" in texts[0]
    assert "command" in texts[0]
    assert runner.calls == []


def test_main_step_still_reports_after_unknown_argument(
    tmp_path: Path,
) -> None:
    """A refused call is an ordinary call: the step continues to its report."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "read_file",
                        {"path": "x.txt", "start_line": 1},
                        "r1",
                    )
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path
    )
    outcome = run_agent_step(_agent_step("s"), 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"


def test_pointer_inherited_by_reserved_receiver_resolves(
    tmp_path: Path,
) -> None:
    """A capped pointer carried into the receiver resolves through its reader."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    big = "0123456789" * 600
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    first_model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "big.txt"}, "r1")]),
            _ai([_report_call()]),
        ]
    )
    store = InMemoryArtifactStore()
    first_deps = make_agent_step_deps(
        model=first_model,
        workspace=workspace,
        tmp_path=tmp_path,
        artifact_store=store,
    )
    first = run_agent_step(_agent_step("s"), 1, first_deps, {})
    assert first.status == "reported"
    assert first.result_name == "done"
    inherited = InheritedConversation(
        source_step="s",
        source_seq=1,
        source_result="done",
        opening_tail=tuple(first.opening_tail),
        units=tuple(first.history),
    )
    second_model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "read_result_slice",
                        {"artifact_id": "r1", "start": 0, "end": 10},
                        "s1",
                    )
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    second_deps = make_agent_step_deps(
        model=second_model,
        workspace=workspace,
        tmp_path=tmp_path,
        artifact_store=store,
    )
    second = run_agent_step(_agent_step("s"), 2, second_deps, {}, inherited)
    assert second.status == "reported"
    texts = [
        str(message.content)
        for message in second_model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    # The opening carries the inherited capped result (and the source's
    # report acknowledgement); the slice resolves after them through the
    # receiver's reader on the shared store.
    assert any("capped:" in text for text in texts)
    assert texts[-1] == big[0:10]


def test_subagent_unknown_args_on_read_file_refused(tmp_path: Path) -> None:
    """The Sub-agent loop refuses unknown arguments without running the tool."""
    from coding_agent.runtime.graph.subagent import run_subagent

    workspace = tmp_path / "work"
    workspace.mkdir()
    marker = "SUBAGENT-SECRET-MARKER-9f2c"
    (workspace / "big.txt").write_text(marker + "z" * 5000, encoding="utf-8")
    model = ScriptedModel(
        [
            _ai(
                [
                    {
                        "name": "read_file",
                        "args": {
                            "path": "big.txt",
                            "start_line": 1,
                            "end_line": 30,
                        },
                        "id": "r1",
                        "type": "tool_call",
                    }
                ]
            ),
            _ai(
                [
                    {
                        "name": REPORT_SUBAGENT_TOOL,
                        "args": {"report": "no findings"},
                        "id": "rep-1",
                        "type": "tool_call",
                    }
                ]
            ),
        ]
    )
    result = run_subagent(
        model,
        description="review the change",
        instructions="review the change",
        call_id="call-1",
        workspace=workspace,
    )
    assert result.status == "reported"
    tool_texts = [
        str(message.content)
        for message in result.messages
        if isinstance(message, ToolMessage)
    ]
    assert len(tool_texts) == 2
    assert tool_texts[0].startswith("Error:")
    assert "start_line" in tool_texts[0]
    assert "end_line" in tool_texts[0]
    assert "read_lines" in tool_texts[0]
    assert marker not in tool_texts[0]


def test_subagent_report_exit_keeps_its_own_validation(
    tmp_path: Path,
) -> None:
    """The Sub-agent report exit is not refused for unknown arguments."""
    from coding_agent.runtime.graph.subagent import run_subagent

    workspace = tmp_path / "work"
    workspace.mkdir()
    model = ScriptedModel(
        [
            _ai(
                [
                    {
                        "name": REPORT_SUBAGENT_TOOL,
                        "args": {"report": "findings here", "bogus": 1},
                        "id": "rep-1",
                        "type": "tool_call",
                    }
                ]
            ),
        ]
    )
    result = run_subagent(
        model,
        description="review the change",
        instructions="review the change",
        call_id="call-1",
        workspace=workspace,
    )
    assert result.status == "reported"
    assert result.report == "findings here"


