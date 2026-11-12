"""JSON Schema (draft 2020-12) for the `version: 2` Workflow Configuration file.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the file)
and §3 (static validation) stay the authority; this schema is an editor aid
only and never a second validator. It covers the structural rules only:
allowed keys (``additionalProperties: false`` everywhere), required keys,
enums, scalar types and bounds, the id pattern, exactly one executor per
step, agent-only ``limits``/``reserved``, per-operation ``with`` keys and
payload field types.

The schema is built in code from the same constants the validator enforces
(``coding_agent.workflow.v2_validate`` field sets, ``coding_agent.workflow.v2_schema``
vocabulary and ``coding_agent.provider.config.PINNED_MODELS``), never edited
by hand, so the two cannot drift apart. The CLI command ``agent config
schema`` prints it; the committed copy is ``schemas/workflow-v2.schema.json``.

Out of scope (stays with ``agent config validate``): cross-references
(``start``/route targets naming real steps, ``with.body`` resolution,
``requires`` evidence), route exhaustiveness, graph analysis, limit
arithmetic, end-label vs. Selection Label disjointness, environment
variables, prompt files, Skill Bundle names and YAML-level refusals.
"""

from __future__ import annotations

import json
from typing import Any

from coding_agent.provider.config import PINNED_MODELS
from coding_agent.workflow.v2_schema import (
    ALL_OUTCOMES,
    CONDITIONAL_RESULTS,
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    EFFORTS,
    FIELD_TYPES,
    INPUT_DRAFTS,
    INPUT_TYPES,
    METERS,
    OPERATIONS,
    RESERVED_RESULTS,
    TARGET_KINDS,
    TOOL_GROUPS,
    YAML_ENDS,
)
from coding_agent.workflow.v2_validate import (
    _AGENT_FIELDS,
    _DEFAULTS_FIELDS,
    _INPUT_FIELDS,
    _INPUT_REQUIRED,
    _MODEL_FIELDS,
    _PRICES_FIELDS,
    _ROOT_FIELDS,
    _ROOT_REQUIRED,
    _STEP_FIELDS,
    _TARGET_FIELDS,
    _WITH_FIELDS,
    _WORKER_FIELDS,
    _WORKFLOW_FIELDS,
    _WORKFLOW_REQUIRED,
)

ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
"""The id shape (§2.7); anchored for JSON Schema's unanchored ``pattern``."""

GUIDE = "docs/ops/workflow-configuration.md"
"""The operator guide each description points at."""

SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
"""The draft 2020-12 meta-schema URI written into ``$schema``."""


def _sorted_required(allowed: frozenset[str], required: frozenset[str]) -> list[str]:
    """Required keys in the validator's error order (sorted)."""
    return sorted(required)


def _forbidden_results() -> list[str]:
    """Result names a Workflow never declares: reserved plus conditional."""
    seen: list[str] = []
    for name in (*RESERVED_RESULTS, *CONDITIONAL_RESULTS):
        if name not in seen:
            seen.append(name)
    return sorted(seen)


def _model_combos() -> list[dict[str, Any]]:
    """One ``properties`` const-triple per pinned (provider, model, endpoint)."""
    pins = sorted(
        PINNED_MODELS.values(), key=lambda pin: (pin.provider, pin.model, pin.endpoint)
    )
    combos: list[dict[str, Any]] = []
    for pin in pins:
        combos.append(
            {
                "properties": {
                    "provider": {"const": pin.provider},
                    "model": {"const": pin.model},
                    "endpoint": {"const": pin.endpoint},
                }
            }
        )
    return combos


def _field_type_schema() -> dict[str, Any]:
    """A payload field type: a scalar, ``{list: T}`` or ``{enum: [...]}``."""
    return {
        "description": (
            "A payload field type (§2.4): a scalar, `{list: T}` or "
            f"`{{enum: [...]}}`. Scalars: {', '.join(FIELD_TYPES)}. "
            f"See {GUIDE}."
        ),
        "anyOf": [
            {"enum": sorted(FIELD_TYPES)},
            {
                "type": "object",
                "properties": {
                    "list": {
                        "enum": sorted(FIELD_TYPES),
                        "description": "The list item scalar type.",
                    }
                },
                "required": ["list"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "enum": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "The allowed string values.",
                    }
                },
                "required": ["enum"],
                "additionalProperties": False,
            },
        ],
    }


