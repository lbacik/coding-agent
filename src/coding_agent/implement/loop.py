from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from coding_agent.github.issues import TargetIssue
from coding_agent.implement.pinned_prefix import PinnedPrefix
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.retry import invoke_with_retry


def build_opening_messages(prefix: PinnedPrefix, issue: TargetIssue) -> list[BaseMessage]:
    """The Pinned Prefix's own elements opened as separate messages — the
    Attempt Header, each injected skill file, then the Target Issue — never
    `PinnedPrefix.rendered`'s concatenation, which exists for human
    inspection only. Which of these a compactor may never reach is a later
    ticket's concern (`L3-IMP-4`); this only lays them out in order."""
    messages: list[BaseMessage] = [SystemMessage(content=prefix.attempt_header)]
    messages.extend(
        SystemMessage(content=f"----- {f.label} -----\n{f.text}") for f in prefix.injected_files
    )
    messages.append(
        HumanMessage(content=f"# Target Issue\n\n#{issue.number}: {issue.title}\n\n{issue.body}")
    )
    return messages


@dataclass(frozen=True)
class ToolLoopResult:
    conversation: tuple[BaseMessage, ...]
    """Every opening message plus every assistant turn and tool result, in
    order. Each assistant turn is exactly the object the model returned
    (ADR 0010) — never rebuilt."""
    tool_call_count: int


def run_tool_loop(
    model: InvokableToolModel,
    tools: Sequence[BaseTool],
    opening_messages: Sequence[BaseMessage],
) -> ToolLoopResult:
    """The bounded tool loop (the runtime contract's `implement` node): bind
    the toolset, invoke, and where the assistant turn calls tools, run each
    and append its `ToolMessage`, then invoke again — until a turn calls
    none. The assistant turn is appended exactly as the model returned it,
    never rebuilt from `.content`/`.tool_calls` (ADR 0010), so a
    provider-specific block this loop does not interpret survives to the
    next request instead of being silently dropped.

    No per-tool-result capping, no compaction, no ceiling enforcement — a
    later ticket's job.
    """
    bound = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}
    conversation: list[BaseMessage] = list(opening_messages)
    tool_call_count = 0

    while True:
        response = invoke_with_retry(bound, conversation)
        conversation.append(response)

        tool_calls = response.tool_calls if isinstance(response, AIMessage) else []
        if not tool_calls:
            break

        for call in tool_calls:
            tool_call_count += 1
            tool = tools_by_name.get(call["name"])
            if tool is None:
                content = f"Error: no such tool {call['name']!r}"
            else:
                try:
                    content = str(tool.invoke(call["args"]))
                except Exception as exc:  # a tool's own failure never ends the loop
                    content = f"Error: {exc}"
            conversation.append(ToolMessage(content=content, tool_call_id=call["id"]))

    return ToolLoopResult(conversation=tuple(conversation), tool_call_count=tool_call_count)
