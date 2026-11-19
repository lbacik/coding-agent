"""Field descriptors: one description per Workflow Configuration leaf.

Slices 1 (#507) and 2 (#508): the descriptor mechanism, the leaf-only
sections (`worker`, `target`, prices, `skills` entries) and `models.<id>`
with its flattened `gateway.*` leaves. Slice 3 (#509) extends the pinned
custom sets below to the workflow sections. Every leaf field of the file is
described exactly once, as ``dataclasses.field`` metadata on the typed
configuration dataclasses; the validator's key sets and the JSON-schema
builder both derive from it.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.9 (every
fault names its YAML path) and ADR 0027 (strict parsing is unchanged).
"""

from __future__ import annotations

import pytest

from coding_agent.workflow._checks import (
    GATEWAY_FIELDS,
    GATEWAY_REQUIRED,
    MODEL_FIELDS,
    MODEL_REQUIRED_COMMON,
    PRICES_FIELDS,
    SKILLS_FIELDS,
    SKILLS_REQUIRED,
    TARGET_FIELDS,
    WORKER_FIELDS,
    Problems,
    check_value,
)
from coding_agent.workflow._fields import (
    CUSTOM_KIND,
    LEAF_KINDS,
    FieldDescriptor,
    described_map,
    descriptors,
    fragment_for,
    is_container,
    schema_properties,
    section_keys,
    section_required,
    sub_keys,
    sub_required,
)
from coding_agent.workflow.schema import (
    ModelDecl,
    ModelPrices,
    SkillBundleRequirement,
    TargetRepository,
    WorkerSettings,
)

# The slice-1 dataclasses: every section whose leaves are fully described.
SLICE_1: tuple[type[object], ...] = (
    ModelPrices,
    TargetRepository,
    WorkerSettings,
    SkillBundleRequirement,
)

# Slice 2 adds `models.<id>` (flattened `gateway.*` leaves included).
SLICE_2: tuple[type[object], ...] = (*SLICE_1, ModelDecl)


def _custom_leaves() -> set[tuple[str, str]]:
    """`(dataclass name, YAML key)` for every custom leaf in slices 1–2."""
    found: set[tuple[str, str]] = set()
    for cls in SLICE_2:
        assert isinstance(cls, type)
        for desc in descriptors(cls):
            if desc.kind == CUSTOM_KIND and not is_container(desc):
                found.add((cls.__name__, desc.key))
    return found


def _containers() -> set[tuple[str, str]]:
    """`(dataclass name, YAML key)` for every container in slices 1–2."""
    found: set[tuple[str, str]] = set()
    for cls in SLICE_2:
        assert isinstance(cls, type)
        for desc in descriptors(cls):
            if is_container(desc):
                found.add((cls.__name__, desc.key))
    return found


def test_custom_leaves_are_pinned() -> None:
    """Every `custom` leaf in slices 1–2 is listed here, with its reason.

    A `custom` leaf keeps a hand-written check (its fault message, schema
    fragment or position fits no generic kind) while its key, requiredness
    and schema fragment are still described once. Adding a `custom` leaf
    means updating this list deliberately — and re-checking the running
    custom count reported on #466 (the ~quarter tripwire).
    """
    assert _custom_leaves() == {
        # `target.kind`: non-empty check plus the closed `github` membership
        # refusal (contract §2.9), in its exact position.
        ("TargetRepository", "kind"),
        # `skills.include`: non-empty-list check plus per-name
        # duplicate tracking, with positional faults.
        ("SkillBundleRequirement", "include"),
        # `models` direct-pin identity: plain non-empty checks whose schema
        # properties carry no `minLength` (byte-identity with the committed
        # schema keeps them custom rather than `nonempty_str`).
        ("ModelDecl", "provider"),
        ("ModelDecl", "model"),
        ("ModelDecl", "endpoint"),
        # `models.<id>.effort`: non-empty check plus the closed low/medium/
        # high membership refusal, in its exact position.
        ("ModelDecl", "effort"),
        # `gateway.kind`: non-empty check plus the code-owned kind membership
        # refusal, in its exact position.
        ("ModelDecl", "gateway.kind"),
        # `gateway.url`: non-empty check plus URL canonicalisation, whose
        # refusal text comes from the canonicaliser.
        ("ModelDecl", "gateway.url"),
    }


def test_slice_2_custom_count_is_reported() -> None:
    """Running custom total after slice 2: 8 of the file's 44 leaves.

    The #466 tripwire fires past about a quarter: the remaining slice-3
    customs (input type/drafts, workflow model/start, step operation) project
    to ~12–13/44, which is reported — with per-field reasons — in the slice-3
    commit and the closing summary rather than hidden in the mechanism.
    """
    found = _custom_leaves()
    assert len(found) == 8, f"custom set changed: {sorted(found)}"


def test_containers_are_pinned() -> None:
    """Every container descriptor (nested mapping, hand-written shape).

    Containers still describe their key and requiredness once; only the
    nested shape stays hand-written in a dedicated builder.
    """
    assert _containers() == {
        # `models.<id>.prices-usd-per-mtok`: leaves described on ModelPrices.
        ("ModelDecl", "prices-usd-per-mtok"),
        # `models.<id>.gateway`: `gateway.*` leaves described on ModelDecl.
        ("ModelDecl", "gateway"),
    }


def test_custom_share_stays_small() -> None:
    """At most about a quarter of slice-1 leaves are `custom` (#466).

    If this fails, stop and report back on #466 instead of adding more
    customs: the descriptor language may need a new generic kind.
    """
    total = sum(len(descriptors(cls)) for cls in SLICE_1)
    customs = len(_custom_leaves()) + len(_containers())
    assert total > 0
    assert customs / total <= 0.25, (
        f"{customs}/{total} leaves are custom: report back on #466"
    )


