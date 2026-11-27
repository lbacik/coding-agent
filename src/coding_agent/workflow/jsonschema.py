"""JSON Schema (draft 2020-12) for the `version: 4` Workflow Configuration file.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the file)
and §3 (static validation) stay the authority; this schema is an editor aid
only and never a second validator. It covers the structural rules only:
allowed keys (``additionalProperties: false`` everywhere), required keys,
enums, scalar types and bounds, the id pattern, exactly one executor per
step, agent-only ``limits``/``reserved``, per-operation ``with`` keys,
payload field types and the ``skills`` declaration shape.

The schema is built in code from the same validator vocabulary (field sets
in ``coding_agent.workflow._checks``, catalog constants in
``coding_agent.workflow.schema`` and the provider-to-endpoint table), never
edited by hand, so the two cannot drift apart. The CLI command
``agent config schema`` prints it; the committed copy is
``schemas/workflow-v4.schema.json``.

Out of scope (stays with ``agent config validate``): cross-references
(``start``/route targets naming real steps, ``with.body`` resolution,
``requires`` evidence), route exhaustiveness, graph analysis, limit
arithmetic, end-label vs. Selection Label disjointness, environment
variables, prompt files, skill-source identity and uniqueness, Skill Bundle
membership names and YAML-level refusals.
"""

from __future__ import annotations

import json
from typing import Any

from coding_agent.workflow.schema import (
    ALL_OUTCOMES,
    ALWAYS_PRESENT_TOOLS,
    CONDITIONAL_RESULTS,
    FIELD_TYPES,
    METERS,
    OPERATIONS,
    RESERVED_RESULTS,
    ROUND_HANDOFF_KINDS,
    TOOL_GROUPS,
    YAML_ENDS,
    AttemptCeiling,
    InputDecl,
    ModelDecl,
    ModelPrices,
    RoundHandoffConfig,
    SkillBundleRequirement,
    TargetRepository,
    WorkerSettings,
    Workflow,
    WorkflowStep,
)
from coding_agent.workflow._checks import (
    AGENT_FIELDS,
    CEILING_FIELDS,
    CEILING_REQUIRED,
    DEFAULTS_FIELDS,
    GATEWAY_FIELDS,
    INPUT_FIELDS,
    INPUT_REQUIRED,
    MODEL_FIELDS,
    MODEL_REQUIRED_COMMON,
    PRICES_FIELDS,
    ROOT_FIELDS,
    ROOT_FIELDS_V4,
    ROOT_REQUIRED,
    ROOT_REQUIRED_V4,
    STEP_FIELDS,
    STEP_REQUIRED,
    WITH_FIELDS,
    WORKER_FIELDS,
    WORKFLOW_FIELDS,
    WORKFLOW_REQUIRED,
)
from coding_agent.workflow._fields import (
    GUIDE,
    ID_PATTERN,
    schema_properties,
    schema_required,
    section_required,
)

SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
"""The draft 2020-12 meta-schema URI written into ``$schema``."""


def _sorted_required(required: frozenset[str]) -> list[str]:
    """Required keys in the validator's error order (sorted)."""
    return sorted(required)


def _with_properties() -> dict[str, Any]:
    """The shared `with` parameter shapes (body/draft/requires)."""
    return {
        "body": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A `document` field reference (`<step>.<result>.<field>`). "
                "The schema checks the shape only; `agent config validate` "
                f"proves it resolves. See {GUIDE}."
            ),
        },
        "draft": {
            "type": "boolean",
            "description": "Open the pull request as a draft.",
        },
        "requires": {
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
        },
    }


def _forbidden_results() -> list[str]:
    """Result names a Workflow never declares: reserved plus conditional."""
    seen: list[str] = []
    for name in (*RESERVED_RESULTS, *CONDITIONAL_RESULTS):
        if name not in seen:
            seen.append(name)
    return sorted(seen)


