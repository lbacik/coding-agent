from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


@dataclass(frozen=True)
class ExchangeUnit:
    """One assistant turn plus every tool result answering it (contract
    §5, ADR 0011 part 3) — the grain compaction evicts at. Splitting a
    unit is a malformed request on Anthropic and breaks the chain the
    Responses API expects, so nothing in this module ever hands out an
    `assistant` without its `results` or the reverse."""

    assistant: AIMessage
    results: tuple[ToolMessage, ...]

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        return (self.assistant, *self.results)


def flatten(units: Sequence[ExchangeUnit]) -> tuple[BaseMessage, ...]:
    """Every unit's messages, in order — what a caller lays after the
    Pinned Prefix to build one request."""
    messages: list[BaseMessage] = []
    for unit in units:
        messages.extend(unit.messages)
    return tuple(messages)


def evict_oldest(
    units: Sequence[ExchangeUnit],
    *,
    estimate: Callable[[ExchangeUnit], int],
    budget_tokens: int,
) -> tuple[ExchangeUnit, ...]:
    """`L3-IMP-4`, `L3-IMP-12`: drop whole units from the oldest end until
    what remains estimates at or under `budget_tokens`, or only one unit
    is left — the most recent turn is never evicted, since it is what the
    next request is answering.

    Takes only `units` and a plain token budget. The Pinned Prefix is not
    in this function's signature at all — not a parameter this function
    happens to ignore, but one no caller can pass — so it cannot be
    evicted by construction (ADR 0011 part 1), the same structural
    assertion `pinned_prefix.assert_no_skill_path_resolver` makes for
    `L3-IMP-14`.
    """
    remaining = list(units)
    while len(remaining) > 1 and sum(estimate(u) for u in remaining) > budget_tokens:
        remaining.pop(0)
    return tuple(remaining)