def _route_target_schema() -> dict[str, Any]:
    """A Route value: a step id or a Workflow End (never a list)."""
    return {
        "description": (
            "A Route target (§8): a step id or a Workflow End "
            "`{end: <category>}`. Never a list: fan-out is deferred. "
            f"End categories: {', '.join(sorted(YAML_ENDS))}. See {GUIDE}."
        ),
        "anyOf": [
            {
                "type": "string",
                "pattern": ID_PATTERN,
                "description": "The target step id.",
            },
            {
                "type": "object",
                "properties": {
                    "end": {
                        "enum": sorted(YAML_ENDS),
                        "description": (
                            "The Workflow End category "
                            f"({', '.join(sorted(YAML_ENDS))})."
                        ),
                    }
                },
                "required": ["end"],
                "additionalProperties": False,
                "description": "A Workflow End.",
            },
        ],
    }


def _with_schema_for(operation: str) -> dict[str, Any]:
    """The ``with`` block schema for one catalog operation."""
    spec = OPERATIONS[operation]
    allowed = sorted(spec.with_keys)
    properties: dict[str, Any] = {}
    if "body" in spec.with_keys:
        properties["body"] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "A `document` field reference (`<step>.<result>.<field>`). "
                "The schema checks the shape only; `agent config validate` "
                f"proves it resolves. See {GUIDE}."
            ),
        }
    if "draft" in spec.with_keys:
        properties["draft"] = {
            "type": "boolean",
            "description": "Open the pull request as a draft.",
        }
    if "requires" in spec.with_keys:
        properties["requires"] = {
            "type": "array",
            "description": (
                "Requirable evidence (first release: "
                "`run-validation: passed` only)."
            ),
            "items": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 1,
                "description": "One `{operation: result}` evidence pair.",
            },
        }
    # Reject the removed shapes with the same closed vocabulary: `facts`
    # and `title` are not allowed keys, so `additionalProperties: false`
    # refuses them structurally.
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "description": (
            f"Parameters for `{operation}` (allowed: "
            f"{', '.join(allowed) if allowed else '(none)'}, "
            f"{'body required' if spec.needs_body else 'no required keys'}). "
            f"See {GUIDE}."
        ),
    }
    if spec.needs_body:
        schema["required"] = ["body"]
    return schema