def _model_combos() -> list[dict[str, Any]]:
    """One ``properties`` constraint per admissible provider and endpoint.

    A direct pin names any non-empty model for a known provider; the branch
    constrains ``provider`` and ``endpoint`` only and leaves ``model`` a free
    string (the validator proves the rest). The gateway branch below admits a
    ``litellm`` gateway with the ``responses`` endpoint and a free-form model
    name.
    """
    from coding_agent.provider.pinned_model import GATEWAY_ENDPOINTS, PROVIDER_ENDPOINTS

    combos: list[dict[str, Any]] = []
    for provider, endpoint in sorted(PROVIDER_ENDPOINTS.items()):
        combos.append(
            {
                "properties": {
                    "provider": {"const": provider},
                    "model": {"type": "string", "minLength": 1},
                    "endpoint": {"const": endpoint},
                },
                # A branch matches only a pin of its own kind: without the
                # presence guard a `properties`-only gateway branch would
                # vacuously match a direct pin carrying the same endpoint.
                "required": ["provider"],
            }
        )
    for kind, endpoint in sorted(GATEWAY_ENDPOINTS.items()):
        combos.append(
            {
                "properties": {
                    "gateway": {
                        "properties": {"kind": {"const": kind}},
                        "required": ["kind"],
                    },
                    "endpoint": {"const": endpoint},
                },
                "required": ["gateway"],
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


def _route_target_schema(*, allow_inherit: bool = True) -> dict[str, Any]:
    """A Route value: a step id or a Workflow End (never a list).

    Step-local Routes accept a strict step-target object
    ``{step: <id>, inherit-conversation?: <bool>}``; only ``true`` opts
    into Conversation Inheritance (§8). End targets stay disjoint
    (``{end: <category>}`` only). ``defaults.routes`` names scalar step
    ids and end objects only, so it uses ``allow_inherit=False``.
    """
    branches: list[dict[str, Any]] = [
        {
            "type": "string",
            "pattern": ID_PATTERN,
            "description": "The target step id.",
        },
    ]
    if allow_inherit:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "string",
                        "pattern": ID_PATTERN,
                        "description": "The target step id.",
                    },
                    "inherit-conversation": {
                        "type": "boolean",
                        "description": (
                            "Opt into Conversation Inheritance (§8): "
                            "only `true` inherits; omitted and `false` "
                            "are fresh."
                        ),
                    },
                },
                "required": ["step"],
                "additionalProperties": False,
                "description": (
                    "A step target, optionally inheriting the source "
                    "conversation (`{step: <id>, "
                    "inherit-conversation: <bool>}`)."
                ),
            }
        )
    branches.append(
        {
            "type": "object",
            "properties": {
                "end": {
                    "enum": sorted(YAML_ENDS),
                    "description": (
                        "The Workflow End category "
                        f"({', '.join(sorted(YAML_ENDS))})."
                    ),
                },
                "round-handoff": {
                    "type": "object",
                    "description": (
                        "A step-local Round Handoff annotation (§8): `ai-assisted` "
                        "publishes the declared note Step Artifact, `hard` "
                        "publishes the model-free runtime account. Valid only on "
                        "a `pushed`/`no-changes` Route from `commit-and-push` in "
                        "an issue Workflow; `agent config validate` proves the "
                        f"placement. See {GUIDE}."
                    ),
                    "properties": {
                        "kind": {
                            "enum": sorted(ROUND_HANDOFF_KINDS),
                            "description": "How the round publishes.",
                        },
                        "note": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "The published note reference "
                                "`<step>.<result>.<field>` (`ai-assisted` only)."
                            ),
                        },
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                    "allOf": [
                        {
                            "if": {
                                "properties": {"kind": {"const": "ai-assisted"}}
                            },
                            "then": {"required": ["kind", "note"]},
                        },
                        {
                            "if": {"properties": {"kind": {"const": "hard"}}},
                            "then": {"not": {"required": ["note"]}},
                        },
                    ],
                },
            },
            "required": ["end"],
            "additionalProperties": False,
            "description": "A Workflow End.",
        },
    )
    return {
        "description": (
            "A Route target (§8): a step id or a Workflow End "
            "`{end: <category>}`. Never a list: fan-out is deferred. "
            f"End categories: {', '.join(sorted(YAML_ENDS))}. See {GUIDE}."
        ),
        "anyOf": branches,
    }


