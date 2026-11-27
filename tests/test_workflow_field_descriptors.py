"""Field descriptors: one description per Workflow Configuration leaf.

Slices 1–3 (#507, #508, #509): the descriptor mechanism, the leaf-only
sections (`worker`, `target`, prices, `skills` entries), `models.<id>` with
its flattened `gateway.*` leaves, and the workflow sections (`input`,
Attempt ceiling, `workflows.<id>` policy keys, defaults ceiling, steps).
Every leaf field of the file is described exactly once, as
``dataclasses.field`` metadata on the typed configuration dataclasses; the
validator's key sets and the JSON-schema builder both derive from it.

Structural rules stay hand-written with no descriptors: one executor per
step, per-operation `with` keys (hence no `with` descriptor — `STEP_FIELDS`
unions the structural key in), route targets and exhaustiveness, the
provider/gateway combinations and conditional requiredness, cross-references
(`model`/`start`/`with.body` resolve against other sections), graph checks
and limit arithmetic. The Step-Allowance meters likewise stay on the closed
code-owned meter vocabulary (`STEP_LIMITS_FIELDS` over `METERS`): the meters
can never gain a field without changing limit arithmetic, so — unlike file
sections — they are a constant, not descriptors. The Effective Configuration
record stays a deliberate hand-written consumer (durable, digest-covered).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.9 (every
fault names its YAML path) and ADR 0027 (strict parsing is unchanged).
"""

from __future__ import annotations

import pytest

