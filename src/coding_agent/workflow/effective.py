"""The Attempt's Effective Configuration: the full record pinned at Claim.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10 (the
Attempt records its Effective Configuration in full: the canonical compiled
Workflow, the file-level settings, every operator prompt file's bytes, the
text of every statically named skill, the Skill Bundle's identity and digest,
and the Pinned Model with its price table, context window and output cap —
one SHA-256 digest covers all of it; environment-variable values are never
part of it) and §16 (recovery uses pinned values only).

Matrix row ids: CFG-41, CFG-42.

The Run Ledger stores the digest of this record beside it
(:func:`coding_agent.ledger.store.digest_effective_configuration`); restart
recovery (:func:`coding_agent.worker.run.recover_unfinished_step_attempts`)
takes the selection label, the end labels and every other
configuration-derived value from it, never from the currently loaded file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.provider.config import PinPolicy
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.skillbundle.verify import BUNDLE_SPEC, COMPANION_DIGESTS
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Config,
    V2EnumType,
    V2FieldType,
    V2Instruction,
    V2ListType,
    V2Step,
    V2Workflow,
)

EFFECTIVE_CONFIGURATION_VERSION = 1
"""The shape version of the pinned Effective Configuration record (CFG-41).

Bumped whenever the record's top-level shape changes. Startup recovery
refuses an unfinished Attempt whose record carries another version instead
of guessing (CFG-42).
"""

TOP_LEVEL_KEYS: tuple[str, ...] = (
    "version",
    "workflow",
    "file",
    "prompts",
    "skills",
    "skill_bundle",
    "pinned_model",
)
"""Every top-level key a full Effective Configuration record carries."""


class PinnedConfigurationRefused(Exception):
    """An unfinished Attempt's pinned record cannot be recovered from (§16).

    Raised where the record is missing, unreadable or lacks the full
    CFG-41 shape — for example an Attempt opened by a build with the
    minimal record. The caller turns this into a startup refusal naming
    the Attempt (CFG-42); recovery never guesses from the current
    configuration.
    """


@dataclass(frozen=True)
class BundleIdentity:
    """The identity and digest of the whole installed Skill Bundle (§2.10)."""

    source_identity: str
    resolved_commit: str
    member_ids: tuple[str, ...]
    companion_digests: Mapping[str, str]


def bundle_identity_from_spec(
    source_identity: str = BUNDLE_SPEC.source_identity,
    resolved_commit: str = BUNDLE_SPEC.resolved_commit,
    member_ids: frozenset[str] = BUNDLE_SPEC.ids,
    companion_digests: Mapping[str, str] = COMPANION_DIGESTS,
) -> BundleIdentity:
    """The installed bundle's identity, after startup verified it (§4.1).

    The Worker verified membership, pin, exposure, references and companion
    digests before any Attempt opens, so the pinned record carries what was
    verified: the source identity, the resolved commit, the member ids and
    the companion digests.
    """
    return BundleIdentity(
        source_identity=source_identity,
        resolved_commit=resolved_commit,
        member_ids=tuple(sorted(member_ids)),
        companion_digests=dict(companion_digests),
    )


def skill_names_for_workflow(workflow: V2Workflow) -> list[str]:
    """Every skill a Workflow's steps name statically, in first-use order."""
    names: list[str] = []
    for step in workflow.steps.values():
        if step.agent is None:
            continue
        for instruction in step.agent.instructions:
            if instruction.skill is not None and instruction.skill not in names:
                names.append(instruction.skill)
    return names


def skill_texts_from_registry(
    registry: ActivationRegistry, names: list[str]
) -> dict[str, str]:
    """The activation text of every statically named skill, verbatim.

    Reads from the Worker's startup activation snapshot, so a skill owning
    Companion Files (today ``tdd``) pins those too, and no skill file is
    read again. A name the registry does not hold raises ``OSError``; the
    caller turns that into a startup refusal before any Attempt opens.
    """
    try:
        return {name: registry.bodies[name] for name in names}
    except KeyError as exc:
        raise OSError(f"skill {exc.args[0]!r} is not in the activation registry") from exc


