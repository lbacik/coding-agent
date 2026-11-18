"""The Attempt's Effective Configuration: the full record pinned at Claim.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10 (the
Attempt records its Effective Configuration in full: the canonical compiled
Workflow, the file-level settings, every operator prompt file's bytes, every
available Skill Bundle member's provenance, inventory and selected review,
the exact activation deliveries with their framing version, and the Pinned
Model with its price table, context window and output cap — one SHA-256
digest covers all of it; environment-variable values are never part of it
except the resolved gateway URL, which is not a secret and carries no
userinfo by rule) and §16 (recovery uses pinned values only).

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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coding_agent.skillbundle.deployment import VerifiedRunInputs

from coding_agent.provider.config import PinPolicy, policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.skillbundle.member_identity import (  # re-exported
    BundleMember as BundleMember,
    InventoryFile as InventoryFile,
    MemberOverlay as MemberOverlay,
)
from coding_agent.skillbundle.policy import PolicyReview
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
    gateway_url_source: str | None = None,
) -> dict[str, object]:
    """The canonical record of the Attempt's Pinned Model (§2.1, §2.10).

    Direct and gateway pins alike record their identity, their price table
    and their resolved per-pin policy: the output cap and the full
    context-window config. A gateway pin keeps its full identity (gateway
    kind, canonical URL, model, endpoint, effort, output cap, threshold).
    Only the ``api_key_env`` name is recorded, never its value.

    ``gateway_url_source`` (``env:<NAME>``) is recorded only for a pin whose
    address came from the environment at startup: a literal pin's record is
    byte-identical to the shape without it, and the digest covers the
    resolved URL either way. The resolved gateway URL is the single
    exception to "environment-variable values are never part of it"
    (§2.10): it is not a secret and carries no userinfo by rule.
    """
    context_window = policy.context_window
    record: dict[str, object] = {
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
    if gateway_url_source is not None:
        record["gateway_url_source"] = gateway_url_source
    return record


def prompt_texts_from_run_inputs(
    run_inputs: VerifiedRunInputs, workflow: V2Workflow
) -> dict[str, str]:
    """The selected Workflow's prompt texts decoded from the verified bytes.

    Run verification hashed every prompt locator's raw bytes in a single
    read; the selected Workflow's texts are those bytes decoded as UTF-8
    strictly. A locator the verification never saw, or bytes that no
    longer decode, cannot happen for a verified value and propagate as an
    error instead of refusing: verification already refused those cases.
    """
    texts: dict[str, str] = {}
    for step in workflow.steps.values():
        if step.agent is None:
            continue
        for instruction in step.agent.instructions:
            locator = instruction.prompt
            if locator is None or locator in texts:
                continue
            texts[locator] = run_inputs.prompt_raws[locator].decode("utf-8")
    return texts


def build_effective_configuration(
    run_inputs: VerifiedRunInputs,
    *,
    workflow: V2Workflow,
    pin: PinnedModel,
    prices: TokenPrices,
) -> dict[str, object]:
    """The full Effective Configuration record the Claim pins (§2.10, CFG-41).

    Built from the verified run inputs plus the pin: the validated
    configuration, the selected Workflow's prompt texts decoded from the
    verified prompt bytes, every available Skill Bundle member's
    provenance, inventory and selected review, the exact activation
    deliveries with their framing version, and :func:`policy_for_pin` for
    the pin. The pin policy, the API-key env name and the gateway URL
    source are derived inside from the pin and the configuration, so the
    only real inputs are the run inputs, the selected Workflow, the Pinned
    Model and the prices. A change to any of them — including a code-owned
    table entry for a direct pin — changes the record, and so the one
    SHA-256 digest the Run Ledger stores beside it.

    Raises ``ValueError`` where ``policy`` carries no output cap or no
    context-window config: such a record could never pass the full-shape
    check, and the supervisor refuses the pin before the Attempt opens
    instead of pinning a half-shaped record.
    """
    config = run_inputs.loaded_config.config
    try:
        model_decl = config.models[workflow.model]
    except KeyError:
        raise ValueError(
            f"workflows.{workflow.id}.model: unknown model {workflow.model!r}"
        ) from None
    gateway_url_source = (
        f"env:{model_decl.gateway_url_env}"
        if model_decl.gateway_url_env is not None
        else None
    )
    return _effective_configuration_record(
        config=config,
        workflow=workflow,
        prompt_texts=prompt_texts_from_run_inputs(run_inputs, workflow),
        bundle_members=run_inputs.members,
        activations=run_inputs.registry.bodies,
        framing_version=run_inputs.framing_version,
        pin=pin,
        prices=prices,
        policy=policy_for_pin(pin),
        api_key_env=model_decl.api_key_env,
        gateway_url_source=gateway_url_source,
    )


def _effective_configuration_record(
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
    gateway_url_source: str | None = None,
) -> dict[str, object]:
    """The record itself, from already-derived identity inputs (CFG-41).

    The only caller is :func:`build_effective_configuration`, which derives
    every input but the pin and prices from the verified run inputs. Kept
    separate so the byte-identical shape stays reviewable in one place.
    """
    if policy.output_cap is None or policy.context_window is None:
        raise ValueError(
            "policy_for_pin resolved no output cap or context-window config; "
            "refuse the pin instead of pinning a half-shaped record"
        )
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
        "pinned_model": _pinned_model_record(
            pin, prices, policy, api_key_env, gateway_url_source
        ),
    }


def _is_hex_digest(value: object, length: int) -> bool:
    """Whether ``value`` is a lowercase hex digest of ``length`` characters."""
    return (
        isinstance(value, str)
        and len(value) == length
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
        if not _is_hex_digest(entry.get("sha256"), 64):
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
        if not _is_hex_digest(member.get("resolvedCommit"), 40):
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
            if not _is_hex_digest(item.get("sha256"), 64):
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
        if not _is_hex_digest(review.get("skillSha256"), 64):
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
        if not _is_hex_digest(entry.get("sha256"), 64):
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
    # The source marker is optional: records pinned by older builds carry
    # none and still recover. Where present it is ``env:<NAME>``.
    gateway_url_source = pinned.get("gateway_url_source")
    if gateway_url_source is not None:
        if (
            not isinstance(gateway_url_source, str)
            or not gateway_url_source.startswith("env:")
            or not gateway_url_source[len("env:") :]
        ):
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


def effective_end_labels_for_all(
    record: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    """The Workflow's outcome → end-labels mapping, pinned at Claim.

    Attempt finalisation applies the labels of the outcome it actually
    records or replays, never of a decision it discarded.
    """
    workflow = record["workflow"]
    assert isinstance(workflow, Mapping)
    outcomes = workflow["outcomes"]
    assert isinstance(outcomes, Mapping)
    mapping: dict[str, tuple[str, ...]] = {}
    for outcome, labels in outcomes.items():
        assert isinstance(labels, (list, tuple))
        mapping[str(outcome)] = tuple(str(label) for label in labels)
    return mapping


def effective_base_branch(record: Mapping[str, Any]) -> str:
    """The configured base branch, from the pinned file-level record."""
    file_section = record["file"]
    assert isinstance(file_section, Mapping)
    target = file_section["target"]
    assert isinstance(target, Mapping)
    base_branch = target["base_branch"]
    assert isinstance(base_branch, str)
    return base_branch


def content_index_for_record(record: Mapping[str, object]) -> dict[str, object]:
    """The content-reference index for one pinned Effective Configuration.

    The Claim boundary pins the full record (the identity core) and its
    SHA-256 digest in the Run Ledger; this index names every digest-addressed
    blob the Attempt Archive must hold before the ledger transaction the
    Claim depends on: every prompt the Workflow needs, every available
    activation's delivery, and every available member's delivered components.
    ``not-delivered`` inventory digests are pinned here with
    ``archived: False`` — their bodies appear in no blob and no delivery.

    Shape: ``{"prompts": {locator: {...}}, "activations": {name: {...}},
    "components": {name: {path: {"sha256": ..., "archived": bool}}}}``.
    Best-effort over partial records: unknown sections read as empty, so a
    legacy or minimal record indexes nothing instead of refusing.
    """

    def _prompt_index(prompts: object) -> dict[str, object]:
        index: dict[str, object] = {}
        if not isinstance(prompts, Mapping):
            return index
        for locator, entry in prompts.items():
            if not isinstance(locator, str) or not isinstance(entry, Mapping):
                continue
            digest = entry.get("sha256")
            size = entry.get("size_bytes")
            if not isinstance(digest, str) or not digest:
                continue
            descriptor: dict[str, object] = {"sha256": digest, "archived": True}
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                descriptor["size_bytes"] = size
            index[locator] = descriptor
        return index

    def _not_delivered_paths(review: object) -> set[str]:
        paths: set[str] = set()
        if not isinstance(review, Mapping):
            return paths
        files = review.get("files")
        if not isinstance(files, list):
            return paths
        for item in files:
            if not isinstance(item, Mapping):
                continue
            if item.get("disposition") != "not-delivered":
                continue
            path = item.get("path")
            if isinstance(path, str) and path:
                paths.add(path)
        return paths

    def _component_index(bundle: object) -> dict[str, object]:
        index: dict[str, object] = {}
        if not isinstance(bundle, Mapping):
            return index
        members = bundle.get("members")
        if not isinstance(members, Mapping):
            return index
        for name, member in members.items():
            if not isinstance(name, str) or not isinstance(member, Mapping):
                continue
            files = member.get("files")
            if not isinstance(files, list):
                continue
            review = member.get("review")
            not_delivered = _not_delivered_paths(review)
            skill_digest = None
            if isinstance(review, Mapping):
                candidate = review.get("skillSha256")
                if isinstance(candidate, str) and candidate:
                    skill_digest = candidate
            per_member: dict[str, object] = {}
            if skill_digest is not None:
                per_member["SKILL.md"] = {"sha256": skill_digest, "archived": True}
            for item in files:
                if not isinstance(item, Mapping):
                    continue
                path = item.get("path")
                digest = item.get("sha256")
                if not isinstance(path, str) or not path:
                    continue
                if not isinstance(digest, str) or not digest:
                    continue
                if path == "SKILL.md" and skill_digest == digest:
                    continue
                per_member[path] = {
                    "sha256": digest,
                    "archived": path not in not_delivered,
                }
            index[name] = per_member
        return index

    def _activation_index(activations: object) -> dict[str, object]:
        index: dict[str, object] = {}
        if not isinstance(activations, Mapping):
            return index
        for name, entry in activations.items():
            if not isinstance(name, str) or not isinstance(entry, Mapping):
                continue
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not digest:
                continue
            descriptor: dict[str, object] = {"sha256": digest, "archived": True}
            size = entry.get("size_bytes")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                descriptor["size_bytes"] = size
            framing = entry.get("framing_version")
            if isinstance(framing, int) and not isinstance(framing, bool):
                descriptor["framing_version"] = framing
            index[name] = descriptor
        return index

    return {
        "prompts": _prompt_index(record.get("prompts")),
        "activations": _activation_index(record.get("activations")),
        "components": _component_index(record.get("skill_bundle")),
    }


def collect_claim_blobs(
    *,
    prompt_texts: Mapping[str, str],
    activation_deliveries: Mapping[str, str],
    activation_components: Mapping[str, Mapping[str, bytes]],
) -> dict[str, bytes]:
    """Every digest-addressed blob the Claim must archive, by digest.

    Prompt blobs are the strictly-decoded startup texts re-encoded as UTF-8
    (which round-trips); delivery blobs are the exact activation bytes;
    component blobs are the delivered ``SKILL.md`` and ``inject`` companion
    bytes the deployment verified — never ``not-delivered`` bodies. Duplicate
    bytes share one digest; the archive file is content-addressed so the
    duplicates collapse there too.
    """
    blobs: dict[str, bytes] = {}
    for locator in sorted(prompt_texts.keys()):
        raw = prompt_texts[locator].encode("utf-8")
        blobs[hashlib.sha256(raw).hexdigest()] = raw
    for name in sorted(activation_deliveries.keys()):
        raw = activation_deliveries[name].encode("utf-8")
        blobs[hashlib.sha256(raw).hexdigest()] = raw
    for name in sorted(activation_components.keys()):
        members = activation_components[name]
        for path in sorted(members.keys()):
            raw = bytes(members[path])
            blobs[hashlib.sha256(raw).hexdigest()] = raw
    return blobs
