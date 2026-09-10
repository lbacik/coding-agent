"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

`L1-3` says tokens are "present and non-zero; cost derivable". This does the
arithmetic on what the probes actually recorded, because "derivable" is the
half worth checking: `usage_metadata` reports token *counts*, never money, and
the buckets inside it are not priced alike.

Run it after `probe.py` and `implement.py`. It reads their transcripts and
takes no money of its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TRANSCRIPTS = Path(__file__).parent / "transcripts"

# Published rates, in dollars per million tokens. Not one of these numbers is
# obtainable from any provider API response or metadata endpoint — that is the
# finding, not an inconvenience. They are a configuration input.
PRICES: dict[str, dict[str, float] | None] = {
    "claude-sonnet-5": {
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.20,      # ~0.1x input
        "cache_creation": 2.50,  # ~1.25x input
    },
    # gpt-5.6-terra: no rate is discoverable from the API, and none is
    # published in anything this prototype can read. Left explicitly unknown
    # rather than guessed, so the gap survives into the resolution.
    "gpt-5.6-terra": None,
}


def naive(usage: dict[str, Any], price: dict[str, float]) -> float:
    """What a first implementation writes: input x input rate, output x output rate."""
    return (
        usage["input_tokens"] * price["input"] + usage["output_tokens"] * price["output"]
    ) / 1_000_000


def created_tokens(details: dict[str, Any]) -> int:
    """Cache-*write* tokens, wherever this provider happens to put them.

    The trap `bucketed` fell into on its first run: `cache_read` normalises on
    both providers, but a cache write lands in `cache_creation` on OpenAI and
    in the Anthropic-specific `ephemeral_5m_input_tokens` / `..._1h_...` keys
    on Anthropic, where `cache_creation` stays zero. A cost function that
    trusts the normalised field alone silently prices Anthropic cache writes
    as ordinary input — under-charging them, since a write costs ~1.25x.
    """
    return (
        (details.get("cache_creation") or 0)
        or (details.get("ephemeral_5m_input_tokens") or 0)
        + (details.get("ephemeral_1h_input_tokens") or 0)
    )


def bucketed(usage: dict[str, Any], price: dict[str, float]) -> float:
    """The same usage, priced per bucket.

    `input_tokens` is the *total* prompt, cache hits included — not the
    uncached remainder — so the naive form charges a cache read at the full
    input rate.
    """
    details = usage.get("input_token_details") or {}
    read = details.get("cache_read", 0) or 0
    created = created_tokens(details)
    fresh = usage["input_tokens"] - read - created
    return (
        fresh * price["input"]
        + read * price["cache_read"]
        + created * price["cache_creation"]
        + usage["output_tokens"] * price["output"]
    ) / 1_000_000


def model_key(name: str | None) -> str | None:
    if not name:
        return None
    for key in PRICES:
        if name.startswith(key):
            return key
    return None


def main() -> None:
    probe = json.loads((TRANSCRIPTS / "probe.json").read_text())
    implement = json.loads((TRANSCRIPTS / "implement.json").read_text())

    print("=== L1-3, the cache round trip: what the two formulas disagree about ===")
    for provider, block in probe.items():
        for entry in block.get("usage_and_cost", {}).get("rounds", []):
            usage, key = entry.get("usage_metadata"), model_key(entry.get("model_name"))
            if not usage:
                continue
            details = usage.get("input_token_details") or {}
            line = (
                f"{provider:10s} round {entry['round']}  "
                f"input={usage['input_tokens']:6d}  "
                f"cache_read={details.get('cache_read', 0):6d}  "
                f"cache_creation={created_tokens(details):6d}  "
                f"reasoning={(usage.get('output_token_details') or {}).get('reasoning', 0):4d}"
            )
            if key and PRICES[key]:
                price = PRICES[key]
                n, b = naive(usage, price), bucketed(usage, price)
                over = f"{n / b:.1f}x" if b else "n/a"
                line += f"  naive=${n:.6f}  bucketed=${b:.6f}  overcharge={over}"
            else:
                line += "  cost=UNPRICEABLE (no rate for this model from any API)"
            print(line)

    print("\n=== L1-6, the whole Attempt: usage summed from the per-response flushes ===")
    for provider, ledger in implement.items():
        total = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0,
                 "reasoning": 0}
        for flush in ledger["usage_flushes"]:
            usage = flush["usage"]
            details = usage.get("input_token_details") or {}
            total["input_tokens"] += usage["input_tokens"]
            total["output_tokens"] += usage["output_tokens"]
            total["cache_read"] += details.get("cache_read", 0) or 0
            total["cache_creation"] += created_tokens(details)
            total["reasoning"] += (usage.get("output_token_details") or {}).get("reasoning", 0) or 0
        key = model_key(ledger["models_seen"][0])
        priced = PRICES.get(key or "")
        summary = (
            f"{provider:10s} {ledger['iterations']} responses  "
            f"input={total['input_tokens']:6d}  output={total['output_tokens']:5d}  "
            f"cache_read={total['cache_read']:6d}  reasoning={total['reasoning']:5d}"
        )
        if priced:
            usage_like = {
                "input_tokens": total["input_tokens"],
                "output_tokens": total["output_tokens"],
                "input_token_details": {
                    "cache_read": total["cache_read"],
                    "cache_creation": total["cache_creation"],
                },
            }
            summary += f"  bucketed=${bucketed(usage_like, priced):.6f}"
        else:
            summary += "  cost=UNPRICEABLE"
        print(summary)

    print(
        "\nNote: the two providers disagree on the token count of an identical prompt "
        "(different tokenizers), so a token ceiling in §5 is not portable across pins."
    )


if __name__ == "__main__":
    main()
