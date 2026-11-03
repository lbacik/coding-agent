"""The Effective Configuration: semantic validation plus pinning.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table:
defaults, secrets as names, the code-owned context table), §2.2 (strict
parsing: the skill allowlist and the Selection Label rules), §2.4
(``agent config validate`` is static and offline; every refusal exits
non-zero naming the YAML path; an unknown or missing ``--workflow`` lists
the available ids) and §2.5 (Effective Configuration and pinning: read once,
recorded at Claim, values never part of it) and
[ADR 0016](../docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md)
(a Workflow fills only the implementation stage).

MVP row ids: W-PIN-2 (the Effective Configuration and its digest recorded at
Claim and disclosed in the pull-request body and terminal comment),
W-CFG-5 (missing or unknown ``--workflow`` lists the available
ids), W-CFG-6 (a ``skill`` outside the Skill Bundle, or a bundle member
outside the top-level allowlist, which is ``implement`` alone),
W-CFG-9 (an unset env var names the variable, never a value),
W-CFG-10 (an unknown ``model`` reference, or a (provider, model) pair
outside the code-owned table), W-CFG-11 (a ``target.kind`` other than
``github``), W-CFG-12 (each forbidden Selection Label case), C-CFG-1 (a
secret-shaped value in ``token_env`` or ``api_key_env`` is refused and never
printed), C-CFG-3 (omitted ``limits``, ``worker`` and ``target.labels``
record every default).

Semantic refusals raise ``WorkflowConfigError`` naming the YAML path and make
no network call: only the YAML file, the prompt files and the process
environment are read. Environment-variable **values** never appear in an
error, in the Effective Configuration or in its digest; the **names** do.
``resolve_effective_configuration`` reads each prompt file once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from coding_agent.skillbundle.verify import BUNDLE_SPEC
from coding_agent.workflow.parser import WorkflowConfigError
from coding_agent.workflow.schema import (
    LimitsDecl,
    ModelDecl,
    SelectorDecl,
    WorkerDecl,
    WorkflowConfig,
)

DEFAULT_WALL_CLOCK_MINUTES = 60.0
"""Default Attempt wall clock (contract §2.1, §11; C-CFG-3)."""

DEFAULT_COST_USD = 20.0
"""Default Attempt cost ceiling in USD (contract §2.1, §11; C-CFG-3)."""

DEFAULT_TOKENS = 4_000_000
"""Default Attempt token meter (contract §2.1, §11; C-CFG-3)."""

DEFAULT_TOOL_CALLS = 600
"""Default Attempt tool-call meter (contract §2.1, §11; C-CFG-3)."""

DEFAULT_POLL_INTERVAL_SECONDS = 60
"""Default idle sleep between selection sweeps (contract §2.1; C-CFG-3)."""

DEFAULT_SHUTDOWN_GRACE_SECONDS = 30
"""Default SIGTERM grace for the live Attempt (contract §2.1; C-CFG-3)."""

DEFAULT_READY_FOR_HUMAN = "ready-for-human"
"""Default ``ready-for-human`` lifecycle label name (contract §2.1; C-CFG-3)."""

TOP_LEVEL_SKILL_ALLOWLIST = frozenset({"implement"})
"""Skills a Workflow Step may name (contract §2.2; W-CFG-6).

In the MVP the allowlist is ``implement`` alone: ``tdd``,
``codebase-design`` and ``code-review`` stay nested and are activated by
``implement``'s own text.
"""

_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
"""What an environment-variable **name** looks like.

Upper-snake on purpose: a secret-shaped value (``github_pat_…``,
``sk-ant-…``, anything with a dash, dot, slash, whitespace or lowercase
letter) never matches, so it is refused as C-CFG-1 instead of being looked
up. Real secrets are never all-uppercase; an all-uppercase value that
matches is a name, even one like ``GITHUB_PAT_TOKEN``.
"""


@dataclass(frozen=True)
class EffectiveLimits:
    """A Workflow's ceilings with every default filled in (§2.1, §2.5)."""

    wall_clock_minutes: float
    cost_usd: float
    tokens: int
    tool_calls: int