def _field_type_record(field_type: V2FieldType) -> object:
    """The canonical record of one Step Result payload field type."""
    if isinstance(field_type, V2ListType):
        return {"list": field_type.item}
    if isinstance(field_type, V2EnumType):
        return {"enum": list(field_type.values)}
    return str(field_type)


def _instruction_record(instruction: V2Instruction) -> dict[str, object]:
    """The canonical record of one ``instructions`` entry."""
    return {"skill": instruction.skill, "prompt": instruction.prompt}


def _agent_record(agent: V2Agent) -> dict[str, object]:
    """The canonical record of one ``agent`` executor block."""
    return {
        "instructions": [_instruction_record(item) for item in agent.instructions],
        "tools": list(agent.tools),
        "inputs": [
            {"reference": item.reference, "optional": item.optional}
            for item in agent.inputs
        ],
        "results": {
            name: {
                field: _field_type_record(field_type)
                for field, field_type in sorted(schema.items())
            }
            for name, schema in sorted(agent.results.items())
        },
    }


def _step_record(step: V2Step) -> dict[str, object]:
    """The canonical record of one Workflow Step."""
    return {
        "id": step.id,
        "agent": _agent_record(step.agent) if step.agent is not None else None,
        "operation": step.operation,
        "with_body": step.with_body,
        "with_draft": step.with_draft,
        "with_requires": [list(pair) for pair in step.with_requires],
        "max_executions": step.max_executions,
        "limits": dict(sorted(step.limits.items())),
        "reserved": step.reserved,
        "routes": {
            result: (
                {"step": target.step}
                if target.step is not None
                else {"end": target.end}
            )
            for result, target in sorted(step.routes.items())
        },
    }


def _workflow_record(workflow: V2Workflow) -> dict[str, object]:
    """The canonical record of the compiled Workflow (§2.2, §2.10)."""
    return {
        "id": workflow.id,
        "model": workflow.model,
        "input": {
            "type": workflow.input.type,
            "label": workflow.input.label,
            "require_all": list(workflow.input.require_all),
            "require_any": list(workflow.input.require_any),
            "exclude": list(workflow.input.exclude),
            "drafts": workflow.input.drafts,
            "skip_open_agent_pr": workflow.input.skip_open_agent_pr,
        },
        "ceiling": {
            "cost_usd": workflow.ceiling.cost_usd,
            "tokens": workflow.ceiling.tokens,
            "tool_calls": workflow.ceiling.tool_calls,
            "wall_clock_minutes": workflow.ceiling.wall_clock_minutes,
        },
        "max_parallel_subagents": workflow.max_parallel_subagents,
        "outcomes": {
            outcome: list(labels)
            for outcome, labels in sorted(workflow.outcomes.items())
        },
        "start": workflow.start,
        "defaults_max_executions": workflow.defaults_max_executions,
        "defaults_routes": {
            result: ({"step": target.step} if target.step is not None else {"end": target.end})
            for result, target in sorted(workflow.defaults_routes.items())
        },
        "steps": {
            step_id: _step_record(step)
            for step_id, step in sorted(workflow.steps.items())
        },
    }


def _file_record(config: V2Config, workflow: V2Workflow) -> dict[str, object]:
    """The canonical record of the file-level settings (§2.1, §2.10).

    Environment-variable names appear here; their values never do.
    """
    return {
        "target": {
            "kind": config.target.kind,
            "repository": config.target.repository,
            "base_branch": config.target.base_branch,
            "token_env": config.target.token_env,
        },
        "model": workflow.model,
        "worker": {
            "poll_interval_seconds": config.worker.poll_interval_seconds,
            "shutdown_grace_seconds": config.worker.shutdown_grace_seconds,
        },
    }