from coding_agent.workflow._checks import (
    CEILING_FIELDS,
    CEILING_REQUIRED,
    DEFAULTS_FIELDS,
    GATEWAY_FIELDS,
    GATEWAY_REQUIRED,
    INPUT_FIELDS,
    INPUT_REQUIRED,
    MODEL_FIELDS,
    MODEL_REQUIRED_COMMON,
    PRICES_FIELDS,
    SKILLS_FIELDS,
    SKILLS_REQUIRED,
    STEP_FIELDS,
    STEP_REQUIRED,
    TARGET_FIELDS,
    WORKER_FIELDS,
    WORKFLOW_FIELDS,
    WORKFLOW_REQUIRED,
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

# The slice-1 dataclasses: every section whose leaves are fully described.
SLICE_1: tuple[type[object], ...] = (
    ModelPrices,
    TargetRepository,
    WorkerSettings,
    SkillBundleRequirement,
)

# Slice 2 adds `models.<id>` (flattened `gateway.*` leaves included).
SLICE_2: tuple[type[object], ...] = (*SLICE_1, ModelDecl)

# Slice 3 adds the workflow sections: `input`, the Attempt ceiling, the
# `workflows.<id>` policy keys (with the dotted `defaults.max-executions`
# leaf) and the step leaves (with `with`/`limits` shapes structural).
# Slice 4 (#612) is declared separately below: it leaves SLICE_3 untouched.
SLICE_3: tuple[type[object], ...] = (
    InputDecl,
    AttemptCeiling,
    Workflow,
    WorkflowStep,
)

# Slice 4 adds the Round Handoff enablement leaves (#612): the two leaves of
# the issue-only `round-handoff` mapping, described on RoundHandoffConfig.
SLICE_4: tuple[type[object], ...] = (RoundHandoffConfig,)

# Every described dataclass: the full file vocabulary after slice 4.
ALL_DESCRIBED: tuple[type[object], ...] = (*SLICE_2, *SLICE_3, *SLICE_4)


def _custom_leaves() -> set[tuple[str, str]]:
    """`(dataclass name, YAML key)` for every custom leaf in slices 1–3."""
    found: set[tuple[str, str]] = set()
    for cls in ALL_DESCRIBED:
        assert isinstance(cls, type)
        for desc in descriptors(cls):
            if desc.kind == CUSTOM_KIND and not is_container(desc):
                found.add((cls.__name__, desc.key))
    return found


def _containers() -> set[tuple[str, str]]:
    """`(dataclass name, YAML key)` for every container in slices 1–3."""
    found: set[tuple[str, str]] = set()
    for cls in ALL_DESCRIBED:
        assert isinstance(cls, type)
        for desc in descriptors(cls):
            if is_container(desc):
                found.add((cls.__name__, desc.key))
    return found


def _leaf_total() -> int:
    """Every described leaf (descriptors minus containers) in slices 1–3."""
    return sum(len(descriptors(cls)) for cls in ALL_DESCRIBED) - len(_containers())


def test_custom_leaves_are_pinned() -> None:
    """Every `custom` leaf in slices 1–3 is listed here, with its reason.

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
        # `input.type`: closed `issue`/`pull-request` membership plus the
        # exactly-one-type refusal and input-type-gated `drafts`/
        # `skip-open-agent-pr` applicability, in its exact position.
        ("InputDecl", "type"),
        # `input.drafts`: closed `include`/`exclude` membership plus the
        # pull-request-only applicability refusal, in its exact position.
        ("InputDecl", "drafts"),
        # `workflows.<id>.model`: cross-reference into `models` (the unknown-
        # model refusal names the available ids), in its exact position.
        ("Workflow", "model"),
        # `workflows.<id>.start`: cross-reference into the Workflow's own
        # step ids, in its exact position.
        ("Workflow", "start"),
        # `steps.<id>.operation`: catalog membership plus input-type pairing
        # and per-operation `with` shape dispatch, in its exact position.
        ("WorkflowStep", "operation"),
        # `steps.<id>.reserved`: the `'reserved' must be a boolean` refusal
        # carries no `got` payload (contract §2.9 pins the text), so the
        # generic `bool` kind — which appends one — does not fit.
        ("WorkflowStep", "reserved"),
    }


def test_custom_total_is_reported() -> None:
    """Running custom total after slice 4: 14 of the file's 46 leaves.

    The #466 tripwire fires past about a quarter; byte-identity (the
    committed schema must stay byte-identical) and the bespoke contract
    fault texts (§2.9 pins every refusal, including the `got`-less
    `reserved` text and the `minLength`-less direct-pin identity) force
    14/46. Each custom above carries its reason; any growth fails here and
    in the pinned set — report back on #466 instead of adding more customs.

    The 43rd leaf is the ``context-window-tokens`` Model Context Window
    (every pin declares it alongside the output cap and threshold): a
    generic ``positive_int`` leaf, so the custom count stays 14. Slice 4
    adds the 44th and 45th leaves — ``max-automatic-pickups``
    (``positive_int``) and ``pickup-label`` (``nonempty_str``) — both
    generic, so the custom count still stays 14. The 46th leaf is the
    ``max-rounds`` pin round bound (``int_1_32``) — generic, so the custom
    count still stays 14.
    """
    found = _custom_leaves()
    assert len(found) == 14, f"custom set changed: {sorted(found)}"
    assert _leaf_total() == 46, f"leaf total changed: {_leaf_total()}"


def test_containers_are_pinned() -> None:
    """Every container descriptor (nested mapping, hand-written shape).

    Containers still describe their key and requiredness once; only the
    nested shape stays hand-written in a dedicated builder. There is no
    `gateway` container (flattened onto ModelDecl as dotted keys, so no
    field carries the bare key `"gateway"`), no `with` container (the
    per-operation `with` keys stay hand-written, so `with` has no descriptor
    and `STEP_FIELDS` unions the structural key in) and no `defaults`
    container (the dotted `defaults.max-executions` leaf already contributes
    the `"defaults"` top-level key).
    """
    assert _containers() == {
        # `models.<id>.prices-usd-per-mtok`: leaves described on ModelPrices.
        ("ModelDecl", "prices-usd-per-mtok"),
        # `workflows.<id>.input` / `.limits`: leaves on InputDecl /
        # AttemptCeiling.
        ("Workflow", "input"),
        ("Workflow", "limits"),
        # `workflows.<id>.outcomes` / `.steps`: end-label maps and the step
        # map stay hand-written (graph-checked, never leaf-checked).
        ("Workflow", "outcomes"),
        ("Workflow", "steps"),
        # `steps.<id>.agent` / `.limits` / `.routes`: the agent block, the
        # code-owned Step Allowance and route targets stay hand-written.
        ("WorkflowStep", "agent"),
        ("WorkflowStep", "limits"),
        ("WorkflowStep", "routes"),
        # `steps.<id>.context-thresholds`: the agent-only Hard Context
        # Threshold policy stays hand-written (pin-dependent bounds are
        # proven by `agent config validate`).
        ("WorkflowStep", "context-thresholds"),
        # `workflows.<id>.round-handoff`: the issue-only enablement mapping
        # stays hand-written (label disjointness and publishing-Route
        # reachability are proven by `agent config validate`).
        ("Workflow", "round-handoff"),
    }


def test_custom_share_stays_small() -> None:
    """Custom leaves stay near a quarter of the file's leaves (#466).

    The denominator is the whole file's leaf total (46 after the Round
    Handoff enablement leaves and the ``max-rounds`` pin leaf), not a slice
    subtotal. Byte-identity and the bespoke contract fault texts force
    14/46 — past a strict quarter, so this bound documents the deliberate
    exception rather than the original tripwire: any growth past it fails
    here and in the pinned set above. If that happens, stop and report back
    on #466 instead of adding more customs: the descriptor language may
    need a new generic kind.
    """
    customs = len(_custom_leaves())
    total = _leaf_total()
    assert total == 46, f"leaf total changed: {total}"
    assert customs / total <= 0.35, (
        f"{customs}/{total} leaves are custom: report back on #466"
    )


def test_derived_vocabulary_matches_literals() -> None:
    """The validator's key sets are derived from descriptors — and pinned.

    Derivation (``section_keys``/``section_required``) keeps the two in sync;
    the literals below keep a descriptor rename loud.
    """
    assert TARGET_FIELDS == {
        "kind",
        "repository",
        "base-branch",
        "token-env",
        "profile",
    }
    assert TARGET_FIELDS == {
        desc.key for desc in descriptors(TargetRepository)
    }
    assert {desc.key for desc in descriptors(TargetRepository) if desc.required} == (
        TARGET_FIELDS - {"profile"}
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
        "context-window-tokens",
        "max-rounds",
    }
    assert MODEL_FIELDS == section_keys(ModelDecl)
    assert MODEL_REQUIRED_COMMON == {
        "model",
        "endpoint",
        "effort",
        "api-key-env",
        "prices-usd-per-mtok",
        "output-cap-tokens",
        "context-threshold-tokens",
        "context-window-tokens",
    }
    assert MODEL_REQUIRED_COMMON == section_required(ModelDecl)

    assert GATEWAY_FIELDS == {"kind", "url", "url-env"}
    assert GATEWAY_FIELDS == sub_keys(ModelDecl, "gateway.")
    assert GATEWAY_REQUIRED == {"kind"}
    assert GATEWAY_REQUIRED == sub_required(ModelDecl, "gateway.")

    assert INPUT_FIELDS == {
        "type",
        "label",
        "require-all",
        "require-any",
        "exclude",
        "drafts",
        "skip-open-agent-pr",
    }
    assert INPUT_FIELDS == section_keys(InputDecl)
    assert INPUT_REQUIRED == {"type", "label"}
    assert INPUT_REQUIRED == section_required(InputDecl)

    assert CEILING_FIELDS == {"cost-usd", "effective-tokens", "tool-calls", "wall-clock-minutes"}
    assert CEILING_FIELDS == section_keys(AttemptCeiling)
    assert CEILING_REQUIRED == CEILING_FIELDS
    assert CEILING_REQUIRED == section_required(AttemptCeiling)

    assert WORKFLOW_FIELDS == {
        "model",
        "input",
        "limits",
        "max-parallel-subagents",
        "outcomes",
        "start",
        "defaults",
        "round-handoff",
        "steps",
    }
    assert WORKFLOW_FIELDS == section_keys(Workflow)
    assert WORKFLOW_REQUIRED == {"model", "input", "limits", "start", "steps"}
    assert WORKFLOW_REQUIRED == section_required(Workflow)

    assert sub_keys(RoundHandoffConfig, "round-handoff.") == {
        "pickup-label",
        "max-automatic-pickups",
    }
    assert sub_required(RoundHandoffConfig, "round-handoff.") == {
        "max-automatic-pickups"
    }

    assert DEFAULTS_FIELDS == {"max-executions", "routes"}
    assert DEFAULTS_FIELDS == sub_keys(Workflow, "defaults.") | {"routes"}

    assert STEP_FIELDS == {
        "agent",
        "operation",
        "with",
        "max-executions",
        "limits",
        "reserved",
        "context-thresholds",
        "routes",
    }
    # `with` is structural (no descriptor): the per-operation `with` keys
    # stay hand-written, so the set unions the structural key in.
    assert STEP_FIELDS == section_keys(WorkflowStep) | {"with"}
    assert STEP_REQUIRED == {"routes"}
    assert STEP_REQUIRED == section_required(WorkflowStep)


def test_described_map_preserves_declaration_order() -> None:
    """Leaf checks run in dataclass declaration order (fault order is pinned)."""
    assert list(described_map(TargetRepository)) == [
        "kind",
        "repository",
        "base-branch",
        "token-env",
        "profile",
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
        "context-window-tokens",
        "max-rounds",
    ]
    assert list(described_map(InputDecl)) == [
        "type",
        "label",
        "require-all",
        "require-any",
        "exclude",
        "drafts",
        "skip-open-agent-pr",
    ]
    assert list(described_map(AttemptCeiling)) == [
        "cost-usd",
        "effective-tokens",
        "tool-calls",
        "wall-clock-minutes",
    ]
    assert list(described_map(Workflow)) == [
        "model",
        "input",
        "limits",
        "max-parallel-subagents",
        "outcomes",
        "start",
        "defaults.max-executions",
        "round-handoff",
        "steps",
    ]
    assert list(described_map(RoundHandoffConfig)) == [
        "round-handoff.max-automatic-pickups",
        "round-handoff.pickup-label",
    ]
    assert list(described_map(WorkflowStep)) == [
        "agent",
        "operation",
        "max-executions",
        "limits",
        "reserved",
        "context-thresholds",
        "routes",
    ]


def _valid_value(kind: str) -> object:
    if kind == "nonempty_str":
        return "x"
    if kind == "bool":
        return True
    if kind == "positive_number":
        return 1.5
    if kind in ("positive_int", "int_ge_1", "int_1_32"):
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

    workflows = schema_properties(Workflow)
    assert workflows["max-parallel-subagents"]["default"] == 2
    assert "default" not in workflows["model"]
    assert "default" not in workflows["start"]

    input_props = schema_properties(InputDecl)
    # The `()` filter defaults must not leak into the schema: the committed
    # properties carry no `default`, hence `schema_default=False`.
    assert "default" not in input_props["require-all"]
    assert "default" not in input_props["require-any"]
    assert "default" not in input_props["exclude"]
    assert "default" not in input_props["drafts"]
    assert "default" not in input_props["skip-open-agent-pr"]

    ceiling = schema_properties(AttemptCeiling)
    assert all("default" not in prop for prop in ceiling.values())

    assert schema_properties(Workflow, "defaults.")["max-executions"]["default"] == 1

    steps = schema_properties(WorkflowStep)
    assert steps["max-executions"]["default"] == 1
    assert steps["reserved"]["default"] is False
    assert "default" not in steps["operation"]