@dataclass(frozen=True)
class EffectiveWorker:
    """The file-level worker settings with every default filled in (§2.1)."""

    poll_interval_seconds: int | float
    shutdown_grace_seconds: int | float


@dataclass(frozen=True)
class EffectiveTarget:
    """The file-level target the Attempt runs under (§2.1, §2.5).

    ``token_env`` is the variable **name**; its value is never read into this
    object (§2.5).
    """

    kind: str
    repository: str
    base_branch: str
    token_env: str
    ready_for_human: str


@dataclass(frozen=True)
class EffectiveModel:
    """The Workflow's referenced Pinned Model (§2.1, §2.5).

    ``api_key_env`` is the variable **name**; its value is never read into
    this object (§2.5).
    """

    id: str
    provider: str
    model: str
    endpoint: str
    effort: str
    api_key_env: str
    input_price: float
    output_price: float
    cache_read_price: float
    cache_write_price: float


@dataclass(frozen=True)
class EffectiveStep:
    """One normalised Workflow Step (§2.1, §2.5).

    ``prompt_content`` holds the bytes of the ``prompt_file`` the step
    activates, read once at startup (§2.5); it is ``None`` for a ``skill``
    step, whose body comes from the installed Skill Bundle instead.
    """

    id: str
    skill: str | None
    prompt_file: str | None
    prompt_content: bytes | None


@dataclass(frozen=True)
class EffectiveWorkflow:
    """The normalised selected Workflow (§2.5)."""

    id: str
    model: str
    label: str
    require_all: tuple[str, ...]
    require_any: tuple[str, ...]
    exclude: tuple[str, ...]
    limits: EffectiveLimits
    steps: tuple[EffectiveStep, ...]


@dataclass(frozen=True)
class EffectivePromptFile:
    """The bytes of one prompt file the Workflow activates (§2.5)."""

    path: str
    content: bytes


@dataclass(frozen=True)
class EffectiveConfiguration:
    """Everything one Attempt runs under, fixed at Claim (§2.5).

    The normalised selected Workflow, the file-level settings it runs under
    (``target``, the referenced model, ``worker``), the bytes of every prompt
    file its steps activate, and the SHA-256 ``digest`` of all of it.
    Environment-variable **names** are part of it; values never are. Frozen
    so that, once recorded, a later edit to the YAML cannot reach back into
    an open Attempt's configuration.
    """

    workflow: EffectiveWorkflow
    target: EffectiveTarget
    model: EffectiveModel
    worker: EffectiveWorker
    prompts: tuple[EffectivePromptFile, ...]
    digest: str
    source: Path


def _code_owned_tables() -> tuple[frozenset[tuple[str, str]], dict[str, frozenset[str]]]:
    """The code-owned context tables in one pass (contract §2.1).

    Single-sourced from ``provider.config.PINNED_MODELS``: the context
    window, compaction threshold and effective token ceiling live in a
    code-owned table keyed by (provider, model). Returns the known
    (provider, model) pairs plus each provider's pinned endpoints. Imported
    lazily so this offline module pays no provider-adapter import at load
    time.
    """
    from coding_agent.provider.config import PINNED_MODELS

    pairs: set[tuple[str, str]] = set()
    mutable_endpoints: dict[str, set[str]] = {}
    for pin in PINNED_MODELS.values():
        pairs.add((pin.provider, pin.model))
        mutable_endpoints.setdefault(pin.provider, set()).add(pin.endpoint)
    endpoints = {
        provider: frozenset(values) for provider, values in mutable_endpoints.items()
    }
    return frozenset(pairs), endpoints


def _check_env_name(value: str, path: str) -> None:
    """Refuse a secret-shaped value where a variable name belongs (C-CFG-1).

    The value itself is never interpolated into the error (§2.4, §2.5).
    """
    if _ENV_NAME_RE.fullmatch(value) is None:
        raise WorkflowConfigError(
            path, "looks like a secret value, not an environment-variable name"
        )


