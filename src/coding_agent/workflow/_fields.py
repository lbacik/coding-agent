"""Field descriptors: each Workflow Configuration leaf described once.

Every leaf field of the Workflow Configuration file is described exactly
once, as ``dataclasses.field`` metadata on the typed configuration
dataclasses in :mod:`coding_agent.workflow.schema` (see :func:`described`).
The metadata holds the YAML key (dotted relative to the dataclass's own
YAML mapping when the dataclass flattens nesting, e.g. ``gateway.url`` on
``ModelDecl``), the kind, an explicit ``required`` flag (YAML requiredness;
never inferred from whether the dataclass field has a default), the
JSON-schema description text, and optionally ``schema_default=False``.

The dataclass default is the only default: the JSON schema emits ``default``
for optional fields whose default is not ``None``, unless
``schema_default=False``.

Kinds form a closed set (:data:`LEAF_KINDS` plus :data:`CUSTOM_KIND`). Each
non-custom kind is bound to exactly one existing leaf-check helper (the
``*_at`` functions in :mod:`coding_agent.workflow._checks`) and one
JSON-schema fragment (see :func:`fragment_for`). A ``custom`` field's check
stays hand-written (its fault message or position does not fit a generic
kind); its descriptor still carries the key, requiredness, description and
— for leaves — the schema fragment. A descriptor with kind ``custom`` and
no schema fragment is a *container*: a nested mapping (``input``,
``limits``, ``steps``, ...) whose shape stays hand-written while its key
and requiredness are derived like any other field.

There is deliberately no generic enum, env-var-name or id kind: every such
leaf carries a bespoke contract refusal message (contract §2.9) or a
positional rule, so all of them are ``custom`` and listed by
``tests/test_workflow_field_descriptors.py``.
"""

from __future__ import annotations

from dataclasses import MISSING, Field, dataclass, field as _dc_field
from dataclasses import fields as _dc_fields
from typing import Any, Iterator, Mapping

GUIDE = "docs/ops/workflow-configuration.md"
"""The operator guide every schema description points at."""

ID_BODY = r"[a-z0-9]+(-[a-z0-9]+)*"
"""The id shape (§2.7), unanchored: ``_checks`` anchors via ``fullmatch``,
the JSON schema via ``^...$``."""

ID_PATTERN = f"^{ID_BODY}$"
"""The id shape anchored for JSON Schema's unanchored ``pattern``."""

CUSTOM_KIND = "custom"
"""The escape-hatch kind: the check (and, for containers, the schema
fragment) stays hand-written."""

LEAF_KINDS = frozenset(
    {
        "nonempty_str",
        "bool",
        "positive_number",
        "positive_int",
        "int_ge_1",
        "str_list",
    }
)
"""The closed generic-kind set: every listed kind has exactly one check
helper and one schema fragment."""

METADATA_KEY = "workflow_field"
"""The ``dataclasses.field`` metadata key holding the :class:`FieldDescriptor`."""


@dataclass(frozen=True)
class FieldDescriptor:
    """One Workflow Configuration leaf (or container) described once."""

    key: str
    """The YAML key, dotted relative to the dataclass's own YAML mapping."""

    kind: str
    """One of :data:`LEAF_KINDS` or :data:`CUSTOM_KIND`."""

    required: bool
    """YAML requiredness; never inferred from the dataclass default."""

    description: str
    """The JSON-schema description text."""

    label: str
    """The ``what`` label the check helpers use in fault messages."""

    schema: Mapping[str, Any] | None
    """The schema fragment for a ``custom`` leaf; ``None`` for containers
    (dedicated builders own their shape) and unused otherwise."""

    schema_default: bool = True
    """Emit ``default`` from the dataclass default (optional non-``None``)."""


def described(
    key: str,
    kind: str,
    *,
    required: bool,
    description: str,
    default: Any = MISSING,
    label: str | None = None,
    schema: Mapping[str, Any] | None = None,
    schema_default: bool = True,
) -> Any:
    """A ``dataclasses.field`` carrying the one description of a leaf.

    ``label`` defaults to the quoted leaf segment (``'input'``); sections
    whose faults name the section prefix (``'target.kind'``,
    ``'input.label'``, ``'gateway.url'``) pass it explicitly.
    """
    if kind != CUSTOM_KIND and kind not in LEAF_KINDS:
        raise ValueError(f"unknown field kind {kind!r} for {key!r}")
    if label is None:
        label = f"'{key.split('.')[-1]}'"
    desc = FieldDescriptor(
        key=key,
        kind=kind,
        required=required,
        description=description,
        label=label,
        schema=dict(schema) if schema is not None else None,
        schema_default=schema_default,
    )
    if default is MISSING:
        return _dc_field(metadata={METADATA_KEY: desc})
    return _dc_field(default=default, metadata={METADATA_KEY: desc})


