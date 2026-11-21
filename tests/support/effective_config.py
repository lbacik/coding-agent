"""A full-shape Effective Configuration record for tests (CFG-41).

Tests that seed unfinished Attempts recovered by
``recover_unfinished_step_attempts`` must pin the full record: recovery
refuses a missing, unreadable or old-shape record (CFG-42) instead of
guessing from the current configuration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION
from coding_agent.skillbundle.policy import PolicyReview
from coding_agent.workflow.effective import (
    BundleMember,
    InventoryFile,
    build_effective_configuration,
)
from coding_agent.workflow.schema import (
    ModelDecl,
    ModelPrices,
    TargetRepository,
    WorkerSettings,
    Workflow,
)
from coding_agent.workflow.schema import WorkflowConfiguration
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration
from support.run_inputs import make_run_inputs

TEST_PIN = PinnedModel(
    provider="anthropic",
    model="claude-sonnet-5",
    endpoint="messages",
    effort="medium",
    output_cap_tokens=32000,
    context_threshold_tokens=80000,
    context_window_tokens=1000000,
)
"""The direct pin test records use (present in the code-owned pair table)."""

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


def _model_decl(
    *,
    model_id: str,
    pin: PinnedModel,
    prices: TokenPrices,
    api_key_env: str,
    gateway_url_env: str | None,
) -> ModelDecl:
    """The ``models`` entry ``workflow.model`` resolves to at startup.

    Mirrors the validated shape: a direct pin names ``provider`` while a
    gateway pin names ``gateway_kind`` with exactly one of ``gateway_url``
    (literal) or ``gateway_url_env`` (resolved from the environment). The
    prices ride the declaration as validated ``ModelPrices``.
    """
    v2_prices = ModelPrices(
        input=prices.input,
        output=prices.output,
        cache_read=prices.cache_read,
        cache_write=prices.cache_write,
    )
    if pin.is_gateway:
        return ModelDecl(
            id=model_id,
            provider=None,
            model=pin.model,
            endpoint=pin.endpoint,
            effort=pin.effort,
            api_key_env=api_key_env,
            prices=v2_prices,
            gateway_kind=pin.provider,
            gateway_url=None if gateway_url_env is not None else pin.gateway_url,
            gateway_url_env=gateway_url_env,
            output_cap_tokens=pin.output_cap_tokens,
            context_threshold_tokens=pin.context_threshold_tokens,
            context_window_tokens=pin.context_window_tokens,
        )
    return ModelDecl(
        id=model_id,
        provider=pin.provider,
        model=pin.model,
        endpoint=pin.endpoint,
        effort=pin.effort,
        api_key_env=api_key_env,
        prices=v2_prices,
        output_cap_tokens=pin.output_cap_tokens,
        context_threshold_tokens=pin.context_threshold_tokens,
        context_window_tokens=pin.context_window_tokens,
    )


def full_effective_configuration(
    workflow: Workflow,
    *,
    base_branch: str = "main",
    prompt_texts: dict[str, str] | None = None,
    skill_texts: dict[str, str] | None = None,
    bundle_members: list[BundleMember] | None = None,
    activations: dict[str, str] | None = None,
    pin: PinnedModel = TEST_PIN,
    prices: TokenPrices = TEST_PRICES,
    api_key_env: str = "ANTHROPIC_API_KEY",
    gateway_url_env: str | None = None,
    gateway_url_source: str | None = None,
) -> dict[str, object]:
    """A full-shape pinned record for ``workflow`` (CFG-41, v4).

    The file-level settings come from a synthetic configuration whose
    target repository is ``example-owner/example-repo`` on
    ``base_branch``; the Pinned Model resolves through
    :func:`policy_for_pin` inside the builder, so the record carries the
    code-owned output cap and context-window config exactly as production
    pins them. The synthetic configuration is a genuine validated-shape
    ``WorkflowConfiguration`` wrapped in a ``LoadedWorkflowConfiguration``, so the
    record runs the same 4-argument build production runs.

    ``skill_texts`` is the legacy static-texts spelling: each entry
    becomes one available member with synthetic provenance/review and an
    activation delivery of the same bytes. Prefer ``bundle_members`` plus
    ``activations`` for on-demand members.

    ``gateway_url_source`` is the legacy ``env:<NAME>`` spelling, kept
    while callers migrate; ``gateway_url_env`` (the bare variable name)
    wins where both are given.
    """
    if gateway_url_env is None and gateway_url_source is not None:
        gateway_url_env = (
            gateway_url_source[len("env:"):]
            if gateway_url_source.startswith("env:")
            else gateway_url_source
        )
    config = WorkflowConfiguration(
        version=4,
        target=TargetRepository(
            kind="github",
            repository="example-owner/example-repo",
            base_branch=base_branch,
            token_env="GITHUB_TOKEN",
        ),
        models={
            workflow.model: _model_decl(
                model_id=workflow.model,
                pin=pin,
                prices=prices,
                api_key_env=api_key_env,
                gateway_url_env=gateway_url_env,
            )
        },
        worker=WorkerSettings(),
        workflows={workflow.id: workflow},
        skills=(),
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
    loaded = LoadedWorkflowConfiguration(
        path=Path("workflows.yaml"), raw=b"", config=config
    )
    run_inputs = make_run_inputs(
        loaded,
        prompt_raws={
            locator: text.encode("utf-8") for locator, text in (prompt_texts or {}).items()
        },
        members=members,
        bodies=deliveries,
        framing_version=DELIVERY_FRAMING_VERSION,
    )
    return build_effective_configuration(
        run_inputs, workflow=workflow, pin=pin, prices=prices
    )
