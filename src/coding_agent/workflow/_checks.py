"""Static validation for Workflow Configuration files (`version: 4`).

`version: 3` is no longer supported — `agent config validate` and `agent run`
refuse it with a migration diagnostic (owned by
``coding_agent.workflow.configuration``). This module holds every
validation phase: keys, models, worker, workflows (phase 1), graph and
limits (phase 2), label disjointness and the ``skills`` declaration.


Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7 (strict
parsing), §2.8 (trust and paths), §2.9 (``agent config validate`` is static
and offline; every fault names its YAML path) and §3 (static validation:
Routes, the graph, limits, input type, tools and data, end labels and the
required-input proof).

Slice S1 of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: CFG-1 … CFG-39, CFG-43, CFG-44, CFG-45, CFG-46. The 35
prototype invalid cases (``tests/fixtures/workflows/invalid/``) are the
table-driven acceptance of this module, with ``examples/workflows.yaml`` as
the valid case.

Static and offline: only the YAML file, the prompt files it names and the
``target.profile`` file it names are read. No environment variable value
is read, no credential is required, and no network call is made. Every
fault is collected as a ``WorkflowConfigError`` naming its YAML path, so
the caller can name every fault.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from coding_agent.workflow._fields import (
    ID_BODY,
    FieldDescriptor,
    described_map,
    descriptors,
    section_keys,
    section_required,
    sub_keys,
    sub_required,
)
from coding_agent.workflow.instance_profile import (
    InstanceProfileError,
    read_instance_profile_text,
)
from coding_agent.workflow.parser import WorkflowConfigError, strict_load_yaml
from coding_agent.workflow.prompt_files import PromptFileError, read_prompt_text
from coding_agent.provider.pinned_model import DEFAULT_MAX_ROUNDS
from coding_agent.workflow.schema import (
    ALL_OUTCOMES,
    ALWAYS_PRESENT_TOOLS,
    CONDITIONAL_RESULTS,
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_ROUND_HANDOFF_PICKUP_LABEL,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    EFFORTS as EFFORTS,
    FIELD_TYPES,
    HARD_CONTEXT_RESULT,
    SOFT_CONTEXT_RESULT,
    INPUT_DRAFTS as INPUT_DRAFTS,
    INPUT_TYPES as INPUT_TYPES,
    INPUT_COMMENTS_REF,
    METERS,
    OPERATIONS,
    RESERVED_RESULTS,
    REQUIRABLE,
    ROUND_HANDOFF_KINDS,
    ROUND_HANDOFF_NOTE_REF,
    RUNTIME_ONLY_OUTCOMES,
    TARGET_KINDS as TARGET_KINDS,
    TOOL_GROUPS,
    YAML_ENDS,
    normalize_skill_source,
    Advisor,
    Agent,
    AttemptCeiling,
    ContextThresholds,
    WorkflowConfiguration,
    EnumType,
    FieldType,
    InputDecl,
    InputRef,
    Instruction,
    ListType,
    ModelDecl,
    ModelPrices,
    RoundHandoffAnnotation,
    RoundHandoffConfig,
    RouteTarget,
    WorkflowStep,
    TargetRepository,
    WorkerSettings,
    Workflow,
    WorkflowConfiguration,
    SkillBundleRequirement,
    default_routes_for_step,
    result_names_for_step,
    threshold_results_for_policy,
)

ROUND_HANDOFF_FIELDS = frozenset({"pickup-label", "max-automatic-pickups"})
"""Allowed `round-handoff` enablement keys (issue Workflows only, §8)."""

ID_RE = re.compile(ID_BODY)
"""What a model, Workflow, step, result or field id looks like (§2.7)."""

ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
"""What an environment-variable **name** looks like (§2.1).

Upper-snake on purpose: a secret-shaped value (``github_pat_…``,
``sk-ant-…``, anything with a dash, dot, slash, whitespace or lowercase
letter) never matches, so it is refused instead of being looked up.
"""

ROOT_FIELDS = frozenset({"version", "target", "models", "worker", "workflows"})
ROOT_REQUIRED = frozenset({"version", "target", "models", "workflows"})
TARGET_FIELDS = section_keys(TargetRepository)
"""Allowed `target` keys, derived from its field descriptors."""
MODEL_FIELDS = section_keys(ModelDecl)
"""Allowed `models.<id>` keys, derived from its field descriptors."""
MODEL_REQUIRED_COMMON = section_required(ModelDecl)
"""`models.<id>` keys required on both direct and gateway pins (§2.1)."""
GATEWAY_FIELDS = sub_keys(ModelDecl, "gateway.")
"""Allowed `gateway` keys, derived from the `gateway.*` descriptors."""
GATEWAY_REQUIRED = sub_required(ModelDecl, "gateway.")
"""Required `gateway` keys, derived from the `gateway.*` descriptors."""
PRICES_FIELDS = section_keys(ModelPrices)
"""Allowed `prices-usd-per-mtok` keys, derived from its descriptors."""
WORKER_FIELDS = section_keys(WorkerSettings)
"""Allowed `worker` keys, derived from its field descriptors."""
WORKFLOW_FIELDS = section_keys(Workflow)
"""Allowed `workflows.<id>` keys, derived from its field descriptors."""
WORKFLOW_REQUIRED = section_required(Workflow)
"""Required `workflows.<id>` keys, derived from its field descriptors."""
INPUT_FIELDS = section_keys(InputDecl)
"""Allowed `input` keys, derived from its field descriptors."""
INPUT_REQUIRED = section_required(InputDecl)
"""Required `input` keys, derived from its field descriptors."""
CEILING_FIELDS = section_keys(AttemptCeiling)
"""Allowed Attempt-Ceiling meters, derived from its field descriptors."""
CEILING_REQUIRED = section_required(AttemptCeiling)
"""Required Attempt-Ceiling meters, derived from its field descriptors."""
DEFAULTS_FIELDS = sub_keys(Workflow, "defaults.") | {"routes"}
"""Allowed `defaults` keys: the `defaults.*` descriptors plus the structural
`routes` map (route targets stay hand-written)."""
STEP_FIELDS = section_keys(WorkflowStep) | {"with"}
"""Allowed step keys: the step descriptors plus the structural `with` block
(per-operation `with` keys stay hand-written, so `with` has no descriptor)."""
STEP_REQUIRED = section_required(WorkflowStep)
"""Required step keys, derived from its field descriptors (`routes` only)."""
STEP_LIMITS_FIELDS = frozenset(METERS)
"""Allowed Step-Allowance meters: the closed code-owned meter vocabulary.

The meters can never gain a field without changing limit arithmetic, so --
unlike file sections -- they stay a constant, not descriptors. Each meter's
leaf check and schema fragment still match the generic `positive_number`
kind.
"""

STEP_THRESHOLD_FIELDS = frozenset({"hard-tokens", "soft-tokens"})
"""Allowed `context-thresholds` keys: the required hard bound plus the
optional soft bound (§3)."""
AGENT_FIELDS = frozenset({"instructions", "tools", "inputs", "results", "advisor"})
ADVISOR_FIELDS = frozenset({"prompt", "model"})
"""Allowed `agent.advisor` keys: the Advisor's prompt file and its pin (§9)."""
WITH_FIELDS = frozenset({"body", "draft", "requires"})

UNSUPPORTED_STEP_KEYS = {
    "model": "a per-step `model` is reserved for a later release; "
    "the Attempt has one Pinned Model",
    "when": "conditions/expressions are not supported; "
    "declare distinct Step Results instead",
    "if": "conditions/expressions are not supported; "
    "declare distinct Step Results instead",
    "condition": "conditions/expressions are not supported; "
    "declare distinct Step Results instead",
    "parallel": "fan-out/fan-in is deferred to a later release",
    "fan-out": "fan-out/fan-in is deferred to a later release",
    "fan-in": "fan-out/fan-in is deferred to a later release",
    "join": "fan-out/fan-in is deferred to a later release",
    "workflow": "nested Workflows are deferred to a later release",
    "retry": "there is no retry construct; route the result back to the step "
    "(bounded by max-executions)",
    "on-limit": "limit handlers are Routes: route `limit-exceeded`",
}
"""Supported-syntax refusals with a targeted message (§2.7)."""


class Problems:
    """The collected faults of one file; every fault names its YAML path."""

    def __init__(self) -> None:
        self.items: list[WorkflowConfigError] = []

    def add(self, path: str, detail: str) -> None:
        self.items.append(WorkflowConfigError(path, detail))

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


def code_owned_tables() -> tuple[frozenset[tuple[str, str]], dict[str, frozenset[str]]]:
    """The code-owned (provider, model) table, imported lazily so this offline
    module pays no provider-adapter import at load time.

    Pairs come from the pinned models; the admissible endpoint per provider
    comes from the shared protocol vocabulary through the per-provider table
    (``PROVIDER_ENDPOINTS``), so a wrong endpoint is refused even for a known
    pair.
    """
    from coding_agent.provider.config import PINNED_MODELS
    from coding_agent.provider.pinned_model import PROVIDER_ENDPOINTS

    pairs: set[tuple[str, str]] = set()
    for pin in PINNED_MODELS.values():
        pairs.add((pin.provider, pin.model))
    endpoints: dict[str, frozenset[str]] = {
        str(provider): frozenset({str(endpoint)})
        for provider, endpoint in PROVIDER_ENDPOINTS.items()
    }
    return frozenset(pairs), endpoints


def gateway_code_owned() -> tuple[
    frozenset[str], dict[str, str], int, int, Callable[[str], str]
]:
    """The code-owned gateway tables, imported lazily like the provider pair.

    Returns the admissible gateway kinds, the admissible endpoint per kind,
    the safety margin, the request overhead and the URL canonicaliser. The validator stays free of the provider-adapter import at
    load time.
    """
    from coding_agent.provider.pinned_model import (
        REQUEST_OVERHEAD_TOKENS,
        SAFETY_MARGIN_TOKENS,
        GATEWAY_ENDPOINTS,
        GATEWAY_KINDS,
        canonical_gateway_url,
    )

    kinds: frozenset[str] = frozenset(str(kind) for kind in GATEWAY_KINDS)
    endpoints: dict[str, str] = {
        str(kind): str(endpoint) for kind, endpoint in GATEWAY_ENDPOINTS.items()
    }
    return (
        kinds,
        endpoints,
        SAFETY_MARGIN_TOKENS,
        REQUEST_OVERHEAD_TOKENS,
        canonical_gateway_url,
    )


# --- Scalar helpers ------------------------------------------------------------


def is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def mapping_at(
    value: Any, where: str, problems: Problems, what: str
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        problems.add(where, f"expected a mapping, got {type(value).__name__}")
        return None
    return value


def nonempty_str_at(
    value: Any, where: str, problems: Problems, what: str
) -> str | None:
    if not isinstance(value, str) or not value:
        problems.add(where, f"{what} must be a non-empty string")
        return None
    return value


def bool_at(value: Any, where: str, problems: Problems, what: str) -> bool | None:
    if not isinstance(value, bool):
        problems.add(where, f"{what} must be a boolean, got {value!r}")
        return None
    return value


def positive_number_at(
    value: Any, where: str, problems: Problems, what: str
) -> float | None:
    if not is_number(value) or float(value) <= 0:
        problems.add(where, f"{what} must be a positive number, got {value!r}")
        return None
    return float(value)


def _is_plain_int(value: Any) -> TypeGuard[int]:
    """Whether ``value`` is a plain ``int``: bools are never integers here."""
    return type(value) is int


def positive_int_at(value: Any, where: str, problems: Problems, what: str) -> int | None:
    if not _is_plain_int(value) or value < 1:
        problems.add(where, f"{what} must be a positive integer, got {value!r}")
        return None
    return value


def int_ge_1_at(value: Any, where: str, problems: Problems, what: str) -> int | None:
    """An integer `>= 1` that is never a bool, float or string (CFG-46)."""
    if not _is_plain_int(value) or value < 1:
        problems.add(where, f"{what} must be an integer >= 1, got {value!r}")
        return None
    return value


def int_1_32_at(value: Any, where: str, problems: Problems, what: str) -> int | None:
    """A pin round bound: a plain integer between 1 and the code-owned cap.

    Bools, floats and strings are refused as type errors; ``0`` and
    negatives name the ``>= 1`` floor; values above the cap name the full
    ``1–32`` range, so ``40`` reads as a range violation rather than a type
    error.
    """
    from coding_agent.provider.pinned_model import MAX_ROUNDS_CAP

    if not _is_plain_int(value):
        problems.add(
            where,
            f"{what} must be an integer between 1 and {MAX_ROUNDS_CAP}, "
            f"got {value!r}",
        )
        return None
    if value < 1:
        problems.add(where, f"{what} must be an integer >= 1, got {value!r}")
        return None
    if value > MAX_ROUNDS_CAP:
        problems.add(
            where,
            f"{what} must be an integer between 1 and {MAX_ROUNDS_CAP}, "
            f"got {value!r}",
        )
        return None
    return value


def str_list_at(
    value: Any, where: str, problems: Problems, what: str
) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry for entry in value
    ):
        problems.add(where, f"{what} must be a list of non-empty strings")
        return None
    return list(value)


def check_value(
    problems: Problems, where: str, value: Any, desc: FieldDescriptor
) -> Any | None:
    """Check one present leaf value against its descriptor kind.

    ``custom`` fields never reach this helper: their check stays
    hand-written in the section validator, in its exact position.
    """
    if desc.kind == "nonempty_str":
        return nonempty_str_at(value, where, problems, desc.label)
    if desc.kind == "bool":
        return bool_at(value, where, problems, desc.label)
    if desc.kind == "positive_number":
        return positive_number_at(value, where, problems, desc.label)
    if desc.kind == "positive_int":
        return positive_int_at(value, where, problems, desc.label)
    if desc.kind == "int_ge_1":
        return int_ge_1_at(value, where, problems, desc.label)
    if desc.kind == "int_1_32":
        return int_1_32_at(value, where, problems, desc.label)
    if desc.kind == "str_list":
        return str_list_at(value, where, problems, desc.label)
    raise AssertionError(
        f"custom field {desc.key!r} is checked by hand-written code, "
        "never by the generic leaf pass"
    )


def check_leaf(
    problems: Problems,
    mapping: Mapping[str, Any],
    where: str,
    desc: FieldDescriptor,
) -> Any | None:
    """Check one leaf of a validated mapping against its descriptor.

    A missing optional leaf is skipped (``None``, no fault): its default
    applies. Anything else runs :func:`check_value` — including a missing
    required leaf, whose ``None`` fails exactly like any bad value (after
    :func:`check_keys` has recorded the missing-field fault itself).
    """
    leaf = desc.key.split(".")[-1]
    if leaf not in mapping and not desc.required:
        return None
    path = f"{where}.{leaf}" if where else leaf
    return check_value(problems, path, mapping.get(leaf), desc)