def _pinned_model_record(
    pin: PinnedModel,
    prices: TokenPrices,
    policy: PinPolicy,
    api_key_env: str,
) -> dict[str, object]:
    """The canonical record of the Attempt's Pinned Model (§2.1, §2.10).

    Direct and gateway pins alike record their identity, their price table
    and their resolved per-pin policy: the output cap and the full
    context-window config. A gateway pin keeps its full identity (gateway
    kind, canonical URL, model, endpoint, effort, output cap, threshold).
    Only the ``api_key_env`` name is recorded, never its value.
    """
    context_window = policy.context_window
    return {
        "key": pin.key,
        "provider": pin.provider,
        "gateway_kind": pin.provider if pin.is_gateway else None,
        "gateway_url": pin.gateway_url,
        "model": pin.model,
        "endpoint": pin.endpoint,
        "effort": pin.effort,
        "api_key_env": api_key_env,
        "prices": {
            "input": prices.input,
            "output": prices.output,
            "cache_read": prices.cache_read,
            "cache_write": prices.cache_write,
        },
        "output_cap_tokens": policy.output_cap,
        "context_threshold_tokens": (
            context_window.threshold_tokens if context_window is not None else None
        ),
        "context_window": (
            {
                "threshold_tokens": context_window.threshold_tokens,
                "safety_margin_tokens": context_window.safety_margin_tokens,
                "request_overhead_tokens": context_window.request_overhead_tokens,
                "handoff_token_cap": context_window.handoff_token_cap,
            }
            if context_window is not None
            else None
        ),
    }


def build_effective_configuration(
    *,
    config: V2Config,
    workflow: V2Workflow,
    prompt_texts: Mapping[str, str],
    skill_texts: Mapping[str, str],
    bundle: BundleIdentity,
    pin: PinnedModel,
    prices: TokenPrices,
    policy: PinPolicy,
    api_key_env: str,
) -> dict[str, object]:
    """The full Effective Configuration record the Claim pins (§2.10, CFG-41).

    Computed from the values the Worker loaded at startup: the validated
    YAML, the prompt texts, the installed Skill Bundle's texts and identity,
    and :func:`policy_for_pin` for the pin. A change to any of them —
    including a code-owned table entry for a direct pin — changes the
    record, and so the one SHA-256 digest the Run Ledger stores beside it.
    """
    return {
        "version": EFFECTIVE_CONFIGURATION_VERSION,
        "workflow": _workflow_record(workflow),
        "file": _file_record(config, workflow),
        "prompts": dict(sorted(prompt_texts.items())),
        "skills": dict(sorted(skill_texts.items())),
        "skill_bundle": {
            "source_identity": bundle.source_identity,
            "resolved_commit": bundle.resolved_commit,
            "member_ids": list(bundle.member_ids),
            "companion_digests": dict(sorted(bundle.companion_digests.items())),
        },
        "pinned_model": _pinned_model_record(pin, prices, policy, api_key_env),
    }