def descriptor_of(field: Field[Any]) -> FieldDescriptor | None:
    """The descriptor on a dataclass field, or ``None`` when structural."""
    desc = field.metadata.get(METADATA_KEY)
    if desc is None:
        return None
    assert isinstance(desc, FieldDescriptor)
    return desc


def descriptors(cls: type[Any]) -> tuple[FieldDescriptor, ...]:
    """Every descriptor on a dataclass, in declaration order."""
    return tuple(
        desc for f in _dc_fields(cls) if (desc := descriptor_of(f)) is not None
    )


def described_map(cls: type[Any]) -> dict[str, FieldDescriptor]:
    """Descriptors on a dataclass keyed by YAML key, in declaration order."""
    return {desc.key: desc for desc in descriptors(cls)}


def iter_described(cls: type[Any]) -> Iterator[tuple[str, Any, FieldDescriptor]]:
    """``(attribute name, default-or-``MISSING`` , descriptor)`` in order."""
    for f in _dc_fields(cls):
        desc = descriptor_of(f)
        if desc is not None:
            yield f.name, f.default, desc


def section_keys(cls: type[Any]) -> frozenset[str]:
    """Top-level YAML keys: the first segment of each descriptor key."""
    return frozenset(desc.key.split(".")[0] for desc in descriptors(cls))


def section_required(cls: type[Any]) -> frozenset[str]:
    """Top-level required YAML keys (a required dotted key counts for its
    top segment)."""
    return frozenset(
        desc.key.split(".")[0] for desc in descriptors(cls) if desc.required
    )


def sub_keys(cls: type[Any], prefix: str) -> frozenset[str]:
    """Single-level YAML keys under a dotted ``prefix`` (e.g. ``gateway.``)."""
    out: set[str] = set()
    for desc in descriptors(cls):
        if not desc.key.startswith(prefix):
            continue
        rest = desc.key[len(prefix) :]
        if rest and "." not in rest:
            out.add(rest)
    return frozenset(out)


def sub_required(cls: type[Any], prefix: str) -> frozenset[str]:
    """Required single-level YAML keys under a dotted ``prefix``."""
    out: set[str] = set()
    for desc in descriptors(cls):
        if not desc.key.startswith(prefix) or not desc.required:
            continue
        rest = desc.key[len(prefix) :]
        if rest and "." not in rest:
            out.add(rest)
    return frozenset(out)


def fragment_for(desc: FieldDescriptor) -> dict[str, Any]:
    """The JSON-schema fragment for a descriptor's kind (no description)."""
    if desc.kind == "nonempty_str":
        return {"type": "string", "minLength": 1}
    if desc.kind == "bool":
        return {"type": "boolean"}
    if desc.kind == "positive_number":
        return {"type": "number", "exclusiveMinimum": 0}
    if desc.kind == "positive_int" or desc.kind == "int_ge_1":
        return {"type": "integer", "minimum": 1}
    if desc.kind == "str_list":
        return {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        }
    if desc.kind == CUSTOM_KIND:
        assert desc.schema is not None, f"custom leaf {desc.key!r} needs a schema"
        return dict(desc.schema)
    raise ValueError(f"unknown field kind {desc.kind!r} for {desc.key!r}")


def is_container(desc: FieldDescriptor) -> bool:
    """Whether a descriptor names a nested mapping (custom, no fragment)."""
    return desc.kind == CUSTOM_KIND and desc.schema is None


def schema_properties(
    cls: type[Any], prefix: str = ""
) -> dict[str, dict[str, Any]]:
    """``{stripped key: property}`` for the leaves under ``prefix``.

    Containers are skipped: dedicated builders own their shape. Key order
    follows declaration order; the committed text sorts keys on dump, so
    only list values (``required``, ``enum``) are order-sensitive.
    """
    props: dict[str, dict[str, Any]] = {}
    for attr, default, desc in iter_described(cls):
        _ = attr
        if not desc.key.startswith(prefix):
            continue
        rest = desc.key[len(prefix) :]
        if not rest or "." in rest or is_container(desc):
            continue
        prop = fragment_for(desc)
        prop["description"] = desc.description
        if not desc.required and desc.schema_default:
            if default is not MISSING and default is not None:
                prop["default"] = default
        props[rest] = prop
    return props


def schema_required(cls: type[Any], prefix: str = "") -> list[str]:
    """Required leaf keys under ``prefix``, in declaration order."""
    return [
        desc.key[len(prefix) :]
        for desc in descriptors(cls)
        if desc.required
        and desc.key.startswith(prefix)
        and "." not in desc.key[len(prefix) :]
        and not is_container(desc)
    ]