def check_leaves(
    problems: Problems,
    mapping: Mapping[str, Any],
    where: str,
    descs: tuple[FieldDescriptor, ...],
    *,
    stop_on_first: bool,
) -> dict[str, Any] | None:
    """Check leaf fields in descriptor order, keyed by descriptor key.

    ``stop_on_first`` mirrors the section's short-circuit behaviour:
    worker, prices and the Attempt ceiling stop at the first bad leaf,
    while target and model leaves collect every fault. Returns ``None``
    when any leaf failed.
    """
    checked: dict[str, Any] = {}
    failed = False
    for desc in descs:
        leaf = desc.key.split(".")[-1]
        value = check_leaf(problems, mapping, where, desc)
        if value is None:
            if leaf not in mapping and not desc.required:
                continue
            failed = True
            if stop_on_first:
                return None
            continue
        checked[desc.key] = value
    return None if failed else checked


def check_env_name(
    problems: Problems, where: str, value: str | None
) -> str | None:
    """Refuse a secret-shaped value where an env-var name belongs (§2.1).

    Runs after the leaf's own check, in the section's exact position: the
    ``target.kind``/``effort`` membership faults precede it, exactly as
    before.
    """
    if value is None:
        return None
    if ENV_NAME_RE.fullmatch(value) is None:
        problems.add(
            where,
            "looks like a secret value, not an environment-variable name",
        )
        return None
    return value


