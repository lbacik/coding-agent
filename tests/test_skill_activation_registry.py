"""The activation registry: one lookup/delivery path for every activation.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (skill
activation). At Worker startup the Skill Bundle's activations are loaded once
into an immutable in-memory registry; static ``instructions: [{skill: X}]``
(keeping their position among prompt instructions) and generic on-demand
activation go through one exact-name lookup and one delivery function, and
return typed outcomes rather than ``Error:``-prefixed strings. Activation
never reads from the filesystem after startup, and the Context-Window Handoff
replays activations from the registry.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.agent_step import (
    ACTIVATION_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    build_agent_step_tools,
    instruction_texts,
    run_agent_step,
)
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.skillbundle.activation import (
    ActivationAlreadyActive,
    ActivationDelivered,
    ActivationRefused,
    ActivationRegistry,
    deliver_activation,
    load_activation_registry,
    lookup_activation,
)
from coding_agent.skillbundle.verify import BUNDLE_SPEC
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.workflow.effective import skill_texts_from_registry
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2Instruction,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import write_skill_bundle

LABEL = "agent:implement"


def _workflow_with_skill(skill: str) -> V2Workflow:
    """One agent step with a static skill instruction, routed to ``done``."""
    return V2Workflow(
        id="implement",
        model="sonnet",
        input=V2Input(type="issue", label=LABEL),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        defaults_routes={"error": V2RouteTarget(end="failed")},
        steps={
            "work": V2Step(
                id="work",
                agent=V2Agent(
                    results={"done": {}},
                    instructions=(V2Instruction(skill=skill),),
                ),
                max_executions=1,
                routes={"done": V2RouteTarget(end="delivered")},
            ),
        },
    )


def _agent_step(skill: str | None = None) -> V2Step:
    instructions: tuple[V2Instruction, ...] = (
        (V2Instruction(skill=skill),) if skill is not None else ()
    )
    return V2Step(
        id="work",
        agent=V2Agent(results={"done": {}}, instructions=instructions),
        max_executions=1,
        routes={"done": V2RouteTarget(end="done")},
    )


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


def _deps(
    attempt: StepAttemptFacts,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    **overrides: Any,
) -> AgentStepDeps:
    params: dict[str, Any] = {
        "attempt": attempt,
        "model": model,
        "workspace": workspace,
        "tree_id": "tree-1",
        "archive_dir": tmp_path / "archive",
    }
    params.update(overrides)
    return AgentStepDeps(**params)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def registry(tmp_path: Path) -> ActivationRegistry:
    """The startup snapshot over a synthetic bundle (loaded once)."""
    return load_activation_registry(write_skill_bundle(tmp_path))


# --- The registry itself -------------------------------------------------------


def test_load_covers_every_bundle_member_verbatim(
    registry: ActivationRegistry, tmp_path: Path
) -> None:
    """Startup loads every member's body whole, companions included."""
    assert set(registry.bodies) == set(BUNDLE_SPEC.names)
    skills_dir = tmp_path / ".agents" / "skills"
    for skill in BUNDLE_SPEC.names:
        body = (skills_dir / skill / "SKILL.md").read_text(encoding="utf-8")
        assert body in registry.bodies[skill]
    for relative in BUNDLE_SPEC.companion_files.get("tdd", ()):
        companion = (tmp_path / ".agents" / "skills" / "tdd" / relative).read_text(
            encoding="utf-8"
        )
        assert companion in registry.bodies["tdd"]


