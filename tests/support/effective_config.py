"""A full-shape Effective Configuration record for tests (CFG-41).

Tests that seed unfinished Attempts recovered by
``recover_unfinished_step_attempts`` must pin the full record: recovery
refuses a missing, unreadable or old-shape record (CFG-42) instead of
guessing from the current configuration.
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.workflow.effective import (
    BundleIdentity,
    build_effective_configuration,
    bundle_identity_from_spec,
)
from coding_agent.workflow.v2_schema import (
    V2Config,
    V2Target,
    V2Worker,
    V2Workflow,
)

TEST_PIN = PinnedModel(
    provider="anthropic",
    model="claude-sonnet-5",
    endpoint="messages",
    effort="medium",
)
"""The direct pin test records use (present in the code-owned tables)."""

TEST_PRICES = TokenPrices(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50)
"""The prices test records pin beside the pin."""


def full_effective_configuration(
    workflow: V2Workflow,
    *,
    base_branch: str = "main",
    prompt_texts: dict[str, str] | None = None,
    skill_texts: dict[str, str] | None = None,
    bundle: BundleIdentity | None = None,
    pin: PinnedModel = TEST_PIN,
    prices: TokenPrices = TEST_PRICES,
    api_key_env: str = "ANTHROPIC_API_KEY",
) -> dict[str, object]:
    """A full-shape pinned record for ``workflow`` (CFG-41).

    The file-level settings come from a synthetic configuration whose
    target repository is ``example-owner/example-repo`` on
    ``base_branch``; the Pinned Model resolves through
    :func:`policy_for_pin`, so the record carries the code-owned output
    cap and context-window config exactly as production pins them.
    """
    config = V2Config(
        version=2,
        target=V2Target(
            kind="github",
            repository="example-owner/example-repo",
            base_branch=base_branch,
            token_env="GITHUB_TOKEN",
        ),
        models={},
        worker=V2Worker(),
        workflows={workflow.id: workflow},
        source=Path("workflows.yaml"),
    )
    return build_effective_configuration(
        config=config,
        workflow=workflow,
        prompt_texts=prompt_texts or {},
        skill_texts=skill_texts or {},
        bundle=bundle if bundle is not None else bundle_identity_from_spec(),
        pin=pin,
        prices=prices,
        policy=policy_for_pin(pin),
        api_key_env=api_key_env,
    )
