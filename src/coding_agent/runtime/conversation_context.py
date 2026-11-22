"""The Conversation Context measure and the Pinned Model's context policy.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.9 (the
context threshold) and §11.4 (Admission): the deterministic context policy of
the Pinned Model, and the one Conversation Context measure every context
decision and Admission hold compares — anchored on the provider-reported
``input_tokens`` of the conversation's last measured request (ADR 0031).
Before a main-conversation request would exceed the Usable Threshold, the
Step Execution ends ``limit-exceeded``; a Sub-agent ends without a report.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from langchain_core.messages import BaseMessage, UsageMetadata
from langchain_core.tools import BaseTool

from coding_agent.runtime.exchange import ExchangeUnit, flatten

def estimate_tokens(text: str) -> int:
    """Use the same deterministic provider-neutral estimate as the prefix."""
    return max(1, len(text.encode("utf-8")) // 4)



@dataclass(frozen=True)
class ContextWindowConfig:
    """The deterministic context policy for one Pinned Model.

    The threshold is an estimate, not a provider usage claim. Overhead covers
    tool schemas and request framing; the safety margin leaves room for small
    provider-side differences. Once the next main-conversation request would
    exceed the usable threshold, the Step Execution ends ``limit-exceeded``.
    """

    threshold_tokens: int
    safety_margin_tokens: int = 1_000
    request_overhead_tokens: int = 1_000

    def __post_init__(self) -> None:
        if self.threshold_tokens <= 0:
            raise ValueError("context-window threshold must be positive")
        if self.safety_margin_tokens < 0:
            raise ValueError("context-window safety margin cannot be negative")
        if self.request_overhead_tokens < 0:
            raise ValueError("context-window request overhead cannot be negative")
        if self.safety_margin_tokens + self.request_overhead_tokens >= self.threshold_tokens:
            raise ValueError("context-window overhead and safety margin must leave usable capacity")

    @property
    def usable_threshold(self) -> int:
        return self.threshold_tokens - self.safety_margin_tokens - self.request_overhead_tokens



def _estimate_messages_text(messages: Sequence[BaseMessage]) -> int:
    """The deterministic estimate of messages alone, without tool schemas."""
    message_text = "".join(
        json.dumps(
            {
                "content": message.content,
                "tool_calls": getattr(message, "tool_calls", ()),
                "additional_kwargs": getattr(message, "additional_kwargs", {}),
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        for message in messages
    )
    return estimate_tokens(message_text)


def estimate_context_tokens(
    opening_messages: Sequence[BaseMessage],
    history: Sequence[ExchangeUnit],
    tools: Sequence[BaseTool],
) -> int:
    """Estimate the request including prefix, history and tools."""
    tool_schemas = json.dumps(
        [tool.args for tool in tools], sort_keys=True, default=str, separators=(",", ":")
    )
    messages = [*opening_messages, *flatten(history)]
    message_text = "".join(
        json.dumps(
            {
                "content": message.content,
                "tool_calls": getattr(message, "tool_calls", ()),
                "additional_kwargs": getattr(message, "additional_kwargs", {}),
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        for message in messages
    )
    return estimate_tokens(message_text + tool_schemas)


ConversationBasis = Literal["anchored", "estimated"]
"""How a Conversation Context value was produced: provider-anchored or estimated."""


@dataclass(frozen=True)
class ConversationContext:
    """The Conversation Context of one conversation's next request.

    The measure every context decision and Admission hold compares (contract
    §§9.8–9.9, 11.4): the ``usage_metadata.input_tokens`` of the same
    conversation's most recent request that reported usage (basis
    ``anchored``), plus ``bytes/4`` of every message appended since —
    including the assistant turn that request produced and its tool results —
    or, with no measured response yet in the conversation, today's
    ``bytes/4`` of the whole request (basis ``estimated``). ``output_tokens``
    is never added. A conversation is the main conversation of one Step
    Execution, or one Sub-agent; a Sub-agent is anchored on its own responses
    only.
    """

    value: int
    """The compared token count: anchor plus appended, or the whole estimate."""

    basis: ConversationBasis
    """``anchored`` where a provider-reported input count anchors the value."""

    anchor_input_tokens: int | None = None
    """The anchoring request's reported ``input_tokens`` (anchored only)."""

    anchor_request_ref: str | None = None
    """A reference to the anchoring request (anchored only)."""

    appended_estimate_tokens: int = 0
    """``bytes/4`` of the messages appended since the anchor (anchored only)."""

    def to_record(self) -> dict[str, object]:
        """The Run Ledger record of the compared value (contract §13.2)."""
        return {
            "value": self.value,
            "basis": self.basis,
            "anchor_input_tokens": self.anchor_input_tokens,
            "anchor_request_ref": self.anchor_request_ref,
            "appended_estimate_tokens": self.appended_estimate_tokens,
        }