def test_registry_is_immutable(registry: ActivationRegistry) -> None:
    """The snapshot cannot be reassigned or mutated in place."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        registry.bodies = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        registry.bodies["tdd"] = "rewritten"  # type: ignore[index]


def test_skill_texts_from_registry(tmp_path: Path) -> None:
    """Static texts derive from the snapshot, not the filesystem."""
    registry = load_activation_registry(write_skill_bundle(tmp_path))
    texts = skill_texts_from_registry(registry, ["tdd", "implement"])
    assert texts == {"tdd": registry.bodies["tdd"], "implement": registry.bodies["implement"]}


def test_skill_texts_from_registry_refuses_an_undeclared_name(
    registry: ActivationRegistry,
) -> None:
    """A statically named skill outside the registry is a startup refusal."""
    with pytest.raises(OSError, match="not in the activation registry"):
        skill_texts_from_registry(registry, ["setup-matt-pocock-skills"])


# --- One lookup/delivery path --------------------------------------------------


def test_static_and_on_demand_deliver_byte_identical_content(
    workspace: Path, tmp_path: Path, registry: ActivationRegistry
) -> None:
    """Both paths share the lookup: the same skill, the same bytes."""
    for skill in sorted(BUNDLE_SPEC.names):
        step = _agent_step(skill)
        deps = _deps(_facts(), ScriptedModel([]), workspace, tmp_path,
                     activation_registry=registry)
        static = instruction_texts(step, deps)
        assert static == [registry.bodies[skill]]

        model = ScriptedModel(
            [
                _ai([_call(ACTIVATION_TOOL_NAME, {"skill": skill}, "a1")]),
                _ai([_report_call()]),
            ]
        )
        deps = _deps(_facts(), model, workspace, tmp_path,
                     activation_registry=registry)
        outcome = run_agent_step(_agent_step(), 1, deps, {})
        assert outcome.status == "reported"
        tool_texts = [
            str(message.content)
            for message in model.invocations[1]
            if isinstance(message, ToolMessage)
        ]
        assert tool_texts == [registry.bodies[skill]]
        assert tool_texts == static


def test_static_instruction_keeps_position_among_prompts(
    workspace: Path, tmp_path: Path, registry: ActivationRegistry
) -> None:
    """The skill body lands in declared order among prompt instructions."""
    skill_body = registry.bodies["tdd"]
    prompt_body = "SYNTHETIC PROMPT BODY."
    step = V2Step(
        id="work",
        agent=V2Agent(
            results={"done": {}},
            instructions=(
                V2Instruction(prompt="prompts/first.md"),
                V2Instruction(skill="tdd"),
                V2Instruction(prompt="prompts/second.md"),
            ),
        ),
        max_executions=1,
        routes={"done": V2RouteTarget(end="done")},
    )
    deps = _deps(
        _facts(),
        ScriptedModel([]),
        workspace,
        tmp_path,
        activation_registry=registry,
        prompt_texts={
            "prompts/first.md": "FIRST " + prompt_body,
            "prompts/second.md": "SECOND " + prompt_body,
        },
    )
    texts = instruction_texts(step, deps)
    assert texts == ["FIRST " + prompt_body, skill_body, "SECOND " + prompt_body]


@pytest.mark.parametrize(
    "name",
    [
        "unknown-skill",
        "setup-matt-pocock-skills",
        "TDD",
        "Tdd",
        "IMPLEMENT",
        "Code-Review",
        "/tdd",
        "tdd/",
        "tdd/tests.md",
        "../tdd",
        "..",
        "",
        " tdd",
        "tdd ",
        "tdd\n",
        "code-review/extra",
    ],
)
def test_refused_names_are_typed_normal_refusals(
    name: str, workspace: Path, tmp_path: Path, registry: ActivationRegistry
) -> None:
    """Unknown, absolute, traversal, path-shaped and cased names refuse cleanly."""
    outcome = lookup_activation(registry, name)
    assert isinstance(outcome, ActivationRefused)
    assert not outcome.text.startswith("Error:")

    delivered = deliver_activation(registry, name, set())
    assert isinstance(delivered, ActivationRefused)

    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": name}, "a1")]),
            _ai([_report_call()]),
        ]
    )
    deps = _deps(_facts(), model, workspace, tmp_path,
                 activation_registry=registry)
    step_outcome = run_agent_step(_agent_step(), 1, deps, {})
    assert step_outcome.status == "reported"
    tool_texts = [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    assert tool_texts == [delivered.text]

    static = instruction_texts(_agent_step(name), deps)
    assert static == [delivered.text]


def test_refusal_marks_nothing_active(
    workspace: Path, tmp_path: Path, registry: ActivationRegistry
) -> None:
    """A refused name leaves the active set alone: the next hit still delivers."""
    activated: set[str] = set()
    refused = deliver_activation(registry, "../tdd", activated)
    assert isinstance(refused, ActivationRefused)
    assert activated == set()
    delivered = deliver_activation(registry, "tdd", activated)
    assert isinstance(delivered, ActivationDelivered)
    assert delivered.content == registry.bodies["tdd"]
    assert activated == {"tdd"}


def test_non_string_names_are_refused(registry: ActivationRegistry) -> None:
    """A non-string activation argument is a typed refusal, not a crash."""
    for name in (None, 123, ["tdd"], {"skill": "tdd"}):
        outcome = deliver_activation(registry, name, set())
        assert isinstance(outcome, ActivationRefused)


def test_undeclared_skill_refuses_from_a_narrower_registry(
    workspace: Path, tmp_path: Path
) -> None:
    """The registry — not the bundle constant — decides what is declared."""
    narrow = ActivationRegistry(bodies={"tdd": "TDD BODY"})
    static = instruction_texts(
        _agent_step("implement"),
        _deps(_facts(), ScriptedModel([]), workspace, tmp_path,
              activation_registry=narrow),
    )
    assert len(static) == 1
    assert "implement" in static[0]
    assert "TDD BODY" not in static[0]


def test_error_prefixed_content_is_delivered_not_refused(
    workspace: Path, tmp_path: Path
) -> None:
    """A body that literally begins with ``Error:`` is content (SKV3-18)."""
    body = "Error: something upstream wrote this first line.\n\nRest."
    registry = ActivationRegistry(
        bodies={"tdd": body, "implement": "x", "code-review": "y", "codebase-design": "z"}
    )
    outcome = deliver_activation(registry, "tdd", set())
    assert isinstance(outcome, ActivationDelivered)
    assert outcome.content == body
    assert outcome.text == body

    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "tdd"}, "a1")]),
            _ai([_report_call()]),
        ]
    )
    deps = _deps(_facts(), model, workspace, tmp_path,
                 activation_registry=registry)
    step_outcome = run_agent_step(_agent_step(), 1, deps, {})
    assert step_outcome.status == "reported"
    tool_texts = [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    assert tool_texts == [body]
    assert instruction_texts(_agent_step("tdd"), deps) == [body]


def test_duplicate_activation_is_acknowledged(
    workspace: Path, tmp_path: Path, registry: ActivationRegistry
) -> None:
    """The second on-demand call is acknowledged; the body goes out once."""
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(ACTIVATION_TOOL_NAME, {"skill": "tdd"}, "a1"),
                    _call(ACTIVATION_TOOL_NAME, {"skill": "tdd"}, "a2"),
                ]
            ),
            _ai([_report_call()]),
        ]
    )
    deps = _deps(_facts(), model, workspace, tmp_path,
                 activation_registry=registry)
    outcome = run_agent_step(_agent_step(), 1, deps, {})
    assert outcome.status == "reported"
    tool_texts = [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    assert tool_texts == [registry.bodies["tdd"], "Skill 'tdd' is already active."]


# --- No filesystem after startup -----------------------------------------------


def test_disk_mutation_changes_no_activation_within_or_between_attempts(
    workspace: Path, tmp_path: Path
) -> None:
    """Editing or deleting skill files after startup changes nothing."""
    skills_dir = write_skill_bundle(tmp_path)
    registry = load_activation_registry(skills_dir)
    original = dict(registry.bodies)

    (skills_dir / "tdd" / "SKILL.md").write_text("MUTATED BODY\n", encoding="utf-8")
    (skills_dir / "tdd" / "tests.md").write_text("MUTATED COMPANION\n", encoding="utf-8")
    (skills_dir / "implement" / "SKILL.md").unlink()

    for attempt_id in ("implement#7/1", "implement#7/2"):
        tools = build_agent_step_tools(
            _agent_step("tdd"),
            _deps(
                _facts(attempt_id=attempt_id),
                ScriptedModel([]),
                workspace,
                tmp_path,
                activation_registry=registry,
            ),
        )
        assert lookup_activation(registry, "tdd") == ActivationDelivered(
            skill="tdd", content=original["tdd"]
        )
        by_name = {tool.name: tool for tool in tools}
        assert str(by_name[ACTIVATION_TOOL_NAME].invoke({"skill": "tdd"})) == original["tdd"]
        assert (
            str(by_name[ACTIVATION_TOOL_NAME].invoke({"skill": "implement"}))
            == original["implement"]
        )


def test_deleted_bundle_dir_still_activates(
    workspace: Path, tmp_path: Path
) -> None:
    """The whole skills directory may vanish after startup without effect."""
    skills_dir = write_skill_bundle(tmp_path)
    registry = load_activation_registry(skills_dir)
    original = dict(registry.bodies)
    shutil.rmtree(skills_dir)
    assert not skills_dir.exists()

    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "code-review"}, "a1")]),
            _ai([_report_call()]),
        ]
    )
    deps = _deps(
        _facts(), model, workspace, tmp_path, activation_registry=registry
    )
    outcome = run_agent_step(_agent_step("code-review"), 1, deps, {})
    assert outcome.status == "reported"
    opening = " ".join(str(message.content) for message in model.invocations[0])
    assert original["code-review"] in opening
    tool_texts = [
        str(message.content)
        for message in model.invocations[1]
        if isinstance(message, ToolMessage)
    ]
    assert tool_texts == [original["code-review"]]


# --- Context-Window Handoff -----------------------------------------------------


def test_handoff_replays_activations_from_the_registry(
    workspace: Path, tmp_path: Path
) -> None:
    """The reopened conversation carries the registry body, not the disk."""
    from coding_agent.runtime.context_handoff import ContinuationSnapshot

    skills_dir = write_skill_bundle(tmp_path)
    registry = load_activation_registry(skills_dir)
    skill_body = registry.bodies["code-review"]
    step = _agent_step("code-review")
    model = ScriptedModel(
        [
            AIMessage(content="p" * 4000, tool_calls=[_call("read_file", {"path": "README.md"}, "big-1")]),
            _ai([_report_call()]),
        ]
    )

    def producer(messages: list[object]) -> ContinuationSnapshot:
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

    deps = _deps(
        _facts(),
        model,
        workspace,
        tmp_path,
        activation_registry=registry,
        handoff_producer=producer,
    )
    from coding_agent.runtime.context_handoff import estimate_context_tokens
    from coding_agent.runtime.graph.agent_step import build_agent_step_tools, compose_step_opening

    tools = build_agent_step_tools(step, deps)
    opening = compose_step_opening(step, deps, {})
    deps.context_window_threshold = estimate_context_tokens(
        list(opening), [], list(tools)
    ) + 400

    (skills_dir / "code-review" / "SKILL.md").write_text(
        "MUTATED AFTER STARTUP\n", encoding="utf-8"
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    fresh_invocation = " ".join(str(message.content) for message in model.invocations[1])
    assert skill_body in fresh_invocation
    assert "MUTATED AFTER STARTUP" not in fresh_invocation


# --- Authority and tool restrictions --------------------------------------------


def test_activation_binds_no_new_tool_and_subagents_stay_read_only(
    workspace: Path, tmp_path: Path, registry: ActivationRegistry
) -> None:
    """Activation grants no tool: the bound set is unchanged, subagents lack it."""
    from coding_agent.runtime.graph.agent_step import bound_tool_names
    from coding_agent.runtime.graph.subagent import (
        SUBAGENT_TOOL_NAMES,
        build_subagent_tools,
    )
    from coding_agent.runtime.graph.subagent import SubagentRecord

    step = _agent_step("tdd")
    deps = _deps(_facts(), ScriptedModel([]), workspace, tmp_path,
                 activation_registry=registry)
    names = bound_tool_names(step)
    assert ACTIVATION_TOOL_NAME in names
    assert "commit" not in " ".join(names)

    tools = build_subagent_tools(workspace, SubagentRecord(description="look"))
    assert {tool.name for tool in tools} == set(SUBAGENT_TOOL_NAMES)
    assert ACTIVATION_TOOL_NAME not in {tool.name for tool in tools}


# --- The Worker-loop seam ---------------------------------------------------------


def _issue_input(number: int, created_at: str) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=f"Add a greeting {number}",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=1001)
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = _issue_input(number, created_at)
        tracker.labeled_events[(number, LABEL)] = LabeledEvent(
            label=LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at="2026-10-01T00:00:00Z",
        )
    return tracker


def _claim_config(workflow: V2Workflow) -> ClaimConfig:
    return ClaimConfig(
        workflow_id=workflow.id,
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login="coding-agent",
        agent_id=1001,
        effective_configuration=full_effective_configuration(workflow),
    )


def test_worker_loop_activates_from_the_registry_across_attempts(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """The seam: two Attempts activate the same registry bytes after deletion."""
    skills_dir = write_skill_bundle(tmp_path)
    registry = load_activation_registry(skills_dir)
    original_tdd = registry.bodies["tdd"]
    original_review = registry.bodies["code-review"]
    # The bundle vanishes after startup: no Attempt may read the filesystem.
    shutil.rmtree(skills_dir)

    workflow = _workflow_with_skill("tdd")
    tracker = _tracker_with((31, "2026-10-02T00:00:00Z"), (32, "2026-10-01T00:00:00Z"))
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "README.md").write_text("hello\n", encoding="utf-8")
    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "tdd"}, "a32")]),
            _ai([_report_call("r32")]),
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "tdd"}, "a31")]),
            _ai([_report_call("r31")]),
        ]
    )
    seen: list[str] = []

    def run_one_attempt(claimed: ConfirmedClaim) -> WorkflowResult:
        facts = _facts(
            attempt_id=claimed.attempt_id,
            input_number=claimed.input_number,
            title=claimed.pin.title,
            body=claimed.pin.body,
            base_revision=claimed.pin.base_revision,
            current_revision=claimed.pin.base_revision,
        )
        deps = AgentStepDeps(
            attempt=facts,
            model=model,
            workspace=work_root,
            tree_id="tree-1",
            archive_dir=tmp_path / "archive",
            activation_registry=registry,
            ledger=ledger,
        )
        effects_before = ledger.list_step_effects(claimed.attempt_id)
        outcome = run_agent_step(workflow.steps["work"], 1, deps, {})
        assert outcome.status == "reported"
        # Activation triggers no Runtime Operation: no new step effects.
        assert ledger.list_step_effects(claimed.attempt_id) == effects_before
        seen.append(" ".join(str(message.content) for message in model.invocations[-2]))
        seen.append(" ".join(str(message.content) for message in model.invocations[-1]))
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="delivered", reason="verified-completion"
        )
        tracker.remove_label(claimed.input_number, "issue", LABEL)
        tracker.unassign_input(claimed.input_number, "issue")
        return WorkflowResult(
            attempt_id=claimed.attempt_id, outcome="delivered", reason="verified-completion"
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=WorkerLoopConfig(
            claim_config=_claim_config(workflow),
            workflow=workflow,
            poll_interval_seconds=60.0,
        ),
        run_one_attempt=run_one_attempt,
        sleep=lambda seconds: None,
        max_attempts=2,
    )

    assert exit_code == 0
    assert [record.attempt_id for record in ledger.unfinished_attempts()] == []
    assert len(seen) == 4
    for index in (0, 2):
        assert original_tdd in seen[index]
        assert original_review not in seen[index]
        assert original_tdd in seen[index + 1]
    assert ledger.get_outcome("implement#32/1") is not None
    assert ledger.get_outcome("implement#31/1") is not None