def _with_schema_for(operation: str) -> dict[str, Any]:
    """The ``with`` block schema for one catalog operation."""
    spec = OPERATIONS[operation]
    allowed = sorted(spec.with_keys)
    shared = _with_properties()
    properties = {key: shared[key] for key in allowed if key in shared}
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
    assert "results" in AGENT_FIELDS
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
                    f"({', '.join(TOOL_GROUPS)}). Always-present tools "
                    f"({', '.join(ALWAYS_PRESENT_TOOLS)}) are "
                    "not declared. See "
                    f"{GUIDE}."
                ),
                "items": {
                    "enum": sorted(set(TOOL_GROUPS) - set(ALWAYS_PRESENT_TOOLS))
                },
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
            "advisor": {
                "type": "object",
                "description": (
                    "The step's Advisor declaration (§9). Required exactly "
                    "where the `advisor` tool group is declared. See "
                    f"{GUIDE}."
                ),
                "properties": {
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "description": "One operator prompt file (§2.8).",
                    },
                    "model": {
                        "type": "string",
                        "pattern": ID_PATTERN,
                        "description": (
                            "A `models` id answering the Advisor "
                            "(else the Pinned Model)."
                        ),
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
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


def _step_context_thresholds_schema() -> dict[str, Any]:
    """An agent step's own Hard Context Threshold policy (agent steps only).

    Shape only: ``agent config validate`` proves the agent-only
    applicability, the pin-dependent bounds (soft strictly between the
    safety margin plus overhead and hard) and the step-local Routes
    (contract §3). A step that omits the policy keeps the Pinned Model's
    Context Threshold and existing outcomes.
    """
    return {
        "type": "object",
        "description": (
            "The step's own Hard Context Threshold policy (agent steps only): "
            "required `hard-tokens`, optional `soft-tokens`. "
            f"See {GUIDE}."
        ),
        "properties": {
            "hard-tokens": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "The step's Hard Context Threshold in tokens: the next "
                    "main-conversation request at or below it passes; above "
                    "it the Step Execution reports `hard-context-threshold`."
                ),
            },
            "soft-tokens": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "The step's Soft Context Threshold in tokens: strictly "
                    "between the safety margin plus overhead and "
                    "`hard-tokens`. A request above it but at or below hard "
                    "reports `soft-context-threshold` onto the inheriting "
                    "Route to the Reserved receiver step."
                ),
            },
        },
        "required": ["hard-tokens"],
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


def _skills_schema() -> dict[str, Any]:
    """The configuration-wide ``skills`` declaration (SKV3-01).

    A list of ``{source, include, ref?}`` entries. The schema checks the
    shape only: ``agent config validate`` proves the Source Identities,
    uniqueness and membership.
    """
    return {
        "type": "array",
        "description": (
            "The Skill Bundle membership (SKV3-01): one entry per skill "
            "source. `source` is an HTTPS Git URL (`https://` or "
            "`git+https://`); `include` names its exact member skills; `ref` "
            "optionally pins the entry. A missing field and `[]` both mean "
            "an empty Skill Bundle. See "
            f"{GUIDE}."
        ),
        "items": {
            "type": "object",
            "description": (
                "One skill source: its URL, its exact member skill names "
                "and its optional pin."
            ),
            "properties": schema_properties(SkillBundleRequirement),
            "required": schema_required(SkillBundleRequirement),
            "additionalProperties": False,
        },
    }


def _round_handoff_schema() -> dict[str, Any]:
    """The optional issue-only ``round-handoff`` enablement mapping (§8).

    ``max-automatic-pickups`` is the required positive limit fixed by the
    human-authorised Attempt for the series; ``pickup-label`` is the Pickup
    Label, defaulting to ``round-finished``. Cross-label disjointness and
    publishing-Route reachability stay with ``agent config validate``.
    """
    return {
        "type": "object",
        "description": (
            "Round Handoff enablement for an issue Workflow: publication and "
            "automatic pickup under a Pickup Label with a positive pickup "
            f"limit. Omitted disables both. See {GUIDE}."
        ),
        "properties": schema_properties(RoundHandoffConfig, "round-handoff."),
        "required": schema_required(RoundHandoffConfig, "round-handoff."),
        "additionalProperties": False,
    }