def _require_env_set(name: str, path: str, env: Mapping[str, str]) -> None:
    """Refuse an unset referenced variable, naming it but never a value."""
    if not env.get(name):
        raise WorkflowConfigError(path, f"environment variable {name!r} is not set")


def _check_target_kind(cfg: WorkflowConfig) -> None:
    """Refuse an unsupported integration (W-CFG-11)."""
    if cfg.target.kind != "github":
        raise WorkflowConfigError(
            "target.kind",
            f"unsupported target kind {cfg.target.kind!r}: must be 'github'",
        )


def _check_model_decl(
    model_id: str,
    decl: ModelDecl,
    pairs: frozenset[tuple[str, str]],
    endpoints: dict[str, frozenset[str]],
) -> None:
    """Refuse a model outside the code-owned context table (W-CFG-10)."""
    if (decl.provider, decl.model) not in pairs:
        raise WorkflowConfigError(
            f"models.{model_id}.model",
            f"unknown (provider, model) pair ({decl.provider!r}, {decl.model!r}): "
            "outside the code-owned context table",
        )
    if decl.endpoint not in endpoints.get(decl.provider, frozenset()):
        raise WorkflowConfigError(
            f"models.{model_id}.endpoint",
            f"unknown endpoint {decl.endpoint!r} for provider {decl.provider!r}",
        )
    if decl.effort not in ("low", "medium", "high"):
        raise WorkflowConfigError(
            f"models.{model_id}.effort",
            f"unknown effort {decl.effort!r}: must be 'low', 'medium' or 'high'",
        )


def _check_model_env_and_decl(
    cfg: WorkflowConfig,
    model_id: str,
    environ: Mapping[str, str],
    pairs: frozenset[tuple[str, str]],
    endpoints: dict[str, frozenset[str]],
) -> ModelDecl:
    """Secret, presence and code-owned-table checks for one model (C-CFG-1, W-CFG-9, W-CFG-10)."""
    decl = cfg.models[model_id]
    _check_env_name(decl.api_key_env, f"models.{model_id}.api_key_env")
    _require_env_set(decl.api_key_env, f"models.{model_id}.api_key_env", environ)
    _check_model_decl(model_id, decl, pairs, endpoints)
    return decl


def _check_selected_workflow(
    cfg: WorkflowConfig,
    workflow_id: str,
    environ: Mapping[str, str],
    pairs: frozenset[tuple[str, str]],
    endpoints: dict[str, frozenset[str]],
) -> ModelDecl:
    """Reference, skill and label checks for one Workflow, plus its model's
    env and table checks (W-CFG-6, W-CFG-9, W-CFG-10, W-CFG-12, C-CFG-1).

    Returns the referenced model declaration for the caller to normalise.
    Shared by ``validate_workflow_semantics`` and
    ``resolve_effective_configuration`` so the two never drift apart.
    """
    workflow = cfg.workflows[workflow_id]
    if workflow.model not in cfg.models:
        raise WorkflowConfigError(
            f"workflows.{workflow_id}.model",
            f"unknown model {workflow.model!r}: available: "
            f"{', '.join(sorted(cfg.models))}",
        )
    model_decl = _check_model_env_and_decl(cfg, workflow.model, environ, pairs, endpoints)
    _check_selector(workflow_id, workflow.selector, _ready_for_human(cfg))
    for index, step in enumerate(workflow.steps):
        if step.skill is not None:
            _check_step_skill(workflow_id, index, step.skill)
    return model_decl


