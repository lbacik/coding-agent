"""Activation delivery: framing, order, notices and replay identity (SKV3-17).

Contract: an activation delivers the complete ``SKILL.md``, then each
reviewed ``inject`` companion whole in policy order, with
operator-authored notices framed outside the upstream bytes (ADR 0007).
``not-delivered`` bodies appear in no delivery and no replay; there is no
skill-resource reader and no implicit network fetch. Static, on-demand and
Context-Window Handoff replay deliveries are byte-identical for the same
member. The framing is versioned; these golden byte vectors freeze the
framing, the component order and the notices. Activation Exchange Units
stay exempt from tool-result capping, and the runtime path fed from a
prepared generation never consults the legacy fixed-companion constants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from coding_agent.runtime.context_handoff import ContinuationSnapshot
from coding_agent.runtime.graph.agent_step import (
    ACTIVATION_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    build_agent_step_tools,
    compose_step_opening,
    instruction_texts,
    run_agent_step,
)
from coding_agent.runtime.result_capping import (
    ArtifactNotFound,
    DEFAULT_RESULT_CAP_LIMIT,
    InMemoryArtifactStore,
)
from coding_agent.skillbundle.delivery import (
    DELIVERY_FRAMING_VERSION,
    compose_delivery,
)
from coding_agent.skillbundle.deployment import (
    VerifiedRunInputs,
    prepare_skill_bundle,
    verify_run_inputs,
)
from coding_agent.workflow.v3_validate import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)
from coding_agent.skillbundle.installer import write_fake_store
from coding_agent.workflow.v2_schema import V2Agent, V2Instruction, V2Step
from support.scripted_model import ScriptedModel
from support.skill_bundle import (
    COMMIT_A,
    SOURCE_A,
    DeclaredMember,
    build_policy_document,
    default_skill_md_text,
    fake_store_spec_for,
    minimal_config_text,
    skills_entries_for,
    write_policy_file,
)


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _setup(
    tmp_path: Path, members: list[DeclaredMember]
) -> tuple[Path, Path, Path, Path]:
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))
    return config, policy, bundle_dir, home


def _load_config(config: Path) -> LoadedWorkflowConfiguration:
    """Load the Workflow Configuration through the single loader."""
    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(fault) for fault in faults]
    return loaded


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, Any] = {
        "attempt_id": "implement#7/1",
        "workflow_id": "implement",
        "input_type": "issue",
        "input_number": 7,
        "title": "Fix the crash",
        "body": "The worker crashes on startup.",
        "base_revision": "abc123",
        "current_revision": "abc123",
    }
    base.update(overrides)
    return StepAttemptFacts(
        attempt_id=str(base["attempt_id"]),
        workflow_id=str(base["workflow_id"]),
        input_type=str(base["input_type"]),
        input_number=int(base["input_number"]),
        title=str(base["title"]),
        body=str(base["body"]),
        base_revision=str(base["base_revision"]),
        current_revision=str(base["current_revision"]),
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(calls: list[dict[str, object]] | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls or []))


def _report_call(call_id: str = "report-1") -> dict[str, object]:
    return _call(
        "report_step_result",
        {"result": "done", "payload_json": json.dumps({})},
        call_id,
    )


def _agent_step(skill: str, tools: tuple[str, ...] = ()) -> V2Step:
    return V2Step(
        id="work",
        agent=V2Agent(
            results={"done": {}},
            instructions=(V2Instruction(skill=skill),),
            tools=tools,
        ),
        max_executions=1,
        routes={},
    )


# --- Golden byte vectors -------------------------------------------------------


def test_framing_version_is_pinned() -> None:
    """The delivery framing version is frozen; a framing change is a version change."""
    assert DELIVERY_FRAMING_VERSION == 1


def test_golden_vector_skill_only_is_verbatim() -> None:
    """With no companions and no notices the delivery is exactly ``SKILL.md``."""
    skill = "# alpha\n\nBody.\n"
    assert compose_delivery(name="alpha", skill_md=skill) == skill


def test_golden_vector_full_delivery_freezes_framing_order_and_notices() -> None:
    """SKILL.md, then companions in order, then notices — every separator versioned."""
    delivery = compose_delivery(
        name="alpha",
        skill_md="# alpha\n\nBody.\n",
        companions=(("docs/a.md", "A\n"), ("docs/b.md", "B\n")),
        notices=(("setup-tool", "runs outside"), ("lint-tool", "also outside")),
    )
    assert delivery == (
        "# alpha\n\nBody.\n"
        "\n\n----- skill-delivery v1: companion alpha/docs/a.md -----\nA\n"
        "\n\n----- skill-delivery v1: companion alpha/docs/b.md -----\nB\n"
        "\n\n----- skill-delivery v1: operator notices for alpha -----\n"
        "- setup-tool: runs outside\n"
        "- lint-tool: also outside"
    )


def test_golden_vector_companions_without_notices_have_no_notices_section() -> None:
    """An empty notice set leaves no notices separator behind."""
    delivery = compose_delivery(
        name="alpha",
        skill_md="SKILL\n",
        companions=(("docs/a.md", "A\n"),),
    )
    assert delivery == (
        "SKILL\n" "\n\n----- skill-delivery v1: companion alpha/docs/a.md -----\nA\n"
    )
    assert "operator notices" not in delivery


def test_compose_delivery_respects_policy_order_not_sorted_order() -> None:
    """Companions follow the caller's (policy) order even when unsorted."""
    delivery = compose_delivery(
        name="alpha",
        skill_md="SKILL\n",
        companions=(("docs/z.md", "Z\n"), ("docs/a.md", "A\n")),
    )
    assert delivery.index("docs/z.md") < delivery.index("docs/a.md")


