from __future__ import annotations

from coding_agent.provider.pinned_model import PinnedModel


# Effective model-work token ceilings are pin-specific, just like prices and
# compaction thresholds. The key is deliberately a PinnedModel key rather
# than a provider name: provider accounting semantics may differ by model.
EffectiveTokenCeilingTable = dict[str, int]


def effective_token_ceiling_for(
    table: EffectiveTokenCeilingTable, pin: PinnedModel
) -> int | None:
    """Return `pin`'s effective-work ceiling, if it is configured."""
    return table.get(pin.key)