def _check_selector(workflow_id: str, selector: SelectorDecl, ready_for_human: str) -> None:
    """Refuse every forbidden Selection Label case (W-CFG-12)."""
    base = f"workflows.{workflow_id}.selector"
    label = selector.label
    if not label:
        raise WorkflowConfigError(f"{base}.label", "must be a non-empty string")
    if label == ready_for_human:
        raise WorkflowConfigError(
            f"{base}.label",
            f"must not equal the ready_for_human name {ready_for_human!r}",
        )
    if label == "wontfix":
        raise WorkflowConfigError(f"{base}.label", "must not be 'wontfix'")
    if label.startswith("wayfinder:"):
        raise WorkflowConfigError(f"{base}.label", "must not match 'wayfinder:*'")
    for field_name in ("require_all", "require_any", "exclude"):
        entries: tuple[str, ...] = getattr(selector, field_name)
        if label in entries:
            raise WorkflowConfigError(
                f"{base}.label",
                f"must not appear in the same workflow's {field_name!r}",
            )


def _check_step_skill(workflow_id: str, index: int, skill: str) -> None:
    """Refuse a skill outside the bundle or outside the allowlist (W-CFG-6)."""
    path = f"workflows.{workflow_id}.steps[{index}].skill"
    if skill not in BUNDLE_SPEC.names:
        raise WorkflowConfigError(
            path, f"unknown skill {skill!r}: not a member of the Skill Bundle"
        )
    if skill not in TOP_LEVEL_SKILL_ALLOWLIST:
        raise WorkflowConfigError(
            path,
            f"skill {skill!r} is not on the top-level allowlist "
            f"(available: {', '.join(sorted(TOP_LEVEL_SKILL_ALLOWLIST))})",
        )


def _available_workflows(cfg: WorkflowConfig) -> str:
    return ", ".join(sorted(cfg.workflows))


def _ready_for_human(cfg: WorkflowConfig) -> str:
    return cfg.target.labels.ready_for_human or DEFAULT_READY_FOR_HUMAN


def _effective_limits(decl: LimitsDecl | None, workflow_id: str) -> EffectiveLimits:
    """Fill the §2.1 ceiling defaults (C-CFG-3)."""
    base = f"workflows.{workflow_id}.limits"
    wall = DEFAULT_WALL_CLOCK_MINUTES
    cost = DEFAULT_COST_USD
    tokens: int | float | None = None
    calls: int | float | None = None
    if decl is not None:
        if decl.wall_clock_minutes is not None:
            wall = float(decl.wall_clock_minutes)
        if decl.cost_usd is not None:
            cost = float(decl.cost_usd)
        tokens = decl.tokens
        calls = decl.tool_calls
    resolved_tokens = DEFAULT_TOKENS if tokens is None else tokens
    resolved_calls = DEFAULT_TOOL_CALLS if calls is None else calls
    for raw_value, name in ((resolved_tokens, "tokens"), (resolved_calls, "tool_calls")):
        if isinstance(raw_value, float) and not raw_value.is_integer():
            raise WorkflowConfigError(
                f"{base}.{name}", f"must be an integer, got {raw_value!r}"
            )
    return EffectiveLimits(
        wall_clock_minutes=wall,
        cost_usd=cost,
        tokens=int(resolved_tokens),
        tool_calls=int(resolved_calls),
    )


def _effective_worker(worker: WorkerDecl | None) -> EffectiveWorker:
    """Fill the §2.1 worker defaults (C-CFG-3)."""
    poll: int | float | None = None
    grace: int | float | None = None
    if worker is not None:
        poll = worker.poll_interval_seconds
        grace = worker.shutdown_grace_seconds
    return EffectiveWorker(
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS if poll is None else poll,
        shutdown_grace_seconds=DEFAULT_SHUTDOWN_GRACE_SECONDS if grace is None else grace,
    )


