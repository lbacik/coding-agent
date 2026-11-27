"""The shared tool-call turn dispatches ordinary calls for both loops (§9).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (unknown
arguments and unknown names never run the tool; success text is redacted;
exceptions are redacted ``Error:`` results; every non-exempt result is
capped after redaction into the caller's store; every handed call bills).
One suite at the turn's interface covers the Step Execution loop and the
Sub-agent loop together: a rule fixed here lands once for both.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.runtime.graph.tool_call_turn import (
    ToolCallTurnResult,
    execute_tool_call,
    run_tool_call_turn,
    tool_call_id_for,
)
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.runtime.result_capping import InMemoryArtifactStore


def _read_tool(seen: list[dict[str, Any]]) -> BaseTool:
    """A ``read_file``-shaped tool recording the args it was invoked with."""

    def _read_file(path: str) -> str:
        seen.append({"path": path})
        return f"contents of {path}"

    _read_file.__name__ = "read_file"
    return StructuredTool.from_function(
        func=_read_file,
        name="read_file",
        description="Read a file.",
    )


def _greet_tool() -> BaseTool:
    """A tool whose parameters all have defaults, for non-Mapping args."""

    def _greet(name: str = "world") -> str:
        return f"hello {name}"

    _greet.__name__ = "greet"
    return StructuredTool.from_function(
        func=_greet,
        name="greet",
        description="Greet.",
    )


def _secret_tool(secret: str) -> BaseTool:
    """A tool echoing a registered secret in its success text."""

    def _leak(path: str) -> str:
        _ = path
        return f"fetched with token {secret}"

    _leak.__name__ = "leak"
    return StructuredTool.from_function(
        func=_leak,
        name="leak",
        description="Leaks.",
    )


def _failing_tool(secret: str) -> BaseTool:
    """A tool raising an exception carrying a registered secret."""

    def _boom(path: str) -> str:
        _ = path
        raise RuntimeError(f"push failed: https://x:{secret}@github.com/o/r")

    _boom.__name__ = "boom"
    return StructuredTool.from_function(
        func=_boom,
        name="boom",
        description="Booms.",
    )


def _big_tool(payload: str) -> BaseTool:
    """A tool returning an over-limit success payload."""

    def _big(path: str = "x") -> str:
        _ = path
        return payload

    _big.__name__ = "big"
    return StructuredTool.from_function(
        func=_big,
        name="big",
        description="Big.",
    )


def _big_failing_tool(payload: str) -> BaseTool:
    """A tool raising an over-limit exception message."""

    def _big_boom(path: str = "x") -> str:
        _ = path
        raise RuntimeError(payload)

    _big_boom.__name__ = "big_boom"
    return StructuredTool.from_function(
        func=_big_boom,
        name="big_boom",
        description="Big boom.",
    )


def _call(
    name: str,
    args: Any,
    call_id: str | None,
) -> dict[str, Any]:
    call: dict[str, Any] = {"name": name, "args": args, "type": "tool_call"}
    if call_id is not None:
        call["id"] = call_id
    return call


def test_unknown_arguments_refused_naming_unknown_and_valid() -> None:
    """Unknown arguments never run the tool; the refusal names both sides."""
    seen: list[dict[str, Any]] = []
    tools: Mapping[str, BaseTool] = {"read_file": _read_tool(seen)}
    result = run_tool_call_turn(
        [_call("read_file", {"path": "a.txt", "start_line": 1}, "r1")],
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    assert result.billable_tool_calls == 1
    assert len(result.messages) == 1
    text = str(result.messages[0].content)
    assert text.startswith("Error:")
    assert "start_line" in text
    assert "path" in text
    assert seen == []


def test_no_such_tool_is_an_error_message() -> None:
    """A call to an unknown tool name is an ordinary ``Error:`` message."""
    result = run_tool_call_turn(
        [_call("no_such_tool", {}, "u1")],
        tools_by_name={},
        registry=SecretRegistry({}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    assert result.billable_tool_calls == 1
    assert len(result.messages) == 1
    assert str(result.messages[0].content) == "Error: no such tool 'no_such_tool'"
    assert result.messages[0].tool_call_id == "u1"


def test_non_mapping_args_treated_as_empty() -> None:
    """A non-Mapping ``args`` payload invokes the tool with ``{}``."""
    tools: Mapping[str, BaseTool] = {"greet": _greet_tool()}
    result = run_tool_call_turn(
        [_call("greet", ["not", "a", "mapping"], "g1")],
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    assert str(result.messages[0].content) == "hello world"


def test_success_result_secret_is_redacted() -> None:
    """Success text carrying a registered secret reaches the model redacted."""
    secret = "ghp-test-secret-value-abc123XYZ"
    tools: Mapping[str, BaseTool] = {"leak": _secret_tool(secret)}
    result = run_tool_call_turn(
        [_call("leak", {"path": "x"}, "l1")],
        tools_by_name=tools,
        registry=SecretRegistry({"GITHUB_TOKEN": secret}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    text = str(result.messages[0].content)
    assert secret not in text
    assert "«redacted:GITHUB_TOKEN»" in text


def test_exception_result_is_redacted_error() -> None:
    """An exception becomes ``Error:`` with redacted class/status, no secret."""
    secret = "ghp-test-secret-value-abc123XYZ"
    tools: Mapping[str, BaseTool] = {"boom": _failing_tool(secret)}
    result = run_tool_call_turn(
        [_call("boom", {"path": "x"}, "b1")],
        tools_by_name=tools,
        registry=SecretRegistry({"GITHUB_TOKEN": secret}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    text = str(result.messages[0].content)
    assert text.startswith("Error:")
    assert "RuntimeError" in text
    assert secret not in text
    assert "«redacted:GITHUB_TOKEN»" in text


def test_over_limit_success_is_capped_with_pointer_and_store() -> None:
    """An oversized success keeps head/tail/pointer; the store keeps all."""
    payload = "0123456789" * 60
    assert len(payload) > 100
    store = InMemoryArtifactStore()
    tools: Mapping[str, BaseTool] = {"big": _big_tool(payload)}
    result = run_tool_call_turn(
        [_call("big", {}, "r1")],
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=100,
        store=store,
    )
    text = str(result.messages[0].content)
    assert len(text) < len(payload)
    assert "capped:" in text
    assert "read_result_slice" in text
    assert "r1" in text
    assert store.read("r1") == payload


def test_over_limit_exception_is_capped_with_pointer_and_store() -> None:
    """An oversized exception string is capped like a success result."""
    payload = "E" * 500
    store = InMemoryArtifactStore()
    tools: Mapping[str, BaseTool] = {"big_boom": _big_failing_tool(payload)}
    result = run_tool_call_turn(
        [_call("big_boom", {}, "e1")],
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=100,
        store=store,
    )
    text = str(result.messages[0].content)
    assert text.startswith("Error:")
    assert "capped:" in text
    assert "read_result_slice" in text
    full = store.read("e1")
    assert full.startswith("Error:")
    assert payload in full
    assert len(text) < len(full)


def test_exempt_tool_names_bypass_capping() -> None:
    """An exempt tool's oversized result arrives whole with no store entry."""
    payload = "y" * 500
    store = InMemoryArtifactStore()
    tools: Mapping[str, BaseTool] = {"big": _big_tool(payload)}
    result = run_tool_call_turn(
        [_call("big", {}, "r1")],
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=100,
        store=store,
        exempt_tool_names=frozenset({"big"}),
    )
    assert str(result.messages[0].content) == payload
    with pytest.raises(Exception):
        store.read("r1")