def is_full_effective_configuration(record: object) -> bool:
    """Whether ``record`` has the full CFG-41 shape (CFG-42).

    ``True`` only where every top-level section is present with the
    expected shape, including the pinned model's prices, output cap and
    full context-window config. Anything else — a missing record, JSON
    that decoded to another shape, or an Attempt opened by a build with
    the minimal record — reads ``False`` and recovery refuses it.
    """
    if not isinstance(record, Mapping):
        return False
    if record.get("version") != EFFECTIVE_CONFIGURATION_VERSION:
        return False
    for key in TOP_LEVEL_KEYS:
        if key not in record:
            return False
    workflow = record.get("workflow")
    if not isinstance(workflow, Mapping):
        return False
    if not isinstance(workflow.get("id"), str):
        return False
    workflow_input = workflow.get("input")
    if not isinstance(workflow_input, Mapping):
        return False
    if not isinstance(workflow_input.get("label"), str):
        return False
    if not isinstance(workflow.get("outcomes"), Mapping):
        return False
    file_section = record.get("file")
    if not isinstance(file_section, Mapping):
        return False
    target = file_section.get("target")
    if not isinstance(target, Mapping):
        return False
    if not isinstance(target.get("base_branch"), str):
        return False
    if not isinstance(record.get("prompts"), Mapping):
        return False
    if not isinstance(record.get("skills"), Mapping):
        return False
    bundle = record.get("skill_bundle")
    if not isinstance(bundle, Mapping):
        return False
    if not isinstance(bundle.get("resolved_commit"), str):
        return False
    pinned = record.get("pinned_model")
    if not isinstance(pinned, Mapping):
        return False
    prices = pinned.get("prices")
    if not isinstance(prices, Mapping):
        return False
    for bucket in ("input", "output", "cache_read", "cache_write"):
        if not isinstance(prices.get(bucket), (int, float)):
            return False
    if not isinstance(pinned.get("output_cap_tokens"), int):
        return False
    context_window = pinned.get("context_window")
    if not isinstance(context_window, Mapping):
        return False
    for key in (
        "threshold_tokens",
        "safety_margin_tokens",
        "request_overhead_tokens",
        "handoff_token_cap",
    ):
        if not isinstance(context_window.get(key), int):
            return False
    return True


def require_full_effective_configuration(
    record: object, attempt_id: str
) -> dict[str, Any]:
    """The pinned record for ``attempt_id``, or a startup refusal (CFG-42).

    Raises :class:`PinnedConfigurationRefused` naming the Attempt and
    telling the operator to drain it with the release that opened it —
    mirroring the legacy-Attempt rule in §16.4 — where the record is
    missing, unreadable or lacks the full shape.
    """
    if not is_full_effective_configuration(record):
        raise PinnedConfigurationRefused(
            f"the state volume holds unfinished Attempt {attempt_id} whose "
            "Effective Configuration is missing, unreadable or from an older "
            "release; drain it with the release that opened it before "
            "upgrading (step-workflow contract §2.10, §16.4)"
        )
    assert isinstance(record, Mapping)
    return dict(record)


def effective_workflow_id(record: Mapping[str, Any]) -> str:
    """The running Workflow's id from its pinned record."""
    workflow = record["workflow"]
    assert isinstance(workflow, Mapping)
    workflow_id = workflow["id"]
    assert isinstance(workflow_id, str)
    return workflow_id


def effective_input_type(record: Mapping[str, Any]) -> str:
    """The Attempt's input type from its pinned record."""
    workflow = record["workflow"]
    assert isinstance(workflow, Mapping)
    workflow_input = workflow["input"]
    assert isinstance(workflow_input, Mapping)
    input_type = workflow_input["type"]
    assert isinstance(input_type, str)
    return input_type


def effective_selection_label(record: Mapping[str, Any]) -> str:
    """The Selection Label recovery applies, from the pinned record."""
    workflow = record["workflow"]
    assert isinstance(workflow, Mapping)
    workflow_input = workflow["input"]
    assert isinstance(workflow_input, Mapping)
    label = workflow_input["label"]
    assert isinstance(label, str)
    return label


def effective_end_labels(record: Mapping[str, Any], outcome: str) -> tuple[str, ...]:
    """The Workflow's end labels for one Terminal Outcome, pinned at Claim."""
    workflow = record["workflow"]
    assert isinstance(workflow, Mapping)
    outcomes = workflow["outcomes"]
    assert isinstance(outcomes, Mapping)
    labels = outcomes.get(outcome, ())
    assert isinstance(labels, (list, tuple))
    return tuple(str(label) for label in labels)


def effective_base_branch(record: Mapping[str, Any]) -> str:
    """The configured base branch, from the pinned file-level record."""
    file_section = record["file"]
    assert isinstance(file_section, Mapping)
    target = file_section["target"]
    assert isinstance(target, Mapping)
    base_branch = target["base_branch"]
    assert isinstance(base_branch, str)
    return base_branch