def check_keys(
    problems: Problems,
    where: str,
    obj: Any,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Refuse unknown fields (with targeted hints at step level) and report
    missing required fields. Returns the mapping, or None when it is not one."""
    mapping = mapping_at(problems=problems, value=obj, what="expected a mapping", where=where)
    if mapping is None:
        return None
    for key in mapping:
        if not isinstance(key, str) or key not in allowed:
            hint = UNSUPPORTED_STEP_KEYS.get(key) if isinstance(key, str) else None
            problems.add(
                where,
                f"unsupported field {key!r}" + (f" ({hint})" if hint else ""),
            )
    for key in sorted(required):
        if key not in mapping:
            key_path = f"{where}.{key}" if where else key
            problems.add(key_path, f"missing required field {key!r}")
    return mapping


def check_id(value: Any, where: str, problems: Problems, what: str) -> str | None:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        problems.add(where, f"{what} id {value!r} must match [a-z0-9]+(-[a-z0-9]+)*")
        return None
    return value


# --- References ----------------------------------------------------------------


def parse_ref(value: Any) -> tuple[str, str, str | None] | None:
    """`<step>.facts` or `<step>.<result>.<field>`; None when not a reference."""
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if not parts or any(ID_RE.fullmatch(part) is None for part in parts):
        return None
    if len(parts) == 2 and parts[1] == "facts":
        return (parts[0], "facts", None)
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    return None


def agent_field_type(
    steps: Mapping[str, Any], step_id: str, result: str, field_name: str
) -> FieldType | None:
    step = steps.get(step_id)
    if not isinstance(step, dict) or "agent" not in step:
        return None
    agent = step.get("agent")
    if not isinstance(agent, dict):
        return None
    results = agent.get("results")
    if not isinstance(results, dict):
        return None
    fields = results.get(result)
    if not isinstance(fields, dict):
        return None
    raw = fields.get(field_name)
    if isinstance(raw, str):
        return raw if raw in FIELD_TYPES else None
    if isinstance(raw, dict) and set(raw) == {"list"} and raw["list"] in FIELD_TYPES:
        return ListType(item=str(raw["list"]))
    if (
        isinstance(raw, dict)
        and set(raw) == {"enum"}
        and isinstance(raw["enum"], list)
        and raw["enum"]
        and all(isinstance(entry, str) for entry in raw["enum"])
    ):
        return EnumType(values=tuple(raw["enum"]))
    return None


def check_ref(
    problems: Problems,
    where: str,
    steps: Mapping[str, Any],
    ref: Any,
    *,
    document_only: bool,
) -> tuple[str, str, str | None] | None:
    parsed = parse_ref(ref)
    if parsed is None:
        problems.add(
            where,
            f"{ref!r} is not a reference "
            "(`<step>.facts` or `<step>.<result>.<field>`); "
            "expressions are not supported",
        )
        return None
    step_id, result, field_name = parsed
    if step_id not in steps:
        problems.add(where, f"{ref!r} names unknown step {step_id!r}")
        return None
    if result == "facts":
        if document_only:
            problems.add(
                where,
                f"{ref!r}: an Execution Summary cannot be published; "
                "use a `document` field",
            )
            return None
        return parsed
    if field_name is None:
        problems.add(where, f"{ref!r} is not a payload field of {step_id!r}")
        return None
    field_type = agent_field_type(steps, step_id, result, field_name)
    if field_type is None:
        problems.add(
            where,
            f"{ref!r} is not a payload field of a model-declared result of {step_id!r}",
        )
        return None
    if document_only and field_type != "document":
        type_name = (
            field_type
            if isinstance(field_type, str)
            else ("{list: ...}" if isinstance(field_type, ListType) else "{enum: [...]}")
        )
        problems.add(
            where,
            f"{ref!r} has type {type_name!r}; publication bodies come only "
            "from `document` fields (Step Artifacts)",
        )
        return None
    return parsed


# --- Results and payloads --------------------------------------------------------


def check_payload(
    problems: Problems,
    where: str,
    results: Any,
    *,
    opted_in: bool = False,
) -> dict[str, dict[str, FieldType]] | None:
    """Validate one agent step's declared Step Results (§3).

    ``opted_in`` is whether the step carries a ``context-thresholds`` policy:
    on such a step both threshold spellings are reserved to the runtime and
    cannot be declared, while steps without the policy keep them as ordinary
    business results (``soft-context-threshold`` is reserved only where the
    policy configures ``soft-tokens``, but a declaration is refused on any
    opted-in step so the spelling can never shadow the runtime result).
    """
    if not isinstance(results, dict) or not results:
        problems.add(where, "an agent step declares at least one Step Result")
        return None
    parsed: dict[str, dict[str, FieldType]] = {}
    for name, fields in results.items():
        result_where = f"{where}.{name}"
        if name in RESERVED_RESULTS or name in CONDITIONAL_RESULTS:
            problems.add(
                result_where,
                f"{name!r} is a runtime-reserved result and cannot be declared",
            )
            continue
        if opted_in and name in (HARD_CONTEXT_RESULT, SOFT_CONTEXT_RESULT):
            problems.add(
                result_where,
                f"{name!r} is reserved to the runtime on a step with "
                "`context-thresholds` and cannot be declared",
            )
            continue
        if check_id(name, result_where, problems, "result") is None:
            continue
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            problems.add(result_where, "a result payload is a mapping of field -> type")
            continue
        parsed_fields: dict[str, FieldType] = {}
        for field_name, field_type in fields.items():
            field_where = f"{result_where}.{field_name}"
            if check_id(field_name, field_where, problems, "field") is None:
                continue
            if isinstance(field_type, str) and field_type in FIELD_TYPES:
                parsed_fields[field_name] = field_type
            elif (
                isinstance(field_type, dict)
                and set(field_type) == {"list"}
                and field_type["list"] in FIELD_TYPES
            ):
                parsed_fields[field_name] = ListType(item=str(field_type["list"]))
            elif (
                isinstance(field_type, dict)
                and set(field_type) == {"enum"}
                and isinstance(field_type["enum"], list)
                and field_type["enum"]
                and all(isinstance(entry, str) for entry in field_type["enum"])
            ):
                parsed_fields[field_name] = EnumType(values=tuple(field_type["enum"]))
            else:
                problems.add(
                    field_where,
                    f"unknown field type {field_type!r} (use one of "
                    f"{', '.join(FIELD_TYPES)}, {{list: T}}, {{enum: [...]}})",
                )
        parsed[str(name)] = parsed_fields
    return parsed


def parse_round_handoff_annotation(
    problems: Problems, where: str, raw: Any
) -> RoundHandoffAnnotation | None:
    """Parse the structural shape of a step-local `round-handoff` annotation.

    Checks the mapping shape only (a mapping, known keys, a known kind, a
    note exactly where the kind requires one). Placement (issue Workflow,
    `commit-and-push` step, `pushed`/`no-changes` result, `unfinished` end)
    and the note's document proof stay with the step validator, which knows
    the step's context.
    """
    mapping = mapping_at(raw, where, problems, "'round-handoff'")
    if mapping is None:
        return None
    for key in mapping:
        if key not in ("kind", "note"):
            problems.add(where, f"unsupported field {key!r} in 'round-handoff'")
    kind = mapping.get("kind")
    if kind not in ROUND_HANDOFF_KINDS:
        problems.add(
            where,
            f"'round-handoff' kind must be one of "
            f"{', '.join(sorted(ROUND_HANDOFF_KINDS))}, got {kind!r}",
        )
        return None
    assert isinstance(kind, str)
    if kind == "hard":
        if "note" in mapping:
            problems.add(
                where, "'note' is only for `ai-assisted` round handoffs, not `hard`"
            )
            return None
        return RoundHandoffAnnotation(kind="hard")
    note = mapping.get("note")
    if not isinstance(note, str) or not note:
        problems.add(
            where, "'note' must be a non-empty `document` field reference "
            "(`<step>.<result>.<field>`) for `ai-assisted` round handoffs"
        )
        return None
    return RoundHandoffAnnotation(kind="ai-assisted", note=note)


def parse_route_target(
    problems: Problems, where: str, target: Any
) -> RouteTarget | None:
    if isinstance(target, list):
        problems.add(where, "a Route has exactly one target; fan-out is deferred")
        return None
    if isinstance(target, dict):
        keys = set(target)
        if keys == {"end"} or keys == {"end", "round-handoff"}:
            category = target["end"]
            if not isinstance(category, str):
                problems.add(where, f"unknown end category {category!r}")
                return None
            annotation: RoundHandoffAnnotation | None = None
            if "round-handoff" in target:
                annotation = parse_round_handoff_annotation(
                    problems, f"{where}.round-handoff", target["round-handoff"]
                )
                if annotation is None:
                    return None
            return RouteTarget(end=category, round_handoff=annotation)
        if "round-handoff" in keys:
            problems.add(
                where,
                "'round-handoff' annotates only an `unfinished` Workflow End "
                "(`{end: unfinished, round-handoff: {...}}`)",
            )
            return None
        if "step" in keys:
            if keys - {"step", "inherit-conversation"}:
                problems.add(
                    where,
                    "a step Route is a step id or "
                    "`{step: <id>, inherit-conversation: <bool>}`",
                )
                return None
            step_id = target["step"]
            if not isinstance(step_id, str):
                problems.add(where, f"unknown target {target!r}")
                return None
            if "inherit-conversation" not in target:
                return RouteTarget(step=step_id)
            flag = target["inherit-conversation"]
            if type(flag) is not bool:
                problems.add(
                    where,
                    f"'inherit-conversation' must be a boolean, got {flag!r}",
                )
                return None
            return RouteTarget(step=step_id, inherit_conversation=flag)
        problems.add(where, "a Workflow End is written `{end: <category>}`")
        return None
    if isinstance(target, str):
        return RouteTarget(step=target)
    problems.add(where, f"unknown target {target!r}")
    return None


# --- File-level blocks -----------------------------------------------------------


def validate_target(
    problems: Problems, data: Mapping[str, Any], base: Path
) -> TargetRepository | None:
    raw = data.get("target")
    mapping = check_keys(
        problems, "target", raw, TARGET_FIELDS, section_required(TargetRepository)
    )
    if mapping is None:
        return None
    descs = described_map(TargetRepository)
    # `kind` is a `custom` leaf: its non-empty check stays hand-written here
    # (in today's position), with the membership refusal below. Only the
    # generic leaves go through the descriptor pass.
    kind = nonempty_str_at(
        mapping.get("kind"), "target.kind", problems, "'target.kind'"
    )
    repository = check_leaf(problems, mapping, "target", descs["repository"])
    base_branch = check_leaf(problems, mapping, "target", descs["base-branch"])
    token_env = check_leaf(problems, mapping, "target", descs["token-env"])
    if kind is not None and kind not in TARGET_KINDS:
        problems.add(
            "target.kind", f"unsupported target kind {kind!r}: must be 'github'"
        )
    token_env = check_env_name(problems, "target.token-env", token_env)
    # `profile` is optional: where present it must name a readable
    # strict-UTF-8 file inside the configuration directory, by the same
    # path rules as `prompt:` locators (ADR 0030). Parsing and the
    # toolchain check belong to `config validate` and the startup
    # `project-profile` phase, not to this shape pass.
    profile: str | None = None
    profile_ok = True
    if "profile" in mapping:
        profile = check_leaf(problems, mapping, "target", descs["profile"])
        if profile is None:
            profile_ok = False
        else:
            try:
                read_instance_profile_text(base, profile)
            except InstanceProfileError as exc:
                problems.add("target.profile", str(exc))
                profile_ok = False
    if (
        kind is None
        or repository is None
        or base_branch is None
        or token_env is None
        or not profile_ok
    ):
        return None
    return TargetRepository(
        kind=kind,
        repository=repository,
        base_branch=base_branch,
        token_env=token_env,
        profile=profile,
    )


def validate_models(
    problems: Problems, data: Mapping[str, Any]
) -> dict[str, ModelDecl] | None:
    raw = data.get("models")
    mapping = mapping_at(raw, "models", problems, "'models'")
    if mapping is None:
        return None
    if not mapping:
        problems.add("models", "'models' must declare at least one model")
        return None
    parsed: dict[str, ModelDecl] = {}
    for model_id in mapping:
        model_path = f"models.{model_id}"
        if check_id(model_id, model_path, problems, "model") is None:
            continue
        entry = check_keys(
            problems, model_path, mapping[model_id], MODEL_FIELDS, MODEL_REQUIRED_COMMON
        )
        if entry is None:
            continue
        has_provider = "provider" in entry
        has_gateway = "gateway" in entry
        if has_provider == has_gateway:
            problems.add(
                model_path,
                "exactly one of 'provider' or 'gateway' is required",
            )
            continue
        descs = described_map(ModelDecl)
        # `provider`, `model`, `endpoint` and `effort` are `custom` leaves:
        # their non-empty checks stay hand-written here (in today's positions),
        # with the effort-membership refusal below. Only the generic leaves go
        # through the descriptor pass.
        model = nonempty_str_at(
            entry.get("model"), f"{model_path}.model", problems, "'model'"
        )
        endpoint = nonempty_str_at(
            entry.get("endpoint"), f"{model_path}.endpoint", problems, "'endpoint'"
        )
        effort = nonempty_str_at(
            entry.get("effort"), f"{model_path}.effort", problems, "'effort'"
        )
        api_key_env = check_leaf(problems, entry, model_path, descs["api-key-env"])
        prices_raw = entry.get("prices-usd-per-mtok")
        prices_mapping = check_keys(
            problems,
            f"{model_path}.prices-usd-per-mtok",
            prices_raw,
            PRICES_FIELDS,
            PRICES_FIELDS,
        )
        prices: dict[str, float] | None = None
        if prices_mapping is not None:
            prices = check_leaves(
                problems,
                prices_mapping,
                f"{model_path}.prices-usd-per-mtok",
                descriptors(ModelPrices),
                stop_on_first=True,
            )
        if effort is not None and effort not in EFFORTS:
            problems.add(
                f"{model_path}.effort",
                f"unknown effort {effort!r}: must be 'low', 'medium' or 'high'",
            )
            effort = None
        api_key_env = check_env_name(
            problems, f"{model_path}.api-key-env", api_key_env
        )
        provider: str | None = None
        gateway_kind: str | None = None
        gateway_url: str | None = None
        gateway_url_env: str | None = None
        if has_gateway:
            gateway = validate_gateway(
                problems, model_path, entry.get("gateway")
            )
            if gateway is None:
                continue
            gateway_kind, gateway_url, gateway_url_env = gateway
        else:
            provider = nonempty_str_at(
                entry.get("provider"), f"{model_path}.provider", problems, "'provider'"
            )
        # Required on every pin, direct or gateway: a missing key fails
        # exactly like a bad value, so `check_value` — never the
        # skip-if-missing `check_leaf` — runs here.
        output_cap = check_value(
            problems,
            f"{model_path}.output-cap-tokens",
            entry.get("output-cap-tokens"),
            descs["output-cap-tokens"],
        )
        threshold = check_value(
            problems,
            f"{model_path}.context-threshold-tokens",
            entry.get("context-threshold-tokens"),
            descs["context-threshold-tokens"],
        )
        window = check_value(
            problems,
            f"{model_path}.context-window-tokens",
            entry.get("context-window-tokens"),
            descs["context-window-tokens"],
        )
        if threshold is not None:
            _, _, safety, overhead, _ = gateway_code_owned()
            if threshold <= safety + overhead:
                problems.add(
                    f"{model_path}.context-threshold-tokens",
                    f"'context-threshold-tokens' ({threshold}) must exceed "
                    f"the code-owned safety margin ({safety}) plus request "
                    f"overhead ({overhead})",
                )
                threshold = None
        if (
            output_cap is not None
            and threshold is not None
            and window is not None
            and threshold + output_cap > window
        ):
            problems.add(
                f"{model_path}.context-threshold-tokens",
                f"'context-threshold-tokens' ({threshold}) plus "
                f"'output-cap-tokens' ({output_cap}) must not exceed "
                f"'context-window-tokens' ({window})",
            )
            threshold = None
        # The pin's round bound: optional, defaulting to the code-owned
        # default. A present-but-invalid value records its fault above and
        # falls back, exactly like an invalid `max-parallel-subagents`.
        max_rounds_raw = check_leaf(problems, entry, model_path, descs["max-rounds"])
        max_rounds = (
            max_rounds_raw
            if type(max_rounds_raw) is int
            else DEFAULT_MAX_ROUNDS
        )
        if (
            model is None
            or endpoint is None
            or effort is None
            or api_key_env is None
            or prices is None
        ):
            continue
        if has_gateway:
            if gateway_kind is None or (gateway_url is None and gateway_url_env is None):
                continue
            if output_cap is None or threshold is None or window is None:
                continue
            parsed[str(model_id)] = ModelDecl(
                id=str(model_id),
                provider=None,
                model=model,
                endpoint=endpoint,
                effort=effort,
                api_key_env=api_key_env,
                prices=ModelPrices(
                    input=prices["input"],
                    output=prices["output"],
                    cache_read=prices["cache-read"],
                    cache_write=prices["cache-write"],
                ),
                gateway_kind=gateway_kind,
                gateway_url=gateway_url,
                gateway_url_env=gateway_url_env,
                output_cap_tokens=output_cap,
                context_threshold_tokens=threshold,
                context_window_tokens=window,
                max_rounds=max_rounds,
            )
        else:
            if provider is None:
                continue
            if output_cap is None or threshold is None or window is None:
                continue
            parsed[str(model_id)] = ModelDecl(
                id=str(model_id),
                provider=provider,
                model=model,
                endpoint=endpoint,
                effort=effort,
                api_key_env=api_key_env,
                prices=ModelPrices(
                    input=prices["input"],
                    output=prices["output"],
                    cache_read=prices["cache-read"],
                    cache_write=prices["cache-write"],
                ),
                gateway_kind=None,
                gateway_url=None,
                gateway_url_env=None,
                output_cap_tokens=output_cap,
                context_threshold_tokens=threshold,
                context_window_tokens=window,
                max_rounds=max_rounds,
            )
    return parsed


def validate_gateway(
    problems: Problems, model_path: str, raw: Any
) -> tuple[str, str | None, str | None] | None:
    """Validate a gateway pin's ``gateway`` block (§2.1).

    Returns the (kind, canonical URL, URL-env name) triple, with exactly one
    of the URL or the URL-env name set; ``None`` when unusable. A literal
    URL is canonicalised (lowercase scheme and host, no trailing ``/``), so
    two spellings of one gateway URL produce the same record. A ``url-env``
    value is only a name — the variable is never read here, so offline
    commands pass with it unset. A value-shaped name is refused like any
    other secret-shaped name.
    """
    gateway_path = f"{model_path}.gateway"
    mapping = check_keys(problems, gateway_path, raw, GATEWAY_FIELDS, GATEWAY_REQUIRED)
    if mapping is None:
        return None
    kinds, _, _, _, canonicalise = gateway_code_owned()
    gateway_descs = described_map(ModelDecl)
    # `kind` and `url` are `custom` leaves: their checks stay hand-written
    # here (in today's positions). Only the generic `url-env` leaf goes
    # through the descriptor pass.
    kind = nonempty_str_at(
        mapping.get("kind"), f"{gateway_path}.kind", problems, "'gateway.kind'"
    )
    if kind is not None and kind not in kinds:
        problems.add(
            f"{gateway_path}.kind",
            f"unknown gateway kind {kind!r}: must be one of "
            f"{', '.join(sorted(kinds))}",
        )
        kind = None
    has_url = "url" in mapping
    has_url_env = "url-env" in mapping
    if has_url == has_url_env:
        problems.add(
            gateway_path,
            "exactly one of 'url' or 'url-env' is required",
        )
        return None
    url: str | None = None
    url_env: str | None = None
    if has_url:
        url_raw = mapping.get("url")
        if not isinstance(url_raw, str) or not url_raw:
            problems.add(
                f"{gateway_path}.url", "'gateway.url' must be a non-empty string"
            )
        else:
            try:
                url = canonicalise(url_raw)
            except ValueError as exc:
                problems.add(f"{gateway_path}.url", str(exc))
    else:
        url_env_raw = check_leaf(
            problems, mapping, gateway_path, gateway_descs["gateway.url-env"]
        )
        url_env = check_env_name(
            problems, f"{gateway_path}.url-env", url_env_raw
        )
    if kind is None or (url is None and url_env is None):
        return None
    return kind, url, url_env


def validate_worker(problems: Problems, data: Mapping[str, Any]) -> WorkerSettings | None:
    if "worker" not in data:
        return WorkerSettings()
    raw = data.get("worker")
    mapping = check_keys(problems, "worker", raw, WORKER_FIELDS, frozenset())
    if mapping is None:
        return None
    checked = check_leaves(
        problems, mapping, "worker", descriptors(WorkerSettings), stop_on_first=True
    )
    if checked is None:
        return None
    return WorkerSettings(
        poll_interval_seconds=checked.get(
            "poll-interval-seconds", DEFAULT_POLL_INTERVAL_SECONDS
        ),
        shutdown_grace_seconds=checked.get(
            "shutdown-grace-seconds", DEFAULT_SHUTDOWN_GRACE_SECONDS
        ),
    )


def check_model_table(
    problems: Problems,
    models: Mapping[str, ModelDecl],
    pairs: frozenset[tuple[str, str]],
    endpoints: Mapping[str, frozenset[str]],
) -> None:
    """Refuse a (provider, model) pair outside the code-owned table (§2.1).

    Gateway pins skip the pair table — their model name is free-form — and
    only prove their endpoint admissible for their gateway kind. The
    code-owned per-pin tables never hold gateway keys.
    """
    _, gateway_endpoints, _, _, _ = gateway_code_owned()
    for model_id in sorted(models):
        entry = models[model_id]
        if entry.gateway_kind is not None:
            kind = str(entry.gateway_kind)
            endpoint = str(entry.endpoint)
            admissible = gateway_endpoints.get(kind)
            if admissible is not None and endpoint != admissible:
                problems.add(
                    f"models.{model_id}.endpoint",
                    f"unknown endpoint {endpoint!r} for gateway kind {kind!r}: "
                    f"must be {admissible!r}",
                )
            continue
        provider = str(entry.provider)
        model = str(entry.model)
        endpoint = str(entry.endpoint)
        if (provider, model) not in pairs:
            problems.add(
                f"models.{model_id}.model",
                f"unknown (provider, model) pair ({provider!r}, {model!r}): "
                "outside the code-owned table",
            )
        elif endpoint not in endpoints.get(provider, frozenset()):
            problems.add(
                f"models.{model_id}.endpoint",
                f"unknown endpoint {endpoint!r} for provider {provider!r}",
            )


def check_graph_compilation(
    problems: Problems,
    config: WorkflowConfiguration,
) -> None:
    """Compile every Workflow's graph without invoking anything (§3).

    Inert: builds each Workflow's LangGraph ``StateGraph`` and compiles it,
    which only wires nodes, routes and end nodes. No step executor runs, so
    no operation, model, tool or credential is invoked or read. A Workflow
    whose graph does not compile is refused naming the Workflow.
    """
    try:
        from coding_agent.runtime.graph.workflow_graph import build_workflow_graph
    except Exception as exc:
        problems.add("(root)", f"workflow graph does not compile: {exc}")
        return
    for workflow_id in sorted(config.workflows):
        try:
            build_workflow_graph(config.workflows[workflow_id])
        except Exception as exc:
            problems.add(
                f"workflows.{workflow_id}",
                f"workflow graph does not compile: {exc}",
            )


# --- Workflow phase 1 --------------------------------------------------------------


def validate_input(
    problems: Problems, workflow_where: str, raw: Any
) -> InputDecl | None:
    """Validate one Workflow's input selector and read its typed form (§2.2, §5).

    Shape-only parsing directly into the typed ``InputDecl``: the single parse
    of the input block. Every fault names its YAML path. Returns the typed
    input, or ``None`` where it is unusable (a fault is recorded).
    """
    input_where = f"{workflow_where}.input"
    mapping = check_keys(problems, input_where, raw, INPUT_FIELDS, INPUT_REQUIRED)
    if mapping is None:
        return None
    descs = described_map(InputDecl)
    # `type` is a `custom` leaf: its closed-membership check stays
    # hand-written here (in today's position). The generic leaves go through
    # the descriptor pass.
    input_type = mapping.get("type")
    if input_type not in INPUT_TYPES:
        problems.add(
            f"{workflow_where}.input.type",
            "must be `issue` or `pull-request` (exactly one input type per Workflow)",
        )
        input_type = None
    label = check_leaf(problems, mapping, input_where, descs["label"])
    filters: dict[str, tuple[str, ...]] = {}
    for field_name in ("require-all", "require-any", "exclude"):
        entries = check_leaf(problems, mapping, input_where, descs[field_name])
        filters[field_name] = tuple(entries) if entries is not None else ()
        if entries is not None and label is not None and label in entries:
            problems.add(
                f"{workflow_where}.input.{field_name}",
                f"the Selection Label {label!r} cannot also be a filter",
            )
    drafts: str | None = None
    if "drafts" in mapping:
        raw_drafts = mapping["drafts"]
        if raw_drafts not in INPUT_DRAFTS:
            problems.add(
                f"{workflow_where}.input.drafts", "`drafts` is `include` or `exclude`"
            )
        elif input_type is not None and input_type != "pull-request":
            problems.add(
                f"{workflow_where}.input.drafts",
                "`drafts` applies only to pull-request inputs",
            )
        else:
            drafts = raw_drafts
    skip = check_leaf(problems, mapping, input_where, descs["skip-open-agent-pr"])
    if skip is not None and input_type is not None and input_type != "issue":
        problems.add(
            f"{workflow_where}.input.skip-open-agent-pr",
            "`skip-open-agent-pr` applies only to issue inputs",
        )
        skip = None
    if not isinstance(input_type, str):
        input_type = None
    if input_type is None or label is None:
        return None
    return InputDecl(
        type=input_type,
        label=label,
        require_all=filters["require-all"],
        require_any=filters["require-any"],
        exclude=filters["exclude"],
        drafts=drafts,
        skip_open_agent_pr=skip,
    )


def validate_ceiling(
    problems: Problems, workflow_where: str, raw: Any
) -> AttemptCeiling | None:
    """Validate one Workflow's Attempt Ceiling and read its typed form (§2.2).

    Shape-only parsing directly into the typed ``AttemptCeiling``: the single
    parse of the limits block. Every fault names its YAML path. Returns the
    typed ceiling, or ``None`` where it is unusable (a fault is recorded).
    """
    mapping = check_keys(
        problems,
        f"{workflow_where}.limits",
        raw,
        CEILING_FIELDS,
        CEILING_REQUIRED,
    )
    if mapping is None:
        return None
    checked = check_leaves(
        problems,
        mapping,
        f"{workflow_where}.limits",
        descriptors(AttemptCeiling),
        stop_on_first=True,
    )
    if checked is None:
        return None
    return AttemptCeiling(
        cost_usd=float(checked["cost-usd"]),
        effective_tokens=float(checked["effective-tokens"]),
        tool_calls=float(checked["tool-calls"]),
        wall_clock_minutes=float(checked["wall-clock-minutes"]),
    )


def validate_outcomes(
    problems: Problems, workflow_where: str, raw: Any
) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    mapping = mapping_at(raw, f"{workflow_where}.outcomes", problems, "'outcomes'")
    if mapping is None:
        return {}
    parsed: dict[str, tuple[str, ...]] = {}
    for outcome, entry in mapping.items():
        outcome_where = f"{workflow_where}.outcomes.{outcome}"
        if outcome not in ALL_OUTCOMES:
            problems.add(
                outcome_where,
                f"unknown Terminal Outcome {outcome!r} "
                f"(one of {', '.join(ALL_OUTCOMES)})",
            )
            continue
        entry_mapping = check_keys(
            problems, outcome_where, entry, frozenset({"labels"}), frozenset({"labels"})
        )
        if entry_mapping is None:
            continue
        labels = str_list_at(
            entry_mapping.get("labels"), f"{outcome_where}.labels", problems, "'labels'"
        )
        if labels is None:
            continue
        parsed[str(outcome)] = tuple(labels)
    return parsed


def validate_instructions(
    problems: Problems,
    step_where: str,
    raw: Any,
    base: Path,
    bundle_names: frozenset[str],
    *,
    undeclared_skill_label: str | None = None,
) -> list[Instruction] | None:
    """Validate one ``instructions`` list.

    ``bundle_names`` is the admissible skill set: the pinned Skill Bundle's
    names for ``version: 2`` files, the declared ``skills`` membership for
    ``version: 3`` files. ``undeclared_skill_label`` names the latter in the
    refusal (``None`` keeps the ``version: 2`` "pinned Skill Bundle" text).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.add(step_where, "'instructions' must be a list")
        return None
    parsed: list[Instruction] = []
    for index, item in enumerate(raw):
        item_where = f"{step_where}[{index}]"
        if (
            not isinstance(item, dict)
            or len(item) != 1
            or next(iter(item)) not in ("skill", "prompt")
        ):
            problems.add(
                item_where, "each instruction is `skill: <name>` or `prompt: <file>`"
            )
            continue
        if "skill" in item:
            skill = item["skill"]
            if not isinstance(skill, str) or not skill:
                problems.add(item_where, "'skill' must be a non-empty string")
                continue
            if skill not in bundle_names:
                if undeclared_skill_label is None:
                    problems.add(
                        item_where,
                        f"skill {skill!r} is not in the pinned Skill Bundle",
                    )
                else:
                    problems.add(
                        item_where,
                        f"skill {skill!r} is not declared in {undeclared_skill_label}",
                    )
                continue
            parsed.append(Instruction(skill=skill))
        else:
            prompt = item["prompt"]
            if not isinstance(prompt, str) or not prompt:
                problems.add(item_where, "'prompt' must be a non-empty string")
                continue
            try:
                read_prompt_text(base, prompt)
            except PromptFileError as exc:
                problems.add(item_where, str(exc))
                continue
            parsed.append(Instruction(prompt=prompt))
    return parsed


def validate_agent_tools(
    problems: Problems, step_where: str, raw: Any
) -> list[str] | None:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(entry, str) for entry in raw):
        problems.add(step_where, "'tools' must be a list of tool-group names")
        return None
    for tool in raw:
        if tool in ALWAYS_PRESENT_TOOLS:
            problems.add(
                step_where, f"{tool!r} is always present and cannot be declared"
            )
        elif tool not in TOOL_GROUPS:
            problems.add(
                step_where,
                f"unknown tool group {tool!r} "
                "(GitHub writes are Runtime Operations, never model tools)",
            )
    return [str(tool) for tool in raw]