def test_every_handed_call_counts_as_billable_in_order() -> None:
    """The turn counts every handed call and preserves the handed order."""
    seen: list[dict[str, Any]] = []
    tools: Mapping[str, BaseTool] = {"read_file": _read_tool(seen)}
    calls = [
        _call("read_file", {"path": "a.txt"}, "a1"),
        _call("no_such_tool", {}, "u1"),
        _call("read_file", {"path": "b.txt", "bogus": 1}, "a2"),
    ]
    result: ToolCallTurnResult = run_tool_call_turn(
        calls,
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    assert result.billable_tool_calls == 3
    assert [message.tool_call_id for message in result.messages] == [
        "a1",
        "u1",
        "a2",
    ]
    assert "contents of a.txt" in str(result.messages[0].content)
    assert str(result.messages[1].content).startswith("Error: no such tool")
    assert str(result.messages[2].content).startswith("Error: unknown argument")
    # Only the two valid calls ran; the refusal and the unknown name did not.
    assert seen == [{"path": "a.txt"}]


def test_missing_call_ids_get_stable_unique_fallback() -> None:
    """Calls without an id get order-stable ``call-<index>`` ids."""
    tools: Mapping[str, BaseTool] = {"greet": _greet_tool()}
    calls = [
        _call("greet", {}, None),
        _call("greet", {}, None),
        _call("greet", {}, "kept"),
    ]
    result = run_tool_call_turn(
        calls,
        tools_by_name=tools,
        registry=SecretRegistry({}),
        limit=4_000,
        store=InMemoryArtifactStore(),
    )
    assert [message.tool_call_id for message in result.messages] == [
        "call-0",
        "call-1",
        "kept",
    ]
    assert tool_call_id_for({"name": "greet"}, 7) == "call-7"
    assert tool_call_id_for({"name": "greet", "id": "x"}, 7) == "x"


def test_single_call_matches_batch_for_refusal() -> None:
    """The single-call entry refuses unknown arguments exactly like a batch."""
    seen: list[dict[str, Any]] = []
    tools: Mapping[str, BaseTool] = {"read_file": _read_tool(seen)}
    store = InMemoryArtifactStore()
    registry = SecretRegistry({})
    message = execute_tool_call(
        {"name": "read_file", "args": {"path": "a.txt", "bogus": 2}, "id": "r1"},
        index=0,
        tools_by_name=tools,
        registry=registry,
        limit=4_000,
        store=store,
    )
    assert str(message.content).startswith("Error:")
    assert "bogus" in str(message.content)
    assert seen == []