def _read_prompt_bytes(cfg: WorkflowConfig, workflow_id: str) -> dict[str, bytes]:
    """Read each prompt file the Workflow activates, exactly once (§2.5).

    Returns the YAML-relative path to its bytes. The structural parser
    already refused missing or escaping files; the containment check here is
    a second line that can only fire on a hand-built ``WorkflowConfig``.
    """
    config_dir = cfg.source.parent.resolve()
    contents: dict[str, bytes] = {}
    for index, step in enumerate(cfg.workflows[workflow_id].steps):
        if step.prompt_file is None or step.prompt_file in contents:
            continue
        candidate = (config_dir / step.prompt_file).resolve()
        if candidate != config_dir and config_dir not in candidate.parents:
            raise WorkflowConfigError(
                f"workflows.{workflow_id}.steps[{index}].prompt_file",
                f"escapes the configuration directory: {step.prompt_file!r}",
            )
        try:
            contents[step.prompt_file] = candidate.read_bytes()
        except OSError as exc:
            raise WorkflowConfigError(
                f"workflows.{workflow_id}.steps[{index}].prompt_file",
                f"prompt file does not exist: {step.prompt_file!r}",
            ) from exc
    return contents


def _canonical_payload(
    *,
    target: EffectiveTarget,
    model: EffectiveModel,
    worker: EffectiveWorker,
    workflow: EffectiveWorkflow,
    prompts: Mapping[str, bytes],
) -> dict[str, object]:
    """The JSON-stable form the digest covers (§2.5).

    Environment-variable **names** are in; values never are. ``source`` is
    out: an absolute checkout path must not move the digest.
    """
    return {
        "target": {
            "kind": target.kind,
            "repository": target.repository,
            "base_branch": target.base_branch,
            "token_env": target.token_env,
            "ready_for_human": target.ready_for_human,
        },
        "model": {
            "id": model.id,
            "provider": model.provider,
            "model": model.model,
            "endpoint": model.endpoint,
            "effort": model.effort,
            "api_key_env": model.api_key_env,
            "prices_usd_per_mtok": {
                "input": model.input_price,
                "output": model.output_price,
                "cache_read": model.cache_read_price,
                "cache_write": model.cache_write_price,
            },
        },
        "worker": {
            "poll_interval_seconds": worker.poll_interval_seconds,
            "shutdown_grace_seconds": worker.shutdown_grace_seconds,
        },
        "workflow": {
            "id": workflow.id,
            "model": workflow.model,
            "selector": {
                "label": workflow.label,
                "require_all": list(workflow.require_all),
                "require_any": list(workflow.require_any),
                "exclude": list(workflow.exclude),
            },
            "limits": {
                "wall_clock_minutes": workflow.limits.wall_clock_minutes,
                "cost_usd": workflow.limits.cost_usd,
                "tokens": workflow.limits.tokens,
                "tool_calls": workflow.limits.tool_calls,
            },
            "steps": [
                {"id": step.id, "skill": step.skill, "prompt_file": step.prompt_file}
                for step in workflow.steps
            ],
        },
        "prompts": {path: content.hex() for path, content in prompts.items()},
    }


def effective_configuration_record(
    effective: EffectiveConfiguration,
) -> dict[str, object]:
    """The JSON-stable record of one Effective Configuration (contract §2.5).

    Exactly the canonical payload the ``digest`` covers: the normalised
    selected Workflow, the file-level settings it runs under (``target``, the
    referenced model, ``worker``), and the hex of every prompt file its steps
    activate. Environment-variable values never appear; the names do, and the
    absolute ``source`` path does not. The Claim stores this record in
    ``attempts``, so the stored digest equals ``effective.digest`` and the
    pull-request body and terminal comment disclose the same bytes
    ``agent config validate --workflow`` prints.

    MVP row ids: W-PIN-2.
    """
    return _canonical_payload(
        target=effective.target,
        model=effective.model,
        worker=effective.worker,
        workflow=effective.workflow,
        prompts={prompt.path: prompt.content for prompt in effective.prompts},
    )


