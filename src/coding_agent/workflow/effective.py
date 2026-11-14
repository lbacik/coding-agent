"""The Attempt's Effective Configuration: the full record pinned at Claim.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10 (the
Attempt records its Effective Configuration in full: the canonical compiled
Workflow, the file-level settings, every operator prompt file's bytes, every
available Skill Bundle member's provenance, inventory and selected review,
the exact activation deliveries with their framing version, and the Pinned
Model with its price table, context window and output cap — one SHA-256
digest covers all of it; environment-variable values are never part of it)
and §16 (recovery uses pinned values only).

Matrix row ids: CFG-41, CFG-42, SKV3-26, SKV3-27.

The record shape is ``version: 2`` (separate from Workflow Configuration
``version: 3``). Its identity core has the sections ``workflow``, ``file``,
``prompts``, ``skill_bundle``, ``activations`` and ``pinned_model`` and
covers every available member — not just the statically named skills'
texts. Canonical in the activation and identity resolution (issue #376).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from coding_agent.provider.config import PinPolicy
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.skillbundle.policy import PolicyReview
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
from coding_agent.workflow.v3_schema import V3Config

EFFECTIVE_CONFIGURATION_VERSION = 2
"""The shape version of the pinned Effective Configuration record (CFG-41).

``version: 2`` (separate from Workflow Configuration ``version: 3``) covers
every available Skill Bundle member — provenance, inventory, selected
review and exact activation delivery — not just the static skills' texts.
Startup recovery refuses an unfinished Attempt whose record carries another
version instead of guessing (CFG-42).
"""

TOP_LEVEL_KEYS: tuple[str, ...] = (
    "version",
    "workflow",
    "file",
    "prompts",
    "skill_bundle",
    "activations",
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
    """The identity and digest of the whole installed Skill Bundle (§2.10).

    Retained for the legacy fixed-bundle path only; the ``version: 2``
    identity core carries per-member provenance, inventory and review in
    ``skill_bundle`` instead of this summary.
    """

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

    Legacy fixed-bundle path only. The Worker verified membership, pin,
    exposure, references and companion digests before any Attempt opens, so
    the pinned record carries what was verified: the source identity, the
    resolved commit, the member ids and the companion digests.
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
    Companion Files pins those too, and no skill file is read again. A name
    the registry does not hold raises ``OSError``; the caller turns that
    into a startup refusal before any Attempt opens.
    """
    try:
        return {name: registry.bodies[name] for name in names}
    except KeyError as exc:
        raise OSError(f"skill {exc.args[0]!r} is not in the activation registry") from exc


@dataclass(frozen=True)
class InventoryFile:
    """One installed artifact file: relative path, content digest, exec bit."""

    path: str
    sha256: str
    executable: bool


@dataclass(frozen=True)
class MemberOverlay:
    """One installer Overlay record applied to a member."""

    id: str
    version: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class BundleMember:
    """One available Skill Bundle member's identity for the core (SKV3-26).

    Every declared member is pinned — including on-demand-only members no
    static instruction names. ``declared_ref`` is the entry's revision
    constraint (``None`` floats); ``resolved_commit`` is the concrete commit
    the deployment verified, so the same bytes at another commit still
    change the identity. ``files`` is the complete recursive inventory
    (including ``not-delivered`` files); ``overlays`` the applied Overlay
    identities; ``review`` the selected complete policy review with its
    dispositions, reference rulings, ordering and notices.
    """

    name: str
    source_identity: str
    resolved_commit: str
    declared_ref: str | None
    relative_source_path: str
    store_relative_path: str
    files: tuple[InventoryFile, ...]
    overlays: tuple[MemberOverlay, ...]
    review: PolicyReview


def dumps_effective_canonical(value: object) -> bytes:
    """Encode the identity core as canonical JSON bytes (SKV3-26/27).

    Sorted keys, compact separators, ``ensure_ascii=True``,
    ``allow_nan=False``, UTF-8 with no BOM and no trailing newline. Model
    numbers stay numbers (finite floats allowed for the price table;
    non-finite values refuse via ``allow_nan=False``).
    """
    text = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_effective_core(core: Mapping[str, object]) -> str:
    """SHA-256 of the canonical identity core bytes (the digest)."""
    return hashlib.sha256(dumps_effective_canonical(core)).hexdigest()


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