def validate_advisor(
    problems: Problems,
    step_where: str,
    raw: Any,
    base: Path,
    models: Mapping[str, ModelDecl] | None,
) -> Advisor | None:
    """Validate one `agent.advisor` mapping (§9).

    The mapping carries exactly `prompt` (required: one operator prompt
    file under the §2.8 trust and path rules) and `model` (optional: a
    `models` id answering the Advisor, else the Pinned Model). Unknown keys,
    a missing or untrusted prompt file, an unknown model id and an
    instructions list or skill activation are refused. Returns the Advisor,
    or `None` where the raw form carries no usable declaration.
    """
    advisor_where = f"{step_where}.agent.advisor"
    if raw is None:
        return None
    mapping = check_keys(problems, advisor_where, raw, ADVISOR_FIELDS, frozenset({"prompt"}))
    if mapping is None:
        return None
    prompt = mapping.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        problems.add(f"{advisor_where}.prompt", "'prompt' must be a non-empty string")
        return None
    try:
        read_prompt_text(base, prompt)
    except PromptFileError as exc:
        problems.add(f"{advisor_where}.prompt", str(exc))
        return None
    model: str | None = None
    if mapping.get("model") is not None:
        model_raw = mapping["model"]
        if not isinstance(model_raw, str) or not model_raw:
            problems.add(f"{advisor_where}.model", "'model' must be a `models` id")
            return None
        if models is not None and model_raw not in models:
            problems.add(
                f"{advisor_where}.model",
                f"unknown model {model_raw!r}: "
                f"available: {', '.join(sorted(models))}",
            )
            return None
        model = model_raw
    return Advisor(prompt=prompt, model=model)