def validate_workflow_semantics(
    cfg: WorkflowConfig, *, env: Mapping[str, str] | None = None
) -> None:
    """Check everything static beyond the structural parse (contract §2.4).

    Covers every Workflow in the file: ``target.kind``, secret shapes and
    presence for every named variable, every model against the code-owned
    table, and every Workflow's model reference, skills and Selection Label.
    Static and offline: reads only ``env`` (W-CFG-13 extends here — still no
    network call). Every refusal raises ``WorkflowConfigError`` naming the
    YAML path.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    _check_target_kind(cfg)
    _check_env_name(cfg.target.token_env, "target.token_env")
    _require_env_set(cfg.target.token_env, "target.token_env", environ)
    pairs, endpoints = _code_owned_tables()
    for workflow_id in sorted(cfg.workflows):
        _check_selected_workflow(cfg, workflow_id, environ, pairs, endpoints)


def resolve_effective_configuration(
    cfg: WorkflowConfig, workflow_id: str | None, *, env: Mapping[str, str] | None = None
) -> EffectiveConfiguration:
    """Build the pinned Effective Configuration for one Workflow (§2.5).

    A missing or unknown ``workflow_id`` is refused with the available ids
    (W-CFG-5). Otherwise the selected Workflow's references, skills, labels,
    secrets and environment are checked exactly as in
    ``validate_workflow_semantics``, the defaults are filled in (C-CFG-3),
    every prompt file the Workflow activates is read once, and the SHA-256
    ``digest`` of all of it is computed. Values of environment variables are
    never part of the result; their names are.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    if workflow_id is None:
        raise WorkflowConfigError(
            "workflows", f"missing --workflow: available: {_available_workflows(cfg)}"
        )
    if workflow_id not in cfg.workflows:
        raise WorkflowConfigError(
            "workflows",
            f"unknown workflow {workflow_id!r}: available: {_available_workflows(cfg)}",
        )
    _check_target_kind(cfg)
    _check_env_name(cfg.target.token_env, "target.token_env")
    _require_env_set(cfg.target.token_env, "target.token_env", environ)
    pairs, endpoints = _code_owned_tables()
    model_decl = _check_selected_workflow(cfg, workflow_id, environ, pairs, endpoints)
    workflow = cfg.workflows[workflow_id]
    ready = _ready_for_human(cfg)

    prompt_bytes = _read_prompt_bytes(cfg, workflow_id)
    steps = tuple(
        EffectiveStep(
            id=step.id,
            skill=step.skill,
            prompt_file=step.prompt_file,
            prompt_content=(
                prompt_bytes[step.prompt_file] if step.prompt_file is not None else None
            ),
        )
        for step in workflow.steps
    )
    effective_target = EffectiveTarget(
        kind=cfg.target.kind,
        repository=cfg.target.repository,
        base_branch=cfg.target.base_branch,
        token_env=cfg.target.token_env,
        ready_for_human=ready,
    )
    effective_model = EffectiveModel(
        id=model_decl.id,
        provider=model_decl.provider,
        model=model_decl.model,
        endpoint=model_decl.endpoint,
        effort=model_decl.effort,
        api_key_env=model_decl.api_key_env,
        input_price=model_decl.prices.input,
        output_price=model_decl.prices.output,
        cache_read_price=model_decl.prices.cache_read,
        cache_write_price=model_decl.prices.cache_write,
    )
    effective_worker = _effective_worker(cfg.worker)
    effective_workflow = EffectiveWorkflow(
        id=workflow.id,
        model=workflow.model,
        label=workflow.selector.label,
        require_all=workflow.selector.require_all,
        require_any=workflow.selector.require_any,
        exclude=workflow.selector.exclude,
        limits=_effective_limits(workflow.limits, workflow_id),
        steps=steps,
    )
    payload = _canonical_payload(
        target=effective_target,
        model=effective_model,
        worker=effective_worker,
        workflow=effective_workflow,
        prompts=prompt_bytes,
    )
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    prompts = tuple(
        EffectivePromptFile(path=path, content=prompt_bytes[path])
        for path in sorted(prompt_bytes)
    )
    return EffectiveConfiguration(
        workflow=effective_workflow,
        target=effective_target,
        model=effective_model,
        worker=effective_worker,
        prompts=prompts,
        digest=digest,
        source=cfg.source,
    )
