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

The record shape is ``version: 4`` (separate from Workflow Configuration
``version: 4``). Its identity core has the sections ``workflow``, ``file``,
``prompts``, ``skill_bundle``, ``activations`` and ``pinned_model`` — plus
an optional ``advisor_pins`` section where a step's Advisor answers on
another pin, each entry that pin's full record — and covers every available
member — not just the statically named skills' texts. Canonical in the
activation and identity resolution (issue #376).

Strict vs tolerant reads: :func:`is_full_effective_configuration` and
:func:`require_full_effective_configuration` are the strict gate — ``True``
only for the full current-version shape, and recovery refuses anything
else. Every other reader in this module (the "Typed record reads" section
below) is independently tolerant: each takes the record itself
(``Mapping | None``), walks it defensively, applies the legacy-shape rules
(``ceiling`` vs older ``limits``, ``effective_tokens`` vs older ``tokens``,
older ``max_parallel_subagents`` placement) and returns a best-effort
value, so display and operator code keeps reading records written by older
releases (ADR 0017). No code outside this module walks ``workflow.*``,
``pinned_model.*``, ``project_profile`` or ``round_handoff`` keys directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coding_agent.skillbundle.deployment import VerifiedRunInputs

from coding_agent.provider.config import PinPolicy, policy_for_pin
from coding_agent.provider.pinned_model import (
    DEFAULT_MAX_ROUNDS,
    MAX_ROUNDS_CAP,
    PinnedModel,
)
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.skillbundle.member_identity import (  # re-exported
    BundleMember as BundleMember,
    InventoryFile as InventoryFile,
    MemberOverlay as MemberOverlay,
)
from coding_agent.skillbundle.policy import PolicyReview
from coding_agent.workflow.schema import (
    Agent,
    EnumType,
    FieldType,
    Instruction,
    ListType,
    WorkflowStep,
    Workflow,
    WorkflowConfiguration,
)

EFFECTIVE_CONFIGURATION_VERSION = 4
"""The shape version of the pinned Effective Configuration record (CFG-41).

``version: 4`` (separate from Workflow Configuration ``version: 4``) covers
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


def skill_names_for_workflow(workflow: Workflow) -> list[str]:
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


def _field_type_record(field_type: FieldType) -> object:
    """The canonical record of one Step Result payload field type."""
    if isinstance(field_type, ListType):
        return {"list": field_type.item}
    if isinstance(field_type, EnumType):
        return {"enum": list(field_type.values)}
    return str(field_type)


def _instruction_record(instruction: Instruction) -> dict[str, object]:
    """The canonical record of one ``instructions`` entry."""
    return {"skill": instruction.skill, "prompt": instruction.prompt}


def _advisor_record(advisor: object) -> dict[str, object] | None:
    """The canonical record of one step's ``agent.advisor`` declaration, if any.

    ``None`` where the step declares no Advisor: steps without one record
    byte-identical bytes without the key. Where present it carries the one
    operator prompt locator and the ``models`` id answering the Advisor —
    ``None`` where the Advisor uses the Workflow's Pinned Model.
    """
    from coding_agent.workflow.schema import Advisor as _Advisor

    if advisor is None:
        return None
    assert isinstance(advisor, _Advisor)
    return {"prompt": advisor.prompt, "model": advisor.model}


def _agent_record(agent: Agent) -> dict[str, object]:
    """The canonical record of one ``agent`` executor block."""
    record: dict[str, object] = {
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
    advisor = _advisor_record(agent.advisor)
    if advisor is not None:
        record["advisor"] = advisor
    return record


def _step_record(step: WorkflowStep) -> dict[str, object]:
    """The canonical record of one Workflow Step.

    ``context_thresholds`` rides only on steps that configure the policy —
    legacy steps record byte-identical bytes without it — as ``hard_tokens``.
    """

    def _route_record(target: object) -> dict[str, object]:
        from coding_agent.workflow.schema import RouteTarget as _RouteTarget

        assert isinstance(target, _RouteTarget)
        if target.step is not None:
            route: dict[str, object] = {"step": target.step}
            if target.inherit_conversation:
                route["inherit_conversation"] = True
            return route
        end_record: dict[str, object] = {"end": target.end}
        if target.round_handoff is not None:
            annotation: dict[str, object] = {"kind": target.round_handoff.kind}
            if target.round_handoff.note is not None:
                annotation["note"] = target.round_handoff.note
            end_record["round_handoff"] = annotation
        return end_record

    record: dict[str, object] = {
        "id": step.id,
        "agent": _agent_record(step.agent) if step.agent is not None else None,
        "operation": step.operation,
        "with_body": step.with_body,
        "with_draft": step.with_draft,
        "with_requires": [list(pair) for pair in step.with_requires],
        "max_executions": step.max_executions,
        "limits": dict(sorted(step.limits.items())),
        "reserved": step.reserved,
        "context_thresholds": _thresholds_record(step),
        "routes": {
            result: _route_record(target)
            for result, target in sorted(step.routes.items())
        },
    }
    if record["context_thresholds"] is None:
        del record["context_thresholds"]
    return record


def _thresholds_record(step: WorkflowStep) -> dict[str, object] | None:
    """The canonical record of one step's Hard Context Threshold policy, if any."""
    policy = step.context_thresholds
    if policy is None:
        return None
    if policy.soft_tokens is not None:
        return {"hard_tokens": policy.hard_tokens, "soft_tokens": policy.soft_tokens}
    return {"hard_tokens": policy.hard_tokens}


def _workflow_record(
    workflow: Workflow, config: WorkflowConfiguration | None = None
) -> dict[str, object]:
    """The canonical record of the compiled Workflow (§2.2, §2.10).

    ``round_handoff`` rides only on Workflows that enable Round Handoff —
    legacy Workflows record byte-identical bytes without it — as the resolved
    ``pickup_label`` (explicit or the ``round-finished`` default) and the
    integer ``max_automatic_pickups`` limit.
    """
    record: dict[str, object] = {
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
            "effective_tokens": workflow.ceiling.effective_tokens,
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
    if workflow.round_handoff is not None:
        record["round_handoff"] = {
            "pickup_label": workflow.round_handoff.pickup_label,
            "max_automatic_pickups": workflow.round_handoff.max_automatic_pickups,
        }
    # Relay keys ride only where declared, so Workflows without a Relay
    # record byte-identical bytes (§19).
    if workflow.input.relay_label is not None:
        assert isinstance(record["input"], dict)
        record["input"]["relay_label"] = workflow.input.relay_label
    if workflow.relays and config is not None and all(
        target in config.workflows for target in workflow.relays.values()
    ):
        # Each entry names the receiving Workflow and its Relay Label, so the
        # terminal writes apply the receiver's label from the pinned record.
        record["relays"] = {
            outcome: {
                "workflow": target,
                "label": config.workflows[target].input.relay_label,
            }
            for outcome, target in sorted(workflow.relays.items())
        }
    if config is not None and workflow.input.relay_label is not None:
        sources: dict[str, list[str]] = {}
        for source_id, source in sorted(config.workflows.items()):
            outcomes = sorted(
                outcome
                for outcome, target in source.relays.items()
                if target == workflow.id
            )
            if outcomes:
                sources[source_id] = outcomes
        if sources:
            record["relay_sources"] = sources
    return record


def _requirements_record(config: WorkflowConfiguration) -> list[dict[str, object]]:
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


def _file_record(config: WorkflowConfiguration, workflow: Workflow) -> dict[str, object]:
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
    and their resolved per-pin policy: the Model Context Window, the output
    cap and the full context-window config. Every pin keeps its full
    identity (provider or gateway kind, canonical URL, model, endpoint,
    effort, window, output cap, threshold). Only the ``api_key_env`` name
    is recorded, never its value.

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
        "max_rounds": pin.max_rounds,
        "api_key_env": api_key_env,
        "prices": {
            "input": prices.input,
            "output": prices.output,
            "cache_read": prices.cache_read,
            "cache_write": prices.cache_write,
        },
        "model_context_window_tokens": policy.model_context_window_tokens,
        "output_cap_tokens": policy.output_cap,
        "context_threshold_tokens": (
            context_window.threshold_tokens if context_window is not None else None
        ),
        "context_window": (
            {
                "threshold_tokens": context_window.threshold_tokens,
                "safety_margin_tokens": context_window.safety_margin_tokens,
                "request_overhead_tokens": context_window.request_overhead_tokens,
            }
            if context_window is not None
            else None
        ),
    }
    if gateway_url_source is not None:
        record["gateway_url_source"] = gateway_url_source
    return record


def prompt_texts_from_run_inputs(
    run_inputs: VerifiedRunInputs, workflow: Workflow
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
        advisor = step.agent.advisor
        if advisor is not None and advisor.prompt not in texts:
            texts[advisor.prompt] = run_inputs.prompt_raws[advisor.prompt].decode(
                "utf-8"
            )
    return texts


def build_effective_configuration(
    run_inputs: VerifiedRunInputs,
    *,
    workflow: Workflow,
    pin: PinnedModel,
    prices: TokenPrices,
    advisor_pins: Mapping[str, PinnedModel] | None = None,
    advisor_prices: Mapping[str, TokenPrices] | None = None,
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
    instead of pinning a half-shaped record. ``advisor_pins`` /
    ``advisor_prices`` carry every referenced Advisor pin that differs from
    the Workflow's Pinned Model (``agent.advisor.model`` where set and
    different, in full as the Pinned Model is recorded, each with its own
    ``api_key_env`` and gateway source from the configuration); pins equal
    to the Workflow's pin record nothing. An Advisor pin without prices, or
    with no output cap or context-window config, raises ``ValueError`` the
    same way. A change to any Advisor pin — or to an ``advisor.prompt``'s
    bytes — changes the record, and so the digest.
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
    advisor_records: dict[str, dict[str, object]] = {}
    for pin_id in sorted((advisor_pins or {}).keys()):
        if pin_id == workflow.model:
            continue
        advisor_pin = (advisor_pins or {})[pin_id]
        try:
            advisor_price = (advisor_prices or {})[pin_id]
        except KeyError:
            raise ValueError(
                f"workflows.{workflow.id}: no prices for Advisor pin {pin_id!r}"
            ) from None
        try:
            advisor_decl = config.models[pin_id]
        except KeyError:
            raise ValueError(
                f"workflows.{workflow.id}: unknown Advisor pin {pin_id!r}"
            ) from None
        advisor_policy = policy_for_pin(advisor_pin)
        if (
            advisor_policy.output_cap is None
            or advisor_policy.context_window is None
        ):
            raise ValueError(
                f"workflows.{workflow.id}: Advisor pin {pin_id!r} resolves "
                "no output cap or context-window config; refuse the pin "
                "instead of pinning a half-shaped record"
            )
        advisor_records[pin_id] = _pinned_model_record(
            advisor_pin,
            advisor_price,
            advisor_policy,
            advisor_decl.api_key_env,
            (
                f"env:{advisor_decl.gateway_url_env}"
                if advisor_decl.gateway_url_env is not None
                else None
            ),
        )
    profile_locator = config.target.profile
    profile_text = run_inputs.loaded_config.profile_text
    project_profile: dict[str, object] | None = None
    if profile_locator is not None and profile_text is not None:
        raw = profile_text.encode("utf-8")
        project_profile = {
            "locator": profile_locator,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
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
        project_profile=project_profile,
        advisor_pins=advisor_records or None,
    )


def _effective_configuration_record(
    *,
    config: WorkflowConfiguration,
    workflow: Workflow,
    prompt_texts: Mapping[str, str],
    bundle_members: Sequence[BundleMember],
    activations: Mapping[str, str],
    framing_version: int,
    pin: PinnedModel,
    prices: TokenPrices,
    policy: PinPolicy,
    api_key_env: str,
    gateway_url_source: str | None = None,
    project_profile: dict[str, object] | None = None,
    advisor_pins: Mapping[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """The record itself, from already-derived identity inputs (CFG-41).

    The only caller is :func:`build_effective_configuration`, which derives
    every input but the pin and prices from the verified run inputs. Kept
    separate so the byte-identical shape stays reviewable in one place.
    Where the Workflow Configuration supplies the Project Profile (ADR 0030)
    the record carries an optional top-level ``project_profile`` section
    (``locator``, ``sha256``, ``size_bytes``); records pinned before that
    change carry no such key and still read as "profile from the Target
    Repository". The key joins the identity core, so the digest covers the
    contract that was used. Likewise ``advisor_pins`` rides only where at
    least one step's Advisor answers on another pin: each entry is that
    pin's full ``_pinned_model_record`` (§2.10), keyed by pin id, and the
    section joins the identity core, so the digest covers every Advisor pin.
    Records pinned before the Advisor slice carry no such key and still
    recover.
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
    record: dict[str, object] = {
        "version": EFFECTIVE_CONFIGURATION_VERSION,
        "workflow": _workflow_record(workflow, config),
        "file": _file_record(config, workflow),
        "prompts": _prompts_record(prompt_texts),
        "skill_bundle": _skill_bundle_record(bundle_members),
        "activations": _activations_record(activations, framing_version),
        "pinned_model": _pinned_model_record(
            pin, prices, policy, api_key_env, gateway_url_source
        ),
    }
    if project_profile is not None:
        record["project_profile"] = dict(project_profile)
    if advisor_pins:
        record["advisor_pins"] = {
            pin_id: dict(section) for pin_id, section in sorted(advisor_pins.items())
        }
    return record


def _is_valid_thresholds_section(value: object) -> bool:
    """Whether an optional step ``context_thresholds`` record is well shaped.

    Absent is valid (legacy steps record none); where present it carries a
    positive integer ``hard_tokens`` and an optional positive integer
    ``soft_tokens`` — nothing else. Anything else reads ``False`` and
    recovery refuses it.
    """
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    hard = value.get("hard_tokens")
    if not isinstance(hard, int) or isinstance(hard, bool) or hard < 1:
        return False
    soft = value.get("soft_tokens")
    if soft is not None and (
        not isinstance(soft, int) or isinstance(soft, bool) or soft < 1
    ):
        return False
    if set(value.keys()) - {"hard_tokens", "soft_tokens"}:
        return False
    return True


def _is_valid_advisor_declaration(value: object) -> bool:
    """Whether an optional step ``agent.advisor`` declaration is well shaped.

    Absent is valid (steps without an Advisor record none, and records
    pinned before the Advisor slice carry none); where present it carries
    the one operator prompt locator (a non-empty string) and the answering
    ``models`` id (``None`` where the Advisor uses the Pinned Model) —
    nothing else.
    """
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return False
    model = value.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        return False
    if set(value.keys()) - {"prompt", "model"}:
        return False
    return True


def _is_valid_steps_section(value: object) -> bool:
    """Whether an optional workflow ``steps`` record keeps its shape.

    Absent is valid (older readers accept a record without it); where present
    every step is a mapping whose optional ``context_thresholds`` record is
    well shaped and whose optional ``agent.advisor`` declaration is well
    shaped, so legacy steps without either keep recovering.
    """
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    for step_id, entry in value.items():
        if not isinstance(step_id, str) or not step_id:
            return False
        if not isinstance(entry, Mapping):
            return False
        if not _is_valid_thresholds_section(entry.get("context_thresholds")):
            return False
        agent = entry.get("agent")
        if isinstance(agent, Mapping):
            if not _is_valid_advisor_declaration(agent.get("advisor")):
                return False
    return True


def _is_valid_round_handoff_section(value: object) -> bool:
    """Whether an optional workflow ``round_handoff`` record is well shaped.

    Absent is valid (records pinned before Round Handoff carry none and keep
    recovering); where present it carries the resolved ``pickup_label``
    (a non-empty string) and the positive integer
    ``max_automatic_pickups`` — nothing else. Anything else reads
    ``False`` and recovery refuses it instead of guessing the series
    limit.
    """
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    pickup_label = value.get("pickup_label")
    if not isinstance(pickup_label, str) or not pickup_label:
        return False
    limit = value.get("max_automatic_pickups")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return False
    if set(value.keys()) - {"pickup_label", "max_automatic_pickups"}:
        return False
    return True


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
    expected ``version: 4`` shape, including every available member's
    provenance and review, the activation framing version with delivery
    digests, the prompt digests, the pinned model's prices, output cap and
    full context-window config, activation coverage of every member, and —
    where present — the well-shaped ``round_handoff`` enablement section
    (absent stays valid for legacy records), the well-shaped optional
    ``advisor_pins`` section (absent stays valid for records pinned before
    the Advisor slice) and the well-shaped optional ``advisor``
    declaration on steps (absent stays valid for legacy steps).
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
    if not _is_valid_round_handoff_section(workflow.get("round_handoff")):
        return False
    if not _is_valid_steps_section(workflow.get("steps")):
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
    if not _is_valid_pin_section(pinned):
        return False
    # The Advisor pins section is optional: records pinned before the
    # Advisor slice carry none and still recover (the effective record stays
    # ``version: 4``). Where present every entry is a full pin section keyed
    # by pin id, exactly as the Pinned Model is recorded.
    advisor_pins = record.get("advisor_pins")
    if advisor_pins is not None:
        if not isinstance(advisor_pins, Mapping):
            return False
        for pin_id, section in advisor_pins.items():
            if not isinstance(pin_id, str) or not pin_id:
                return False
            if not _is_valid_pin_section(section):
                return False
    # The instance-supplied profile section is optional: records pinned
    # before ADR 0030 carry none and still read as "profile from the Target
    # Repository" (the effective record stays ``version: 4``). Where present
    # it is ``{"locator": str, "sha256": hex64, "size_bytes": int>=0}``.
    project_profile = record.get("project_profile")
    if project_profile is not None:
        if not isinstance(project_profile, Mapping):
            return False
        locator = project_profile.get("locator")
        if not isinstance(locator, str) or not locator:
            return False
        if not _is_hex_digest(project_profile.get("sha256"), 64):
            return False
        size = project_profile.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
    return True


def _is_valid_pin_section(value: object) -> bool:
    """Whether one pin's record keeps its full §2.10 shape.

    Every pin — the Pinned Model and each Advisor pin — carries its price
    table, the Model Context Window, the output cap and the full
    context-window config. The gateway source marker stays optional:
    records pinned by older builds carry none and still recover; where
    present it is ``env:<NAME>``.
    """
    if not isinstance(value, Mapping):
        return False
    prices = value.get("prices")
    if not isinstance(prices, Mapping):
        return False
    for bucket in ("input", "output", "cache_read", "cache_write"):
        if not isinstance(prices.get(bucket), (int, float)):
            return False
    if not isinstance(value.get("model_context_window_tokens"), int):
        return False
    if not isinstance(value.get("output_cap_tokens"), int):
        return False
    context_window = value.get("context_window")
    if not isinstance(context_window, Mapping):
        return False
    for key in (
        "threshold_tokens",
        "safety_margin_tokens",
        "request_overhead_tokens",
    ):
        if not isinstance(context_window.get(key), int):
            return False
    gateway_url_source = value.get("gateway_url_source")
    if gateway_url_source is not None:
        if (
            not isinstance(gateway_url_source, str)
            or not gateway_url_source.startswith("env:")
            or not gateway_url_source[len("env:") :]
        ):
            return False
    if "max_rounds" not in value:
        return True
    max_rounds = value.get("max_rounds")
    if (
        type(max_rounds) is not int
        or max_rounds < 1
        or max_rounds > MAX_ROUNDS_CAP
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


def effective_relay_label(record: Mapping[str, Any]) -> str | None:
    """The pinned Relay Label the Workflow receives Relays under, if any (§19)."""
    workflow_input = _workflow_section(record).get("input")
    if not isinstance(workflow_input, Mapping):
        return None
    label = workflow_input.get("relay_label")
    return label if isinstance(label, str) and label else None


def effective_relay_target(
    record: Mapping[str, Any], outcome: str
) -> tuple[str, str] | None:
    """The ``(Workflow id, Relay Label)`` a Terminal Outcome relays to (§19).

    Read from the pinned record, never the loaded file. ``None`` where the
    outcome declares no Relay or the entry is malformed.
    """
    relays = _workflow_section(record).get("relays")
    if not isinstance(relays, Mapping):
        return None
    entry = relays.get(outcome)
    if not isinstance(entry, Mapping):
        return None
    workflow = entry.get("workflow")
    label = entry.get("label")
    if not isinstance(workflow, str) or not workflow:
        return None
    if not isinstance(label, str) or not label:
        return None
    return workflow, label


def effective_relay_declared(
    record: Mapping[str, Any], source_workflow: str, outcome: str
) -> bool:
    """Whether the pinned record declares a Relay ``source`` → this Workflow (§19).

    The receiver's own configuration vouches for the Relay: the record names
    every ``(source Workflow, outcome)`` pair that relays into it.
    """
    sources = _workflow_section(record).get("relay_sources")
    if not isinstance(sources, Mapping):
        return False
    outcomes = sources.get(source_workflow)
    return isinstance(outcomes, (list, tuple)) and outcome in outcomes


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
    profile_text: str | None = None,
) -> dict[str, bytes]:
    """Every digest-addressed blob the Claim must archive, by digest.

    Prompt blobs are the strictly-decoded startup texts re-encoded as UTF-8
    (which round-trips); delivery blobs are the exact activation bytes;
    component blobs are the delivered ``SKILL.md`` and ``inject`` companion
    bytes the deployment verified — never ``not-delivered`` bodies. Where the
    Workflow Configuration supplies the Project Profile (ADR 0030) its bytes
    join the blobs like prompt bytes, so the Attempt Archive pins the
    contract that was used. Duplicate
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
    if profile_text is not None:
        raw = profile_text.encode("utf-8")
        blobs[hashlib.sha256(raw).hexdigest()] = raw
    return blobs


# --- Typed record reads (tolerant) ---
#
# Each reader takes the Effective Configuration record itself
# (``Mapping | None``), never a status/detail wrapper, and returns a
# best-effort value for display and operator code: a missing or malformed
# record reads as absent, never as zero and never as a refusal. The
# legacy-shape rules live here and nowhere else — new keys beat legacy
# keys, ``effective_tokens`` beats raw ``tokens``, and a boolean is never
# numeric. Recovery keeps refusing through
# :func:`is_full_effective_configuration` / :func:`require_full_effective_configuration`
# above; these readers do not gate on it.


def _as_float_meter(value: object) -> float | None:
    """``value`` as a float where it is a real number, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int_meter(value: object) -> int | None:
    """``value`` as an int where it is a real number, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_usable_number(value: object) -> int | float | None:
    """``value`` as a usable token count, or ``None`` where not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _workflow_section(record: Mapping[str, object] | None) -> Mapping[str, Any]:
    """The record's ``workflow`` section, or ``{}`` where none is pinned."""
    if isinstance(record, Mapping):
        workflow = record.get("workflow")
        if isinstance(workflow, Mapping):
            return workflow
    return {}


def effective_ceiling(record: Mapping[str, object] | None) -> dict[str, Any]:
    """The Attempt Ceiling from the pinned Effective Configuration (§11.2).

    Reads the pinned ``workflow.ceiling`` (``cost_usd``,
    ``effective_tokens``, ``tool_calls``, ``wall_clock_minutes`` → seconds),
    falling back to the legacy ``workflow.limits`` shape and the pre-v4
    ``tokens`` meter rows older releases carry; a missing or non-numeric
    meter reads as ``None`` rather than zero so an absent ceiling is never
    mistaken for a spent one. The canonical reader: every ceiling consumer
    asks this question so current and legacy shapes cannot diverge again.
    """
    workflow = _workflow_section(record)
    raw_ceiling: object = workflow.get("ceiling")
    if not isinstance(raw_ceiling, Mapping):
        raw_ceiling = workflow.get("limits")
    limits: Mapping[str, object] = (
        raw_ceiling if isinstance(raw_ceiling, Mapping) else {}
    )
    wall_minutes = _as_float_meter(limits.get("wall_clock_minutes"))
    effective = _as_int_meter(limits.get("effective_tokens"))
    if effective is None:
        effective = _as_int_meter(limits.get("tokens"))
    return {
        "cost_usd": _as_float_meter(limits.get("cost_usd")),
        "effective_tokens": effective,
        "tool_calls": _as_int_meter(limits.get("tool_calls")),
        "wall_clock_seconds": (
            wall_minutes * 60.0 if wall_minutes is not None else None
        ),
    }


def effective_steps(record: Mapping[str, object] | None) -> Mapping[str, Any]:
    """The record's ``workflow.steps`` mapping, or ``{}`` where none is pinned."""
    steps = _workflow_section(record).get("steps")
    return steps if isinstance(steps, Mapping) else {}


def effective_step_entry(
    record: Mapping[str, object] | None, step_id: str | None
) -> Mapping[str, Any]:
    """The record's entry for ``step_id``, or ``{}`` where none is pinned."""
    if not step_id:
        return {}
    entry = effective_steps(record).get(step_id)
    return entry if isinstance(entry, Mapping) else {}


def step_context_policy(
    record: Mapping[str, object] | None, step_id: str | None
) -> Mapping[str, Any] | None:
    """The step's ``context_thresholds`` policy record, if configured."""
    policy = effective_step_entry(record, step_id).get("context_thresholds")
    return policy if isinstance(policy, Mapping) else None


def is_operation_step(
    record: Mapping[str, object] | None, step_id: str | None
) -> bool:
    """Whether ``step_id`` is an operation (non-agent) step."""
    entry = effective_step_entry(record, step_id)
    if not entry:
        return False
    if entry.get("operation") is not None:
        return True
    return entry.get("agent") is None


def pinned_model_section(
    record: Mapping[str, object] | None,
) -> Mapping[str, Any]:
    """The record's pinned-model section, or ``{}`` where none is pinned."""
    if isinstance(record, Mapping):
        pinned = record.get("pinned_model")
        if isinstance(pinned, Mapping):
            return pinned
    return {}


def pinned_context_window(
    record: Mapping[str, object] | None,
) -> Mapping[str, Any]:
    """The pinned ``context_window`` triple, or ``{}`` where none is pinned."""
    window = pinned_model_section(record).get("context_window")
    return window if isinstance(window, Mapping) else {}


def model_context_window_tokens(
    record: Mapping[str, object] | None,
) -> int | float | None:
    """The Pinned Model's Model Context Window, or ``None`` where unpinned.

    The pin's ``model_context_window_tokens`` (§16): the largest request
    the model accepts, published as ``context_window``.
    """
    return _as_usable_number(
        pinned_model_section(record).get("model_context_window_tokens")
    )


def pinned_context_threshold_tokens(
    record: Mapping[str, object] | None,
) -> int | float | None:
    """The pinned Context Threshold, or ``None`` where none is pinned.

    The ``context_window.threshold_tokens`` from the record (mirrored as
    ``pinned_model.context_threshold_tokens``).
    """
    threshold: object = pinned_context_window(record).get("threshold_tokens")
    if threshold is None:
        threshold = pinned_model_section(record).get("context_threshold_tokens")
    return _as_usable_number(threshold)


def record_context_margins(
    record: Mapping[str, object] | None,
) -> tuple[Any, Any]:
    """The safety margin and request overhead in force, with code defaults."""
    from coding_agent.runtime.context_budget import code_owned_margins

    default_safety, default_overhead = code_owned_margins()
    window = pinned_context_window(record)
    safety = _as_usable_number(window.get("safety_margin_tokens"))
    if safety is None:
        safety = default_safety
    overhead = _as_usable_number(window.get("request_overhead_tokens"))
    if overhead is None:
        overhead = default_overhead
    return safety, overhead


def pin_default_usable_threshold(
    record: Mapping[str, object] | None,
) -> int | float | None:
    """The pin-default Usable Threshold, or ``None`` where unpinned.

    The pin's Context Threshold less the safety margin and the request
    overhead (§16), published as ``context_threshold`` for steps without
    their own policy. Steps with a policy resolve through
    :func:`selected_usable_thresholds`.
    """
    from coding_agent.runtime.context_budget import pin_usable_threshold

    threshold = pinned_context_threshold_tokens(record)
    if threshold is None:
        return None
    safety, overhead = record_context_margins(record)
    return pin_usable_threshold(threshold, safety, overhead)


def selected_usable_thresholds(
    record: Mapping[str, object] | None, step_id: str | None
) -> tuple[Any, Any]:
    """The selected and optional hard Usable Thresholds (§16 v1.2.1).

    Returns ``(selected, hard_or_None)`` where ``selected`` is the current
    agent Step Execution's soft Usable Threshold where the step configures
    one, else its hard or the pin-default Usable Threshold. Where a step
    configures both soft and hard thresholds, the hard Usable Threshold is
    also returned for ``native.context_hard_usable_threshold_tokens``.
    Threshold figures use configured tokens.
    """
    from coding_agent.runtime.context_budget import (
        hard_usable_threshold,
        select_usable_threshold,
        soft_usable_threshold,
    )

    safety, overhead = record_context_margins(record)
    policy = step_context_policy(record, step_id)
    if policy is not None:
        hard_raw = _as_usable_number(policy.get("hard_tokens"))
        soft_raw = _as_usable_number(policy.get("soft_tokens"))
        hard_usable = (
            hard_usable_threshold(hard_raw, safety, overhead)
            if hard_raw is not None
            else None
        )
        soft_usable = (
            soft_usable_threshold(soft_raw, safety, overhead)
            if soft_raw is not None
            else None
        )
        if hard_raw is not None and soft_raw is not None:
            return (
                select_usable_threshold(soft_usable, hard_usable, None),
                hard_usable,
            )
        if hard_raw is not None:
            return (hard_usable, None)
        if soft_raw is not None:
            # Defensive: validation requires hard alongside soft.
            return (soft_usable, None)
    return (pin_default_usable_threshold(record), None)


def round_handoff_section(
    record: Mapping[str, object] | None,
) -> Mapping[str, Any]:
    """The record's ``workflow.round_handoff`` section, or ``{}`` where absent."""
    section = _workflow_section(record).get("round_handoff")
    return section if isinstance(section, Mapping) else {}


def round_handoff_policy(
    record: Mapping[str, object] | None,
) -> tuple[str, int] | None:
    """The pinned Pickup Label and series limit, if Round Handoff is enabled.

    The ``round_handoff`` enablement section (the resolved ``pickup_label``
    and the positive ``max_automatic_pickups`` limit). ``None`` where the
    record pins no well-shaped section — Round Handoff stays disabled.
    """
    section = round_handoff_section(record)
    if not section:
        return None
    pickup_label = section.get("pickup_label")
    limit = section.get("max_automatic_pickups")
    if not isinstance(pickup_label, str) or not pickup_label:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return None
    return pickup_label, limit


def route_round_handoff_kind(
    record: Mapping[str, object] | None,
    ending_step: str | None,
    step_result: str | None,
) -> str | None:
    """The taken Route's ``round_handoff`` kind, if the end publishes one."""
    if not ending_step or not step_result:
        return None
    step = effective_step_entry(record, ending_step)
    if not step:
        return None
    routes = step.get("routes")
    if not isinstance(routes, Mapping):
        return None
    route = routes.get(step_result)
    if not isinstance(route, Mapping):
        return None
    annotation = route.get("round_handoff")
    if not isinstance(annotation, Mapping):
        return None
    kind = annotation.get("kind")
    return kind if isinstance(kind, str) and kind else None


def project_profile_section(record: Mapping[str, object] | None) -> Any:
    """The record's raw ``project_profile`` section, or ``None`` where absent.

    Returns the section untouched so the caller keeps the existing
    missing/malformed semantics: absent reads as "profile from the Target
    Repository", while a malformed section stays the caller's refusal.
    """
    if not isinstance(record, Mapping):
        return None
    return record.get("project_profile")


def max_parallel_subagents(record: Mapping[str, object] | None) -> int:
    """The Attempt's ``max_parallel_subagents`` (§2.1, §8, ADR 0020).

    Reads ``workflow.max_parallel_subagents`` from the record where present
    (falling back to the legacy ``workflow.limits.max_parallel_subagents``
    rows older releases carry), else the contract default of 2. A missing,
    non-integer or ``< 1`` value falls back to 2: the strict parser refuses
    such files before any Attempt runs (C-CFG-4), so a fallback here only
    covers scripted tests and older records.
    """
    from coding_agent.workflow.schema import DEFAULT_MAX_PARALLEL_SUBAGENTS

    if record is None:
        return DEFAULT_MAX_PARALLEL_SUBAGENTS
    workflow = _workflow_section(record)
    if not workflow:
        return DEFAULT_MAX_PARALLEL_SUBAGENTS
    value: object = workflow.get("max_parallel_subagents")
    if value is None:
        limits = workflow.get("limits")
        if isinstance(limits, Mapping):
            value = limits.get("max_parallel_subagents")
    if isinstance(value, bool) or type(value) is not int or value < 1:
        return DEFAULT_MAX_PARALLEL_SUBAGENTS
    return value


def make_test_record(
    *,
    workflow_id: str = "test",
    model: str = "test-model",
    ceiling: Mapping[str, object] | None = None,
    steps: Mapping[str, Mapping[str, object]] | None = None,
    max_parallel_subagents_value: int = 2,
    round_handoff: Mapping[str, object] | None = None,
    relay_label: str | None = None,
    relays: Mapping[str, Mapping[str, str]] | None = None,
    relay_sources: Mapping[str, Sequence[str]] | None = None,
    project_profile: Mapping[str, object] | None = None,
    pin: Mapping[str, object] | None = None,
    base_branch: str = "main",
    prompt_texts: Mapping[str, str] | None = None,
    member_name: str = "test-skill",
) -> dict[str, object]:
    """A full-shape Effective Configuration record for tests (CFG-41, v4).

    Starts from a minimal record that passes
    :func:`is_full_effective_configuration` — one workflow step, one
    prompt, one Skill Bundle member with synthetic provenance/review and
    its exact activation delivery, and the pinned model with its price
    table, Model Context Window, output cap and full context-window
    config — then applies the overrides. ``ceiling`` merges over the
    default four-meter ceiling, ``steps`` replaces the default steps,
    ``pin`` merges over the default pinned-model section, and
    ``round_handoff`` / ``project_profile`` ride only where given, so
    records that predate either section stay expressible by omitting
    them. Tests that need legacy shapes (``limits``, ``tokens``) keep
    hand-building partial mappings and assert through the tolerant
    readers; everything with current-shape intent builds through here
    so partial records stop being possible.
    """
    texts = dict(prompt_texts) if prompt_texts is not None else {"prompt:main": "test prompt"}
    member_body = "test skill"
    member_digest = hashlib.sha256(member_body.encode("utf-8")).hexdigest()
    default_ceiling: dict[str, object] = {
        "cost_usd": 5.0,
        "effective_tokens": 100_000,
        "tool_calls": 200,
        "wall_clock_minutes": 60,
    }
    if ceiling is not None:
        default_ceiling.update(ceiling)
    default_steps: dict[str, object] = {
        "build": {
            "id": "build",
            "agent": {
                "instructions": [{"skill": None, "prompt": "prompt:main"}],
                "tools": [],
                "inputs": [],
                "results": {},
            },
            "operation": None,
            "with_body": False,
            "with_draft": False,
            "with_requires": [],
            "max_executions": 1,
            "limits": {},
            "reserved": False,
            "routes": {"done": {"end": "done"}},
        }
    }
    workflow_section: dict[str, object] = {
        "id": workflow_id,
        "model": model,
        "input": {
            "type": "cli",
            "label": "test",
            "require_all": [],
            "require_any": [],
            "exclude": [],
            "drafts": False,
            "skip_open_agent_pr": False,
        },
        "ceiling": default_ceiling,
        "max_parallel_subagents": max_parallel_subagents_value,
        "outcomes": {"done": ["done"]},
        "start": "build",
        "defaults_max_executions": 1,
        "defaults_routes": {},
        "steps": dict(steps) if steps is not None else default_steps,
    }
    if round_handoff is not None:
        workflow_section["round_handoff"] = dict(round_handoff)
    if relay_label is not None:
        workflow_section["input"]["relay_label"] = relay_label  # type: ignore[index]
    if relays:
        workflow_section["relays"] = {
            outcome: dict(entry) for outcome, entry in relays.items()
        }
    if relay_sources:
        workflow_section["relay_sources"] = {
            source: list(outcomes) for source, outcomes in relay_sources.items()
        }
    prompts_section: dict[str, object] = {}
    for locator in sorted(texts.keys()):
        raw = texts[locator].encode("utf-8")
        prompts_section[locator] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    default_pin: dict[str, object] = {
        "key": "test:test-model",
        "provider": "test",
        "gateway_kind": None,
        "gateway_url": None,
        "model": model,
        "endpoint": "test",
        "effort": "medium",
        "max_rounds": DEFAULT_MAX_ROUNDS,
        "api_key_env": "TEST_API_KEY",
        "prices": {
            "input": 2.00,
            "output": 10.00,
            "cache_read": 0.20,
            "cache_write": 2.50,
        },
        "model_context_window_tokens": 200_000,
        "output_cap_tokens": 32_000,
        "context_threshold_tokens": 80_000,
        "context_window": {
            "threshold_tokens": 80_000,
            "safety_margin_tokens": 1_000,
            "request_overhead_tokens": 500,
        },
    }
    if pin is not None:
        for key, value in pin.items():
            if key == "prices" and isinstance(value, Mapping):
                base_prices = default_pin["prices"]
                assert isinstance(base_prices, dict)
                merged_prices = dict(base_prices)
                merged_prices.update(value)
                default_pin["prices"] = merged_prices
            elif key == "context_window" and isinstance(value, Mapping):
                base_window = default_pin["context_window"]
                assert isinstance(base_window, dict)
                merged_window = dict(base_window)
                merged_window.update(value)
                default_pin["context_window"] = merged_window
            else:
                default_pin[key] = value
    delivery_raw = member_body.encode("utf-8")
    member_section: dict[str, object] = {
        "sourceIdentity": "git+https://example.com/org/skills-a.git",
        "resolvedCommit": "a" * 40,
        "declaredRef": None,
        "relativeSourcePath": "",
        "storeRelativePath": member_name,
        "files": [
            {
                "path": "SKILL.md",
                "sha256": member_digest,
                "executable": False,
            }
        ],
        "overlays": [],
        "review": {
            "sourceIdentity": "git+https://example.com/org/skills-a.git",
            "name": member_name,
            "skillSha256": member_digest,
            "requires": [],
            "files": [],
            "references": [],
            "reason": "synthetic review",
        },
    }
    record: dict[str, object] = {
        "version": EFFECTIVE_CONFIGURATION_VERSION,
        "workflow": workflow_section,
        "file": {
            "target": {
                "kind": "github",
                "repository": "example-owner/example-repo",
                "base_branch": base_branch,
                "token_env": "GITHUB_TOKEN",
            },
            "model": model,
            "worker": {
                "poll_interval_seconds": 5,
                "shutdown_grace_seconds": 30,
            },
            "skills": [],
        },
        "prompts": prompts_section,
        "skill_bundle": {"members": {member_name: member_section}},
        "activations": {
            member_name: {
                "framing_version": 1,
                "sha256": hashlib.sha256(delivery_raw).hexdigest(),
                "size_bytes": len(delivery_raw),
            }
        },
        "pinned_model": default_pin,
    }
    if project_profile is not None:
        record["project_profile"] = dict(project_profile)
    return record
