"""JSON Schemas for the pinned operator contract's ``agentctl`` replies.

Contract: ``agent-operator-contract`` major 1 at ``06ee46c``
(``docs/spec.md`` §4-§6, ``examples/agentctl/``). The upstream
``v1.0.0`` tag — and with it the published schemas — does not exist
yet, so the schema documents vendored under ``conformance/schemas/``
are the pinned fixtures written as JSON Schema (draft 2020-12). They
mirror the shapes :mod:`coding_agent.agentctl` emits field for field;
when ``v1.0.0`` lands they are replaced by the published documents and
this module keeps loading whatever is on disk.

Validation here uses only the standard library: the suite must run in
the same minimal gate environment as the rest of the tests, with no
new dependency. :func:`validate_envelope` and
:func:`validate_result` implement the subset of JSON Schema the
vendored documents use (``type``, ``required``, ``properties``,
``enum``, ``const`` and ``anyOf`` of those); anything richer raises
:exc:`SchemaError` rather than silently passing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "conformance" / "schemas"
"""The vendored schema documents (``conformance/schemas/``)."""

_ENVELOPE_SCHEMA = "envelope.schema.json"

_RESULT_SCHEMA_FOR_OP: dict[str, str] = {
    "identity": "identity-result.schema.json",
    "status": "status-result.schema.json",
    "command": "command-result.schema.json",
    "stop": "mutation-result.schema.json",
    "resume": "mutation-result.schema.json",
    "next": "mutation-result.schema.json",
    "attempts-list": "attempts-list-result.schema.json",
    "attempts-show": "attempts-show-result.schema.json",
}


class SchemaError(Exception):
    """A schema document is missing, unreadable, or uses an unsupported keyword."""


def _schema_path(name: str) -> Path:
    return SCHEMA_DIR / name


def load_schema(name: str) -> dict[str, Any]:
    """Load one vendored schema document by file name."""
    path = _schema_path(name)
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SchemaError(f"cannot read schema {name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"schema {name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaError(f"schema {name} is not an object")
    return raw


def _check_type(value: Any, expected: str, where: str, problems: list[str]) -> None:
    ok = (
        (expected == "object" and isinstance(value, dict))
        or (expected == "array" and isinstance(value, list))
        or (expected == "string" and isinstance(value, str))
        or (expected == "boolean" and isinstance(value, bool))
        or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (
            expected == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        or (expected == "null" and value is None)
    )
    if not ok:
        problems.append(f"{where}: expected {expected}, got {type(value).__name__}")


def _validate_against(schema: Any, value: Any, where: str, problems: list[str]) -> None:
    """Append one problem per mismatch of ``value`` against ``schema``."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{where}: schema node is not an object")
    for key in schema:
        if key not in (
            "$schema",
            "$id",
            "title",
            "description",
            "type",
            "required",
            "properties",
            "enum",
            "const",
            "anyOf",
            "items",
            "additionalProperties",
        ):
            raise SchemaError(f"{where}: unsupported schema keyword {key!r}")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{where}: expected constant {schema['const']!r}")
        return
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: value {value!r} not in enum {schema['enum']!r}")
        return
    if "anyOf" in schema:
        options = schema["anyOf"]
        if not isinstance(options, list) or not options:
            raise SchemaError(f"{where}: anyOf must be a non-empty list")
        option_problems: list[list[str]] = []
        for option in options:
            sub: list[str] = []
            _validate_against(option, value, where, sub)
            if not sub:
                return
            option_problems.append(sub)
        problems.append(
            f"{where}: matches no anyOf branch ({len(option_problems)} tried)"
        )
        return
    expected = schema.get("type")
    if expected is not None:
        if isinstance(expected, list):
            matches = False
            for choice in expected:
                attempt_problems: list[str] = []
                _check_type(value, str(choice), where, attempt_problems)
                if not attempt_problems:
                    matches = True
                    break
            if not matches:
                problems.append(f"{where}: expected one of {expected}")
                return
        else:
            before = len(problems)
            _check_type(value, str(expected), where, problems)
            if len(problems) != before:
                return
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise SchemaError(f"{where}: required must be a list")
        for name in required:
            if name not in value:
                problems.append(f"{where}: missing required property {name!r}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaError(f"{where}: properties must be an object")
        for name, subschema in properties.items():
            if name in value and isinstance(value, dict):
                _validate_against(subschema, value[name], f"{where}.{name}", problems)
    if isinstance(value, list) and "items" in schema:
        items = schema["items"]
        for index, entry in enumerate(value):
            _validate_against(items, entry, f"{where}[{index}]", problems)


def validate_envelope(envelope: Any) -> list[str]:
    """Validate a full ``agentctl`` envelope; empty means valid."""
    schema = load_schema(_ENVELOPE_SCHEMA)
    problems: list[str] = []
    _validate_against(schema, envelope, "envelope", problems)
    return problems


def validate_result(operation: str, result: Any) -> list[str]:
    """Validate one operation's ``result`` object; empty means valid."""
    try:
        name = _RESULT_SCHEMA_FOR_OP[operation]
    except KeyError as exc:
        raise SchemaError(f"unknown operation {operation!r}") from exc
    schema = load_schema(name)
    problems: list[str] = []
    _validate_against(schema, result, f"result({operation})", problems)
    return problems


def validate_reply(operation: str, envelope: Any) -> list[str]:
    """Validate an ``ok: true`` envelope and its operation result."""
    problems = validate_envelope(envelope)
    if problems:
        return problems
    if not isinstance(envelope, dict):
        return ["envelope: not an object"]
    if envelope.get("ok") is not True:
        return ["envelope: ok is not true"]
    return validate_result(operation, envelope.get("result"))


__all__ = [
    "SCHEMA_DIR",
    "SchemaError",
    "load_schema",
    "validate_envelope",
    "validate_reply",
    "validate_result",
]
