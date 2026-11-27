"""One assistant turn's ordinary tool calls: validate, invoke, redact, cap.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (every
ordinary tool result is redacted through the caller's registry and capped
after redaction into the caller's artifact store).

The Step Execution loop (``runtime/graph/agent_step.py``) and the Sub-agent
loop (``runtime/graph/subagent.py``) used to dispatch an assistant turn's
tool calls through two independent copies of this sequence. Both loops now
delegate each ordinary call to :func:`execute_tool_call` — the loops keep
their per-call sequencing (exit-tool interleaving, the Sub-agent mid-batch
stop abort) while the validate/invoke/redact/cap rule lives here alone.
:func:`run_tool_call_turn` batches the same entry for whole turns: one
batch of ordinary calls goes in, the ordered :class:`ToolMessage` results
plus the billable-call count come out. Each loop keeps only its own
exit/special tooling (the step loop's report, spawn grouping and advisor
barrier; the Sub-agent loop's report exit) and its own
budget/limit/deadline/context/stop decisions — this turn writes to no
budget and makes no limit, deadline, context or stop decision.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from coding_agent.runtime.redaction import SecretRegistry, format_exception_redacted
from coding_agent.runtime.result_capping import (
    ArtifactStore,
    cap_tool_result,
    tool_param_names,
    unknown_argument_error,
)


@dataclass(frozen=True)
class ToolCallTurnResult:
    """The outcome of one assistant turn's ordinary tool calls (§9).

    ``messages`` carries one :class:`ToolMessage` per handed call, in the
    handed order; ``billable_tool_calls`` counts every handed call — the
    caller pre-excludes its own exit/non-billable tool names before handing
    the batch over, then applies the returned count to its own budget
    mechanism.
    """

    messages: list[ToolMessage] = field(default_factory=list)
    billable_tool_calls: int = 0


def tool_call_id_for(call: Mapping[str, Any], index: int) -> str:
    """The stable id for one requested call: the model's, or ``call-<index>``.

    One scheme for both loops: a call the model sent without an id still
    yields a unique, order-stable id the capping pointer can key on.
    """
    raw_id = call.get("id")
    return str(raw_id) if raw_id else f"call-{index}"


def invoke_tool_redacted(
    tool: BaseTool,
    args: Mapping[str, Any],
    registry: SecretRegistry,
) -> str:
    """Invoke one tool to its redacted result text, exceptions included.

    Success text is redacted through ``registry``; an invocation exception
    becomes an ``Error:``-prefixed redacted class/status string. Shared by
    the ordinary-call entry below and the Sub-agent report exit, which keeps
    its own validation and capping path but the same invocation shape.
    """
    try:
        return registry.redact(str(tool.invoke(dict(args))))
    except Exception as exc:
        return f"Error: {format_exception_redacted(exc, registry)}"


def execute_tool_call(
    call: Mapping[str, Any],
    *,
    index: int,
    tools_by_name: Mapping[str, BaseTool],
    registry: SecretRegistry,
    limit: int,
    store: ArtifactStore,
    exempt_tool_names: Collection[str] = frozenset(),
) -> ToolMessage:
    """Run one ordinary tool call to its redacted, capped :class:`ToolMessage`.

    Unknown tool names and unknown arguments never run the tool: each
    becomes an ordinary ``Error:`` result (an unknown name is short and
    fixed, so — as in both loops before — it is returned uncapped). Success
    text is redacted through ``registry``; an invocation exception becomes
    an ``Error:``-prefixed redacted class/status string. Every non-exempt
    ran-or-refused result is capped after redaction via ``cap_tool_result``
    into ``store`` — including exception and refusal strings, so an
    oversized refusal keeps its head, tail and ``read_result_slice``
    pointer with the full content in the store.
    """
    name = str(call.get("name"))
    tool_call_id = tool_call_id_for(call, index)
    tool = tools_by_name.get(name)
    if tool is None:
        return ToolMessage(
            content=f"Error: no such tool {name!r}",
            tool_call_id=tool_call_id,
        )
    raw_args: Any = call.get("args", {})
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
    declared = tool_param_names(tool)
    unknown: set[str] = set(args) - declared if declared is not None else set()
    if unknown:
        assert declared is not None
        content = unknown_argument_error(name, unknown, declared)
    else:
        content = invoke_tool_redacted(tool, args, registry)
    if name not in exempt_tool_names:
        content = cap_tool_result(
            content, tool_call_id=tool_call_id, limit=limit, store=store
        )
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def run_tool_call_turn(
    calls: Sequence[Mapping[str, Any]],
    *,
    tools_by_name: Mapping[str, BaseTool],
    registry: SecretRegistry,
    limit: int,
    store: ArtifactStore,
    exempt_tool_names: Collection[str] = frozenset(),
) -> ToolCallTurnResult:
    """Run one batch of ordinary tool calls in order (§9).

    Every handed call is billable — the caller pre-excludes its own
    exit/non-billable names — so the returned count is ``len(calls)``.
    """
    messages: list[ToolMessage] = [
        execute_tool_call(
            call,
            index=index,
            tools_by_name=tools_by_name,
            registry=registry,
            limit=limit,
            store=store,
            exempt_tool_names=exempt_tool_names,
        )
        for index, call in enumerate(calls)
    ]
    return ToolCallTurnResult(messages=messages, billable_tool_calls=len(calls))