def _agent_schema() -> dict[str, Any]:
    """The ``agent:`` executor block."""
    assert "results" in _AGENT_FIELDS
    return {
        "type": "object",
        "description": f"An `agent` executor block (§9). See {GUIDE}.",
        "properties": {
            "instructions": {
                "type": "array",
                "description": f"Ordered `skill:`/`prompt:` items (§9.3). See {GUIDE}.",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "skill": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "A Skill Bundle skill name.",
                                }
                            },
                            "required": ["skill"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "A prompt file path.",
                                }
                            },
                            "required": ["prompt"],
                            "additionalProperties": False,
                        },
                    ],
                },
            },
            "tools": {
                "type": "array",
                "description": (
                    "Granted tool groups "
                    f"({', '.join(TOOL_GROUPS)}). Always-present tools are "
                    "not declared. See "
                    f"{GUIDE}."
                ),
                "items": {"enum": sorted(TOOL_GROUPS)},
            },
            "inputs": {
                "type": "array",
                "description": (
                    "Step data references (`<step>.<result>.<field>`, "
                    "`<step>.facts`, or `{optional: <ref>}`). The schema "
                    "checks the shape only. See "
                    f"{GUIDE}."
                ),
                "items": {
                    "anyOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "object",
                            "properties": {
                                "optional": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "An optional input reference.",
                                }
                            },
                            "required": ["optional"],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "results": {
                "type": "object",
                "description": (
                    "The closed set of Step Results, each a payload mapping. "
                    f"Reserved names ({', '.join(_forbidden_results())}) "
                    "cannot be declared. See "
                    f"{GUIDE}."
                ),
                "minProperties": 1,
                "propertyNames": {
                    "pattern": ID_PATTERN,
                    "not": {"enum": _forbidden_results()},
                },
                "additionalProperties": {
                    "type": "object",
                    "description": "A result payload: field -> type.",
                    "propertyNames": {"pattern": ID_PATTERN},
                    "additionalProperties": _field_type_schema(),
                },
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _step_limits_schema() -> dict[str, Any]:
    """A Step Allowance: any subset of meters (all four when reserved)."""
    return {
        "type": "object",
        "description": (
            "The Step Allowance (agent steps only): any subset of meters. "
            "A Reserved Step declares all four. See "
            f"{GUIDE}."
        ),
        "properties": {
            meter: {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": f"The `{meter}` allowance.",
            }
            for meter in METERS
        },
        "additionalProperties": False,
    }


def _operation_with_branches() -> list[dict[str, Any]]:
    """One ``if``/``then`` per catalog operation for its ``with`` keys."""
    branches: list[dict[str, Any]] = []
    for operation in sorted(OPERATIONS):
        branches.append(
            {
                "if": {
                    "properties": {"operation": {"const": operation}},
                    "required": ["operation"],
                },
                "then": {"properties": {"with": _with_schema_for(operation)}},
            }
        )
    return branches


def build_v2_json_schema() -> dict[str, Any]:
    """Build the draft 2020-12 JSON Schema for a `version: 2` file.

    The schema is assembled from the validator's own vocabulary (field sets
    in ``coding_agent.workflow.v2_validate``, catalog constants in
    ``coding_agent.workflow.v2_schema`` and ``PINNED_MODELS``), so the two
    cannot drift apart. It is offline and reads no environment variables.
    """
    forbidden = _forbidden_results()
    assert forbidden
    assert _WITH_FIELDS == frozenset({"body", "draft", "requires"})
    route_target = _route_target_schema()
    field_type = _field_type_schema()
    assert field_type

    step_routes_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "Routes by result name: every result the step can produce. "
            "Keys match step/result ids; values are step ids or Workflow "
            f"Ends. See {GUIDE}."
        ),
        "propertyNames": {"pattern": ID_PATTERN},
        "additionalProperties": route_target,
    }
    defaults_routes_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "Default routes for the runtime-reserved results "
            f"(`{', '.join(sorted(RESERVED_RESULTS))}`) only. See {GUIDE}."
        ),
        "propertyNames": {"enum": sorted(RESERVED_RESULTS)},
        "additionalProperties": route_target,
    }

    step_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "One Workflow Step: exactly one executor (`agent` or "
            "`operation`) plus the control keys. `limits`/`reserved` appear "
            f"on agent steps only. See {GUIDE}."
        ),
        "properties": {
            "agent": _agent_schema(),
            "operation": {
                "enum": sorted(OPERATIONS),
                "description": (
                    "A catalog Runtime Operation "
                    f"({', '.join(sorted(OPERATIONS))}). See {GUIDE}."
                ),
            },
            "with": {
                "type": "object",
                "description": (
                    "Operation parameters; allowed keys depend on "
                    f"`operation`. See {GUIDE}."
                ),
                "properties": {
                    "body": {
                        "type": "string",
                        "minLength": 1,
                        "description": "A `document` field reference.",
                    },
                    "draft": {
                        "type": "boolean",
                        "description": "Open the pull request as a draft.",
                    },
                    "requires": {
                        "type": "array",
                        "description": "Requirable evidence.",
                        "items": {
                            "type": "object",
                            "minProperties": 1,
                            "maxProperties": 1,
                        },
                    },
                },
                "additionalProperties": False,
            },
            "max-executions": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": (
                    "The Execution Ceiling (default from `defaults`, else 1). "
                    f"See {GUIDE}."
                ),
            },
            "limits": _step_limits_schema(),
            "reserved": {
                "type": "boolean",
                "default": False,
                "description": (
                    "A Reserved Step (agent steps only): declares all four "
                    f"meters. See {GUIDE}."
                ),
            },
            "routes": step_routes_schema,
        },
        "required": ["routes"],
        "additionalProperties": False,
        "oneOf": [
            {
                "description": "An agent step.",
                "required": ["agent"],
                "not": {"required": ["operation"]},
            },
            {
                "description": "An operation step.",
                "required": ["operation"],
                "not": {"required": ["agent"]},
            },
        ],
        "allOf": [
            {
                "if": {"required": ["operation"]},
                "then": {
                    "not": {
                        "anyOf": [{"required": ["limits"]}, {"required": ["reserved"]}]
                    },
                    "description": (
                        "`limits`/`reserved` apply to agent steps only."
                    ),
                },
            },
            {
                "if": {"required": ["agent"]},
                "then": {
                    "not": {"required": ["with"]},
                    "description": (
                        "`with` holds operation parameters; agent steps "
                        "configure `agent`."
                    ),
                },
            },
            {
                "if": {
                    "properties": {"reserved": {"const": True}},
                    "required": ["reserved"],
                },
                "then": {
                    "required": ["limits"],
                    "properties": {"limits": {"required": sorted(METERS)}},
                    "description": "A Reserved Step declares all four meters.",
                },
            },
            *_operation_with_branches(),
        ],
    }

    workflow_schema: dict[str, Any] = {
        "type": "object",
        "description": f"One Workflow: policy for one scenario (§2.2). See {GUIDE}.",
        "properties": {
            "model": {
                "type": "string",
                "pattern": ID_PATTERN,
                "description": (
                    "The id of one `models` entry: the Attempt's one Pinned "
                    f"Model. See {GUIDE}."
                ),
            },
            "input": {
                "type": "object",
                "description": f"Which Workflow Inputs the Worker selects. See {GUIDE}.",
                "properties": {
                    "type": {
                        "enum": sorted(INPUT_TYPES),
                        "description": "Exactly one input type per Workflow.",
                    },
                    "label": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The Selection Label.",
                    },
                    "require-all": {
                        "type": "array",
                        "description": "Narrowing filter: all of these labels.",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "require-any": {
                        "type": "array",
                        "description": "Narrowing filter: any of these labels.",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "exclude": {
                        "type": "array",
                        "description": "Excluded labels.",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "drafts": {
                        "enum": sorted(INPUT_DRAFTS),
                        "description": (
                            "Draft eligibility (pull-request inputs only)."
                        ),
                    },
                    "skip-open-agent-pr": {
                        "type": "boolean",
                        "description": (
                            "Skip an issue with an open agent pull request "
                            "(issue inputs only)."
                        ),
                    },
                },
                "required": _sorted_required(_INPUT_FIELDS, _INPUT_REQUIRED),
                "additionalProperties": False,
            },
            "limits": {
                "type": "object",
                "description": (
                    "The Attempt Ceiling: all four meters are required. See "
                    f"{GUIDE}."
                ),
                "properties": {
                    meter: {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": f"The `{meter}` ceiling.",
                    }
                    for meter in METERS
                },
                "required": sorted(METERS),
                "additionalProperties": False,
            },
            "max-parallel-subagents": {
                "type": "integer",
                "minimum": 1,
                "default": DEFAULT_MAX_PARALLEL_SUBAGENTS,
                "description": (
                    "How many Sub-agents of one group may run at once. See "
                    f"{GUIDE}."
                ),
            },
            "outcomes": {
                "type": "object",
                "description": (
                    "End labels per Terminal Outcome "
                    f"({', '.join(ALL_OUTCOMES)}). There is no `canceled`. "
                    f"See {GUIDE}."
                ),
                "propertyNames": {"enum": sorted(ALL_OUTCOMES)},
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "labels": {
                            "type": "array",
                            "description": "Labels applied for this outcome.",
                            "items": {"type": "string", "minLength": 1},
                        }
                    },
                    "required": ["labels"],
                    "additionalProperties": False,
                },
            },
            "start": {
                "type": "string",
                "pattern": ID_PATTERN,
                "description": f"The id of the first step. See {GUIDE}.",
            },
            "defaults": {
                "type": "object",
                "description": (
                    "`max-executions` and `routes` for the reserved results "
                    f"only. No `defaults.limits`. See {GUIDE}."
                ),
                "properties": {
                    "max-executions": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "The default Execution Ceiling.",
                    },
                    "routes": defaults_routes_schema,
                },
                "required": [],
                "additionalProperties": False,
            },
            "steps": {
                "type": "object",
                "description": f"A map from step id to step (§2.3). See {GUIDE}.",
                "minProperties": 1,
                "propertyNames": {"pattern": ID_PATTERN},
                "additionalProperties": step_schema,
            },
        },
        "required": _sorted_required(_WORKFLOW_FIELDS, _WORKFLOW_REQUIRED),
        "additionalProperties": False,
    }

    model_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "One `models.<id>` entry: a Pinned Model (§2.1). The "
            "(provider, model, endpoint) triple must be code-owned. See "
            f"{GUIDE}."
        ),
        "properties": {
            "provider": {
                "type": "string",
                "description": "The model provider.",
            },
            "model": {
                "type": "string",
                "description": "The pinned model id.",
            },
            "endpoint": {
                "type": "string",
                "description": "The pinned endpoint (part of the pin).",
            },
            "effort": {
                "enum": sorted(EFFORTS),
                "description": "The pinned effort level.",
            },
            "api-key-env": {
                "type": "string",
                "minLength": 1,
                "description": "The environment-variable name holding the key.",
            },
            "prices-usd-per-mtok": {
                "type": "object",
                "description": "Per-bucket prices in USD per million tokens.",
                "properties": {
                    "input": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "The input price.",
                    },
                    "output": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "The output price.",
                    },
                    "cache-read": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "The cache-read price.",
                    },
                    "cache-write": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "The cache-write price.",
                    },
                },
                "required": sorted(_PRICES_FIELDS),
                "additionalProperties": False,
            },
        },
        "required": sorted(_MODEL_FIELDS),
        "additionalProperties": False,
        "allOf": [{"anyOf": _model_combos()}],
    }

    schema: dict[str, Any] = {
        "$schema": SCHEMA_URI,
        "$id": "https://example.invalid/schemas/workflow-v2.schema.json",
        "title": "Workflow Configuration (version: 2)",
        "description": (
            "Editor aid for the `version: 2` Workflow Configuration file. "
            "Structure only; `agent config validate` remains required "
            f"(contract §3). See {GUIDE}."
        ),
        "type": "object",
        "properties": {
            "version": {
                "const": 2,
                "description": f"Must be `2`. See {GUIDE}.",
            },
            "target": {
                "type": "object",
                "description": f"The Target Repository and credential. See {GUIDE}.",
                "properties": {
                    "kind": {
                        "enum": sorted(TARGET_KINDS),
                        "description": "The target kind.",
                    },
                    "repository": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The `OWNER/REPO` repository.",
                    },
                    "base-branch": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The Base Revision branch.",
                    },
                    "token-env": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "The environment-variable name holding the "
                            "GitHub token."
                        ),
                    },
                },
                "required": sorted(_TARGET_FIELDS),
                "additionalProperties": False,
            },
            "models": {
                "type": "object",
                "description": f"Pinned Model definitions, referenced by id. See {GUIDE}.",
                "minProperties": 1,
                "propertyNames": {"pattern": ID_PATTERN},
                "additionalProperties": model_schema,
            },
            "worker": {
                "type": "object",
                "description": f"Optional worker settings. See {GUIDE}.",
                "properties": {
                    "poll-interval-seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "default": DEFAULT_POLL_INTERVAL_SECONDS,
                        "description": "Idle sleep between selection sweeps.",
                    },
                    "shutdown-grace-seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "default": DEFAULT_SHUTDOWN_GRACE_SECONDS,
                        "description": "SIGTERM grace for the live Attempt.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            "workflows": {
                "type": "object",
                "description": f"Workflow id -> Workflow. See {GUIDE}.",
                "minProperties": 1,
                "propertyNames": {"pattern": ID_PATTERN},
                "additionalProperties": workflow_schema,
            },
        },
        "required": _sorted_required(_ROOT_FIELDS, _ROOT_REQUIRED),
        "additionalProperties": False,
    }
    # Touch the remaining imported field sets so a rename breaks loudly
    # instead of silently widening the schema.
    assert _ROOT_FIELDS == frozenset(
        {"version", "target", "models", "worker", "workflows"}
    )
    assert _WORKFLOW_FIELDS >= {"model", "input", "limits", "start", "steps"}
    assert _STEP_FIELDS >= {"agent", "operation", "routes"}
    assert _AGENT_FIELDS >= {"results"}
    assert _DEFAULTS_FIELDS >= {"max-executions", "routes"}
    assert _INPUT_FIELDS >= {"type", "label"}
    assert _WORKER_FIELDS >= set()
    return schema


def dump_v2_json_schema() -> str:
    """The committed schema text: pretty-printed with stable key order."""
    return json.dumps(build_v2_json_schema(), indent=2, sort_keys=True) + "\n"
