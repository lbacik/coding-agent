"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

The two pins under test. Lifted from `prototypes/s3-provider-contract/providers.py`
and trimmed to what a long tool loop needs, because #24 already established the
shapes and this prototype has no business rediscovering them.

The one thing kept in full is the OpenAI comment: it is the whole reason this
prototype runs on both pins rather than one. On the Responses API the assistant
turn carries a `reasoning` block with `encrypted_content` that the provider
expects to see again, and compaction is history rewriting by another name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EFFORT = "medium"  # the pin the project owner chose for both providers


@dataclass(frozen=True)
class Pin:
    name: str
    model: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    # Anthropic prices and reports a cache; OpenAI's Responses API caches on its
    # own terms and exposes no breakpoint to set. Only one side can answer
    # "what does re-sending the pinned prefix every turn cost with a
    # breakpoint", which is #24's open number.
    supports_cache_breakpoint: bool = False

    def build(self, **overrides: Any) -> Any:
        from langchain.chat_models import init_chat_model

        merged = {"model": self.model, **self.kwargs, **overrides}
        return init_chat_model(**{k: v for k, v in merged.items() if v is not None})


ANTHROPIC = Pin(
    name="anthropic",
    model="anthropic:claude-sonnet-5",
    kwargs={
        "max_tokens": 8000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
    },
    supports_cache_breakpoint=True,
)

OPENAI = Pin(
    name="openai",
    model="openai:gpt-5.6-terra",
    # Not a preference: /v1/chat/completions refuses function tools together
    # with a reasoning effort, and a work node needs both (#24, ADR 0010).
    kwargs={"reasoning_effort": EFFORT, "use_responses_api": True},
)

PINS = {p.name: p for p in (ANTHROPIC, OPENAI)}