def validate_agent_inputs(
    problems: Problems,
    step_where: str,
    steps: Mapping[str, Any],
    raw: Any,
    *,
    input_type: str | None,
) -> tuple[InputRef, ...] | None:
    """Validate one agent step's declared inputs (§2.5).

    Returns the typed inputs in declaration order, or ``None`` where the
    list itself is unusable. The required-input proof runs in phase 2 over
    the typed Workflow; the special note and trusted-comments references
    validate here, and every fault names its YAML path.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.add(step_where, "'inputs' must be a list of references")
        return None
    typed: list[InputRef] = []
    for index, item in enumerate(raw):
        item_where = f"{step_where}[{index}]"
        optional = False
        ref: Any = item
        if isinstance(item, dict) and set(item) == {"optional"}:
            optional = True
            ref = item["optional"]
        elif isinstance(item, dict):
            problems.add(
                item_where,
                f"{item!r} is not a reference "
                "(`<step>.facts` or `<step>.<result>.<field>`); "
                "expressions are not supported",
            )
            continue
        if isinstance(ref, str) and ref.split(".")[0] == "round-handoff":
            if input_type == "pull-request":
                problems.add(
                    item_where,
                    f"{ref!r} is available only in issue Workflows, "
                    "not pull-request Workflows",
                )
                continue
            if ref != ROUND_HANDOFF_NOTE_REF or not optional:
                problems.add(
                    item_where,
                    f"{ref!r} is not the Round Handoff note input; declare "
                    f"`{{optional: {ROUND_HANDOFF_NOTE_REF}}}`",
                )
                continue
            typed.append(InputRef(reference=ref, optional=True))
            continue
        if ref == INPUT_COMMENTS_REF:
            if input_type == "pull-request":
                problems.add(
                    item_where,
                    f"{ref!r} is available only in issue Workflows, "
                    "not pull-request Workflows",
                )
                continue
            # The input is always present — required and `{optional: ...}`
            # both validate, and neither constrains the step's path.
            typed.append(InputRef(reference=ref, optional=optional))
            continue
        check_ref(problems, item_where, steps, ref, document_only=False)
        if isinstance(ref, str):
            typed.append(InputRef(reference=ref, optional=optional))
    return tuple(typed)


def check_round_handoff_placement(
    problems: Problems,
    workflow_where: str,
    step_id: str,
    result_key: str,
    is_agent: bool,
    operation: str | None,
    parsed_target: RouteTarget,
    input_type: str | None,
) -> bool:
    """Whether a `round-handoff` annotation sits on a publishing Route.

    Accepts only an `unfinished` Workflow End Route for `pushed` or
    `no-changes` from an issue Workflow's `commit-and-push` step — the only
    Routes that can publish a Round Handoff (§8). With enablement omitted the
    Route keeps ordinary `unfinished` semantics; the annotation never appears
    in pull-request Workflows. Returns whether the placement is usable.
    """
    route_where = f"{workflow_where}.steps.{step_id}.routes.{result_key}"
    if input_type == "pull-request":
        problems.add(
            route_where,
            "'round-handoff' is available only in issue Workflows, "
            "not pull-request Workflows",
        )
        return False
    if is_agent or operation != "commit-and-push":
        problems.add(
            route_where,
            "'round-handoff' annotates only a `commit-and-push` step's Route",
        )
        return False
    if result_key not in ("pushed", "no-changes"):
        problems.add(
            route_where,
            "'round-handoff' annotates only a `pushed` or `no-changes` Route, "
            f"not {result_key!r}",
        )
        return False
    if parsed_target.end != "unfinished":
        problems.add(
            route_where,
            "'round-handoff' annotates only an `unfinished` Workflow End, "
            f"not {parsed_target.end!r}",
        )
        return False
    return True


def validate_round_handoff_enablement(
    problems: Problems,
    workflow_where: str,
    raw: Any,
    input_type: str | None,
) -> tuple[str, int] | None:
    """Validate one Workflow's `round-handoff` enablement mapping (§8).

    A mapping with optional ``pickup-label`` (non-empty string, default
    ``round-finished``) and required ``max-automatic-pickups`` (positive
    integer). Null mappings or values, unknown keys and invalid types are
    refused. Only ``issue`` Workflows may enable it. Returns the resolved
    ``(pickup label, limit)``, or ``None`` where it is unusable.
    """
    where = f"{workflow_where}.round-handoff"
    if raw is None:
        return None
    if input_type == "pull-request":
        problems.add(
            where,
            "'round-handoff' is available only in issue Workflows, "
            "not pull-request Workflows",
        )
        return None
    mapping = check_keys(
        problems, where, raw, ROUND_HANDOFF_FIELDS, frozenset({"max-automatic-pickups"})
    )
    if mapping is None:
        return None
    pickup_raw = mapping.get("pickup-label", DEFAULT_ROUND_HANDOFF_PICKUP_LABEL)
    if not isinstance(pickup_raw, str) or not pickup_raw:
        problems.add(
            f"{where}.pickup-label",
            "'pickup-label' must be a non-empty label name",
        )
        return None
    limit_raw = mapping.get("max-automatic-pickups")
    if isinstance(limit_raw, bool) or not isinstance(limit_raw, int) or limit_raw < 1:
        problems.add(
            f"{where}.max-automatic-pickups",
            "'max-automatic-pickups' must be a positive integer",
        )
        return None
    return (pickup_raw, limit_raw)


def validate_context_thresholds(
    problems: Problems, step_where: str, raw: Any
) -> ContextThresholds | None:
    """Validate one agent step's `context-thresholds` policy (§3).

    A mapping with required positive integer `hard-tokens` and optional
    positive integer `soft-tokens`; nulls, unknown fields and invalid types
    are refused. Returns the policy, or ``None`` where it is unusable (a
    fault is recorded). Pin-dependent bounds (the safety margin, the request
    overhead, the soft-vs-hard order, the output cap and the Model Context
    Window) are proven in phase 2 against the Workflow's Pinned Model.
    """
    where = f"{step_where}.context-thresholds"
    mapping = check_keys(problems, where, raw, STEP_THRESHOLD_FIELDS, frozenset({"hard-tokens"}))
    if mapping is None:
        return None
    hard = positive_int_at(
        mapping.get("hard-tokens"), f"{where}.hard-tokens", problems, "'hard-tokens'"
    )
    if hard is None:
        return None
    soft: int | None = None
    if mapping.get("soft-tokens") is not None:
        soft = positive_int_at(
            mapping.get("soft-tokens"), f"{where}.soft-tokens", problems, "'soft-tokens'"
        )
        if soft is None:
            return None
    return ContextThresholds(hard_tokens=hard, soft_tokens=soft)


def validate_step(
    problems: Problems,
    workflow_where: str,
    step_id: str,
    step_raw: Any,
    steps_raw: Mapping[str, Any],
    input_type: str | None,
    defaults_max: int | None,
    defaults_routes: Mapping[str, RouteTarget],
    base: Path,
    bundle_names: frozenset[str],
    *,
    undeclared_skill_label: str | None = None,
    models: Mapping[str, ModelDecl] | None = None,
) -> WorkflowStep | None:
    """Validate one step and read its typed form: the single read (§3).

    Shape-only parsing directly into the typed ``WorkflowStep``: known keys,
    the executor, the ``context-thresholds`` policy, routes and
    ``max-executions`` each parse exactly once here. Every fault names its
    YAML path. Returns the typed step, or ``None`` where it is unusable (a
    fault is recorded). Pin-dependent bounds stay in phase 2, over the typed
    Workflow.
    """
    step_where = f"{workflow_where}.steps.{step_id}"
    mapping = check_keys(problems, step_where, step_raw, STEP_FIELDS, STEP_REQUIRED)
    if mapping is None:
        return None
    is_agent = "agent" in mapping
    is_operation = "operation" in mapping
    if is_agent == is_operation:
        problems.add(
            step_where, "a step has exactly one executor: `agent` or `operation`"
        )
        return None
    opted_in = is_agent and isinstance(mapping.get("context-thresholds"), dict)
    policy: ContextThresholds | None = None
    if "context-thresholds" in mapping and not is_agent:
        problems.add(
            f"{step_where}.context-thresholds",
            "`context-thresholds` apply only to agent steps; "
            "operations hold no conversation",
        )
        opted_in = False
    elif opted_in:
        policy = validate_context_thresholds(
            problems, step_where, mapping["context-thresholds"]
        )

    agent: Agent | None = None
    operation: str | None = None
    with_body: str | None = None
    with_draft: bool | None = None
    with_requires: list[tuple[str, str]] = []

    if is_agent:
        agent_raw = mapping["agent"]
        agent_mapping = check_keys(
            problems, f"{step_where}.agent", agent_raw, AGENT_FIELDS, frozenset({"results"})
        )
        if agent_mapping is None:
            return None
        instructions = validate_instructions(
            problems, f"{step_where}.agent.instructions", agent_mapping.get("instructions"),
            base, bundle_names,
            undeclared_skill_label=undeclared_skill_label,
        )
        tools = validate_agent_tools(
            problems, f"{step_where}.agent.tools", agent_mapping.get("tools")
        )
        agent_inputs = validate_agent_inputs(
            problems, f"{step_where}.agent.inputs", steps_raw,
            agent_mapping.get("inputs"),
            input_type=input_type,
        )
        results = check_payload(
            problems,
            f"{step_where}.agent.results",
            agent_mapping.get("results"),
            opted_in=opted_in,
        )
        advisor = validate_advisor(
            problems, step_where, agent_mapping.get("advisor"), base, models
        )
        if instructions is None or tools is None or agent_inputs is None or results is None:
            return None
        if "advisor" in tools and agent_mapping.get("advisor") is None:
            problems.add(
                f"{step_where}.agent.advisor",
                "the `advisor` tool group requires an `agent.advisor` mapping "
                "with `prompt` (and optional `model`)",
            )
            return None
        if "advisor" not in tools and advisor is not None:
            problems.add(
                f"{step_where}.agent.tools",
                "an `agent.advisor` mapping requires the `advisor` tool group",
            )
            return None
        agent = Agent(
            instructions=tuple(instructions),
            tools=tuple(tools),
            inputs=agent_inputs,
            results=results,
            advisor=advisor,
        )
        if "with" in mapping:
            problems.add(
                step_where,
                "`with` holds operation parameters; agent steps configure `agent`",
            )
    else:
        operation_raw = mapping["operation"]
        if not isinstance(operation_raw, str) or not operation_raw:
            problems.add(f"{step_where}.operation", "must be a non-empty string")
            return None
        operation = operation_raw
        spec = OPERATIONS.get(operation)
        if spec is None:
            problems.add(
                f"{step_where}.operation",
                f"unknown Runtime Operation {operation!r} "
                f"(catalog: {', '.join(sorted(OPERATIONS))})",
            )
            return None
        if spec.input_type is not None and input_type is not None and spec.input_type != input_type:
            other = "issue" if spec.input_type == "pull-request" else "pull-request"
            problems.add(
                f"{step_where}.operation",
                f"{operation!r} is available only in `{spec.input_type}` Workflows "
                f"(this one is `{other}`)",
            )
        with_raw = mapping.get("with") or {}
        with_mapping = check_keys(problems, f"{step_where}.with", with_raw, spec.with_keys)
        if with_mapping is None:
            return None
        if "facts" in with_mapping:
            problems.add(
                f"{step_where}.with.facts",
                "there is no facts-mode comment; the runtime's terminal comment "
                "already reports the Attempt facts",
            )
        if "title" in with_mapping:
            problems.add(
                f"{step_where}.with.title",
                "the pull-request title is a fixed runtime template "
                "over the Target Issue title",
            )
        if spec.needs_body and "body" not in with_mapping:
            problems.add(f"{step_where}.with", f"{operation!r} needs `body`")
        if "body" in with_mapping:
            body_raw = with_mapping["body"]
            if isinstance(body_raw, str) and body_raw.split(".")[0] == "round-handoff":
                problems.add(
                    f"{step_where}.with.body",
                    f"{body_raw!r} is available only as an optional agent input "
                    f"(`{{optional: {ROUND_HANDOFF_NOTE_REF}}}`), "
                    "never as an operation body",
                )
            elif body_raw == INPUT_COMMENTS_REF or body_raw == {
                "optional": INPUT_COMMENTS_REF
            }:
                problems.add(
                    f"{step_where}.with.body",
                    f"{INPUT_COMMENTS_REF!r} is available only as a declared "
                    "agent step input, never as an operation body",
                )
            else:
                parsed_body = check_ref(
                    problems, f"{step_where}.with.body", steps_raw,
                    body_raw, document_only=True,
                )
                if parsed_body is not None and isinstance(body_raw, str):
                    with_body = body_raw
        if "draft" in with_mapping:
            draft = bool_at(
                with_mapping["draft"], f"{step_where}.with.draft", problems, "'draft'"
            )
            if draft is not None:
                with_draft = draft
        for index, item in enumerate(with_mapping.get("requires") or []):
            pair: tuple[str, str] | None = None
            if isinstance(item, dict) and len(item) == 1:
                key, value = next(iter(item.items()))
                if isinstance(key, str) and isinstance(value, str):
                    pair = (key, value)
            if pair not in REQUIRABLE:
                problems.add(
                    f"{step_where}.with.requires[{index}]",
                    f"{item!r} is not requirable evidence "
                    "(supported: run-validation: passed)",
                )
            elif pair is not None:
                with_requires.append(pair)
        if "limits" in mapping:
            problems.add(
                f"{step_where}.limits",
                "`limits` apply only to agent steps; operations are bounded "
                "by code-owned timeouts",
            )
        if mapping.get("reserved"):
            problems.add(
                f"{step_where}.reserved", "only agent steps can be Reserved Steps"
            )

    step_descs = described_map(WorkflowStep)
    max_executions = defaults_max if defaults_max is not None else 1
    parsed_max = check_leaf(problems, mapping, step_where, step_descs["max-executions"])
    if "max-executions" in mapping:
        if parsed_max is None:
            return None
        max_executions = int(parsed_max)

    limits: dict[str, float] = {}
    if "limits" in mapping and is_agent:
        limits_mapping = check_keys(
            problems, f"{step_where}.limits", mapping["limits"], STEP_LIMITS_FIELDS
        )
        if limits_mapping is None:
            return None
        for meter, value in limits_mapping.items():
            parsed_value = positive_number_at(
                value, f"{step_where}.limits.{meter}", problems, f"'{meter}'"
            )
            if parsed_value is None:
                return None
            limits[meter] = parsed_value

    reserved = mapping.get("reserved") is True
    if "reserved" in mapping and not isinstance(mapping["reserved"], bool):
        problems.add(f"{step_where}.reserved", "'reserved' must be a boolean")
        reserved = False
    if reserved and is_agent:
        missing = [meter for meter in METERS if meter not in limits]
        if missing:
            problems.add(
                f"{step_where}.limits",
                "a Reserved Step declares all four meters "
                f"(missing: {', '.join(missing)})",
            )
            return None
    elif reserved and is_operation:
        return None
    if reserved and not is_agent:
        return None

    # Routes: the single parse of the step's route table. Result names come
    # from the typed components above, via the shared implementation.
    routes_raw = mapping.get("routes")
    routes_mapping = mapping_at(routes_raw, f"{step_where}.routes", problems, "'routes'")
    if routes_mapping is None:
        return None
    with_requires_tuple = tuple(with_requires)
    result_names = result_names_for_step(
        agent, policy, operation, with_requires_tuple, input_type
    )
    effective_defaults = default_routes_for_step(dict(defaults_routes), result_names)
    for key in routes_mapping:
        if key == "*" or key == "default":
            problems.add(
                f"{step_where}.routes.{key}",
                "there is no wildcard route; route every result explicitly",
            )
        elif key not in result_names:
            problems.add(
                f"{step_where}.routes.{key}",
                f"{key!r} is not a possible result of this step "
                f"(results: {', '.join(result_names)})",
            )
    explicit_thresholds = set(threshold_results_for_policy(policy))
    for result in result_names:
        if result in explicit_thresholds:
            continue
        if result not in routes_mapping and result not in effective_defaults:
            suffix = (
                ""
                if result in RESERVED_RESULTS
                else " (business results are never defaulted)"
            )
            problems.add(
                f"{step_where}.routes", f"result {result!r} is not routed{suffix}"
            )
    if policy is not None:
        if HARD_CONTEXT_RESULT not in routes_mapping:
            problems.add(
                f"{step_where}.routes",
                f"result {HARD_CONTEXT_RESULT!r} needs a step-local route",
            )
        if (
            policy.soft_tokens is not None
            and SOFT_CONTEXT_RESULT not in routes_mapping
        ):
            problems.add(
                f"{step_where}.routes",
                f"result {SOFT_CONTEXT_RESULT!r} needs a step-local route",
            )
    # The typed step keeps its step-local Routes only: `defaults.routes`
    # apply at use time via `Workflow.effective_routes` (and the Effective
    # Configuration records the local table, as `build_config` once did).
    # `effective_defaults` above only proves every result is routed.
    merged: dict[str, RouteTarget] = {}
    for key, target in routes_mapping.items():
        if key not in result_names:
            continue
        parsed_target = parse_route_target(
            problems, f"{step_where}.routes.{key}", target
        )
        if parsed_target is None:
            continue
        if parsed_target.end is not None:
            category = parsed_target.end
            if category in RUNTIME_ONLY_OUTCOMES:
                problems.add(
                    f"{step_where}.routes.{key}",
                    f"`{category}` is a runtime-only Terminal Outcome "
                    "and cannot be selected",
                )
                continue
            if category not in YAML_ENDS:
                problems.add(
                    f"{step_where}.routes.{key}",
                    f"unknown end category {category!r} "
                    f"(one of {', '.join(sorted(YAML_ENDS))})",
                )
                continue
            if (
                key in RESERVED_RESULTS
                or (opted_in and key == HARD_CONTEXT_RESULT)
            ) and not YAML_ENDS[category]:
                problems.add(
                    f"{step_where}.routes.{key}",
                    f"runtime-reserved {key!r} cannot route directly to `{category}`",
                )
                continue
        elif parsed_target.step is not None and parsed_target.step not in steps_raw:
            problems.add(
                f"{step_where}.routes.{key}", f"unknown target {parsed_target.step!r}"
            )
            continue
        if parsed_target.inherit_conversation:
            if not is_agent:
                problems.add(
                    f"{step_where}.routes.{key}",
                    "'inherit-conversation' requires agent steps at both ends",
                )
                continue
            if parsed_target.step is None:
                problems.add(
                    f"{step_where}.routes.{key}",
                    "'inherit-conversation' requires agent steps at both ends",
                )
                continue
            target_raw = steps_raw.get(parsed_target.step)
            if not isinstance(target_raw, dict) or "agent" not in target_raw:
                problems.add(
                    f"{step_where}.routes.{key}",
                    "'inherit-conversation' requires agent steps at both ends",
                )
                continue
        if parsed_target.round_handoff is not None:
            if not check_round_handoff_placement(
                problems,
                workflow_where,
                step_id,
                key,
                is_agent,
                operation,
                parsed_target,
                input_type,
            ):
                continue
            annotation = parsed_target.round_handoff
            if annotation.kind == "ai-assisted":
                assert annotation.note is not None
                parsed_note = check_ref(
                    problems,
                    f"{step_where}.routes.{key}.round-handoff.note",
                    steps_raw,
                    annotation.note,
                    document_only=True,
                )
                if parsed_note is None:
                    continue
        merged[key] = parsed_target
    return WorkflowStep(
        id=step_id,
        agent=agent,
        operation=operation,
        with_body=with_body,
        with_draft=with_draft,
        with_requires=tuple(with_requires),
        context_thresholds=policy,
        max_executions=max_executions,
        limits=limits,
        reserved=reserved,
        routes=merged,
    )


def validate_defaults(
    problems: Problems,
    workflow_where: str,
    raw: Any,
    steps_raw: Mapping[str, Any] | None,
) -> tuple[int | None, dict[str, RouteTarget]]:
    if raw is None:
        return 1, {}
    mapping = check_keys(problems, f"{workflow_where}.defaults", raw, DEFAULTS_FIELDS)
    if mapping is None:
        return None, {}
    if isinstance(raw, dict) and "limits" in raw:
        problems.add(f"{workflow_where}.defaults", "there is no `defaults.limits`")
    workflow_descs = described_map(Workflow)
    max_executions = check_leaf(
        problems, mapping, f"{workflow_where}.defaults", workflow_descs["defaults.max-executions"]
    )
    if "max-executions" not in mapping:
        max_executions = 1
    routes: dict[str, RouteTarget] = {}
    defaults_routes = mapping.get("routes") or {}
    defaults_mapping = mapping_at(
        defaults_routes, f"{workflow_where}.defaults.routes", problems, "'routes'"
    )
    if defaults_mapping is not None:
        for key, target in defaults_mapping.items():
            if key not in RESERVED_RESULTS:
                problems.add(
                    f"{workflow_where}.defaults.routes.{key}",
                    "defaults may route only runtime-reserved results "
                    "(`error`, `limit-exceeded`)",
                )
                continue
            if isinstance(target, dict) and "round-handoff" in target:
                problems.add(
                    f"{workflow_where}.defaults.routes.{key}",
                    "defaults.routes holds scalar step ids and Workflow Ends only",
                )
                continue
            parsed = parse_route_target(
                problems, f"{workflow_where}.defaults.routes.{key}", target
            )
            if parsed is None:
                continue
            if parsed.step is not None and isinstance(target, dict):
                problems.add(
                    f"{workflow_where}.defaults.routes.{key}",
                    "defaults.routes holds scalar step ids and Workflow Ends only",
                )
                continue
            if parsed.inherit_conversation:
                problems.add(
                    f"{workflow_where}.defaults.routes.{key}",
                    "defaults.routes holds scalar step ids and Workflow Ends only",
                )
                continue
            if parsed.end is not None:
                category = parsed.end
                if category in RUNTIME_ONLY_OUTCOMES:
                    problems.add(
                        f"{workflow_where}.defaults.routes.{key}",
                        f"`{category}` is a runtime-only Terminal Outcome "
                        "and cannot be selected",
                    )
                    continue
                if category not in YAML_ENDS:
                    problems.add(
                        f"{workflow_where}.defaults.routes.{key}",
                        f"unknown end category {category!r}",
                    )
                    continue
                if not YAML_ENDS[category]:
                    problems.add(
                        f"{workflow_where}.defaults.routes.{key}",
                        f"runtime-reserved {key!r} cannot route directly to `{category}`",
                    )
                    continue
            elif parsed.step is not None and steps_raw is not None:
                if parsed.step not in steps_raw:
                    problems.add(
                        f"{workflow_where}.defaults.routes.{key}",
                        f"unknown target {parsed.step!r}",
                    )
                    continue
            routes[key] = parsed
    return max_executions, routes


def validate_workflow(
    problems: Problems,
    workflow_id: str,
    workflow_raw: Any,
    models: Mapping[str, ModelDecl] | None,
    base: Path,
    bundle_names: frozenset[str],
    *,
    undeclared_skill_label: str | None = None,
) -> Workflow | None:
    """Validate one Workflow and read its typed form: the single read (§2.2, §3).

    Shape-only parsing directly into the typed ``Workflow``: the input, the
    ceiling, outcomes, round-handoff enablement, ``defaults`` and every step
    each parse exactly once here. Every fault names its YAML path. Returns the
    typed ``Workflow``, or ``None`` where it is unusable — the mapping, the
    steps, the input or the ceiling unusable (a fault is recorded; phase 2
    skips that Workflow). Steps are always validated for their own faults
    before that gating, so no phase-1 fault is lost.
    """
    workflow_where = f"workflows.{workflow_id}"
    mapping = check_keys(
        problems, workflow_where, workflow_raw, WORKFLOW_FIELDS, WORKFLOW_REQUIRED
    )
    if mapping is None:
        return None
    if models is not None and mapping.get("model") not in models:
        problems.add(
            f"{workflow_where}.model",
            f"unknown model {mapping.get('model')!r}: "
            f"available: {', '.join(sorted(models))}",
        )
    model_raw = mapping.get("model")
    model = model_raw if isinstance(model_raw, str) else ""
    input_decl = validate_input(problems, workflow_where, mapping.get("input"))
    input_type = input_decl.type if input_decl is not None else None
    ceiling = validate_ceiling(problems, workflow_where, mapping.get("limits"))
    max_parallel_raw = check_leaf(
        problems,
        mapping,
        workflow_where,
        described_map(Workflow)["max-parallel-subagents"],
    )
    max_parallel = (
        int(max_parallel_raw)
        if isinstance(max_parallel_raw, int) and not isinstance(max_parallel_raw, bool)
        else DEFAULT_MAX_PARALLEL_SUBAGENTS
    )
    outcomes = validate_outcomes(
        problems, workflow_where, mapping.get("outcomes")
    )
    enabled: tuple[str, int] | None = None
    if "round-handoff" in mapping:
        enabled = validate_round_handoff_enablement(
            problems, workflow_where, mapping.get("round-handoff"), input_type
        )
        if enabled is not None and input_decl is not None:
            pickup_label, _ = enabled
            raw_input = mapping.get("input")
            if isinstance(raw_input, dict):
                for field_name in ("require-all", "require-any", "exclude"):
                    entries = raw_input.get(field_name) or []
                    if isinstance(entries, list) and pickup_label in entries:
                        problems.add(
                            f"{workflow_where}.input.{field_name}",
                            f"the Pickup Label {pickup_label!r} cannot also be a filter",
                        )

    steps_raw = mapping.get("steps")
    if not isinstance(steps_raw, dict) or not steps_raw:
        problems.add(f"{workflow_where}.steps", "a mapping of step id -> step is required")
        return None
    for step_id in steps_raw:
        if check_id(step_id, f"{workflow_where}.steps.{step_id}", problems, "step") is None:
            continue
        if step_id == "input":
            problems.add(
                f"{workflow_where}.steps.{step_id}",
                "'input' is reserved for the `input.comments` declared "
                "input and cannot be a step id",
            )
            continue
    start_raw = mapping.get("start")
    if start_raw not in steps_raw:
        problems.add(f"{workflow_where}.start", f"{start_raw!r} is not a step id")
        start = ""
    else:
        start = str(start_raw)

    defaults_max, defaults_routes = validate_defaults(
        problems, workflow_where, mapping.get("defaults"), steps_raw
    )

    typed_steps: dict[str, WorkflowStep] = {}
    for step_id, step_raw in steps_raw.items():
        if not isinstance(step_id, str) or ID_RE.fullmatch(step_id) is None:
            continue
        if step_id == "input":
            continue
        typed = validate_step(
            problems,
            workflow_where,
            step_id,
            step_raw,
            steps_raw,
            input_type,
            defaults_max,
            defaults_routes,
            base,
            bundle_names,
            undeclared_skill_label=undeclared_skill_label,
            models=models,
        )
        if typed is not None:
            typed_steps[str(step_id)] = typed

    if input_decl is None or ceiling is None:
        return None
    round_handoff: RoundHandoffConfig | None = None
    if enabled is not None:
        pickup_label, limit = enabled
        round_handoff = RoundHandoffConfig(
            max_automatic_pickups=limit,
            pickup_label=pickup_label,
        )
    return Workflow(
        id=workflow_id,
        model=model,
        input=input_decl,
        ceiling=ceiling,
        max_parallel_subagents=max_parallel,
        outcomes=outcomes,
        start=start,
        defaults_max_executions=(
            defaults_max
            if isinstance(defaults_max, int) and not isinstance(defaults_max, bool)
            else 1
        ),
        defaults_routes=defaults_routes,
        round_handoff=round_handoff,
        steps=typed_steps,
    )


# --- Workflow phase 2: limits, graph, required inputs ------------------------------


def check_limits_arithmetic(
    problems: Problems, workflow_where: str, workflow: Workflow
) -> None:
    """Proves the Attempt Ceiling covers the Reserved Steps (§3, §11).

    Runs over the typed ``Workflow``: the Reserved Step allowances plus the
    Work Pool hold the Attempt Ceiling, and every non-reserved agent Step
    Allowance fits the Work Pool. The single read already parsed every limit.
    """
    ceiling = workflow.ceiling.by_meter()
    reserved_allow = {
        step_id: step.limits
        for step_id, step in workflow.steps.items()
        if step.reserved
    }
    pool = {
        meter: ceiling[meter] - sum(allow.get(meter, 0.0) for allow in reserved_allow.values())
        for meter in METERS
    }
    for meter in METERS:
        if pool[meter] <= 0:
            problems.add(
                f"{workflow_where}.limits.{meter}",
                f"Reserved Step allowances ({ceiling[meter] - pool[meter]}) "
                f"must stay below the Attempt Ceiling ({ceiling[meter]})",
            )
    if any(pool[meter] <= 0 for meter in METERS):
        return
    for step_id, step in workflow.steps.items():
        if step.agent is None or step.reserved:
            continue
        for meter, value in step.limits.items():
            if value > pool[meter]:
                problems.add(
                    f"{workflow_where}.steps.{step_id}.limits.{meter}",
                    f"Step Allowance {value} exceeds the Work Pool {pool[meter]}",
                )


@dataclass(frozen=True)
class AdmissionFloor:
    """One maximal Admission hold for a Workflow's Pinned Model (§3, §11.4).

    The largest single hold is Usable Threshold + output cap: the whole
    Conversation Context (at most the Usable Threshold) priced as uncached
    input, plus the request's output cap. A gateway pin prices the context
    part at max(`input`, `cache-write`); a direct pin at the `input` price.
    Prices are USD per million tokens, so the `cost-usd` minimum divides by
    one million. Usable Threshold is the pin's context threshold minus the
    code-owned safety margin and request overhead.
    """

    usable_threshold: int
    output_cap: int
    token_minimum: int
    cost_minimum: float
    input_price: float
    output_price: float
    gateway: bool
    pin_input_price: float
    pin_cache_write_price: float


FLOOR_METERS: tuple[str, str] = ("effective-tokens", "cost-usd")
"""The meters one maximal Admission hold is measured in (§3)."""


def _pin_for_workflow(
    models: Mapping[str, ModelDecl] | None,
    model_id: str,
) -> ModelDecl | None:
    """The parsed pin entry for the Workflow's model, if usable."""
    if models is None:
        return None
    return models.get(model_id)


