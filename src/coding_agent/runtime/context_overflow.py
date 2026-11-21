"""Provider Context Overflow: two signals, one classification.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (a provider
Context Overflow is a classified, expected path: the main conversation ends
``error``, a Sub-agent ends ``no-report`` / ``context-window`` with the cause
recorded, and the handoff turn is a failed handoff).

Two signals, one classification:

- ``refused``: the provider refused the request — a
  :class:`ContextOverflowError` through LangChain on every route (OpenAI's
  separate input ceiling included);
- ``cut-short``: a successful response cut short because the request and its
  response did not fit — Anthropic ``model_context_window_exceeded``.

``cut-short`` is not an output-cap truncation and skips bounded correction
(§9.5). An output-cap truncation (``max_tokens`` / ``length``) keeps the
bounded-correction path and is recorded as ``output-cap``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import BaseMessage

ContextOverflowSignal = Literal["refused", "cut-short"]
"""How a Context Overflow surfaced: a refused request or a cut-short response."""

CutShortKind = Literal["output-cap", "context-window"]
"""How a response was cut short, as recorded on ``model_response`` (§13.2)."""

CONTEXT_OVERFLOW_KIND = "context_overflow"
"""Run Ledger event kind: a refused request's Context Overflow fact (§13.2).

A refused request has no response, so it has no ``model_response`` event.
This event records the overflow fact with the hold it charged: the signal,
the provider error class and status, the compared Conversation Context, the
Usable Threshold in force, the declared Model Context Window and the charged
hold id.
"""

REFUSED_SIGNAL: ContextOverflowSignal = "refused"
CUT_SHORT_SIGNAL: ContextOverflowSignal = "cut-short"

OUTPUT_CAP: CutShortKind = "output-cap"
CONTEXT_WINDOW_CUT: CutShortKind = "context-window"


def _metadata_of(response: BaseMessage) -> Mapping[str, object]:
    metadata: object = getattr(response, "response_metadata", None)
    if isinstance(metadata, Mapping):
        return metadata
    return {}


def _strings_of(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, Mapping):
        reason = value.get("reason")
        if isinstance(reason, str):
            found.append(reason)
    return found


def cut_short_of(response: BaseMessage) -> CutShortKind | None:
    """How ``response`` was cut short, or ``None`` where it was not truncated.

    A ``context-window`` cut-short is an Anthropic
    ``model_context_window_exceeded`` signal: the raw ``finish_reason`` (or
    ``stop_reason``), the normalised ``context_exceeded`` finish, or an
    incomplete-details reason naming a context window. An ``output-cap``
    truncation is a ``max_tokens`` / ``length`` / ``max_output_tokens``
    signal. Anything else — including an ordinary stop or tool call — is
    not a cut-short.
    """
    metadata = _metadata_of(response)
    candidates: list[str] = []
    for key in ("finish_reason", "normalized_finish_reason", "stop_reason"):
        value = metadata.get(key)
        if isinstance(value, str):
            candidates.append(value)
    incomplete = metadata.get("incomplete_details")
    candidates.extend(_strings_of(incomplete))
    lowered = [candidate.lower() for candidate in candidates]
    for candidate in lowered:
        if (
            candidate == "model_context_window_exceeded"
            or candidate == "context_exceeded"
            or candidate == "context_window_exceeded"
            or candidate == "context_length_exceeded"
            or "model_context_window_exceeded" in candidate
        ):
            return CONTEXT_WINDOW_CUT
    for candidate in lowered:
        if candidate in ("max_tokens", "length", "max_output_tokens"):
            return OUTPUT_CAP
    incomplete_reason: object = None
    if isinstance(incomplete, Mapping):
        incomplete_reason = incomplete.get("reason")
    if isinstance(incomplete_reason, str):
        text = incomplete_reason.lower()
        if "context" in text and (
            "exceed" in text or "overflow" in text or "window" in text
        ):
            return CONTEXT_WINDOW_CUT
    return None


def is_context_window_cut_short(response: BaseMessage) -> bool:
    """Whether ``response`` was cut short by the context window (§9.5)."""
    return cut_short_of(response) == CONTEXT_WINDOW_CUT


def is_output_cap_cut_short(response: BaseMessage) -> bool:
    """Whether ``response`` was truncated at the output cap (§9.5)."""
    return cut_short_of(response) == OUTPUT_CAP


class ResponseCutShortByContext(ContextOverflowError):
    """A successful response cut short by the context window (signal ``cut-short``).

    Raised after the shared model-request lifecycle settles the response's
    hold with its provider-reported usage and records its ``model_response``
    event with ``cut_short: "context-window"`` — so the hold is never charged
    twice and the spend stays measured, never a worst case. Carries the
    compared Conversation Context, the Usable Threshold and the declared
    Model Context Window for the caller's detail and Execution Summary.
    """

    def __init__(
        self,
        message: str,
        *,
        response: BaseMessage,
        conversation_value: int | None = None,
        conversation_basis: str | None = None,
        usable_threshold: int | None = None,
        model_context_window: int | None = None,
    ) -> None:
        super().__init__(message)
        self.response = response
        self.conversation_value = conversation_value
        self.conversation_basis = conversation_basis
        self.usable_threshold = usable_threshold
        self.model_context_window = model_context_window
        self.overflow_signal: ContextOverflowSignal = CUT_SHORT_SIGNAL
        # Aliases the shared-lifecycle overflow attributes read by the
        # provider-failure detail and summary, so a cut-short response
        # carries the same compared context as a refused request.
        self._overflow_usable = usable_threshold
        self._overflow_window = model_context_window


def overflow_signal_of(exc: BaseException) -> ContextOverflowSignal:
    """The Context Overflow signal an exception carries (§9.6, §9.8).

    A cut-short response raised as :class:`ResponseCutShortByContext`
    carries ``cut-short``; any other :class:`ContextOverflowError` is a
    refused request.
    """
    signal: object = getattr(exc, "overflow_signal", None)
    if signal == CUT_SHORT_SIGNAL:
        return CUT_SHORT_SIGNAL
    return REFUSED_SIGNAL
