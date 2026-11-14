"""JSON Schema (draft 2020-12) for the `version: 3` Workflow Configuration file.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the file)
and §3 (static validation) stay the authority; this schema is an editor aid
only and never a second validator. It covers the structural rules only:
allowed keys (``additionalProperties: false`` everywhere), required keys,
enums, scalar types and bounds, the id pattern, exactly one executor per
step, agent-only ``limits``/``reserved``, per-operation ``with`` keys,
payload field types and the ``skills`` declaration shape.

The schema is built from the same validator vocabulary as the `version: 2`
schema (``coding_agent.workflow.v2_jsonschema``) plus the v3 skills block,
never edited by hand, so the two cannot drift apart. The CLI command
``agent config schema`` prints it; the committed copy is
``schemas/workflow-v3.schema.json``.

Out of scope (stays with ``agent config validate``): cross-references
(``start``/route targets naming real steps, ``with.body`` resolution,
``requires`` evidence), route exhaustiveness, graph analysis, limit
arithmetic, end-label vs. Selection Label disjointness, environment
variables, prompt files, skill-source identity and uniqueness, Skill Bundle
membership names and YAML-level refusals.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from coding_agent.workflow.v2_jsonschema import GUIDE, SCHEMA_URI, build_v2_json_schema
from coding_agent.workflow.v3_validate import _ROOT_FIELDS_V3, _ROOT_REQUIRED_V3


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
            "properties": {
                "source": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The skill source: an HTTPS Git URL in `https://` "
                        "or `git+https://` spelling."
                    ),
                },
                "include": {
                    "type": "array",
                    "description": (
                        "The source's exact member skill names (case, "
                        "spaces and underscores preserved)."
                    ),
                    "minItems": 1,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "description": "One exact member skill name.",
                    },
                },
                "ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The entry's pin (a branch, tag or commit); "
                        "absent floats on the source's default."
                    ),
                },
            },
            "required": ["source", "include"],
            "additionalProperties": False,
        },
    }


def build_v3_json_schema() -> dict[str, Any]:
    """Build the draft 2020-12 JSON Schema for a `version: 3` file.

    A `version: 3` file is a `version: 2` file plus the ``skills``
    declaration, so this starts from the v2 schema (same validator
    vocabulary) and only changes the version marker, the identity and the
    added block. It is offline and reads no environment variables.
    """
    schema = copy.deepcopy(build_v2_json_schema())
    properties = schema["properties"]
    assert isinstance(properties, dict)

    properties["version"] = {
        "const": 3,
        "description": f"Must be `3`. See {GUIDE}.",
    }
    properties["skills"] = _skills_schema()

    schema["$id"] = "https://example.invalid/schemas/workflow-v3.schema.json"
    schema["title"] = "Workflow Configuration (version: 3)"
    schema["description"] = (
        "Editor aid for the `version: 3` Workflow Configuration file: "
        "a `version: 2` file plus the configuration-wide `skills` "
        "declaration. Structure only; `agent config validate` remains "
        f"required (contract §3). See {GUIDE}."
    )
    # The v3 root adds exactly the optional `skills` block.
    assert _ROOT_FIELDS_V3 == frozenset(
        {"version", "target", "models", "worker", "workflows", "skills"}
    )
    assert _ROOT_REQUIRED_V3 == frozenset({"version", "target", "models", "workflows"})
    assert set(properties) == set(_ROOT_FIELDS_V3)
    assert schema["required"] == sorted(_ROOT_REQUIRED_V3)
    assert schema["$schema"] == SCHEMA_URI
    return schema


def dump_v3_json_schema() -> str:
    """The committed schema text: pretty-printed with stable key order."""
    return json.dumps(build_v3_json_schema(), indent=2, sort_keys=True) + "\n"