def build_workflow_json_schema() -> dict[str, Any]:
    """Build the draft 2020-12 JSON Schema for a `version: 4` file.

    The schema is assembled from the validator's own vocabulary (field sets
    in ``coding_agent.workflow._checks``, catalog constants in
    ``coding_agent.workflow.schema`` and the provider-to-endpoint table), so
    the two cannot drift apart. It is offline and reads no environment
    variables.
    """
    forbidden = _forbidden_results()
    assert forbidden
    assert WITH_FIELDS == frozenset({"body", "draft", "requires"})
    assert not (set(TOOL_GROUPS) & set(ALWAYS_PRESENT_TOOLS))
    route_target = _route_target_schema(allow_inherit=True)
    defaults_route_target = _route_target_schema(allow_inherit=False)

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
        "additionalProperties": defaults_route_target,
    }

    step_props = schema_properties(WorkflowStep)
    step_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "One Workflow Step: exactly one executor (`agent` or "
            "`operation`) plus the control keys. `limits`/`reserved`/"
            f"`context-thresholds` appear on agent steps only. See {GUIDE}."
        ),
        "properties": {
            "agent": _agent_schema(),
            "operation": step_props["operation"],
            "with": {
                "type": "object",
                "description": (
                    "Operation parameters; allowed keys depend on "
                    f"`operation`. See {GUIDE}."
                ),
                "properties": _with_properties(),
                "additionalProperties": False,
            },
            "max-executions": step_props["max-executions"],
            "limits": _step_limits_schema(),
            "reserved": step_props["reserved"],
            "context-thresholds": _step_context_thresholds_schema(),
            "routes": step_routes_schema,
        },
        "required": _sorted_required(STEP_REQUIRED),
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
                        "anyOf": [
                            {"required": ["limits"]},
                            {"required": ["reserved"]},
                            {"required": ["context-thresholds"]},
                        ]
                    },
                    "description": (
                        "`limits`/`reserved`/`context-thresholds` apply to "
                        "agent steps only."
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

    workflow_props = schema_properties(Workflow)
    workflow_schema: dict[str, Any] = {
        "type": "object",
        "description": f"One Workflow: policy for one scenario (§2.2). See {GUIDE}.",
        "properties": {
            "model": workflow_props["model"],
            "input": {
                "type": "object",
                "description": f"Which Workflow Inputs the Worker selects. See {GUIDE}.",
                "properties": schema_properties(InputDecl),
                "required": _sorted_required(INPUT_REQUIRED),
                "additionalProperties": False,
            },
            "limits": {
                "type": "object",
                "description": (
                    "The Attempt Ceiling: all four meters are required. See "
                    f"{GUIDE}."
                ),
                "properties": schema_properties(AttemptCeiling),
                "required": _sorted_required(CEILING_REQUIRED),
                "additionalProperties": False,
            },
            "max-parallel-subagents": workflow_props["max-parallel-subagents"],
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
            "start": workflow_props["start"],
            "round-handoff": _round_handoff_schema(),
            "defaults": {
                "type": "object",
                "description": (
                    "`max-executions` and `routes` for the reserved results "
                    f"only. No `defaults.limits`. See {GUIDE}."
                ),
                "properties": {
                    "max-executions": schema_properties(Workflow, "defaults.")[
                        "max-executions"
                    ],
                    "routes": defaults_routes_schema,
                },
                "required": schema_required(Workflow, "defaults."),
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
        "required": _sorted_required(WORKFLOW_REQUIRED),
        "additionalProperties": False,
    }

    model_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "One `models.<id>` entry: a Pinned Model (§2.1) — either a direct "
            "`provider` pin, whose provider and endpoint must be "
            "code-owned while `model` is free-form, or a `gateway` pin. "
            "Every pin carries its own "
            "Model Context Window, output cap and context-window "
            "threshold. See "
            f"{GUIDE}."
        ),
        "properties": {
            **schema_properties(ModelDecl),
            "gateway": {
                "type": "object",
                "description": (
                    "The Model Gateway (gateway pins only): its kind and "
                    "address — exactly one of `url` (a literal "
                    "`http`/`https` URL) or `url-env` (the "
                    "environment-variable name holding it)."
                ),
                "properties": schema_properties(ModelDecl, "gateway."),
                "required": ["kind"],
                "additionalProperties": False,
                "oneOf": [
                    {
                        "description": "A literal gateway URL.",
                        "required": ["url"],
                        "not": {"required": ["url-env"]},
                    },
                    {
                        "description": "A gateway URL from the environment.",
                        "required": ["url-env"],
                        "not": {"required": ["url"]},
                    },
                ],
            },
            "prices-usd-per-mtok": {
                "type": "object",
                "description": "Per-bucket prices in USD per million tokens.",
                "properties": schema_properties(ModelPrices),
                "required": sorted(PRICES_FIELDS),
                "additionalProperties": False,
            },
        },
        "required": sorted(MODEL_REQUIRED_COMMON),
        "additionalProperties": False,
        "allOf": [
            {"anyOf": _model_combos()},
            {
                "oneOf": [
                    {
                        "description": "A direct `provider` pin.",
                        "required": ["provider"],
                        "not": {"required": ["gateway"]},
                    },
                    {
                        "description": "A `gateway` pin.",
                        "required": ["gateway"],
                        "not": {"required": ["provider"]},
                    },
                ]
            },
        ],
    }
    # The dotted `gateway.*` descriptors group under `gateway`; every other
    # `models.<id>` descriptor is a top-level property. A regrouping mistake
    # would silently drop a key from the schema, so both groupings are pinned.
    assert set(model_schema["properties"]) == set(MODEL_FIELDS)
    gateway_props = model_schema["properties"]["gateway"]
    assert isinstance(gateway_props, dict)
    assert set(gateway_props["properties"]) == set(GATEWAY_FIELDS)

    schema: dict[str, Any] = {
        "$schema": SCHEMA_URI,
        "$id": "https://example.invalid/schemas/workflow-v4.schema.json",
        "title": "Workflow Configuration (version: 4)",
        "description": (
            "Editor aid for the `version: 4` Workflow Configuration file: "
            "every pin declares its token budget plus the "
            "configuration-wide `skills` declaration. Structure only; "
            f"`agent config validate` remains required (contract §3). See {GUIDE}."
        ),
        "type": "object",
        "properties": {
            "version": {
                "const": 4,
                "description": f"Must be `4`. See {GUIDE}.",
            },
            "target": {
                "type": "object",
                "description": f"The Target Repository and credential. See {GUIDE}.",
                "properties": schema_properties(TargetRepository),
                "required": sorted(section_required(TargetRepository)),
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
                "properties": schema_properties(WorkerSettings),
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
            "skills": _skills_schema(),
        },
        "required": _sorted_required(ROOT_REQUIRED_V4),
        "additionalProperties": False,
    }
    # Touch the remaining imported field sets so a rename breaks loudly
    # instead of silently widening the schema.
    assert ROOT_FIELDS_V4 == frozenset(
        {"version", "target", "models", "worker", "workflows", "skills"}
    )
    assert ROOT_REQUIRED_V4 == frozenset({"version", "target", "models", "workflows"})
    assert set(schema["properties"]) == set(ROOT_FIELDS_V4)
    assert schema["required"] == sorted(ROOT_REQUIRED_V4)
    assert schema["$schema"] == SCHEMA_URI
    assert set(workflow_schema["properties"]) == set(WORKFLOW_FIELDS)
    assert workflow_schema["required"] == _sorted_required(WORKFLOW_REQUIRED)
    assert set(step_schema["properties"]) == set(STEP_FIELDS)
    assert step_schema["required"] == sorted(STEP_REQUIRED)
    input_schema = workflow_schema["properties"]["input"]
    assert isinstance(input_schema, dict)
    assert set(input_schema["properties"]) == set(INPUT_FIELDS)
    ceiling_schema = workflow_schema["properties"]["limits"]
    assert isinstance(ceiling_schema, dict)
    assert set(ceiling_schema["properties"]) == set(CEILING_FIELDS)
    defaults_schema = workflow_schema["properties"]["defaults"]
    assert isinstance(defaults_schema, dict)
    assert set(defaults_schema["properties"]) == {"max-executions", "routes"}
    assert AGENT_FIELDS >= {"results"}
    assert DEFAULTS_FIELDS >= {"max-executions", "routes"}
    assert INPUT_FIELDS >= {"type", "label"}
    assert WORKER_FIELDS >= set()
    assert MODEL_FIELDS >= {
        "provider",
        "gateway",
        "output-cap-tokens",
        "context-threshold-tokens",
        "context-window-tokens",
    }
    assert MODEL_REQUIRED_COMMON >= {
        "model",
        "endpoint",
        "prices-usd-per-mtok",
        "output-cap-tokens",
        "context-threshold-tokens",
        "context-window-tokens",
    }
    return schema


def dump_workflow_json_schema() -> str:
    """The committed schema text: pretty-printed with stable key order."""
    return json.dumps(build_workflow_json_schema(), indent=2, sort_keys=True) + "\n"
