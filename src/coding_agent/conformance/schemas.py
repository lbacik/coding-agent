"""Conformance validation against the published operator contract (issue #359).

Contract: ``agent-operator-contract`` pinned at tag ``v1.0.4`` (see
``contract-pin.txt`` and the ``agent-operator-contract`` dev dependency in
``pyproject.toml``). The upstream schema documents are the source of
truth; this module loads them from the installed package
(:mod:`agent_operator_contract.documents`) and validates with a full
Draft 2020-12 validator (``jsonschema``). No schema keyword is skipped
and no contract schema is copied into this repository.
"""

from __future__ import annotations

from typing import Any

from agent_operator_contract import documents as upstream
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

_OPERATIONS_TO_SCHEMA: dict[str, str] = {
    "identity": "identity",
    "status": "status",
    "command": "command",
    "stop": "mutation-result",
    "resume": "mutation-result",
    "next": "mutation-result",
    "next-issue": "mutation-result",
    "attempts-list": "attempts-list",
    "attempts-show": "attempts-show",
    "envelope": "envelope",
}
"""Agentctl operation name to upstream schema document name."""


class SchemaError(Exception):
    """A schema document is missing, unreadable, or an operation is unknown."""


def load_schema(name: str) -> dict[str, Any]:
    """Load one upstream schema document by operation or document name."""
    document = _OPERATIONS_TO_SCHEMA.get(name, name)
    try:
        schema: dict[str, Any] = upstream.load_schema(document)
    except Exception as exc:
        raise SchemaError(f"cannot load upstream schema {name!r}: {exc}") from exc
    if not isinstance(schema, dict):
        raise SchemaError(f"upstream schema {name!r} is not an object")
    return schema


def _problems_for(schema_name: str, value: Any, where: str) -> list[str]:
    """All validation problems for ``value`` against upstream ``schema_name``."""
    try:
        schema = upstream.load_schema(schema_name)
    except Exception as exc:
        raise SchemaError(f"cannot load upstream schema {schema_name!r}: {exc}") from exc
    validator = Draft202012Validator(schema)
    problems: list[str] = []
    for error in validator.iter_errors(value):
        path = "/".join(str(part) for part in error.absolute_path)
        location = f"{where}/{path}" if path else where
        problems.append(f"{location}: {error.message}")
    return problems


def validate_envelope(envelope: Any) -> list[str]:
    """Validate a full ``agentctl`` envelope; empty means valid."""
    return _problems_for("envelope", envelope, "envelope")


def validate_result(operation: str, result: Any) -> list[str]:
    """Validate one operation's ``result`` object; empty means valid."""
    try:
        schema_name = _OPERATIONS_TO_SCHEMA[operation]
    except KeyError as exc:
        raise SchemaError(f"unknown operation {operation!r}") from exc
    if schema_name == "envelope":
        raise SchemaError(f"unknown operation {operation!r}")
    return _problems_for(schema_name, result, f"result({operation})")


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
    "SchemaError",
    "load_schema",
    "validate_envelope",
    "validate_reply",
    "validate_result",
]
