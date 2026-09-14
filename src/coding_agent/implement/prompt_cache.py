from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage

_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}


def _as_content_blocks(content: object) -> list[dict[str, object]] | None:
    """`content` normalized to Anthropic's block-list wire shape, or
    `None` where there is nothing eligible to attach a breakpoint to --
    empty content, or a shape (e.g. a list mixing non-dict elements) this
    module does not recognize."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else None
    if isinstance(content, list) and content and all(isinstance(block, dict) for block in content):
        return [dict(block) for block in content]
    return None


def _tag_trailing_block(message: BaseMessage) -> BaseMessage:
    """A copy of `message` with an ephemeral `cache_control` breakpoint on
    its last content block -- never a mutation of `message` itself, so
    whatever called this keeps its own copy (`history`, the audit-trail
    `conversation`) exactly as the provider or a tool produced it (ADR
    0010). Falls back to `message` unchanged where there is no eligible
    block to tag."""
    blocks = _as_content_blocks(message.content)
    if blocks is None:
        return message
    blocks[-1] = {**blocks[-1], "cache_control": _CACHE_CONTROL}
    return message.model_copy(update={"content": blocks})


def apply_cache_breakpoints(
    messages: Sequence[BaseMessage], *, prefix_length: int
) -> list[BaseMessage]:
    """Two Anthropic cache breakpoints over the messages a request is
    about to send: one at the end of the Pinned Prefix
    (`messages[:prefix_length]`, unchanging for the whole Attempt) and one
    at the end of `messages` itself (the growing tail, one `ExchangeUnit`
    longer each turn). Anthropic matches a request's prefix against
    whatever a prior request already cached up to a breakpoint, so
    marking the same two positions turn after turn turns a linearly
    growing conversation into a linearly-priced one instead of re-billing
    the whole history from scratch every turn (`ceilings.UsageBreakdown`
    already prices a cache read and a cache write differently from a
    plain input token -- this is what makes either ever happen on the
    wire).

    Returns a new list; never mutates `messages` or any element of it, so
    a caller's own `history`/`conversation` -- kept for compaction and the
    audit trail -- stay exactly as produced. Anthropic-specific: a caller
    on another provider's pin never calls this.
    """
    if not messages:
        return []
    result = list(messages)
    breakpoints = {len(result) - 1}
    if 0 < prefix_length <= len(result):
        breakpoints.add(prefix_length - 1)
    for index in breakpoints:
        result[index] = _tag_trailing_block(result[index])
    return result
