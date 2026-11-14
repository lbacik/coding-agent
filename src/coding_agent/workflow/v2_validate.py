"""Static validation for `version: 2` step-workflow configuration files.

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

Static and offline: only the YAML file and the prompt files it names are
read. No environment variable value is read, no credential is required,
and no network call is made. Every fault is
collected as a ``WorkflowConfigError`` naming its YAML path;
``validate_v2_file`` returns all of them so the CLI can name every fault.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_agent.workflow.parser import WorkflowConfigError, strict_load_yaml
from coding_agent.workflow.prompt_files import PromptFileError, read_prompt_text
from coding_agent.workflow.v2_schema import (
    ALL_OUTCOMES,
    ALWAYS_PRESENT_TOOLS,
    CONDITIONAL_RESULTS,
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    EFFORTS as EFFORTS,
    FIELD_TYPES,
    INPUT_DRAFTS as INPUT_DRAFTS,
    INPUT_TYPES as INPUT_TYPES,
    METERS,
    OPERATIONS,
    RESERVED_RESULTS,
    REQUIRABLE,
    RUNTIME_ONLY_OUTCOMES,
    TARGET_KINDS as TARGET_KINDS,
    TOOL_GROUPS,
    YAML_ENDS,
    V2Agent,
    V2Ceiling,
    V2Config,
    V2EnumType,
    V2FieldType,
    V2Input,
    V2InputRef,
    V2Instruction,
    V2ListType,
    V2ModelDecl,
    V2Prices,
    V2RouteTarget,
    V2Step,
    V2Target,
    V2Worker,
    V2Workflow,
)

_V2_ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
"""What a model, Workflow, step, result or field id looks like (§2.7)."""

_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
"""What an environment-variable **name** looks like (§2.1).

Upper-snake on purpose: a secret-shaped value (``github_pat_…``,
``sk-ant-…``, anything with a dash, dot, slash, whitespace or lowercase
letter) never matches, so it is refused instead of being looked up.
"""

_ROOT_FIELDS = frozenset({"version", "target", "models", "worker", "workflows"})
_ROOT_REQUIRED = frozenset({"version", "target", "models", "workflows"})
_TARGET_FIELDS = frozenset({"kind", "repository", "base-branch", "token-env"})
_MODEL_FIELDS = frozenset(
    {
        "provider",
        "gateway",
        "model",
        "endpoint",
        "effort",
        "api-key-env",
        "prices-usd-per-mtok",
        "output-cap-tokens",
        "context-threshold-tokens",
    }
)
_MODEL_REQUIRED_COMMON = frozenset(
    {"model", "endpoint", "effort", "api-key-env", "prices-usd-per-mtok"}
)
"""`models.<id>` keys required on both direct and gateway pins (§2.1)."""
_GATEWAY_FIELDS = frozenset({"kind", "url"})
_PRICES_FIELDS = frozenset({"input", "output", "cache-read", "cache-write"})
_WORKER_FIELDS = frozenset({"poll-interval-seconds", "shutdown-grace-seconds"})
_WORKFLOW_FIELDS = frozenset(
    {
        "model",
        "input",
        "limits",
        "max-parallel-subagents",
        "outcomes",
        "start",
        "defaults",
        "steps",
    }
)
_WORKFLOW_REQUIRED = frozenset({"model", "input", "limits", "start", "steps"})
_INPUT_FIELDS = frozenset(
    {
        "type",
        "label",
        "require-all",
        "require-any",
        "exclude",
        "drafts",
        "skip-open-agent-pr",
    }
)
_INPUT_REQUIRED = frozenset({"type", "label"})
_DEFAULTS_FIELDS = frozenset({"max-executions", "routes"})
_STEP_FIELDS = frozenset(
    {"agent", "operation", "with", "max-executions", "limits", "reserved", "routes"}
)
_AGENT_FIELDS = frozenset({"instructions", "tools", "inputs", "results"})
_WITH_FIELDS = frozenset({"body", "draft", "requires"})

_UNSUPPORTED_STEP_KEYS = {
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


class _Problems:
    """The collected faults of one file; every fault names its YAML path."""

    def __init__(self) -> None:
        self.items: list[WorkflowConfigError] = []

    def add(self, path: str, detail: str) -> None:
        self.items.append(WorkflowConfigError(path, detail))

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


@dataclass
class _WorkflowFacts:
    """Phase-1 output for one Workflow; phase 2 proves the graph over it."""

    input_type: str | None = None
    ceiling: dict[str, float] | None = None
    routes_all: dict[str, dict[str, V2RouteTarget]] = field(default_factory=dict)
    required_inputs: dict[str, list[tuple[str, tuple[str, str, str | None]]]] = field(
        default_factory=dict
    )
    reserved_allow: dict[str, dict[str, float]] = field(default_factory=dict)
    plain_allow: dict[str, dict[str, float]] = field(default_factory=dict)
    start: str | None = None
    label: str | None = None
    outcomes: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _code_owned_tables() -> tuple[frozenset[tuple[str, str]], dict[str, frozenset[str]]]:
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


def _bundle_names() -> frozenset[str]:
    """The pinned Skill Bundle's skill names, imported lazily."""
    from coding_agent.skillbundle.verify import BUNDLE_SPEC

    return BUNDLE_SPEC.names


def _gateway_code_owned() -> tuple[
    frozenset[str], dict[str, str], int, int, int, Callable[[str], str]
]:
    """The code-owned gateway tables, imported lazily like the provider pair.

    Returns the admissible gateway kinds, the admissible endpoint per kind,
    the safety margin, the request overhead, the handoff cap and the URL
    canonicaliser. The validator stays free of the provider-adapter import at
    load time.
    """
    from coding_agent.provider.pinned_model import (
        DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP,
        DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS,
        DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS,
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
        DEFAULT_GATEWAY_SAFETY_MARGIN_TOKENS,
        DEFAULT_GATEWAY_REQUEST_OVERHEAD_TOKENS,
        DEFAULT_GATEWAY_HANDOFF_TOKEN_CAP,
        canonical_gateway_url,
    )


# --- Scalar helpers ------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mapping_at(
    value: Any, where: str, problems: _Problems, what: str
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        problems.add(where, f"expected a mapping, got {type(value).__name__}")
        return None
    return value


def _nonempty_str_at(
    value: Any, where: str, problems: _Problems, what: str
) -> str | None:
    if not isinstance(value, str) or not value:
        problems.add(where, f"{what} must be a non-empty string")
        return None
    return value