def _pin_output_and_window(pin: ModelDecl) -> tuple[int, int] | None:
    """The pin's validated output cap and Model Context Window, if usable."""
    output_cap = pin.output_cap_tokens
    window = pin.context_window_tokens
    if output_cap is None or window is None:
        return None
    return output_cap, window


def admission_floor_for(pin: ModelDecl) -> AdmissionFloor | None:
    """The floor for a parsed pin entry, or None when it carries no usable
    token budget (a phase-1 fault already refused it)."""
    from coding_agent.runtime.context_budget import usable_from_raw

    threshold = pin.context_threshold_tokens
    if threshold is None:
        return None
    _, _, safety, overhead, _ = gateway_code_owned()
    usable = usable_from_raw(threshold, safety, overhead)
    if usable is None or usable <= 0:
        return None
    return admission_floor_with_usable(pin, usable)


def admission_floor_with_usable(
    pin: ModelDecl, usable: int
) -> AdmissionFloor | None:
    """The floor for a parsed pin entry at one Usable Threshold.

    The largest admissible input carries ``usable`` context tokens priced as
    uncached input (a gateway pin prices at max(`input`, `cache-write`)),
    plus the pin's full output cap. ``None`` where the pin carries no usable
    budget, including a non-positive Usable Threshold (the Context Budget
    module refuses it).
    """
    cap = pin.output_cap_tokens
    if cap is None:
        return None
    input_price = pin.prices.input
    output_price = pin.prices.output
    cache_write_price = pin.prices.cache_write
    gateway = pin.gateway_kind is not None
    from coding_agent.runtime.context_budget import (
        admission_floor,
        effective_input_price,
    )

    bound = admission_floor(
        usable,
        cap,
        input_price=float(input_price),
        output_price=float(output_price),
        cache_write_price=float(cache_write_price),
        is_gateway=gateway,
    )
    if bound is None:
        return None
    token_minimum, cost_minimum = bound
    effective_input = effective_input_price(
        float(input_price), float(cache_write_price), is_gateway=gateway
    )
    return AdmissionFloor(
        usable_threshold=usable,
        output_cap=cap,
        token_minimum=token_minimum,
        cost_minimum=cost_minimum,
        input_price=effective_input,
        output_price=float(output_price),
        gateway=gateway,
        pin_input_price=float(input_price),
        pin_cache_write_price=float(cache_write_price),
    )


def _floor_bits(floor: AdmissionFloor) -> dict[str, str]:
    """The per-meter minimum components, in the window rule's shape."""
    token_bits = (
        f"Usable Threshold {floor.usable_threshold} "
        f"+ output-cap-tokens {floor.output_cap}"
    )
    if floor.gateway:
        cost_bits = (
            f"Usable Threshold {floor.usable_threshold} "
            f"* input price {floor.input_price} "
            f"(max of input {floor.pin_input_price} "
            f"and cache-write {floor.pin_cache_write_price}) "
            f"+ output-cap-tokens {floor.output_cap} "
            f"* output price {floor.output_price}"
        )
    else:
        cost_bits = (
            f"Usable Threshold {floor.usable_threshold} "
            f"* input price {floor.input_price} "
            f"+ output-cap-tokens {floor.output_cap} "
            f"* output price {floor.output_price}"
        )
    return {"effective-tokens": token_bits, "cost-usd": cost_bits}


def _floor_minima(floor: AdmissionFloor) -> dict[str, float]:
    """The per-meter minima for one Admission floor, keyed by meter."""
    return {
        "effective-tokens": floor.token_minimum,
        "cost-usd": floor.cost_minimum,
    }


def check_admission_floor(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
    models: Mapping[str, ModelDecl] | None,
) -> None:
    """Refuse any limit below one maximal Admission hold (§3, decision 5).

    Static, offline, with no warning: the Attempt Ceiling must hold the
    largest per-step maximal hold over all agent steps, the Work Pool the
    largest over non-reserved agent steps, and every Reserved Step allowance
    and every non-reserved Step Allowance that declares `effective-tokens`
    or `cost-usd` must hold its own step's maximal hold. One step's maximal
    hold is its largest admissible input — its soft Usable Threshold where
    the step configures soft, else its hard or pin-default Usable Threshold,
    raised to the pin-default Usable Threshold where the step can start
    Sub-agents — plus the pin's full output cap (priced at the pin's
    prices for `cost-usd`). A limit that does not declare the meter is not
    checked for it. The guarantee is one full-context request per fresh limit.

    Runs over the typed ``Workflow``: thresholds, allowances, Advisor pins
    and the subagents tool group all come from the single read.
    """
    workflow_id = workflow.id
    ceiling = workflow.ceiling.by_meter()
    pin = _pin_for_workflow(models, workflow.model)
    if pin is None:
        return
    pin_floor = admission_floor_for(pin)
    if pin_floor is None:
        return
    from coding_agent.runtime.context_budget import (
        select_usable_threshold,
        usable_from_raw,
    )

    _, _, safety, overhead, _ = gateway_code_owned()
    floors: dict[str, AdmissionFloor] = {}
    floors_minima: dict[str, dict[str, float]] = {}
    floors_bits: dict[str, dict[str, str]] = {}
    for step_id in sorted(workflow.steps):
        step = workflow.steps[step_id]
        if step.agent is None:
            continue
        policy = step.context_thresholds
        hard = policy.hard_tokens if policy is not None else None
        soft = policy.soft_tokens if policy is not None else None
        hard_usable = (
            usable_from_raw(hard, safety, overhead) if hard is not None else None
        )
        soft_usable = (
            usable_from_raw(soft, safety, overhead) if soft is not None else None
        )
        usable = select_usable_threshold(
            soft_usable, hard_usable, pin_floor.usable_threshold
        )
        assert usable is not None
        if "subagents" in step.agent.tools:
            usable = max(usable, pin_floor.usable_threshold)
        floor = admission_floor_with_usable(pin, usable)
        if floor is None:
            continue
        floors[step_id] = floor
        minima = _floor_minima(floor)
        bits = _floor_bits(floor)
        advisor_id = step.agent.advisor.model if step.agent.advisor is not None else None
        advisor_pin = models.get(advisor_id) if models is not None and advisor_id is not None else None
        advisor_floor = (
            admission_floor_for(advisor_pin)
            if advisor_pin is not None
            else None
        )
        if advisor_floor is not None:
            advisor_minima = _floor_minima(advisor_floor)
            advisor_bits = _floor_bits(advisor_floor)
            for meter in FLOOR_METERS:
                if advisor_minima[meter] > minima[meter]:
                    minima[meter] = advisor_minima[meter]
                    bits[meter] = (
                        f"{advisor_bits[meter]} (Advisor pin {advisor_id!r})"
                    )
        floors_minima[step_id] = minima
        floors_bits[step_id] = bits
    if not floors:
        return
    ceiling_id = max(floors, key=lambda item: floors_minima[item]["effective-tokens"])
    pool_ids = [step_id for step_id in floors if not workflow.steps[step_id].reserved]
    pool_id = (
        max(pool_ids, key=lambda item: floors_minima[item]["effective-tokens"])
        if pool_ids
        else None
    )
    ceiling_bits = floors_bits[ceiling_id]
    ceiling_minima = floors_minima[ceiling_id]
    for meter in FLOOR_METERS:
        value = ceiling.get(meter)
        if value is not None and value < ceiling_minima[meter]:
            problems.add(
                f"{workflow_where}.limits.{meter}",
                f"Attempt Ceiling {value} is below one maximal Admission hold "
                f"in workflow {workflow_id!r}: {meter!r} minimum {ceiling_minima[meter]} "
                f"({ceiling_bits[meter]})",
            )
    reserved_allow = {
        step_id: step.limits
        for step_id, step in workflow.steps.items()
        if step.reserved
    }
    pool = {
        meter: ceiling[meter]
        - sum(allow.get(meter, 0.0) for allow in reserved_allow.values())
        for meter in METERS
    }
    if pool_id is not None:
        pool_bits = floors_bits[pool_id]
        pool_minima = floors_minima[pool_id]
        for meter in FLOOR_METERS:
            if 0 < pool[meter] < pool_minima[meter]:
                problems.add(
                    f"{workflow_where}.limits.{meter}",
                    f"Work Pool {pool[meter]} is below one maximal Admission hold "
                    f"in workflow {workflow_id!r}: {meter!r} minimum {pool_minima[meter]} "
                    f"({pool_bits[meter]})",
                )
    for step_id in sorted(reserved_allow):
        allow = reserved_allow[step_id]
        step_bits = floors_bits.get(step_id, ceiling_bits)
        step_minima = floors_minima.get(step_id, ceiling_minima)
        for meter in FLOOR_METERS:
            if meter in allow and allow[meter] < step_minima[meter]:
                problems.add(
                    f"{workflow_where}.steps.{step_id}.limits.{meter}",
                    f"Reserved Step {step_id!r} allowance {allow[meter]} is below "
                    f"one maximal Admission hold in workflow {workflow_id!r}: "
                    f"{meter!r} minimum {step_minima[meter]} ({step_bits[meter]})",
                )
    for step_id in sorted(workflow.steps):
        step = workflow.steps[step_id]
        if step.agent is None or step.reserved:
            continue
        allow = step.limits
        step_bits = floors_bits.get(step_id, ceiling_bits)
        step_minima = floors_minima.get(step_id, ceiling_minima)
        for meter in FLOOR_METERS:
            if meter in allow and allow[meter] < step_minima[meter]:
                problems.add(
                    f"{workflow_where}.steps.{step_id}.limits.{meter}",
                    f"Step Allowance {allow[meter]} of step {step_id!r} is below "
                    f"one maximal Admission hold in workflow {workflow_id!r}: "
                    f"{meter!r} minimum {step_minima[meter]} ({step_bits[meter]})",
                )