# --- Prepared-generation delivery ----------------------------------------------


def _reviewed_member() -> DeclaredMember:
    """One member with SKILL.md, an inject companion, a not-delivered file and a notice."""
    skill_md = default_skill_md_text("alpha", body="Run /setup-tool now.")
    return DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        extra_files={
            "docs/guide.md": (b"# Guide\n\nGuide body.\n", False),
            "notes.txt": (b"NOT-DELIVERED-MARKER-4821\n", False),
        },
        disposition_overrides={"notes.txt": "not-delivered"},
        references=(
            {
                "classification": "unavailable-nonrequired-reference",
                "notice": "setup runs outside attempts",
                "origin": "SKILL.md",
                "reason": "irrelevant during attempt",
                "target": "setup-tool",
                "text": "Run /setup-tool now.",
            },
        ),
    )


def _delivery_of(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _with_env(monkeypatch)
    member = _reviewed_member()
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    loaded = _load_config(config)
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    return result.registry.bodies["alpha"]


def test_prepared_delivery_is_skill_then_inject_then_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden shape holds end to end: upstream bytes first, notices last and outside."""
    delivery = _delivery_of(tmp_path, monkeypatch)
    skill_md = default_skill_md_text("alpha", body="Run /setup-tool now.")
    assert delivery.startswith(skill_md)
    assert delivery == (
        skill_md
        + "\n\n----- skill-delivery v1: companion alpha/docs/guide.md -----\n"
        + "# Guide\n\nGuide body.\n"
        + "\n\n----- skill-delivery v1: operator notices for alpha -----\n"
        + "- setup-tool: setup runs outside attempts"
    )


def test_not_delivered_bodies_appear_in_no_delivery_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The not-delivered marker is absent from the static, on-demand and replay bytes."""
    _with_env(monkeypatch)
    member = _reviewed_member()
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    loaded = _load_config(config)
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    delivery = result.registry.bodies["alpha"]
    assert "NOT-DELIVERED-MARKER-4821" not in delivery
    assert "Guide body." in delivery

    from coding_agent.skillbundle.activation import ActivationRegistry

    registry = ActivationRegistry(bodies=dict(result.registry.bodies))
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)

    step = _agent_step("alpha")
    deps = AgentStepDeps(
        attempt=_facts(),
        model=ScriptedModel([]),
        workspace=workspace,
        tree_id="tree-1",
        activation_registry=registry,
    )
    assert instruction_texts(step, deps) == [delivery]

    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "alpha"}, "a1")]),
            _ai([_report_call()]),
        ]
    )
    deps = AgentStepDeps(
        attempt=_facts(),
        model=model,
        workspace=workspace,
        tree_id="tree-1",
        activation_registry=registry,
    )
    outcome = run_agent_step(_agent_step(""), 1, deps, {})
    assert outcome.status == "reported"
    tool_texts = [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    assert tool_texts == [delivery]
    assert all("NOT-DELIVERED-MARKER-4821" not in text for text in tool_texts)


def test_static_on_demand_and_handoff_replay_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reopened conversation carries the same delivery bytes as the first opening."""
    _with_env(monkeypatch)
    member = _reviewed_member()
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    loaded = _load_config(config)
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    delivery = result.registry.bodies["alpha"]

    from coding_agent.runtime.context_handoff import estimate_context_tokens
    from coding_agent.skillbundle.activation import ActivationRegistry

    registry = ActivationRegistry(bodies=dict(result.registry.bodies))
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    (workspace / "big.txt").write_text("x" * 3000 + "\n", encoding="utf-8")

    step = _agent_step("alpha", tools=("workspace-read",))
    model = ScriptedModel(
        [
            _ai([_call("read_file", {"path": "big.txt"}, "big-1")]),
            _ai([_report_call()]),
        ]
    )

    def producer(messages: list[BaseMessage]) -> ContinuationSnapshot:
        return ContinuationSnapshot(
            version=1,
            attempt_id="implement#7/1",
            base_revision="abc123",
            branch="agent/implement/7/1-x",
            commit="abc123",
            changed_files=("x.py",),
            commands=({"command": "true", "result": "ok"},),
            validation_evidence=None,
            unresolved_questions=(),
            next_intended_action="continue",
        )

    deps = AgentStepDeps(
        attempt=_facts(),
        model=model,
        workspace=workspace,
        tree_id="tree-1",
        activation_registry=registry,
        handoff_producer=producer,
    )
    tools = build_agent_step_tools(step, deps)
    opening = compose_step_opening(step, deps, {})
    deps.context_window_threshold = estimate_context_tokens(
        list(opening), [], list(tools)
    ) + 400

    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    first = " ".join(str(message.content) for message in model.invocations[0])
    second = " ".join(str(message.content) for message in model.invocations[1])
    assert delivery in first
    assert delivery in second
    # The replay carries the static delivery plus the snapshot note, nothing re-read.
    assert "Continuation Snapshot" in second


def test_activation_exchange_units_are_exempt_from_result_capping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized delivery still arrives whole: no pointer, no stored artifact."""
    store = InMemoryArtifactStore()
    big_skill = default_skill_md_text("alpha", body=("Body line.\n" * 600))
    assert len(big_skill) > DEFAULT_RESULT_CAP_LIMIT
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=big_skill,
    )
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    loaded = _load_config(config)
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    delivery = result.registry.bodies["alpha"]
    assert len(delivery) > DEFAULT_RESULT_CAP_LIMIT

    from coding_agent.skillbundle.activation import ActivationRegistry

    registry = ActivationRegistry(bodies=dict(result.registry.bodies))
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)

    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "alpha"}, "a1")]),
            _ai([_report_call()]),
        ]
    )
    deps = AgentStepDeps(
        attempt=_facts(),
        model=model,
        workspace=workspace,
        tree_id="tree-1",
        activation_registry=registry,
        artifact_store=store,
    )
    outcome = run_agent_step(_agent_step(""), 1, deps, {})
    assert outcome.status == "reported"
    tool_texts = [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    assert tool_texts == [delivery]
    assert "capped:" not in tool_texts[0]
    assert "read_result_slice" not in tool_texts[0]
    with pytest.raises(ArtifactNotFound):
        store.read("a1")


def test_prepared_path_ignores_legacy_companion_constants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poisoning the fixed tdd constants changes nothing on the prepared path."""
    _with_env(monkeypatch)
    import coding_agent.skillbundle.verify as verify_module

    monkeypatch.setattr(
        verify_module,
        "BUNDLE_SPEC",
        verify_module.BundleSpec(
            ids=frozenset({"skill:other"}),
            source_identity="git+https://example.com/poisoned.git",
            resolved_commit="0" * 40,
            companion_files={"tdd": ("poisoned.md",)},
        ),
    )
    monkeypatch.setattr(verify_module, "COMPANION_DIGESTS", {"tdd/tests.md": "0" * 64})
    member = DeclaredMember(
        name="tdd",
        source=SOURCE_A,
        commit=COMMIT_A,
        extra_files={"docs/guide.md": (b"Guide body.\n", False)},
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    loaded = _load_config(config)
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    delivery = result.registry.bodies["tdd"]
    assert "Guide body.\n" in delivery
    assert "poisoned" not in delivery


def test_store_can_vanish_after_verification_without_changing_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No skill-resource reader: the registry answers after the store is gone."""
    import shutil

    _with_env(monkeypatch)
    member = _reviewed_member()
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    loaded = _load_config(config)
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    delivery = result.registry.bodies["alpha"]
    shutil.rmtree(home)
    assert not home.exists()

    from coding_agent.skillbundle.activation import (
        ActivationRegistry,
        lookup_activation,
    )

    registry = ActivationRegistry(bodies=dict(result.registry.bodies))
    outcome = lookup_activation(registry, "alpha")
    assert hasattr(outcome, "text")
    assert outcome.text == delivery
    assert "NOT-DELIVERED-MARKER-4821" not in delivery
