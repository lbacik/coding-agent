"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

The two pins under test, and the one place that knows how each provider spells
"medium effort". Kept apart from `probe.py` so the asymmetry is readable on its
own: the same intent needs a different parameter name on each side, which is
the first thing `L1-1`'s "one tool loop serves both" has to survive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EFFORT = "medium"  # the pin the project owner chose for both providers


@dataclass(frozen=True)
class Pin:
    """One provider's pinned model, exactly as the Attempt would open it."""

    name: str
    model: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    # A model on the same provider that lacks a capability the runtime needs.
    # L1-4 asks for a preflight refusal; something has to be refusable.
    deficient_model: str | None = None
    models_endpoint: str = ""

    def build(self, **overrides: Any) -> Any:
        """The pin, with overrides applied. An override of `None` *removes* the
        key rather than sending a null: a deficient model rejects the effort
        knobs outright, and a null would be a different request from an absent
        one."""
        from langchain.chat_models import init_chat_model

        merged = {"model": self.model, **self.kwargs, **overrides}
        return init_chat_model(**{k: v for k, v in merged.items() if v is not None})


ANTHROPIC = Pin(
    name="anthropic",
    model="anthropic:claude-sonnet-5",
    # Sonnet 5 rejects `thinking={"type": "enabled", "budget_tokens": N}` with a
    # 400 and says so: adaptive thinking plus `output_config.effort` is the only
    # shape it accepts. `max_tokens` is required by the Anthropic API.
    kwargs={
        "max_tokens": 8000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
    },
    # Haiku 4.5 is the same provider, one generation back: its own capability
    # tree reports `effort` unsupported, so it is refusable on a declared fact.
    deficient_model="anthropic:claude-haiku-4-5",
    models_endpoint="https://api.anthropic.com/v1/models/claude-sonnet-5",
)

OPENAI = Pin(
    name="openai",
    model="openai:gpt-5.6-terra",
    # `use_responses_api` is not a preference here, it is the only way the pin
    # runs at all. `init_chat_model("openai:...")` defaults ChatOpenAI to
    # /v1/chat/completions, and that endpoint refuses function tools together
    # with a reasoning effort:
    #
    #   400 Function tools with reasoning_effort are not supported for
    #       gpt-5.6-terra in /v1/chat/completions. To use function tools, use
    #       /v1/responses or set reasoning_effort to 'none'.
    #
    # Tools and a pinned effort are both non-negotiable for a work node, so the
    # Responses API it is. The cost is that the assistant turn now carries a
    # `reasoning` block with `encrypted_content` that the loop must carry back.
    kwargs={"reasoning_effort": EFFORT, "use_responses_api": True},
    # Completions-only: no tool calling at all, the one capability every work
    # node depends on and neither provider declares.
    deficient_model="openai:gpt-3.5-turbo-instruct",
    models_endpoint="https://api.openai.com/v1/models/gpt-5.6-terra",
)

PINS = [ANTHROPIC, OPENAI]
