from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from coding_agent.github.issues import TargetIssue
from coding_agent.implement.ceilings import LoopCeilings, UsageLedger, UsageTotals, ceiling_crossed
from coding_agent.implement.exchange import ExchangeUnit, evict_oldest, flatten
from coding_agent.implement.pinned_prefix import PinnedPrefix, estimate_tokens
from coding_agent.implement.result_capping import ArtifactStore, cap_tool_result
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.provider.retry import invoke_with_retry


def build_opening_messages(prefix: PinnedPrefix, issue: TargetIssue) -> list[BaseMessage]:
    """The Pinned Prefix's own elements opened as separate messages — the
    Attempt Header, each injected skill file, then the Target Issue — never
    `PinnedPrefix.rendered`'s concatenation, which exists for human
    inspection only. This whole list is the Pinned Prefix contract §5
    describes: `run_tool_loop` never hands any of it to the compactor
    (ADR 0011)."""
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
    """Every opening message plus every assistant turn and tool result
    ever produced, in order — the full audit trail, never itself pruned
    by compaction (only what a later request sends to the model is).
    Each assistant turn is exactly the object the model returned (ADR
    0010) — never rebuilt. A capped tool result's content here is the
    capped text actually sent, not the full content stashed in the
    `ArtifactStore`."""
    tool_call_count: int
    usage: UsageTotals
    stopped_by: str | None
    """The ceiling name `ceilings.ceiling_crossed` returned when this loop
    stopped early (`L3-IMP-6`), or `None` where it stopped because a turn
    called no tool — a normal, unbounded completion."""


def _estimate_unit_tokens(unit: ExchangeUnit) -> int:
    return estimate_tokens("".join(str(m.content) for m in unit.messages))


def _estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    return estimate_tokens("".join(str(m.content) for m in messages))


def run_tool_loop(
    model: InvokableToolModel,
    tools: Sequence[BaseTool],
    opening_messages: Sequence[BaseMessage],
    *,
    price: TokenPrices,
    compaction_threshold: int,
    result_cap_limit: int,
    result_store: ArtifactStore,
    ceilings: LoopCeilings,
    usage_ledger: UsageLedger,
    clock: Callable[[], float] = time.monotonic,
) -> ToolLoopResult:
    """The bounded tool loop (the runtime contract's `implement` node): bind
    the toolset, invoke, and where the assistant turn calls tools, run each
    and append its `ToolMessage`, then invoke again — until a turn calls
    none, or a ceiling stops it first.

    `opening_messages` is the whole Pinned Prefix (the Attempt Header, the
    injected skill files, and the Target Issue — contract §5); it is sent
    on every request but never touched by compaction. Every assistant turn
    plus the tool results answering it is instead folded into an
    `ExchangeUnit` and kept in a separate, evictable history — the eviction
    function that trims it (`exchange.evict_oldest`) is handed only that
    history and a token budget, never `opening_messages`, so the Pinned
    Prefix cannot be dropped by construction (ADR 0011).

    An oversized tool result is capped to head, tail and a pointer before
    it is ever appended (`L3-IMP-5`). Usage is flushed to `usage_ledger`
    after every model response and every tool call; the instant a
    configured ceiling is crossed, the loop stops without making another
    model call or running further tool calls for the turn just answered
    (`L3-IMP-6`) — whatever usage was already flushed stays flushed.

    The assistant turn is appended exactly as the model returned it,
    never rebuilt from `.content`/`.tool_calls` (ADR 0010), so a
    provider-specific block this loop does not interpret survives to the
    next request instead of being silently dropped.
    """
    bound = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}
    conversation: list[BaseMessage] = list(opening_messages)
    history: list[ExchangeUnit] = []
    tool_call_count = 0
    stopped_by: str | None = None
    start = clock()
    prefix_tokens = _estimate_messages_tokens(opening_messages)

    while True:
        sent: list[BaseMessage] = [*opening_messages, *flatten(history)]
        response = invoke_with_retry(bound, sent)
        conversation.append(response)

        usage_metadata = response.usage_metadata if isinstance(response, AIMessage) else None
        totals = usage_ledger.flush_model_response(usage_metadata, price)
        stopped_by = ceiling_crossed(totals, clock() - start, ceilings)
        if stopped_by is not None:
            break

        tool_calls = response.tool_calls if isinstance(response, AIMessage) else []
        if not tool_calls:
            break
        # `tool_calls` is only ever non-empty on the `isinstance` branch
        # above, so `response` is an `AIMessage` here -- narrowed
        # explicitly for `ExchangeUnit`, which an `ExchangeUnit` never
        # holds anything else (ADR 0010: the assistant turn is always the
        # provider's own `AIMessage`).
        assert isinstance(response, AIMessage)

        results: list[ToolMessage] = []
        for call in tool_calls:
            # Tool-call count, unlike wall clock/cost/tokens, is knowable
            # before spending it: checked here so the call that would
            # cross it never runs, rather than running and only then
            # discovering the overshoot.
            if ceilings.max_tool_calls is not None and usage_ledger.totals.tool_calls >= ceilings.max_tool_calls:
                stopped_by = "tool_calls"
                break
            tool_call_count += 1
            # A provider is expected to always send one; a fallback keeps
            # the artifact store and `ToolMessage` keyed on a real string
            # rather than propagating `None` into either.
            call_id = call["id"] or f"unidentified-{tool_call_count}"
            tool = tools_by_name.get(call["name"])
            if tool is None:
                content = f"Error: no such tool {call['name']!r}"
            else:
                try:
                    content = str(tool.invoke(call["args"]))
                except Exception as exc:  # a tool's own failure never ends the loop
                    content = f"Error: {exc}"
            content = cap_tool_result(
                content, tool_call_id=call_id, limit=result_cap_limit, store=result_store
            )
            message = ToolMessage(content=content, tool_call_id=call_id)
            conversation.append(message)
            results.append(message)

            totals = usage_ledger.flush_tool_call()
            stopped_by = ceiling_crossed(totals, clock() - start, ceilings)
            if stopped_by is not None:
                break

        history.append(ExchangeUnit(assistant=response, results=tuple(results)))
        if stopped_by is not None:
            break

        total_estimate = prefix_tokens + sum(_estimate_unit_tokens(u) for u in history)
        if total_estimate > compaction_threshold:
            budget = max(0, compaction_threshold - prefix_tokens)
            history = list(evict_oldest(history, estimate=_estimate_unit_tokens, budget_tokens=budget))

    return ToolLoopResult(
        conversation=tuple(conversation),
        tool_call_count=tool_call_count,
        usage=usage_ledger.totals,
        stopped_by=stopped_by,
    )
