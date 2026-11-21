from __future__ import annotations

from dataclasses import dataclass

from coding_agent.provider.pinned_model import (
    HANDOFF_TOKEN_CAP,
    REQUEST_OVERHEAD_TOKENS,
    SAFETY_MARGIN_TOKENS,
    PinnedModel,
)
from coding_agent.runtime.context_handoff import ContextWindowConfig

PINNED_MODELS: dict[str, PinnedModel] = {
    "anthropic": PinnedModel(
        provider="anthropic", model="claude-sonnet-5", endpoint="messages", effort="medium"
    ),
    "openai": PinnedModel(
        provider="openai", model="gpt-5.6-luna", endpoint="responses", effort="medium"
    ),
}
"""The code-owned (provider, model) pair table (§2.1).

These entries admit the pair and endpoint only and carry no token budget:
every pin the Worker runs declares its own ``context-window-tokens``,
``context-threshold-tokens`` and ``output-cap-tokens`` in the Workflow
Configuration. There is no code-owned window, output-cap or price table.
"""


@dataclass(frozen=True)
class PinPolicy:
    """The per-pin values one pin resolves to.

    Step-workflow contract §2.1, §2.10, §11.4: every pin — direct or
    gateway — declares its Model Context Window, its context-window
    threshold and its per-request output cap in the Workflow
    Configuration. This is the single per-pin lookup every reader uses —
    keyed on the pin, never by indexing a code-owned table. The safety
    margin, request overhead and handoff cap stay code-owned defaults.
    """

    output_cap: int | None
    """The per-request output cap (``max_tokens``), or ``None`` when undeclared."""

    context_window: ContextWindowConfig | None
    """The deterministic context policy, or ``None`` when undeclared."""

    model_context_window_tokens: int | None = None
    """The Model Context Window, or ``None`` when undeclared."""


def policy_for_pin(pin: PinnedModel) -> PinPolicy:
    """The per-pin policy for ``pin``, from the pin's own declared fields.

    Every pin resolves its output cap, its Model Context Window and its
    context-window threshold from itself; the safety margin, request
    overhead and handoff cap stay code-owned defaults. A pin missing any
    of the three declared fields yields ``None`` members; the caller
    refuses with the pin's key. The probe is sent with the output cap, so
    an output cap above the model's maximum is refused there.
    """
    if (
        pin.output_cap_tokens is None
        or pin.context_threshold_tokens is None
        or pin.context_window_tokens is None
    ):
        return PinPolicy(
            output_cap=None, context_window=None, model_context_window_tokens=None
        )
    try:
        context_window = ContextWindowConfig(
            threshold_tokens=pin.context_threshold_tokens,
            safety_margin_tokens=SAFETY_MARGIN_TOKENS,
            request_overhead_tokens=REQUEST_OVERHEAD_TOKENS,
            handoff_token_cap=HANDOFF_TOKEN_CAP,
        )
    except ValueError:
        return PinPolicy(
            output_cap=pin.output_cap_tokens,
            context_window=None,
            model_context_window_tokens=pin.context_window_tokens,
        )
    return PinPolicy(
        output_cap=pin.output_cap_tokens,
        context_window=context_window,
        model_context_window_tokens=pin.context_window_tokens,
    )
