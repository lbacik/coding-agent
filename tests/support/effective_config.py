"""A full-shape Effective Configuration record for tests (CFG-41).

Tests that seed unfinished Attempts recovered by
``recover_unfinished_step_attempts`` must pin the full record: recovery
refuses a missing, unreadable or old-shape record (CFG-42) instead of
guessing from the current configuration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION
from coding_agent.skillbundle.policy import PolicyReview
from coding_agent.workflow.effective import (
    BundleMember,
    InventoryFile,
    build_effective_configuration,
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


def _synthetic_review(name: str, body: str, source: str = "git+https://example.com/org/skills-a.git") -> PolicyReview:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return PolicyReview(
        source_identity=source,
        name=name,
        skill_sha256=digest,
        requires=(),
        files=(),
        references=(),
        reason="synthetic review",
    )


def full_effective_configuration(
    workflow: V2Workflow,
    *,
    base_branch: str = "main",
    prompt_texts: dict[str, str] | None = None,
    skill_texts: dict[str, str] | None = None,
    bundle_members: list[BundleMember] | None = None,
    activations: dict[str, str] | None = None,
    pin: PinnedModel = TEST_PIN,
    prices: TokenPrices = TEST_PRICES,
    api_key_env: str = "ANTHROPIC_API_KEY",
) -> dict[str, object]:
    """A full-shape pinned record for ``workflow`` (CFG-41, v2).

    The file-level settings come from a synthetic configuration whose
    target repository is ``example-owner/example-repo`` on
    ``base_branch``; the Pinned Model resolves through
    :func:`policy_for_pin`, so the record carries the code-owned output
    cap and context-window config exactly as production pins them.

    ``skill_texts`` is the legacy static-texts spelling: each entry
    becomes one available member with synthetic provenance/review and an
    activation delivery of the same bytes. Prefer ``bundle_members`` plus
    ``activations`` for on-demand members.
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
    texts = dict(skill_texts or {})
    if bundle_members is None:
        members = []
        for name in sorted(texts.keys()):
            body = texts[name]
            members.append(
                BundleMember(
                    name=name,
                    source_identity="git+https://example.com/org/skills-a.git",
                    resolved_commit="a" * 40,
                    declared_ref=None,
                    relative_source_path="",
                    store_relative_path=name,
                    files=(
                        InventoryFile(
                            path="SKILL.md",
                            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                            executable=False,
                        ),
                    ),
                    overlays=(),
                    review=_synthetic_review(name, body),
                )
            )
    else:
        members = list(bundle_members)
    deliveries = dict(activations) if activations is not None else dict(texts)
    if activations is None and bundle_members is not None:
        deliveries = {member.name: texts.get(member.name, "") for member in members}
    return build_effective_configuration(
        config=config,
        workflow=workflow,
        prompt_texts=prompt_texts or {},
        bundle_members=members,
        activations=deliveries,
        framing_version=DELIVERY_FRAMING_VERSION,
        pin=pin,
        prices=prices,
        policy=policy_for_pin(pin),
        api_key_env=api_key_env,
    )