def check_reachability(
    problems: Problems, workflow_where: str, workflow: Workflow
) -> None:
    """Every step is reachable from `start` over the effective Routes (§3).

    Runs over the typed ``Workflow``: the shared ``effective_routes``
    implementation supplies the step's Routes after ``defaults.routes``.
    """
    if not workflow.start:
        return
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    seen: set[str] = set()
    todo = [workflow.start]
    while todo:
        step = todo.pop()
        if step in seen:
            continue
        seen.add(step)
        for target in routes.get(step, {}).values():
            if target.step is not None:
                todo.append(target.step)
    for step_id in routes:
        if step_id not in seen:
            problems.add(
                f"{workflow_where}.steps.{step_id}", "unreachable from `start`"
            )


def check_limit_exceeded_acyclic(
    problems: Problems, workflow_where: str, workflow: Workflow
) -> None:
    """The `limit-exceeded` Routes form no cycle (§3).

    Runs over the typed ``Workflow`` and its effective Routes.
    """
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    for step_id in routes:
        chain = [step_id]
        target = routes[step_id].get("limit-exceeded")
        current = target.step if target is not None else None
        while isinstance(current, str):
            if current in chain:
                cycle = chain[chain.index(current):] + [current]
                problems.add(
                    f"{workflow_where}.steps.{step_id}",
                    "`limit-exceeded` Routes form a cycle: " + " -> ".join(cycle),
                )
                break
            chain.append(current)
            nxt = routes.get(current, {}).get("limit-exceeded")
            current = nxt.step if nxt is not None else None


HARD_HANDOFF_ENDS = frozenset({"unfinished", "failed"})
"""Workflow Ends a Hard Handoff branch may reach (model-free fallback)."""


def check_step_thresholds(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
    models: Mapping[str, ModelDecl] | None,
) -> None:
    """Pin-dependent bounds for every opted-in step (§3, decision 5).

    Static and offline: hard must exceed the code-owned safety margin plus
    request overhead, and hard plus the pin's output cap must fit the Model
    Context Window. Where a soft bound is configured it must sit strictly
    between the safety margin plus overhead and the hard bound. Faults name
    the offending `context-thresholds` leaf.

    Runs over the typed ``Workflow``: the policies come from the single read.
    """
    pin = _pin_for_workflow(models, workflow.model)
    if pin is None:
        return
    output_and_window = _pin_output_and_window(pin)
    if output_and_window is None:
        return
    output_cap, window = output_and_window
    _, _, safety, overhead, _ = gateway_code_owned()
    floor_sum = safety + overhead
    for step_id in sorted(workflow.steps):
        policy = workflow.steps[step_id].context_thresholds
        if policy is None:
            continue
        hard = policy.hard_tokens
        step_where = f"{workflow_where}.steps.{step_id}.context-thresholds"
        if hard <= floor_sum:
            problems.add(
                f"{step_where}.hard-tokens",
                f"'hard-tokens' ({hard}) must exceed the code-owned safety "
                f"margin ({safety}) plus request overhead ({overhead})",
            )
        if hard + output_cap > window:
            problems.add(
                f"{step_where}.hard-tokens",
                f"'hard-tokens' ({hard}) plus 'output-cap-tokens' "
                f"({output_cap}) must not exceed 'context-window-tokens' "
                f"({window})",
            )
        soft = policy.soft_tokens
        if soft is not None and (soft <= floor_sum or soft >= hard):
            problems.add(
                f"{step_where}.soft-tokens",
                f"'soft-tokens' ({soft}) must sit strictly between the "
                f"code-owned safety margin ({safety}) plus request overhead "
                f"({overhead}) and 'hard-tokens' ({hard})",
            )


def check_hard_handoff_routes(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
) -> None:
    """Every Hard Handoff branch stays model-free through Runtime Operations.

    From each opted-in step's `hard-context-threshold` Route every reachable
    Runtime Operation outcome — business results, the effective `error` and
    `limit-exceeded` and the conditional catalog results — must route onward
    through operation steps only to an `unfinished` or `failed` Workflow End.
    An agent successor (which would reopen model work) and any reachable
    cycle — including one bounded by an Execution Ceiling — are refused. A
    direct permitted Workflow End is valid; an unrouted result is the routes
    phase's fault, not this traversal's.

    Runs over the typed ``Workflow`` and its effective Routes.
    """
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    for step_id in sorted(workflow.steps):
        if workflow.steps[step_id].context_thresholds is None:
            continue
        start_target = routes.get(step_id, {}).get(HARD_CONTEXT_RESULT)
        if start_target is None or start_target.end is not None:
            continue
        first = start_target.step
        assert first is not None
        first_step = workflow.steps.get(first)
        if first_step is None or first_step.agent is not None:
            if first_step is not None:
                problems.add(
                    f"{workflow_where}.steps.{step_id}"
                    f".routes.{HARD_CONTEXT_RESULT}",
                    f"hard handoff cannot enter agent step {first!r}: "
                    "the fallback stays model-free",
                )
            continue
        done: set[str] = set()
        _visit_handoff_operation(
            problems, workflow_where, workflow, routes, first, [step_id, first], done
        )


def _visit_handoff_operation(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
    routes: Mapping[str, Mapping[str, RouteTarget]],
    step_id: str,
    path: list[str],
    done: set[str],
) -> None:
    """Depth-first proof of one Hard Handoff operation step's fan-out."""
    if step_id in done:
        return
    done.add(step_id)
    if step_id not in workflow.steps:
        return
    targets = routes.get(step_id, {})
    for result in workflow.result_names(step_id):
        target = targets.get(result)
        if target is None:
            continue
        if target.end is not None:
            if target.end not in HARD_HANDOFF_ENDS and result not in RESERVED_RESULTS:
                problems.add(
                    f"{workflow_where}.steps.{step_id}.routes.{result}",
                    f"hard handoff cannot end `{target.end}`: "
                    "the fallback ends `unfinished` or `failed`",
                )
            continue
        assert target.step is not None
        nxt = target.step
        nxt_step = workflow.steps.get(nxt)
        if nxt_step is None:
            continue
        if nxt_step.agent is not None:
            problems.add(
                f"{workflow_where}.steps.{step_id}.routes.{result}",
                f"hard handoff cannot enter agent step {nxt!r}: "
                "the fallback stays model-free",
            )
            continue
        if nxt in path:
            cycle = path[path.index(nxt):] + [nxt]
            problems.add(
                f"{workflow_where}.steps.{step_id}",
                "hard handoff Routes form a cycle: " + " -> ".join(cycle),
            )
            continue
        _visit_handoff_operation(
            problems, workflow_where, workflow, routes, nxt, [*path, nxt], done
        )


def check_soft_handoff_routes(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
) -> None:
    """Every soft Route enters a Reserved hard-only receiver; the receiver's
    failure Routes stay model-free through Runtime Operations.

    From each soft-configured step's `soft-context-threshold` Route the
    target must be a step-local inheriting Route to an agent Reserved Step
    carrying its own hard-only `context-thresholds` policy (all four Step
    Allowance meters come with `reserved: true`). The receiver's hard Route
    is proven by :func:`check_hard_handoff_routes`; here the receiver's
    effective `error` and `limit-exceeded` Routes — the paths taken on
    unavailable history, receiver guard or Admission refusal, reserve and
    Attempt Ceiling exhaustion, provider Context Overflow and invalid reports
    — are traversed like Hard Handoff branches: every reachable Runtime
    Operation outcome routes through operation steps only to an `unfinished`
    or `failed` Workflow End. An agent successor and any reachable cycle are
    refused; a direct permitted Workflow End is valid.

    Runs over the typed ``Workflow`` and its effective Routes.
    """
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    for step_id in sorted(workflow.steps):
        policy = workflow.steps[step_id].context_thresholds
        if policy is None or policy.soft_tokens is None:
            continue
        soft_target = routes.get(step_id, {}).get(SOFT_CONTEXT_RESULT)
        if soft_target is None:
            continue
        route_where = (
            f"{workflow_where}.steps.{step_id}.routes.{SOFT_CONTEXT_RESULT}"
        )
        if soft_target.end is not None:
            problems.add(
                route_where,
                f"{SOFT_CONTEXT_RESULT!r} cannot end `{soft_target.end}`: "
                "the soft Route enters a Reserved receiver step",
            )
            continue
        if not soft_target.inherit_conversation:
            problems.add(
                route_where,
                f"{SOFT_CONTEXT_RESULT!r} must inherit the conversation "
                "(`{step: <id>, inherit-conversation: true}`): the receiver "
                "writes its note from the working history",
            )
            continue
        receiver = soft_target.step
        assert receiver is not None
        receiver_step = workflow.steps.get(receiver)
        if receiver_step is None or receiver_step.agent is None:
            problems.add(
                route_where,
                f"soft handoff cannot enter {receiver!r}: "
                "the receiver is an agent Reserved Step",
            )
            continue
        if not receiver_step.reserved:
            problems.add(
                route_where,
                f"soft handoff cannot enter {receiver!r}: "
                "the receiver is a Reserved Step declaring all four meters",
            )
            continue
        receiver_policy = receiver_step.context_thresholds
        if receiver_policy is None or receiver_policy.soft_tokens is not None:
            problems.add(
                route_where,
                f"soft handoff cannot enter {receiver!r}: "
                "the receiver carries an explicit hard-only "
                "`context-thresholds` policy",
            )
            continue
        receiver_routes = routes.get(receiver, {})
        for result in ("error", "limit-exceeded"):
            target = receiver_routes.get(result)
            if target is None:
                continue
            if target.end is not None:
                continue
            assert target.step is not None
            nxt = target.step
            nxt_step = workflow.steps.get(nxt)
            if nxt_step is None:
                continue
            if nxt_step.agent is not None:
                problems.add(
                    f"{workflow_where}.steps.{receiver}.routes.{result}",
                    f"hard handoff cannot enter agent step {nxt!r}: "
                    "the fallback stays model-free",
                )
                continue
            done: set[str] = set()
            _visit_handoff_operation(
                problems,
                workflow_where,
                workflow,
                routes,
                nxt,
                [receiver, nxt],
                done,
            )


def must_analysis(
    start: str,
    steps: Mapping[str, WorkflowStep],
    routes: Mapping[str, Mapping[str, RouteTarget]],
) -> dict[str, dict[str, set[str]]]:
    """Entry facts: possible latest results of each step when `start` is entered.

    A refusal edge implies the refused step has already run, so the analysis
    never treats that step's latest result as absent on that edge (§3).
    """
    absent = "<absent>"
    entry: dict[str, dict[str, set[str]]] = {
        start: {name: {absent} for name in steps}
    }
    work = [start]
    while work:
        current = work.pop()
        state = entry[current]
        outgoing: list[tuple[str, dict[str, set[str]]]] = []
        for result, target in routes.get(current, {}).items():
            if target.step is not None:
                outgoing.append((target.step, {**state, current: {result}}))
        limit_target = routes.get(current, {}).get("limit-exceeded")
        prior = state.get(current, {absent}) - {absent}
        if limit_target is not None and limit_target.step is not None and prior:
            outgoing.append((limit_target.step, {**state, current: set(prior)}))
        for target_step, outcome in outgoing:
            existing = entry.get(target_step)
            if existing is None:
                entry[target_step] = {name: set(values) for name, values in outcome.items()}
                work.append(target_step)
            else:
                changed = False
                for name, values in outcome.items():
                    if not values <= existing[name]:
                        existing[name] |= values
                        changed = True
                if changed:
                    work.append(target_step)
    return entry


def _resolve_required_ref(
    workflow: Workflow, ref: str
) -> tuple[str, str, str | None] | None:
    """Re-resolve a declared required input against the typed ``Workflow``.

    Phase 1 already proved every reference with :func:`check_ref`; phase 2
    re-derives the ``(step, result, field)`` triple purely from the typed
    model so the required-input proof shares the single read. ``None`` where
    the reference no longer resolves (its phase-1 fault stands alone, with no
    cascading proof fault).
    """
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    named, result, field_name = parsed
    target = workflow.steps.get(named)
    if target is None:
        return None
    if result == "facts":
        return parsed
    if target.agent is None:
        return None
    fields = target.agent.results.get(result)
    if fields is None or field_name not in fields:
        return None
    return parsed


def required_inputs_for(
    workflow: Workflow,
) -> dict[str, list[tuple[str, tuple[str, str, str | None]]]]:
    """Every required declared input by step: ``(reference, parsed)`` (§2.5).

    Agent ``inputs`` that are required (neither the always-present
    ``input.comments`` nor the note input constrain a path), plus each
    operation ``with.body``. The single implementation the phase-2 proof
    reads from the typed ``Workflow``.
    """
    resolved: dict[str, list[tuple[str, tuple[str, str, str | None]]]] = {}
    for step_id, step in workflow.steps.items():
        required: list[tuple[str, tuple[str, str, str | None]]] = []
        if step.agent is not None:
            for declared in step.agent.inputs:
                if declared.optional:
                    continue
                if declared.reference in (ROUND_HANDOFF_NOTE_REF, INPUT_COMMENTS_REF):
                    continue
                parsed = _resolve_required_ref(workflow, declared.reference)
                if parsed is not None:
                    required.append((declared.reference, parsed))
        if step.with_body is not None:
            parsed = _resolve_required_ref(workflow, step.with_body)
            if parsed is not None:
                required.append((step.with_body, parsed))
        for target in step.routes.values():
            annotation = target.round_handoff
            if annotation is not None and annotation.kind == "ai-assisted":
                assert annotation.note is not None
                parsed = _resolve_required_ref(workflow, annotation.note)
                if parsed is not None:
                    required.append((annotation.note, parsed))
        if required:
            resolved[step_id] = required
    return resolved