def _bool_at(value: Any, where: str, problems: _Problems, what: str) -> bool | None:
    if not isinstance(value, bool):
        problems.add(where, f"{what} must be a boolean, got {value!r}")
        return None
    return value


def _positive_number_at(
    value: Any, where: str, problems: _Problems, what: str
) -> float | None:
    if not _is_number(value) or float(value) <= 0:
        problems.add(where, f"{what} must be a positive number, got {value!r}")
        return None
    return float(value)


def _positive_int_at(value: Any, where: str, problems: _Problems, what: str) -> int | None:
    if isinstance(value, bool) or type(value) is not int or value < 1:
        problems.add(where, f"{what} must be a positive integer, got {value!r}")
        return None
    return value


def _int_ge_1_at(value: Any, where: str, problems: _Problems, what: str) -> int | None:
    """An integer `>= 1` that is never a bool, float or string (CFG-46)."""
    if isinstance(value, bool) or type(value) is not int or value < 1:
        problems.add(where, f"{what} must be an integer >= 1, got {value!r}")
        return None
    return value


def _str_list_at(
    value: Any, where: str, problems: _Problems, what: str
) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry for entry in value
    ):
        problems.add(where, f"{what} must be a list of non-empty strings")
        return None
    return list(value)


def _check_keys(
    problems: _Problems,
    where: str,
    obj: Any,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Refuse unknown fields (with targeted hints at step level) and report
    missing required fields. Returns the mapping, or None when it is not one."""
    mapping = _mapping_at(problems=problems, value=obj, what="expected a mapping", where=where)
    if mapping is None:
        return None
    for key in mapping:
        if not isinstance(key, str) or key not in allowed:
            hint = _UNSUPPORTED_STEP_KEYS.get(key) if isinstance(key, str) else None
            problems.add(
                where,
                f"unsupported field {key!r}" + (f" ({hint})" if hint else ""),
            )
    for key in sorted(required):
        if key not in mapping:
            key_path = f"{where}.{key}" if where else key
            problems.add(key_path, f"missing required field {key!r}")
    return mapping


def _check_id(value: Any, where: str, problems: _Problems, what: str) -> str | None:
    if not isinstance(value, str) or _V2_ID_RE.fullmatch(value) is None:
        problems.add(where, f"{what} id {value!r} must match [a-z0-9]+(-[a-z0-9]+)*")
        return None
    return value


# --- References ----------------------------------------------------------------


def _parse_ref(value: Any) -> tuple[str, str, str | None] | None:
    """`<step>.facts` or `<step>.<result>.<field>`; None when not a reference."""
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if not parts or any(_V2_ID_RE.fullmatch(part) is None for part in parts):
        return None
    if len(parts) == 2 and parts[1] == "facts":
        return (parts[0], "facts", None)
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    return None


def _agent_field_type(
    steps: Mapping[str, Any], step_id: str, result: str, field_name: str
) -> V2FieldType | None:
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
        return V2ListType(item=str(raw["list"]))
    if (
        isinstance(raw, dict)
        and set(raw) == {"enum"}
        and isinstance(raw["enum"], list)
        and raw["enum"]
        and all(isinstance(entry, str) for entry in raw["enum"])
    ):
        return V2EnumType(values=tuple(raw["enum"]))
    return None


def _check_ref(
    problems: _Problems,
    where: str,
    steps: Mapping[str, Any],
    ref: Any,
    *,
    document_only: bool,
) -> tuple[str, str, str | None] | None:
    parsed = _parse_ref(ref)
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
    field_type = _agent_field_type(steps, step_id, result, field_name)
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
            else ("{list: ...}" if isinstance(field_type, V2ListType) else "{enum: [...]}")
        )
        problems.add(
            where,
            f"{ref!r} has type {type_name!r}; publication bodies come only "
            "from `document` fields (Step Artifacts)",
        )
        return None
    return parsed


# --- Results and payloads --------------------------------------------------------


def _check_payload(
    problems: _Problems, where: str, results: Any
) -> dict[str, dict[str, V2FieldType]] | None:
    if not isinstance(results, dict) or not results:
        problems.add(where, "an agent step declares at least one Step Result")
        return None
    parsed: dict[str, dict[str, V2FieldType]] = {}
    for name, fields in results.items():
        result_where = f"{where}.{name}"
        if name in RESERVED_RESULTS or name in CONDITIONAL_RESULTS:
            problems.add(
                result_where,
                f"{name!r} is a runtime-reserved result and cannot be declared",
            )
            continue
        if _check_id(name, result_where, problems, "result") is None:
            continue
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            problems.add(result_where, "a result payload is a mapping of field -> type")
            continue
        parsed_fields: dict[str, V2FieldType] = {}
        for field_name, field_type in fields.items():
            field_where = f"{result_where}.{field_name}"
            if _check_id(field_name, field_where, problems, "field") is None:
                continue
            if isinstance(field_type, str) and field_type in FIELD_TYPES:
                parsed_fields[field_name] = field_type
            elif (
                isinstance(field_type, dict)
                and set(field_type) == {"list"}
                and field_type["list"] in FIELD_TYPES
            ):
                parsed_fields[field_name] = V2ListType(item=str(field_type["list"]))
            elif (
                isinstance(field_type, dict)
                and set(field_type) == {"enum"}
                and isinstance(field_type["enum"], list)
                and field_type["enum"]
                and all(isinstance(entry, str) for entry in field_type["enum"])
            ):
                parsed_fields[field_name] = V2EnumType(values=tuple(field_type["enum"]))
            else:
                problems.add(
                    field_where,
                    f"unknown field type {field_type!r} (use one of "
                    f"{', '.join(FIELD_TYPES)}, {{list: T}}, {{enum: [...]}})",
                )
        parsed[str(name)] = parsed_fields
    return parsed


# --- Per-step results ------------------------------------------------------------


def _step_results(step: Mapping[str, Any], input_type: str | None) -> list[str]:
    """Every result name a step can produce: declared or catalog results plus
    the runtime-reserved pair (§3)."""
    if "agent" in step:
        results: list[str] = []
        agent = step.get("agent")
        if isinstance(agent, dict) and isinstance(agent.get("results"), dict):
            results.extend(str(name) for name in agent["results"])
        return results + list(RESERVED_RESULTS)
    operation = step.get("operation")
    spec = OPERATIONS.get(operation) if isinstance(operation, str) else None
    if spec is None:
        return list(RESERVED_RESULTS)
    results = list(spec.business)
    if step.get("operation") == "commit-and-push" and input_type == "pull-request":
        results.append("stale-head")
    with_block = step.get("with")
    if isinstance(with_block, dict) and with_block.get("requires"):
        results.append("requirement-unmet")
    return results + list(RESERVED_RESULTS)


def _parse_route_target(
    problems: _Problems, where: str, target: Any
) -> V2RouteTarget | None:
    if isinstance(target, list):
        problems.add(where, "a Route has exactly one target; fan-out is deferred")
        return None
    if isinstance(target, dict):
        if set(target) != {"end"}:
            problems.add(where, "a Workflow End is written `{end: <category>}`")
            return None
        category = target["end"]
        if not isinstance(category, str):
            problems.add(where, f"unknown end category {category!r}")
            return None
        return V2RouteTarget(end=category)
    if isinstance(target, str):
        return V2RouteTarget(step=target)
    problems.add(where, f"unknown target {target!r}")
    return None


# --- File-level blocks -----------------------------------------------------------


def _validate_target(
    problems: _Problems, data: Mapping[str, Any]
) -> dict[str, str] | None:
    raw = data.get("target")
    mapping = _check_keys(problems, "target", raw, _TARGET_FIELDS, _TARGET_FIELDS)
    if mapping is None:
        return None
    kind = _nonempty_str_at(mapping.get("kind"), "target.kind", problems, "'target.kind'")
    repository = _nonempty_str_at(
        mapping.get("repository"), "target.repository", problems, "'target.repository'"
    )
    base_branch = _nonempty_str_at(
        mapping.get("base-branch"), "target.base-branch", problems, "'target.base-branch'"
    )
    token_env = _nonempty_str_at(
        mapping.get("token-env"), "target.token-env", problems, "'target.token-env'"
    )
    if kind is not None and kind not in TARGET_KINDS:
        problems.add(
            "target.kind", f"unsupported target kind {kind!r}: must be 'github'"
        )
    if token_env is not None and _ENV_NAME_RE.fullmatch(token_env) is None:
        problems.add(
            "target.token-env",
            "looks like a secret value, not an environment-variable name",
        )
    if (
        kind is None
        or repository is None
        or base_branch is None
        or token_env is None
    ):
        return None
    return {
        "kind": kind,
        "repository": repository,
        "base_branch": base_branch,
        "token_env": token_env,
    }


def _validate_models(
    problems: _Problems, data: Mapping[str, Any]
) -> dict[str, dict[str, Any]] | None:
    raw = data.get("models")
    mapping = _mapping_at(raw, "models", problems, "'models'")
    if mapping is None:
        return None
    if not mapping:
        problems.add("models", "'models' must declare at least one model")
        return None
    parsed: dict[str, dict[str, Any]] = {}
    for model_id in mapping:
        model_path = f"models.{model_id}"
        if _check_id(model_id, model_path, problems, "model") is None:
            continue
        entry = _check_keys(
            problems, model_path, mapping[model_id], _MODEL_FIELDS, _MODEL_REQUIRED_COMMON
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
        model = _nonempty_str_at(
            entry.get("model"), f"{model_path}.model", problems, "'model'"
        )
        endpoint = _nonempty_str_at(
            entry.get("endpoint"), f"{model_path}.endpoint", problems, "'endpoint'"
        )
        effort = _nonempty_str_at(
            entry.get("effort"), f"{model_path}.effort", problems, "'effort'"
        )
        api_key_env = _nonempty_str_at(
            entry.get("api-key-env"),
            f"{model_path}.api-key-env",
            problems,
            "'api-key-env'",
        )
        prices_raw = entry.get("prices-usd-per-mtok")
        prices_mapping = _check_keys(
            problems,
            f"{model_path}.prices-usd-per-mtok",
            prices_raw,
            _PRICES_FIELDS,
            _PRICES_FIELDS,
        )
        prices: dict[str, float] | None = None
        if prices_mapping is not None:
            prices = {}
            for meter in ("input", "output", "cache-read", "cache-write"):
                value = _positive_number_at(
                    prices_mapping.get(meter),
                    f"{model_path}.prices-usd-per-mtok.{meter}",
                    problems,
                    f"'{meter}'",
                )
                if value is None:
                    prices = None
                    break
                prices[meter] = value
        if effort is not None and effort not in EFFORTS:
            problems.add(
                f"{model_path}.effort",
                f"unknown effort {effort!r}: must be 'low', 'medium' or 'high'",
            )
            effort = None
        if api_key_env is not None and _ENV_NAME_RE.fullmatch(api_key_env) is None:
            problems.add(
                f"{model_path}.api-key-env",
                "looks like a secret value, not an environment-variable name",
            )
            api_key_env = None
        provider: str | None = None
        gateway_kind: str | None = None
        gateway_url: str | None = None
        output_cap: int | None = None
        threshold: int | None = None
        if has_gateway:
            gateway = _validate_gateway(
                problems, model_path, entry.get("gateway")
            )
            if gateway is None:
                continue
            gateway_kind, gateway_url = gateway
            output_cap = _positive_int_at(
                entry.get("output-cap-tokens"),
                f"{model_path}.output-cap-tokens",
                problems,
                "'output-cap-tokens'",
            )
            threshold = _positive_int_at(
                entry.get("context-threshold-tokens"),
                f"{model_path}.context-threshold-tokens",
                problems,
                "'context-threshold-tokens'",
            )
            if output_cap is not None and threshold is not None:
                _, _, safety, overhead, _, _ = _gateway_code_owned()
                if threshold <= safety + overhead:
                    problems.add(
                        f"{model_path}.context-threshold-tokens",
                        f"'context-threshold-tokens' ({threshold}) must exceed "
                        f"the code-owned safety margin ({safety}) plus request "
                        f"overhead ({overhead})",
                    )
                    threshold = None
        else:
            provider = _nonempty_str_at(
                entry.get("provider"), f"{model_path}.provider", problems, "'provider'"
            )
            for token_key in ("output-cap-tokens", "context-threshold-tokens"):
                if token_key in entry:
                    problems.add(
                        f"{model_path}.{token_key}",
                        f"'{token_key}' is a gateway-pin field; a direct "
                        "'provider' pin must not declare it",
                    )
                    provider = None
        if (
            model is None
            or endpoint is None
            or effort is None
            or api_key_env is None
            or prices is None
        ):
            continue
        if has_gateway:
            if gateway_kind is None or gateway_url is None:
                continue
            if output_cap is None or threshold is None:
                continue
            parsed[str(model_id)] = {
                "provider": None,
                "model": model,
                "endpoint": endpoint,
                "effort": effort,
                "api_key_env": api_key_env,
                "prices": prices,
                "gateway_kind": gateway_kind,
                "gateway_url": gateway_url,
                "output_cap_tokens": output_cap,
                "context_threshold_tokens": threshold,
            }
        else:
            if provider is None:
                continue
            parsed[str(model_id)] = {
                "provider": provider,
                "model": model,
                "endpoint": endpoint,
                "effort": effort,
                "api_key_env": api_key_env,
                "prices": prices,
                "gateway_kind": None,
                "gateway_url": None,
                "output_cap_tokens": None,
                "context_threshold_tokens": None,
            }
    return parsed


def _validate_gateway(
    problems: _Problems, model_path: str, raw: Any
) -> tuple[str, str] | None:
    """Validate a gateway pin's ``gateway`` block (§2.1).

    Returns the (kind, canonical URL) pair; ``None`` when unusable. The URL
    is canonicalised (lowercase scheme and host, no trailing ``/``), so two
    spellings of one gateway URL produce the same record.
    """
    gateway_path = f"{model_path}.gateway"
    mapping = _check_keys(problems, gateway_path, raw, _GATEWAY_FIELDS, _GATEWAY_FIELDS)
    if mapping is None:
        return None
    kinds, _, _, _, _, canonicalise = _gateway_code_owned()
    kind = _nonempty_str_at(
        mapping.get("kind"), f"{gateway_path}.kind", problems, "'gateway.kind'"
    )
    if kind is not None and kind not in kinds:
        problems.add(
            f"{gateway_path}.kind",
            f"unknown gateway kind {kind!r}: must be one of "
            f"{', '.join(sorted(kinds))}",
        )
        kind = None
    url_raw = mapping.get("url")
    url: str | None = None
    if not isinstance(url_raw, str) or not url_raw:
        problems.add(
            f"{gateway_path}.url", "'gateway.url' must be a non-empty string"
        )
    else:
        try:
            url = canonicalise(url_raw)
        except ValueError as exc:
            problems.add(f"{gateway_path}.url", str(exc))
    if kind is None or url is None:
        return None
    return kind, url


def _validate_worker(problems: _Problems, data: Mapping[str, Any]) -> V2Worker | None:
    if "worker" not in data:
        return V2Worker()
    raw = data.get("worker")
    mapping = _check_keys(problems, "worker", raw, _WORKER_FIELDS, frozenset())
    if mapping is None:
        return None
    poll = DEFAULT_POLL_INTERVAL_SECONDS
    grace = DEFAULT_SHUTDOWN_GRACE_SECONDS
    if "poll-interval-seconds" in mapping:
        value = _positive_number_at(
            mapping["poll-interval-seconds"],
            "worker.poll-interval-seconds",
            problems,
            "'poll-interval-seconds'",
        )
        if value is None:
            return None
        poll = value
    if "shutdown-grace-seconds" in mapping:
        value = _positive_number_at(
            mapping["shutdown-grace-seconds"],
            "worker.shutdown-grace-seconds",
            problems,
            "'shutdown-grace-seconds'",
        )
        if value is None:
            return None
        grace = value
    return V2Worker(poll_interval_seconds=poll, shutdown_grace_seconds=grace)


def _check_model_table(
    problems: _Problems,
    models: Mapping[str, Mapping[str, Any]],
    pairs: frozenset[tuple[str, str]],
    endpoints: Mapping[str, frozenset[str]],
) -> None:
    """Refuse a (provider, model) pair outside the code-owned table (§2.1).

    Gateway pins skip the pair table — their model name is free-form — and
    only prove their endpoint admissible for their gateway kind. The
    code-owned per-pin tables never hold gateway keys.
    """
    _, gateway_endpoints, _, _, _, _ = _gateway_code_owned()
    for model_id in sorted(models):
        entry = models[model_id]
        if entry.get("gateway_kind") is not None:
            kind = str(entry["gateway_kind"])
            endpoint = str(entry["endpoint"])
            admissible = gateway_endpoints.get(kind)
            if admissible is not None and endpoint != admissible:
                problems.add(
                    f"models.{model_id}.endpoint",
                    f"unknown endpoint {endpoint!r} for gateway kind {kind!r}: "
                    f"must be {admissible!r}",
                )
            continue
        provider = str(entry["provider"])
        model = str(entry["model"])
        endpoint = str(entry["endpoint"])
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


def _check_graph_compilation(
    problems: _Problems,
    config: V2Config,
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


def _validate_input(
    problems: _Problems, workflow_where: str, raw: Any
) -> tuple[str | None, str | None]:
    """Returns (input_type, label); each None when unusable."""
    mapping = _check_keys(
        problems, f"{workflow_where}.input", raw, _INPUT_FIELDS, _INPUT_REQUIRED
    )
    if mapping is None:
        return None, None
    input_type = mapping.get("type")
    if input_type not in INPUT_TYPES:
        problems.add(
            f"{workflow_where}.input.type",
            "must be `issue` or `pull-request` (exactly one input type per Workflow)",
        )
        input_type = None
    label = _nonempty_str_at(
        mapping.get("label"),
        f"{workflow_where}.input.label",
        problems,
        "'input.label'",
    )
    for field_name in ("require-all", "require-any", "exclude"):
        if field_name in mapping:
            entries = _str_list_at(
                mapping[field_name],
                f"{workflow_where}.input.{field_name}",
                problems,
                f"'{field_name}'",
            )
            if entries is not None and label is not None and label in entries:
                problems.add(
                    f"{workflow_where}.input.{field_name}",
                    f"the Selection Label {label!r} cannot also be a filter",
                )
    if "drafts" in mapping:
        drafts = mapping["drafts"]
        if drafts not in INPUT_DRAFTS:
            problems.add(
                f"{workflow_where}.input.drafts", "`drafts` is `include` or `exclude`"
            )
        elif input_type is not None and input_type != "pull-request":
            problems.add(
                f"{workflow_where}.input.drafts",
                "`drafts` applies only to pull-request inputs",
            )
    if "skip-open-agent-pr" in mapping:
        if _bool_at(
            mapping["skip-open-agent-pr"],
            f"{workflow_where}.input.skip-open-agent-pr",
            problems,
            "'skip-open-agent-pr'",
        ) is None:
            pass
        elif input_type is not None and input_type != "issue":
            problems.add(
                f"{workflow_where}.input.skip-open-agent-pr",
                "`skip-open-agent-pr` applies only to issue inputs",
            )
    if not isinstance(input_type, str):
        input_type = None
    return input_type, label


def _validate_ceiling(
    problems: _Problems, workflow_where: str, raw: Any
) -> dict[str, float] | None:
    mapping = _check_keys(
        problems,
        f"{workflow_where}.limits",
        raw,
        frozenset(METERS),
        frozenset(METERS),
    )
    if mapping is None:
        return None
    ceiling: dict[str, float] = {}
    for meter in METERS:
        value = _positive_number_at(
            mapping.get(meter), f"{workflow_where}.limits.{meter}", problems, f"'{meter}'"
        )
        if value is None:
            return None
        ceiling[meter] = value
    return ceiling


def _validate_outcomes(
    problems: _Problems, workflow_where: str, raw: Any
) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    mapping = _mapping_at(raw, f"{workflow_where}.outcomes", problems, "'outcomes'")
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
        entry_mapping = _check_keys(
            problems, outcome_where, entry, frozenset({"labels"}), frozenset({"labels"})
        )
        if entry_mapping is None:
            continue
        labels = _str_list_at(
            entry_mapping.get("labels"), f"{outcome_where}.labels", problems, "'labels'"
        )
        if labels is None:
            continue
        parsed[str(outcome)] = tuple(labels)
    return parsed


def _validate_instructions(
    problems: _Problems,
    step_where: str,
    raw: Any,
    base: Path,
    bundle_names: frozenset[str],
    *,
    undeclared_skill_label: str | None = None,
) -> list[V2Instruction] | None:
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
    parsed: list[V2Instruction] = []
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
            parsed.append(V2Instruction(skill=skill))
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
            parsed.append(V2Instruction(prompt=prompt))
    return parsed


def _validate_agent_tools(
    problems: _Problems, step_where: str, raw: Any
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


def _validate_agent_inputs(
    problems: _Problems,
    step_where: str,
    steps: Mapping[str, Any],
    raw: Any,
) -> list[tuple[str, tuple[str, str, str | None]]] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.add(step_where, "'inputs' must be a list of references")
        return None
    required: list[tuple[str, tuple[str, str, str | None]]] = []
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
        parsed = _check_ref(problems, item_where, steps, ref, document_only=False)
        if parsed is not None and not optional:
            required.append((ref if isinstance(ref, str) else str(ref), parsed))
    return required


def _validate_step(
    problems: _Problems,
    workflow_where: str,
    step_id: str,
    step_raw: Any,
    steps_raw: Mapping[str, Any],
    input_type: str | None,
    defaults_max: int | None,
    base: Path,
    bundle_names: frozenset[str],
    facts: _WorkflowFacts,
    *,
    undeclared_skill_label: str | None = None,
) -> None:
    step_where = f"{workflow_where}.steps.{step_id}"
    mapping = _check_keys(problems, step_where, step_raw, _STEP_FIELDS, frozenset({"routes"}))
    if mapping is None:
        return
    is_agent = "agent" in mapping
    is_operation = "operation" in mapping
    if is_agent == is_operation:
        problems.add(
            step_where, "a step has exactly one executor: `agent` or `operation`"
        )
        return

    required_inputs: list[tuple[str, tuple[str, str, str | None]]] = []
    operation: str | None = None
    with_body: str | None = None
    with_draft: bool | None = None
    with_requires: list[tuple[str, str]] = []

    if is_agent:
        agent_raw = mapping["agent"]
        agent_mapping = _check_keys(
            problems, f"{step_where}.agent", agent_raw, _AGENT_FIELDS, frozenset({"results"})
        )
        if agent_mapping is None:
            return
        instructions = _validate_instructions(
            problems, f"{step_where}.agent.instructions", agent_mapping.get("instructions"),
            base, bundle_names,
            undeclared_skill_label=undeclared_skill_label,
        )
        tools = _validate_agent_tools(
            problems, f"{step_where}.agent.tools", agent_mapping.get("tools")
        )
        agent_inputs = _validate_agent_inputs(
            problems, f"{step_where}.agent.inputs", steps_raw,
            agent_mapping.get("inputs"),
        )
        results = _check_payload(
            problems, f"{step_where}.agent.results", agent_mapping.get("results")
        )
        if instructions is None or tools is None or agent_inputs is None or results is None:
            return
        required_inputs.extend(agent_inputs)
        if "with" in mapping:
            problems.add(
                step_where,
                "`with` holds operation parameters; agent steps configure `agent`",
            )
    else:
        operation_raw = mapping["operation"]
        if not isinstance(operation_raw, str) or not operation_raw:
            problems.add(f"{step_where}.operation", "must be a non-empty string")
            return
        operation = operation_raw
        spec = OPERATIONS.get(operation)
        if spec is None:
            problems.add(
                f"{step_where}.operation",
                f"unknown Runtime Operation {operation!r} "
                f"(catalog: {', '.join(sorted(OPERATIONS))})",
            )
            return
        if spec.input_type is not None and input_type is not None and spec.input_type != input_type:
            other = "issue" if spec.input_type == "pull-request" else "pull-request"
            problems.add(
                f"{step_where}.operation",
                f"{operation!r} is available only in `{spec.input_type}` Workflows "
                f"(this one is `{other}`)",
            )
        with_raw = mapping.get("with") or {}
        with_mapping = _check_keys(problems, f"{step_where}.with", with_raw, spec.with_keys)
        if with_mapping is None:
            return
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
            parsed_body = _check_ref(
                problems, f"{step_where}.with.body", steps_raw,
                with_mapping["body"], document_only=True,
            )
            if parsed_body is not None and isinstance(with_mapping["body"], str):
                required_inputs.append((with_mapping["body"], parsed_body))
                with_body = with_mapping["body"]
        if "draft" in with_mapping:
            draft = _bool_at(
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

    facts.required_inputs[step_id] = required_inputs

    max_executions = defaults_max if defaults_max is not None else 1
    if "max-executions" in mapping:
        parsed_max = _positive_int_at(
            mapping["max-executions"],
            f"{step_where}.max-executions",
            problems,
            "'max-executions'",
        )
        if parsed_max is None:
            return
        max_executions = parsed_max

    limits: dict[str, float] = {}
    if "limits" in mapping and is_agent:
        limits_mapping = _check_keys(
            problems, f"{step_where}.limits", mapping["limits"], frozenset(METERS)
        )
        if limits_mapping is None:
            return
        for meter, value in limits_mapping.items():
            parsed_value = _positive_number_at(
                value, f"{step_where}.limits.{meter}", problems, f"'{meter}'"
            )
            if parsed_value is None:
                return
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
            return
        facts.reserved_allow[step_id] = dict(limits)
    elif reserved and is_operation:
        return
    elif is_agent and limits:
        facts.plain_allow[step_id] = dict(limits)
    if reserved and not is_agent:
        return

    # Routes.
    routes_raw = mapping.get("routes")
    routes_mapping = _mapping_at(routes_raw, f"{step_where}.routes", problems, "'routes'")
    if routes_mapping is None:
        return
    result_names = _step_results(mapping, input_type)
    effective_defaults: dict[str, V2RouteTarget] = {}
    for key, target in facts.routes_all.get("__defaults__", {}).items():
        if key in result_names:
            effective_defaults[key] = target
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
    for result in result_names:
        if result not in routes_mapping and result not in effective_defaults:
            suffix = (
                ""
                if result in RESERVED_RESULTS
                else " (business results are never defaulted)"
            )
            problems.add(
                f"{step_where}.routes", f"result {result!r} is not routed{suffix}"
            )
    merged: dict[str, V2RouteTarget] = dict(effective_defaults)
    for key, target in routes_mapping.items():
        if key not in result_names:
            continue
        parsed_target = _parse_route_target(
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
            if key in RESERVED_RESULTS and not YAML_ENDS[category]:
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
        merged[key] = parsed_target
    facts.routes_all[step_id] = merged


def _validate_defaults(
    problems: _Problems,
    workflow_where: str,
    raw: Any,
    steps_raw: Mapping[str, Any] | None,
) -> tuple[int | None, dict[str, V2RouteTarget]]:
    if raw is None:
        return 1, {}
    mapping = _check_keys(problems, f"{workflow_where}.defaults", raw, _DEFAULTS_FIELDS)
    if mapping is None:
        return None, {}
    if isinstance(raw, dict) and "limits" in raw:
        problems.add(f"{workflow_where}.defaults", "there is no `defaults.limits`")
    max_executions: int | None = 1
    if "max-executions" in mapping:
        max_executions = _positive_int_at(
            mapping["max-executions"],
            f"{workflow_where}.defaults.max-executions",
            problems,
            "'max-executions'",
        )
    routes: dict[str, V2RouteTarget] = {}
    defaults_routes = mapping.get("routes") or {}
    defaults_mapping = _mapping_at(
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
            parsed = _parse_route_target(
                problems, f"{workflow_where}.defaults.routes.{key}", target
            )
            if parsed is None:
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


def _validate_workflow(
    problems: _Problems,
    workflow_id: str,
    workflow_raw: Any,
    models: Mapping[str, Mapping[str, Any]] | None,
    base: Path,
    bundle_names: frozenset[str],
    facts: _WorkflowFacts,
    *,
    undeclared_skill_label: str | None = None,
) -> None:
    workflow_where = f"workflows.{workflow_id}"
    mapping = _check_keys(
        problems, workflow_where, workflow_raw, _WORKFLOW_FIELDS, _WORKFLOW_REQUIRED
    )
    if mapping is None:
        return
    if models is not None and mapping.get("model") not in models:
        problems.add(
            f"{workflow_where}.model",
            f"unknown model {mapping.get('model')!r}: "
            f"available: {', '.join(sorted(models))}",
        )
    input_type, label = _validate_input(problems, workflow_where, mapping.get("input"))
    facts.input_type = input_type
    facts.label = label
    ceiling = _validate_ceiling(problems, workflow_where, mapping.get("limits"))
    facts.ceiling = ceiling
    if "max-parallel-subagents" in mapping:
        _int_ge_1_at(
            mapping["max-parallel-subagents"],
            f"{workflow_where}.max-parallel-subagents",
            problems,
            "'max-parallel-subagents'",
        )
    facts.outcomes = _validate_outcomes(
        problems, workflow_where, mapping.get("outcomes")
    )

    steps_raw = mapping.get("steps")
    if not isinstance(steps_raw, dict) or not steps_raw:
        problems.add(f"{workflow_where}.steps", "a mapping of step id -> step is required")
        return
    for step_id in steps_raw:
        if _check_id(step_id, f"{workflow_where}.steps.{step_id}", problems, "step") is None:
            continue
    start = mapping.get("start")
    if start not in steps_raw:
        problems.add(f"{workflow_where}.start", f"{start!r} is not a step id")
    else:
        facts.start = str(start)

    defaults_max, defaults_routes = _validate_defaults(
        problems, workflow_where, mapping.get("defaults"), steps_raw
    )
    facts.routes_all["__defaults__"] = defaults_routes

    for step_id, step_raw in steps_raw.items():
        if not isinstance(step_id, str) or _V2_ID_RE.fullmatch(step_id) is None:
            continue
        _validate_step(
            problems,
            workflow_where,
            step_id,
            step_raw,
            steps_raw,
            input_type,
            defaults_max,
            base,
            bundle_names,
            facts,
            undeclared_skill_label=undeclared_skill_label,
        )


# --- Workflow phase 2: limits, graph, required inputs ------------------------------


def _check_limits_arithmetic(
    problems: _Problems, workflow_where: str, facts: _WorkflowFacts
) -> None:
    if facts.ceiling is None:
        return
    ceiling = facts.ceiling
    if any(meter not in ceiling for meter in METERS):
        return
    pool = {
        meter: ceiling[meter] - sum(allow.get(meter, 0.0) for allow in facts.reserved_allow.values())
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
    for step_id, allow in facts.plain_allow.items():
        for meter, value in allow.items():
            if value > pool[meter]:
                problems.add(
                    f"{workflow_where}.steps.{step_id}.limits.{meter}",
                    f"Step Allowance {value} exceeds the Work Pool {pool[meter]}",
                )


def _check_reachability(
    problems: _Problems, workflow_where: str, facts: _WorkflowFacts
) -> None:
    if facts.start is None:
        return
    routes = {sid: targets for sid, targets in facts.routes_all.items() if sid != "__defaults__"}
    seen: set[str] = set()
    todo = [facts.start]
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


def _check_limit_exceeded_acyclic(
    problems: _Problems, workflow_where: str, facts: _WorkflowFacts
) -> None:
    routes = {sid: targets for sid, targets in facts.routes_all.items() if sid != "__defaults__"}
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


def _must_analysis(
    start: str,
    steps: Mapping[str, Any],
    routes: Mapping[str, Mapping[str, V2RouteTarget]],
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


def _check_required_inputs(
    problems: _Problems,
    workflow_where: str,
    steps_raw: Mapping[str, Any],
    facts: _WorkflowFacts,
) -> None:
    if facts.start is None:
        return
    routes = {sid: targets for sid, targets in facts.routes_all.items() if sid != "__defaults__"}
    entry = _must_analysis(facts.start, steps_raw, routes)
    for step_id, required in facts.required_inputs.items():
        for ref, (named, result, _field) in required:
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


# --- Build the typed config ----------------------------------------------------------


def _build_field_type(raw: Any) -> V2FieldType:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and set(raw) == {"list"}:
        return V2ListType(item=str(raw["list"]))
    values = raw.get("enum", []) if isinstance(raw, dict) else []
    return V2EnumType(values=tuple(str(entry) for entry in values))


def _build_config(
    data: Mapping[str, Any],
    source: Path,
    target: Mapping[str, str],
    models: Mapping[str, Mapping[str, Any]],
    worker: V2Worker,
) -> V2Config:
    typed_models: dict[str, V2ModelDecl] = {}
    for model_id, entry in models.items():
        prices = entry["prices"]
        assert isinstance(prices, dict)
        provider_raw = entry.get("provider")
        gateway_kind_raw = entry.get("gateway_kind")
        gateway_url_raw = entry.get("gateway_url")
        output_cap_raw = entry.get("output_cap_tokens")
        threshold_raw = entry.get("context_threshold_tokens")
        typed_models[model_id] = V2ModelDecl(
            id=model_id,
            provider=str(provider_raw) if provider_raw is not None else None,
            model=str(entry["model"]),
            endpoint=str(entry["endpoint"]),
            effort=str(entry["effort"]),
            api_key_env=str(entry["api_key_env"]),
            prices=V2Prices(
                input=float(prices["input"]),
                output=float(prices["output"]),
                cache_read=float(prices["cache-read"]),
                cache_write=float(prices["cache-write"]),
            ),
            gateway_kind=(
                str(gateway_kind_raw) if gateway_kind_raw is not None else None
            ),
            gateway_url=str(gateway_url_raw) if gateway_url_raw is not None else None,
            output_cap_tokens=int(output_cap_raw) if output_cap_raw is not None else None,
            context_threshold_tokens=(
                int(threshold_raw) if threshold_raw is not None else None
            ),
        )
    typed_workflows: dict[str, V2Workflow] = {}
    raw_workflows = data["workflows"]
    assert isinstance(raw_workflows, dict)
    for workflow_id, workflow_raw in raw_workflows.items():
        assert isinstance(workflow_raw, dict)
        raw_input = workflow_raw["input"]
        assert isinstance(raw_input, dict)
        raw_limits = workflow_raw["limits"]
        assert isinstance(raw_limits, dict)
        raw_steps = workflow_raw["steps"]
        assert isinstance(raw_steps, dict)
        typed_steps: dict[str, V2Step] = {}
        for step_id, step_raw in raw_steps.items():
            assert isinstance(step_raw, dict)
            agent: V2Agent | None = None
            if "agent" in step_raw:
                agent_raw = step_raw["agent"]
                assert isinstance(agent_raw, dict)
                instructions: list[V2Instruction] = []
                for item in agent_raw.get("instructions") or []:
                    assert isinstance(item, dict)
                    if "skill" in item:
                        instructions.append(V2Instruction(skill=str(item["skill"])))
                    else:
                        instructions.append(V2Instruction(prompt=str(item["prompt"])))
                inputs: list[V2InputRef] = []
                for item in agent_raw.get("inputs") or []:
                    if isinstance(item, dict) and set(item) == {"optional"}:
                        inputs.append(V2InputRef(reference=str(item["optional"]), optional=True))
                    else:
                        inputs.append(V2InputRef(reference=str(item)))
                results: dict[str, dict[str, V2FieldType]] = {}
                results_raw = agent_raw.get("results") or {}
                assert isinstance(results_raw, dict)
                for result_name, fields_raw in results_raw.items():
                    fields: dict[str, V2FieldType] = {}
                    for field_name, field_raw in (fields_raw or {}).items():
                        fields[str(field_name)] = _build_field_type(field_raw)
                    results[str(result_name)] = fields
                agent = V2Agent(
                    instructions=tuple(instructions),
                    tools=tuple(str(tool) for tool in agent_raw.get("tools") or []),
                    inputs=tuple(inputs),
                    results=results,
                )
            with_raw = step_raw.get("with") or {}
            assert isinstance(with_raw, dict)
            requires: list[tuple[str, str]] = []
            for item in with_raw.get("requires") or []:
                assert isinstance(item, dict) and len(item) == 1
                key, value = next(iter(item.items()))
                requires.append((str(key), str(value)))
            routes: dict[str, V2RouteTarget] = {}
            routes_raw = step_raw.get("routes") or {}
            assert isinstance(routes_raw, dict)
            for result_name, target_raw in routes_raw.items():
                if isinstance(target_raw, dict):
                    routes[str(result_name)] = V2RouteTarget(end=str(target_raw["end"]))
                else:
                    routes[str(result_name)] = V2RouteTarget(step=str(target_raw))
            typed_steps[str(step_id)] = V2Step(
                id=str(step_id),
                agent=agent,
                operation=str(step_raw["operation"]) if "operation" in step_raw else None,
                with_body=str(with_raw["body"]) if "body" in with_raw else None,
                with_draft=bool(with_raw["draft"]) if "draft" in with_raw else None,
                with_requires=tuple(requires),
                max_executions=int(
                    step_raw.get(
                        "max-executions",
                        (workflow_raw.get("defaults") or {}).get("max-executions", 1),
                    )
                ),
                limits={
                    str(meter): float(value)
                    for meter, value in (step_raw.get("limits") or {}).items()
                },
                reserved=step_raw.get("reserved") is True,
                routes=routes,
            )
        defaults_raw = workflow_raw.get("defaults") or {}
        assert isinstance(defaults_raw, dict)
        defaults_routes: dict[str, V2RouteTarget] = {}
        for result_name, target_raw in (defaults_raw.get("routes") or {}).items():
            if isinstance(target_raw, dict):
                defaults_routes[str(result_name)] = V2RouteTarget(end=str(target_raw["end"]))
            else:
                defaults_routes[str(result_name)] = V2RouteTarget(step=str(target_raw))
        outcomes: dict[str, tuple[str, ...]] = {}
        for outcome, entry in (workflow_raw.get("outcomes") or {}).items():
            assert isinstance(entry, dict)
            outcomes[str(outcome)] = tuple(str(label) for label in entry["labels"])
        raw_ceiling = workflow_raw["limits"]
        assert isinstance(raw_ceiling, dict)
        typed_workflows[str(workflow_id)] = V2Workflow(
            id=str(workflow_id),
            model=str(workflow_raw["model"]),
            input=V2Input(
                type=str(raw_input["type"]),
                label=str(raw_input["label"]),
                require_all=tuple(str(e) for e in raw_input.get("require-all") or []),
                require_any=tuple(str(e) for e in raw_input.get("require-any") or []),
                exclude=tuple(str(e) for e in raw_input.get("exclude") or []),
                drafts=str(raw_input["drafts"]) if "drafts" in raw_input else None,
                skip_open_agent_pr=bool(raw_input["skip-open-agent-pr"])
                if "skip-open-agent-pr" in raw_input
                else None,
            ),
            ceiling=V2Ceiling(
                cost_usd=float(raw_limits["cost-usd"]),
                tokens=float(raw_limits["tokens"]),
                tool_calls=float(raw_limits["tool-calls"]),
                wall_clock_minutes=float(raw_limits["wall-clock-minutes"]),
            ),
            max_parallel_subagents=int(workflow_raw.get("max-parallel-subagents", 2)),
            outcomes=outcomes,
            start=str(workflow_raw["start"]),
            defaults_max_executions=int(defaults_raw.get("max-executions", 1)),
            defaults_routes=defaults_routes,
            steps=typed_steps,
        )
    return V2Config(
        version=2,
        target=V2Target(
            kind=target["kind"],
            repository=target["repository"],
            base_branch=target["base_branch"],
            token_env=target["token_env"],
        ),
        models=typed_models,
        worker=worker,
        workflows=typed_workflows,
        source=source,
    )


# --- Entry point -----------------------------------------------------------------------


def validate_v2_file(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[V2Config | None, list[WorkflowConfigError]]:
    """Validate a `version: 2` file; returns the config plus every fault.

    Static and offline: reads the YAML file and every prompt file it names
    as raw bytes, and makes no network call. No runtime secret is required
    or read: only credential *names* and the code-owned model tables are
    checked. The ``env`` argument is ignored and kept only for backward
    compatibility.
    The config is only returned when the fault list is empty.
    """
    source = Path(config_path)
    _ = env
    problems = _Problems()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        problems.add(str(source), f"cannot read workflow configuration: {exc}")
        return None, problems.items
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
    _check_keys(problems, "", data, _ROOT_FIELDS, _ROOT_REQUIRED)
    version = data.get("version")
    if type(version) is not int or version != 2:
        problems.add("version", f"unsupported version {version!r}: must be 2")
        return None, problems.items

    base = source.parent.resolve()
    bundle_names = _bundle_names()
    target = _validate_target(problems, data)
    models = _validate_models(problems, data)
    worker = _validate_worker(problems, data)

    raw_workflows = data.get("workflows")
    workflows_mapping = _mapping_at(raw_workflows, "workflows", problems, "'workflows'")
    if workflows_mapping is not None and not workflows_mapping:
        problems.add("workflows", "'workflows' must declare at least one workflow")
        workflows_mapping = None
    facts_by_workflow: dict[str, tuple[dict[str, Any], _WorkflowFacts]] = {}
    if workflows_mapping is not None:
        for wid in workflows_mapping:
            if _check_id(wid, f"workflows.{wid}", problems, "workflow") is None:
                continue
            facts = _WorkflowFacts()
            _validate_workflow(
                problems, str(wid), workflows_mapping[wid], models, base,
                bundle_names, facts,
            )
            if isinstance(workflows_mapping[wid], dict):
                facts_by_workflow[str(wid)] = (workflows_mapping[wid], facts)

    if workflow_id is not None and (
        workflows_mapping is None or workflow_id not in workflows_mapping
    ):
        available = ", ".join(sorted(workflows_mapping)) if workflows_mapping else ""
        problems.add(
            "workflows",
            f"unknown workflow {workflow_id!r}: available: {available}",
        )

    if target is not None and models is not None:
        pairs, endpoints = _code_owned_tables()
        _check_model_table(problems, models, pairs, endpoints)

    # Phase 2 runs per Workflow whose phase 1 left no faults behind.
    for wid, (workflow_raw, facts) in facts_by_workflow.items():
        workflow_where = f"workflows.{wid}"
        before = len(problems)
        steps_raw = workflow_raw.get("steps")
        if not isinstance(steps_raw, dict) or not steps_raw:
            continue
        _check_limits_arithmetic(problems, workflow_where, facts)
        _check_reachability(problems, workflow_where, facts)
        _check_limit_exceeded_acyclic(problems, workflow_where, facts)
        if len(problems) != before:
            continue
        _check_required_inputs(problems, workflow_where, steps_raw, facts)

    # End labels never equal a Selection Label in the same file (§3).
    selection_labels: dict[str, str] = {}
    for wid, (_raw, facts) in facts_by_workflow.items():
        if facts.label is not None:
            selection_labels[wid] = facts.label
    if selection_labels:
        claimed = set(selection_labels.values())
        for wid, (_raw, facts) in facts_by_workflow.items():
            for outcome, labels in facts.outcomes.items():
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

    if problems or target is None or models is None or worker is None:
        return None, problems.items
    if workflows_mapping is None:
        return None, problems.items
    try:
        config = _build_config(data, source, target, models, worker)
    except (KeyError, TypeError, ValueError, AssertionError) as exc:
        problems.add("(root)", f"does not parse: {exc}")
        return None, problems.items
    _check_graph_compilation(problems, config)
    if problems:
        return None, problems.items
    return config, []


def load_v2_config(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> V2Config:
    """Load and validate a `version: 2` file, raising the first fault.

    Static and offline like ``validate_v2_file``; prefer that entry point
    when every fault must be named.
    """
    config, faults = validate_v2_file(config_path, workflow_id, env=env)
    if config is None:
        raise faults[0] if faults else WorkflowConfigError("(root)", "invalid")
    return config