def test_derived_vocabulary_matches_literals() -> None:
    """The validator's key sets are derived from descriptors — and pinned.

    Derivation (``section_keys``/``section_required``) keeps the two in sync;
    the literals below keep a descriptor rename loud.
    """
    assert TARGET_FIELDS == {"kind", "repository", "base-branch", "token-env"}
    assert TARGET_FIELDS == {
        desc.key for desc in descriptors(TargetRepository)
    }
    assert {desc.key for desc in descriptors(TargetRepository) if desc.required} == (
        TARGET_FIELDS
    )

    assert PRICES_FIELDS == {"input", "output", "cache-read", "cache-write"}
    assert PRICES_FIELDS == {desc.key for desc in descriptors(ModelPrices)}

    assert WORKER_FIELDS == {"poll-interval-seconds", "shutdown-grace-seconds"}
    assert WORKER_FIELDS == {desc.key for desc in descriptors(WorkerSettings)}
    assert [desc.key for desc in descriptors(WorkerSettings) if desc.required] == []

    assert SKILLS_FIELDS == {"source", "include", "ref"}
    assert SKILLS_FIELDS == {
        desc.key for desc in descriptors(SkillBundleRequirement)
    }
    assert SKILLS_REQUIRED == {"source", "include"}
    assert SKILLS_REQUIRED == {
        desc.key for desc in descriptors(SkillBundleRequirement) if desc.required
    }

    assert MODEL_FIELDS == {
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
    assert MODEL_FIELDS == section_keys(ModelDecl)
    assert MODEL_REQUIRED_COMMON == {
        "model",
        "endpoint",
        "effort",
        "api-key-env",
        "prices-usd-per-mtok",
    }
    assert MODEL_REQUIRED_COMMON == section_required(ModelDecl)

    assert GATEWAY_FIELDS == {"kind", "url", "url-env"}
    assert GATEWAY_FIELDS == sub_keys(ModelDecl, "gateway.")
    assert GATEWAY_REQUIRED == {"kind"}
    assert GATEWAY_REQUIRED == sub_required(ModelDecl, "gateway.")


def test_described_map_preserves_declaration_order() -> None:
    """Leaf checks run in dataclass declaration order (fault order is pinned)."""
    assert list(described_map(TargetRepository)) == [
        "kind",
        "repository",
        "base-branch",
        "token-env",
    ]
    assert list(described_map(ModelPrices)) == [
        "input",
        "output",
        "cache-read",
        "cache-write",
    ]
    assert list(described_map(WorkerSettings)) == [
        "poll-interval-seconds",
        "shutdown-grace-seconds",
    ]
    assert list(described_map(SkillBundleRequirement)) == [
        "source",
        "include",
        "ref",
    ]
    assert list(described_map(ModelDecl)) == [
        "provider",
        "model",
        "endpoint",
        "effort",
        "api-key-env",
        "prices-usd-per-mtok",
        "gateway.kind",
        "gateway.url",
        "gateway.url-env",
        "output-cap-tokens",
        "context-threshold-tokens",
    ]


def _valid_value(kind: str) -> object:
    if kind == "nonempty_str":
        return "x"
    if kind == "bool":
        return True
    if kind == "positive_number":
        return 1.5
    if kind in ("positive_int", "int_ge_1"):
        return 2
    if kind == "str_list":
        return ["a"]
    raise AssertionError(f"no sample value for kind {kind!r}")


def test_every_generic_kind_has_a_check_and_a_fragment() -> None:
    """Each closed kind binds one `*_at` helper and one schema fragment."""
    assert CUSTOM_KIND not in LEAF_KINDS
    for kind in sorted(LEAF_KINDS):
        desc = FieldDescriptor(
            key="leaf",
            kind=kind,
            required=True,
            description="A leaf.",
            label="'leaf'",
            schema=None,
        )
        fragment = fragment_for(desc)
        assert isinstance(fragment, dict) and fragment

        problems = Problems()
        checked = check_value(problems, "section.leaf", _valid_value(kind), desc)
        assert not problems
        assert checked is not None


def test_custom_never_reaches_the_generic_leaf_pass() -> None:
    """`custom` leaves refuse the generic pass: their check is hand-written."""
    problems = Problems()
    desc = FieldDescriptor(
        key="kind",
        kind=CUSTOM_KIND,
        required=True,
        description="The target kind.",
        label="'target.kind'",
        schema={"enum": ["github"]},
    )
    with pytest.raises(AssertionError):
        check_value(problems, "target.kind", "github", desc)


def test_unknown_kind_is_rejected() -> None:
    """The kind set is closed: a typo fails at import time, not in review."""
    from coding_agent.workflow._fields import described

    with pytest.raises(ValueError):
        described("x", "whatever", required=True, description="Bogus.")


def test_schema_defaults_come_from_dataclass_defaults() -> None:
    """Optional non-`None` defaults emit `default`; `None` defaults do not."""
    worker = schema_properties(WorkerSettings)
    assert worker["poll-interval-seconds"]["default"] == 60.0
    assert worker["shutdown-grace-seconds"]["default"] == 30.0

    skills = schema_properties(SkillBundleRequirement)
    assert "default" not in skills["source"]
    assert "default" not in skills["include"]
    assert "default" not in skills["ref"]

    prices = schema_properties(ModelPrices)
    assert all("default" not in prop for prop in prices.values())