def check_required_inputs(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
) -> None:
    """Every required declared input is proven on every path to its step (§3).

    Runs over the typed ``Workflow``: the required inputs resolve from the
    single read, and the entry facts flow over the effective Routes.
    """
    if not workflow.start:
        return
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    entry = must_analysis(workflow.start, workflow.steps, routes)
    for step_id, required in required_inputs_for(workflow).items():
        for ref, (named, result, field) in required:
            got = entry.get(step_id, {}).get(named, {"<absent>"})
            if result == "facts":
                if "<absent>" in got:
                    problems.add(
                        f"{workflow_where}.steps.{step_id}",
                        f"required input {ref!r} is not proven: {named!r} may be "
                        "absent on some path; declare it optional",
                    )
            elif got != {result}:
                problems.add(
                    f"{workflow_where}.steps.{step_id}",
                    f"required input {ref!r} is not proven: latest {named!r} "
                    f"may be {sorted(got)}; declare it optional",
                )


def note_inputs_for(workflow: Workflow) -> dict[str, list[str]]:
    """Where the Round Handoff note input is declared: step id -> item paths.

    Derived purely from the typed ``Workflow``: the single implementation the
    phase-2 note checks read. Item paths enumerate the typed inputs in order,
    which match the file order on every Workflow these checks run on (they
    are skipped where the Workflow already carries faults).
    """
    workflow_where = f"workflows.{workflow.id}"
    note_inputs: dict[str, list[str]] = {}
    for step_id, step in workflow.steps.items():
        if step.agent is None:
            continue
        for index, declared in enumerate(step.agent.inputs):
            if declared.reference == ROUND_HANDOFF_NOTE_REF:
                item_where = (
                    f"{workflow_where}.steps.{step_id}.agent.inputs[{index}]"
                )
                note_inputs.setdefault(step_id, []).append(item_where)
    return note_inputs


def first_agent_steps(
    workflow: Workflow,
    routes: Mapping[str, Mapping[str, RouteTarget]],
) -> set[str]:
    """Every step that can open a Continuation: the first agent step of a path.

    Follows the effective Routes from ``start`` through Runtime Operations,
    stopping each path at its first agent step or Workflow End. Only these
    steps may declare the special ``round-handoff.note`` input: a fresh
    Attempt carries no note, and every Continuation enters through one of
    them.
    """
    first: set[str] = set()
    visited: set[str] = set()
    todo = [workflow.start]
    while todo:
        current = todo.pop()
        if current in visited:
            continue
        visited.add(current)
        step = workflow.steps.get(current)
        if step is None:
            continue
        if step.agent is not None:
            first.add(current)
            continue
        if step.operation is None:
            continue
        for target in routes.get(current, {}).values():
            if target.step is not None and target.step in workflow.steps:
                todo.append(target.step)
    return first


def check_round_handoff_notes(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
) -> None:
    """Accept ``optional: round-handoff.note`` only on possible first agent steps.

    Skipped where the Workflow already carries faults: a broken graph would
    otherwise name innocent declarations. Runs over the typed ``Workflow``.
    """
    note_inputs = note_inputs_for(workflow)
    if not workflow.start or not note_inputs:
        return
    if any(
        fault.path == workflow_where or fault.path.startswith(workflow_where + ".")
        for fault in problems.items
    ):
        return
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    first = first_agent_steps(workflow, routes)
    for step_id in sorted(note_inputs):
        if step_id in first:
            continue
        for item_where in note_inputs[step_id]:
            problems.add(
                item_where,
                f"'{ROUND_HANDOFF_NOTE_REF}' may only be declared on a possible "
                "first agent step reached from `start`",
            )


def check_round_handoff_enablement(
    problems: Problems,
    workflow_where: str,
    workflow: Workflow,
) -> None:
    """Prove an enabled Workflow can publish and every Continuation reads the note.

    Requires at least one annotated publishing Route when `round-handoff` is
    present, at least one possible first agent step, and the optional note on
    every such step. Skipped where the Workflow already carries faults. Runs
    over the typed ``Workflow``.
    """
    if workflow.round_handoff is None:
        return
    if any(
        fault.path == workflow_where or fault.path.startswith(workflow_where + ".")
        for fault in problems.items
    ):
        return
    annotations = [
        (step_id, result)
        for step_id, step in workflow.steps.items()
        for result, target in workflow.effective_routes(step_id).items()
        if target.round_handoff is not None
    ]
    if not annotations:
        problems.add(
            f"{workflow_where}.round-handoff",
            "an enabled Workflow needs at least one reachable publishing Route "
            "(`round-handoff` on a `pushed`/`no-changes` Route)",
        )
        return
    routes = {sid: workflow.effective_routes(sid) for sid in workflow.steps}
    if not workflow.start:
        return
    first = first_agent_steps(workflow, routes)
    if not first:
        problems.add(
            f"{workflow_where}.round-handoff",
            "an enabled Workflow needs at least one possible first agent step",
        )
        return
    note_inputs = note_inputs_for(workflow)
    for step_id in sorted(first):
        if step_id not in note_inputs:
            problems.add(
                f"{workflow_where}.steps.{step_id}.agent.inputs",
                f"'{ROUND_HANDOFF_NOTE_REF}' must be declared on every possible "
                "first agent step of an enabled Workflow",
            )


# --- `skills` declaration (one more phase, same orchestration) ---------------

ROOT_FIELDS_V4 = frozenset({"version", "target", "models", "worker", "workflows", "skills"})
ROOT_REQUIRED_V4 = frozenset({"version", "target", "models", "workflows"})
SKILLS_FIELDS = section_keys(SkillBundleRequirement)
"""Allowed `skills` entry keys, derived from its field descriptors."""
SKILLS_REQUIRED = section_required(SkillBundleRequirement)
"""Required `skills` entry keys, derived from its field descriptors."""

UNDECLARED_SKILL_LABEL = "`skills`"
"""How the v3 undeclared-skill refusal names the declaration to check."""


def validate_skills(
    problems: Problems, data: Mapping[str, Any]
) -> tuple[tuple[SkillBundleRequirement, ...] | None, frozenset[str]]:
    """Validate the configuration-wide ``skills`` declaration (SKV3-01–03).

    Returns the parsed entries plus the declared skill names; ``None`` (with
    faults recorded) when the block is unusable. A missing ``skills`` field
    and ``skills: []`` both mean an empty Skill Bundle (SKV3-04, static
    part). Every fault names its YAML path and never echoes a
    credential-bearing source component.
    """
    if "skills" not in data:
        return (), frozenset()
    raw = data["skills"]
    if not isinstance(raw, list):
        problems.add("skills", "'skills' must be a list of {source, include, ref?} entries")
        return None, frozenset()
    parsed: list[SkillBundleRequirement] = []
    seen_sources: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for index, entry in enumerate(raw):
        where = f"skills[{index}]"
        mapping = check_keys(problems, where, entry, SKILLS_FIELDS, SKILLS_REQUIRED)
        if mapping is None:
            continue
        entry_ok = True
        descs = described_map(SkillBundleRequirement)
        source = check_leaf(problems, mapping, where, descs["source"])
        normalised: str | None = None
        if source is None:
            entry_ok = False
        else:
            try:
                normalised = normalize_skill_source(source)
            except ValueError as exc:
                problems.add(f"{where}.source", str(exc))
                entry_ok = False
        names: list[str] = []
        include_raw = mapping.get("include")
        if not isinstance(include_raw, list) or not include_raw:
            problems.add(
                f"{where}.include", "'include' must be a non-empty list of skill names"
            )
            entry_ok = False
        else:
            for position, name in enumerate(include_raw):
                item_where = f"{where}.include[{position}]"
                if not isinstance(name, str) or not name:
                    problems.add(item_where, "skill name must be a non-empty string")
                    entry_ok = False
                    continue
                first = seen_names.get(name)
                if first is not None:
                    problems.add(
                        item_where,
                        f"skill {name!r} is already declared at {first}",
                    )
                    entry_ok = False
                    continue
                seen_names[name] = item_where
                names.append(name)
        ref = check_leaf(problems, mapping, where, descs["ref"])
        if "ref" in mapping and ref is None:
            entry_ok = False
        if not entry_ok or normalised is None:
            continue
        first_source = seen_sources.get(normalised)
        if first_source is not None:
            problems.add(
                where,
                f"duplicate skill source {normalised!r}: already declared at {first_source}",
            )
            continue
        seen_sources[normalised] = where
        parsed.append(SkillBundleRequirement(source=normalised, include=tuple(names), ref=ref))
    if problems:
        return None, frozenset(seen_names)
    return tuple(parsed), frozenset(seen_names)



def validate_configuration_text(
    text: str,
    source: Path,
    workflow_id: str | None = None,
) -> tuple[WorkflowConfiguration | None, list[WorkflowConfigError]]:
    """Validate already-read configuration text; the file stays unread."""
    problems = Problems()
    try:
        data = strict_load_yaml(text)
    except WorkflowConfigError as exc:
        detail = exc.detail
        if "anchors are not allowed" in detail or "aliases are not allowed" in detail:
            detail = "anchors/aliases are not allowed"
        problems.add(exc.path, detail)
        return None, problems.items
    if not isinstance(data, dict):
        problems.add("(root)", "must be a YAML mapping at the top level")
        return None, problems.items
    check_keys(problems, "", data, ROOT_FIELDS_V4, ROOT_REQUIRED_V4)
    version = data.get("version")
    if type(version) is not int or version != 4:
        problems.add("version", f"unsupported version {version!r}: must be 4")
        return None, problems.items

    base = source.parent.resolve()
    skills, declared_names = validate_skills(problems, data)
    target = validate_target(problems, data, base)
    models = validate_models(problems, data)
    worker = validate_worker(problems, data)

    raw_workflows = data.get("workflows")
    workflows_mapping = mapping_at(raw_workflows, "workflows", problems, "'workflows'")
    if workflows_mapping is not None and not workflows_mapping:
        problems.add("workflows", "'workflows' must declare at least one workflow")
        workflows_mapping = None
    # The single read: each Workflow parses exactly once into its typed form.
    # A Workflow that is unusable (its mapping, steps, input or ceiling
    # unusable) yields no typed form; its phase-1 faults stand and phase 2
    # skips it.
    typed_workflows: dict[str, Workflow] = {}
    if workflows_mapping is not None:
        for wid in workflows_mapping:
            if check_id(wid, f"workflows.{wid}", problems, "workflow") is None:
                continue
            typed = validate_workflow(
                problems,
                str(wid),
                workflows_mapping[wid],
                models,
                base,
                declared_names,
                undeclared_skill_label=UNDECLARED_SKILL_LABEL,
            )
            if typed is not None:
                typed_workflows[str(wid)] = typed

    if workflow_id is not None and (
        workflows_mapping is None or workflow_id not in workflows_mapping
    ):
        available = ", ".join(sorted(workflows_mapping)) if workflows_mapping else ""
        problems.add(
            "workflows",
            f"unknown workflow {workflow_id!r}: available: {available}",
        )

    if target is not None and models is not None:
        pairs, endpoints = code_owned_tables()
        check_model_table(problems, models, pairs, endpoints)

    # Phase 2 proves the graph-wide rules over the typed Workflows. A
    # Workflow the single read refused carries its phase-1 faults already and
    # is skipped here. Every fault names its YAML path; there is no
    # re-parse and no "does not parse" fallback.
    for wid, workflow in typed_workflows.items():
        workflow_where = f"workflows.{wid}"
        before = len(problems)
        check_limits_arithmetic(problems, workflow_where, workflow)
        check_step_thresholds(problems, workflow_where, workflow, models)
        check_admission_floor(problems, workflow_where, workflow, models)
        check_reachability(problems, workflow_where, workflow)
        check_limit_exceeded_acyclic(problems, workflow_where, workflow)
        check_hard_handoff_routes(problems, workflow_where, workflow)
        check_soft_handoff_routes(problems, workflow_where, workflow)
        if len(problems) != before:
            continue
        check_required_inputs(problems, workflow_where, workflow)
        check_round_handoff_notes(problems, workflow_where, workflow)
        check_round_handoff_enablement(problems, workflow_where, workflow)

    # End labels never equal a Selection Label in the same file (§3).
    selection_labels: dict[str, str] = {
        wid: workflow.input.label for wid, workflow in typed_workflows.items()
    }
    if selection_labels:
        claimed = set(selection_labels.values())
        for wid, workflow in typed_workflows.items():
            for outcome, labels in workflow.outcomes.items():
                for label in labels:
                    if label in claimed:
                        owner = next(
                            owner_id
                            for owner_id, owned in selection_labels.items()
                            if owned == label
                        )
                        problems.add(
                            f"workflows.{wid}.outcomes.{outcome}",
                            f"end label {label!r} equals the Selection Label of "
                            f"workflow {owner!r}: the Worker never applies "
                            "a Selection Label",
                        )
    # A Pickup Label never equals a Selection Label or end label (§8).
    pickup_labels: dict[str, str] = {
        wid: workflow.round_handoff.pickup_label
        for wid, workflow in typed_workflows.items()
        if workflow.round_handoff is not None
    }
    if pickup_labels:
        claimed_selection = set(selection_labels.values())
        for wid, pickup in pickup_labels.items():
            for owner_id, owned in selection_labels.items():
                if pickup == owned:
                    problems.add(
                        f"workflows.{wid}.round-handoff.pickup-label",
                        f"pickup label {pickup!r} equals the Selection Label of "
                        f"workflow {owner_id!r}",
                    )
        for wid, workflow in typed_workflows.items():
            for outcome, labels in workflow.outcomes.items():
                for label in labels:
                    for owner_id, pickup in pickup_labels.items():
                        if label == pickup:
                            problems.add(
                                f"workflows.{wid}.outcomes.{outcome}",
                                f"end label {label!r} equals the Pickup Label of "
                                f"workflow {owner_id!r}",
                            )

    if problems or target is None or models is None or worker is None:
        return None, problems.items
    if workflows_mapping is None or skills is None:
        return None, problems.items
    config = WorkflowConfiguration(
        version=4,
        target=target,
        models=models,
        worker=worker,
        workflows=typed_workflows,
        skills=skills,
        source=source,
    )
    check_graph_compilation(problems, config)
    if problems:
        return None, problems.items
    return config, []