def _requirements_record(config: V2Config | V3Config) -> list[dict[str, object]]:
    """The normalized configuration-wide Skill Bundle Requirements (§2.10).

    Declaration order is preserved: it is the operator's stated intent the
    preparation binds. A ``version: 2``-shaped config carries none.
    """
    entries: list[dict[str, object]] = []
    for entry in getattr(config, "skills", ()):
        record: dict[str, object] = {
            "sourceIdentity": entry.source,
            "include": list(entry.include),
        }
        if entry.ref is not None:
            record["ref"] = entry.ref
        entries.append(record)
    return entries


def _file_record(config: V2Config | V3Config, workflow: V2Workflow) -> dict[str, object]:
    """The canonical record of the file-level settings (§2.1, §2.10).

    Environment-variable names appear here; their values never do. The
    normalized configuration-wide Skill Bundle Requirements ride here, so
    a membership or revision-constraint change moves the digest while
    absolute roots, image tags and producer versions stay out of it.
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
        "skills": _requirements_record(config),
    }


def _prompts_record(prompt_texts: Mapping[str, str]) -> dict[str, object]:
    """The canonical prompts section: digest and size per locator (SKV3-26).

    Keyed by the exact configured reference; the digest is over the raw
    UTF-8 bytes (re-encoded from the strictly decoded startup text, which
    round-trips), so a prompt byte change moves the digest. Blob bytes
    themselves live digest-addressed in the Attempt Archive.
    """
    record: dict[str, object] = {}
    for locator in sorted(prompt_texts.keys()):
        raw = prompt_texts[locator].encode("utf-8")
        record[locator] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    return record


def _review_record(review: PolicyReview) -> dict[str, object]:
    """The canonical selected policy review, order preserved (SKV3-26).

    ``files`` and ``references`` keep policy order: companion order and
    notice order changes move the digest. Inventory digests of
    ``not-delivered`` files are pinned here.
    """
    return {
        "sourceIdentity": review.source_identity,
        "name": review.name,
        "skillSha256": review.skill_sha256,
        "requires": list(review.requires),
        "files": [
            {
                "path": item.path,
                "sha256": item.sha256,
                "executable": item.executable,
                "disposition": item.disposition,
                "reason": item.reason,
            }
            for item in review.files
        ],
        "references": [
            {
                "origin": item.origin,
                "text": item.text,
                "classification": item.classification,
                "target": item.target,
                "reason": item.reason,
                "notice": item.notice,
            }
            for item in review.references
        ],
        "reason": review.reason,
    }


def _member_record(member: BundleMember) -> dict[str, object]:
    """The canonical record of one available member (SKV3-26).

    Inventory is sorted by UTF-8 path bytes (a set); overlays keep their
    recorded order. Verification mode, producer versions, absolute paths,
    image tags, timestamps, unused reviews and unrelated artifacts are
    never part of it.
    """
    files = sorted(member.files, key=lambda item: item.path.encode("utf-8"))
    return {
        "sourceIdentity": member.source_identity,
        "resolvedCommit": member.resolved_commit,
        "declaredRef": member.declared_ref,
        "relativeSourcePath": member.relative_source_path,
        "storeRelativePath": member.store_relative_path,
        "files": [
            {"path": item.path, "sha256": item.sha256, "executable": item.executable}
            for item in files
        ],
        "overlays": [
            {"id": item.id, "version": item.version, "paths": list(item.paths)}
            for item in member.overlays
        ],
        "review": _review_record(member.review),
    }


def _skill_bundle_record(members: Sequence[BundleMember]) -> dict[str, object]:
    """The canonical skill-bundle section: every available member (SKV3-26)."""
    return {
        "members": {
            member.name: _member_record(member) for member in sorted(members, key=lambda m: m.name)
        }
    }


def _activations_record(
    deliveries: Mapping[str, str], framing_version: int
) -> dict[str, object]:
    """The canonical activations section (SKV3-26).

    Per member: the operator-authored framing version plus the exact
    delivery's size and SHA-256. Component order, companion bytes and
    notices change the delivery digest, so framing and ordering stay
    accounted for without archiving upstream bytes in the core.
    """
    record: dict[str, object] = {}
    for name in sorted(deliveries.keys()):
        raw = deliveries[name].encode("utf-8")
        record[name] = {
            "framing_version": framing_version,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    return record


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
    config: V2Config | V3Config,
    workflow: V2Workflow,
    prompt_texts: Mapping[str, str],
    bundle_members: Sequence[BundleMember],
    activations: Mapping[str, str],
    framing_version: int,
    pin: PinnedModel,
    prices: TokenPrices,
    policy: PinPolicy,
    api_key_env: str,
) -> dict[str, object]:
    """The full Effective Configuration record the Claim pins (§2.10, CFG-41).

    Computed from the values the Worker loaded at startup: the validated
    YAML, the prompt texts, every available Skill Bundle member's
    provenance, inventory and selected review, the exact activation
    deliveries with their framing version, and :func:`policy_for_pin` for
    the pin. A change to any of them — including a code-owned table entry
    for a direct pin — changes the record, and so the one SHA-256 digest
    the Run Ledger stores beside it.
    """
    member_names = {member.name for member in bundle_members}
    activation_names = set(activations.keys())
    if member_names != activation_names:
        missing = sorted(member_names - activation_names)
        extra = sorted(activation_names - member_names)
        detail = "; ".join(
            part
            for part in (
                f"missing activations for {missing}" if missing else "",
                f"activations without members for {extra}" if extra else "",
            )
            if part
        )
        raise ValueError(f"activations must cover every available member: {detail}")
    return {
        "version": EFFECTIVE_CONFIGURATION_VERSION,
        "workflow": _workflow_record(workflow),
        "file": _file_record(config, workflow),
        "prompts": _prompts_record(prompt_texts),
        "skill_bundle": _skill_bundle_record(bundle_members),
        "activations": _activations_record(activations, framing_version),
        "pinned_model": _pinned_model_record(pin, prices, policy, api_key_env),
    }


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_hex40(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def is_full_effective_configuration(record: object) -> bool:
    """Whether ``record`` has the full CFG-41 shape (CFG-42).

    ``True`` only where every top-level section is present with the
    expected ``version: 2`` shape, including every available member's
    provenance and review, the activation framing version with delivery
    digests, the prompt digests, the pinned model's prices, output cap and
    full context-window config, and activation coverage of every member.
    Anything else — a missing record, JSON that decoded to another shape,
    or an Attempt opened by a build with the minimal or ``version: 1``
    record — reads ``False`` and recovery refuses it.
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
    prompts = record.get("prompts")
    if not isinstance(prompts, Mapping):
        return False
    for locator, entry in prompts.items():
        if not isinstance(locator, str) or not locator:
            return False
        if not isinstance(entry, Mapping):
            return False
        if not _is_hex64(entry.get("sha256")):
            return False
        size = entry.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
    bundle = record.get("skill_bundle")
    if not isinstance(bundle, Mapping):
        return False
    members = bundle.get("members")
    if not isinstance(members, Mapping):
        return False
    for name, member in members.items():
        if not isinstance(name, str) or not name:
            return False
        if not isinstance(member, Mapping):
            return False
        if not isinstance(member.get("sourceIdentity"), str):
            return False
        if not _is_hex40(member.get("resolvedCommit")):
            return False
        declared_ref = member.get("declaredRef")
        if declared_ref is not None and not isinstance(declared_ref, str):
            return False
        files = member.get("files")
        if not isinstance(files, list):
            return False
        for item in files:
            if not isinstance(item, Mapping):
                return False
            if not isinstance(item.get("path"), str):
                return False
            if not _is_hex64(item.get("sha256")):
                return False
            if type(item.get("executable")) is not bool:
                return False
        overlays = member.get("overlays")
        if not isinstance(overlays, list):
            return False
        review = member.get("review")
        if not isinstance(review, Mapping):
            return False
        if not isinstance(review.get("sourceIdentity"), str):
            return False
        if not isinstance(review.get("name"), str):
            return False
        if not _is_hex64(review.get("skillSha256")):
            return False
        if not isinstance(review.get("requires"), list):
            return False
        if not isinstance(review.get("files"), list):
            return False
        if not isinstance(review.get("references"), list):
            return False
    activations = record.get("activations")
    if not isinstance(activations, Mapping):
        return False
    for name, entry in activations.items():
        if not isinstance(name, str) or not name:
            return False
        if not isinstance(entry, Mapping):
            return False
        if type(entry.get("framing_version")) is not int:
            return False
        if not _is_hex64(entry.get("sha256")):
            return False
        size = entry.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
    if set(activations.keys()) != set(members.keys()):
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