@dataclass
class ConversationContextState:
    """One conversation's anchoring state: the last measured request, if any.

    The main conversation of a Step Execution holds one (a new Step
    Execution starts a fresh state, so its first request is ``estimated``);
    each Sub-agent run holds its own, so a Sub-agent is
    anchored on its own responses only. A response without complete usage
    metadata never moves the anchor.

    ``peak`` is the largest Conversation Context compared for this
    conversation so far (``None`` where nothing was compared yet): every
    compared request notes its value through :meth:`note_compared`, so a
    Sub-agent's ``subagent_ended`` event can carry the peak over its requests
    even where the run raises instead of returning. The peak never moves the
    anchor and never affects a decision; it is a measurement only.
    """

    anchor_input_tokens: int | None = None
    anchor_request_ref: str | None = None
    anchor_message_count: int = 0
    peak: ConversationContext | None = None
    """The largest compared Conversation Context noted on this state, if any."""

    def measure(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool] = (),
    ) -> ConversationContext:
        """The Conversation Context of the next request over ``messages``."""
        if self.anchor_input_tokens is None:
            value = estimate_context_tokens(list(messages), [], list(tools))
            return ConversationContext(
                value=value,
                basis="estimated",
                appended_estimate_tokens=value,
            )
        appended = _estimate_messages_text(list(messages)[self.anchor_message_count :])
        return ConversationContext(
            value=self.anchor_input_tokens + appended,
            basis="anchored",
            anchor_input_tokens=self.anchor_input_tokens,
            anchor_request_ref=self.anchor_request_ref,
            appended_estimate_tokens=appended,
        )

    def note_compared(self, context: ConversationContext) -> None:
        """Note one compared Conversation Context toward this conversation's peak.

        The peak is the largest compared value (with its basis); ties keep the
        first. Noting never moves the anchor: only :meth:`note_response` does.
        Callers note every request they compare — including one whose send
        then fails — so the peak covers every compared request.
        """
        if self.peak is None or context.value > self.peak.value:
            self.peak = context

    def note_response(
        self, response: BaseMessage, message_count: int, ref: str
    ) -> None:
        """Anchor on one accepted response, where it reports complete usage.

        ``message_count`` is the conversation length in the request that
        produced the response (before the response was appended), so the next
        measure prices exactly the messages appended since. A response without
        complete ``usage_metadata`` leaves the anchor untouched.
        """
        from coding_agent.runtime.ceilings import (
            response_has_complete_usage,
            usage_breakdown,
        )

        if not response_has_complete_usage(response):
            return
        raw = getattr(response, "usage_metadata", None)
        if not isinstance(raw, dict):
            return
        breakdown = usage_breakdown(cast(UsageMetadata, raw))
        self.anchor_input_tokens = breakdown.input_tokens
        self.anchor_request_ref = ref
        self.anchor_message_count = max(0, message_count)


def measure_conversation_context(
    messages: Sequence[BaseMessage],
    tools: Sequence[BaseTool] = (),
    state: ConversationContextState | None = None,
) -> ConversationContext:
    """The Conversation Context of one conversation's next request.

    The one measure every context decision and Admission hold uses: anchored
    on the state's last provider-reported ``input_tokens`` where there is one,
    else today's ``bytes/4`` of the whole request (basis ``estimated``). A
    ``None`` state always measures ``estimated``.
    """
    holder = state if state is not None else ConversationContextState()
    return holder.measure(messages, tools)


@dataclass(frozen=True)
class RecordedContext:
    """One ledger record's compared Conversation Context, as read back.

    Records written before the context travelled in the payload carry no
    value: they read ``unavailable``, never zero.
    """

    value: int | None
    basis: str


def recorded_conversation_context(payload: Mapping[str, object]) -> RecordedContext:
    """Read the compared Conversation Context back from a ledger payload."""
    raw = payload.get("conversation_context")
    if not isinstance(raw, Mapping):
        return RecordedContext(value=None, basis="unavailable")
    value = raw.get("value")
    basis = raw.get("basis")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or basis not in ("anchored", "estimated")
    ):
        return RecordedContext(value=None, basis="unavailable")
    return RecordedContext(value=value, basis=str(basis))
