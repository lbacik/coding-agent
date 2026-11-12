"""One `agent` Workflow Step: conversation, tools and the Step Result report.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (one fresh
conversation per Step Execution, the opening frame, instructions, tool
groups, the named Step Result report with payload validation and bounded
correction, Step Artifacts, inputs and the Execution Summary, Sub-agents
without review attribution, the Context-Window Handoff and the single
Pinned Model).

Slice S4 of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: STEP-1 … STEP-14, STEP-16.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.context_handoff import ContinuationSnapshot
from coding_agent.runtime.graph.agent_step import (
    ACTIVATION_TOOL_NAME,
    MAX_REPORT_CORRECTIONS,
    REPORT_TOOL_NAME,
    SLICE_TOOL_NAME,
    AgentStepDeps,
    EvidenceStore,
    PriorStepExecution,
    StepAttemptFacts,
    bound_tool_names,
    compose_attempt_header,
    compose_execution_summary,
    compose_step_opening,
    resolve_step_inputs,
    run_agent_step,
    store_step_artifacts,
    validate_step_report,
)
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2FieldType,
    V2Input,
    V2InputRef,
    V2Instruction,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.scripted_model import ScriptedModel

SECRET = "github_pat_testtoken"


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


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, V2FieldType]],
    *,
    tools: tuple[str, ...] = ("workspace-read",),
    instructions: tuple[V2Instruction, ...] = (),
    inputs: tuple[V2InputRef, ...] = (),
    routes: dict[str, V2RouteTarget] | None = None,
    max_executions: int = 1,
) -> V2Step:
    targets = routes or {"done": V2RouteTarget(end="completed")}
    return V2Step(
        id=step_id,
        agent=V2Agent(
            instructions=instructions,
            tools=tools,
            inputs=inputs,
            results=results,
        ),
        max_executions=max_executions,
        routes=dict(targets),
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(calls: list[dict[str, object]] | None = None, content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls or []))


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
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
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


def _workflow(
    workflow_id: str,
    steps: dict[str, V2Step],
    start: str,
    *,
    defaults_routes: dict[str, V2RouteTarget] | None = None,
) -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type="issue", label="agent:test"),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start=start,
        defaults_routes=defaults_routes
        or {"error": V2RouteTarget(end="failed"), "limit-exceeded": V2RouteTarget(end="unfinished")},
        steps=steps,
    )


# --- STEP-1: fresh conversation per Step Execution -------------------------------


def test_second_step_opens_with_no_message_history(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-1: the second step's conversation carries no first-step history."""
    first = _agent_step("one", {"done": {"summary": "document"}})
    second = _agent_step("two", {"done": {"summary": "document"}})
    attempt = _facts()
    marker = "first-step-marker-4d2a"
    model = ScriptedModel(
        [
            _ai([_report_call("done", {"summary": "first writes " + marker})]),
            _ai([_report_call("done", {"summary": "second report"})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    run_agent_step(first, 1, deps, {})
    run_agent_step(second, 1, deps, {})
    assert len(model.invocations) == 2
    second_opening = " ".join(str(message.content) for message in model.invocations[1])
    assert marker not in second_opening


def test_workspace_carries_over_between_steps(workspace: Path, tmp_path: Path) -> None:
    """STEP-1: the shared workspace is the only implicit continuity."""
    first = _agent_step(
        "one", {"done": {}}, tools=("workspace-read", "workspace-write")
    )
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "write_file",
                        {"path": "note.txt", "content": "from step one"},
                        "w1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(first, 1, deps, {})
    assert outcome.status == "reported"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "from step one"


# --- STEP-2: instruction activation -----------------------------------------------


def test_instructions_activate_in_declared_order_verbatim(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-2: skill and prompt bodies open the conversation verbatim, in order."""
    skill_body = "# code-review\n\nSYNTHETIC SKILL BODY 8f3a."
    prompt_body = "SYNTHETIC PROMPT BODY c1d9."
    step = _agent_step(
        "review",
        {"reviewed": {"body": "document"}},
        tools=("workspace-read",),
        instructions=(
            V2Instruction(skill="code-review"),
            V2Instruction(prompt="prompts/review-policy.md"),
        ),
    )
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("reviewed", {"body": "findings"})])])
    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        skill_texts={"code-review": skill_body},
        prompt_texts={"prompts/review-policy.md": prompt_body},
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    opening = list(model.invocations[0])
    texts = [str(message.content) for message in opening]
    skill_at = next(i for i, text in enumerate(texts) if skill_body in text)
    prompt_at = next(i for i, text in enumerate(texts) if prompt_body in text)
    assert skill_at < prompt_at


def test_activation_grants_no_tool_and_no_authority(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-2/STEP-13: activating `code-review` binds no tool and triggers nothing."""
    skill_body = "# code-review\n\nSYNTHETIC SKILL BODY."
    step = _agent_step(
        "review",
        {"reviewed": {"body": "document"}},
        tools=("workspace-read",),
        instructions=(V2Instruction(skill="code-review"),),
    )
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai([_call(ACTIVATION_TOOL_NAME, {"skill": "code-review"}, "a1")]),
            _ai([_report_call("reviewed", {"body": "findings"})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path, skill_texts={"code-review": skill_body})
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    first_bind = model.bind_history[0][1]
    assert ACTIVATION_TOOL_NAME in first_bind
    assert "report_review" not in first_bind
    assert "commit" not in " ".join(first_bind)


# --- STEP-3: tool groups -------------------------------------------------------------


def test_only_declared_groups_plus_always_present_are_bound(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-3: `workspace-read` binds readers plus report/activation/slice."""
    step = _agent_step("s", {"done": {}}, tools=("workspace-read",))
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    names = model.bind_history[0][1]
    assert set(names) == {
        "read_file",
        "list_directory",
        "find_files",
        "search_text",
        "read_lines",
        "read_change",
        REPORT_TOOL_NAME,
        ACTIVATION_TOOL_NAME,
        SLICE_TOOL_NAME,
    }


def test_no_github_tool_is_ever_bound(workspace: Path, tmp_path: Path) -> None:
    """STEP-3: no label/comment/assignment/review tool exists in any group."""
    for groups in [
        ("workspace-read",),
        ("workspace-write",),
        ("shell",),
        ("tests",),
        ("subagents",),
        ("workspace-read", "workspace-write", "shell", "tests", "subagents"),
    ]:
        assert all(
            not any(
                marker in name
                for marker in ("label", "comment", "assign", "pull", "github")
                if marker != "comment" or name != SLICE_TOOL_NAME
            )
            or name in (REPORT_TOOL_NAME, ACTIVATION_TOOL_NAME, SLICE_TOOL_NAME)
            for name in bound_tool_names(
                _agent_step("s", {"done": {}}, tools=tuple(groups))
            )
            if name not in (REPORT_TOOL_NAME, ACTIVATION_TOOL_NAME, SLICE_TOOL_NAME)
        )
    step = _agent_step(
        "s",
        {"done": {}},
        tools=("workspace-read", "workspace-write", "shell", "tests", "subagents"),
    )
    assert "spawn_subagent" in bound_tool_names(step)


# --- STEP-4: the opening frame ----------------------------------------------------------


def test_opening_frame_has_header_input_inputs_and_contract(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-4: header, input as data, declared inputs and the result contract."""
    step = _agent_step(
        "process",
        {"done": {"summary": "document"}},
        tools=("workspace-read",),
        inputs=(V2InputRef(reference="preflight.facts"),),
    )
    attempt = _facts(title="Crash on boot", body="Traceback included.")
    model = ScriptedModel([_ai([_report_call("done", {"summary": "fixed"})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    priors = {
        "preflight": PriorStepExecution(
            step_id="preflight", seq=1, result="ready", payload={"ready": True},
            summary={"changed_files": [], "commands": []},
        )
    }
    outcome = run_agent_step(step, 1, deps, priors)
    assert outcome.status == "reported"
    header = compose_attempt_header(attempt)
    assert "implement#7/1" in header
    assert "issue #7" in header
    assert "Acceptance Criteria" not in header
    assert "Seam Set" not in header
    opening = compose_step_opening(
        step, deps, {"preflight.facts": priors["preflight"].summary}
    )
    texts = "\n".join(str(message.content) for message in opening)
    assert "Crash on boot" in texts
    assert "data" in texts
    assert "preflight.facts" in texts
    assert "done" in texts and "summary" in texts
    assert "Acceptance Criteria" not in texts
    assert "Seam Set" not in texts


# --- STEP-5/STEP-6: bounded correction -----------------------------------------------


def test_malformed_reports_are_corrected_then_end_error(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-5: undeclared names and schema violations return tool errors."""
    step = _agent_step("s", {"done": {"count": "integer"}})
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai([_report_call("bogus", {})]),
            _ai([_report_call("done", {"count": "not-an-int"})]),
            _ai([_report_call("done", {"count": 3})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.result_name == "done"
    assert outcome.corrections == 2


def test_correction_budget_exhausted_ends_error(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-5: after the bounded corrections the Step Execution ends `error`."""
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("bogus", {})]) for _ in range(MAX_REPORT_CORRECTIONS + 1)]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert outcome.payload is None


def test_responses_without_a_report_take_the_same_bounded_path(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-6: silent responses draw continuations, then end `error`."""
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([]) for _ in range(MAX_REPORT_CORRECTIONS + 2)])
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert outcome.result_name is None


# --- STEP-7: evidence-ref ------------------------------------------------------------


def test_evidence_ref_holds_only_for_a_pass_on_the_current_tree(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-7: unrun, failed and stale test ids are malformed reports."""
    schema: dict[str, dict[str, V2FieldType]] = {"done": {"criteria": "evidence-ref"}}
    step = _agent_step("s", schema)
    evidence = EvidenceStore()
    accepted, payload, errors = validate_step_report(
        step, "done", {"criteria": "pkg::test_ok"}, evidence=evidence, tree_id="tree-1"
    )
    assert accepted is None and errors
    evidence.record("pkg::test_bad", False, "tree-1")
    accepted, _, errors = validate_step_report(
        step, "done", {"criteria": "pkg::test_bad"}, evidence=evidence, tree_id="tree-1"
    )
    assert accepted is None and errors
    evidence.record("pkg::test_old", True, "tree-0")
    accepted, _, errors = validate_step_report(
        step, "done", {"criteria": "pkg::test_old"}, evidence=evidence, tree_id="tree-1"
    )
    assert accepted is None and errors
    evidence.record("pkg::test_ok", True, "tree-1")
    accepted, payload, errors = validate_step_report(
        step, "done", {"criteria": "pkg::test_ok"}, evidence=evidence, tree_id="tree-1"
    )
    assert (accepted, errors) == ("done", [])
    assert payload == {"criteria": "pkg::test_ok"}


def test_evidence_ref_failure_is_corrected_not_accepted(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-7: a report naming an unbacked test is returned, then corrected."""
    step = _agent_step(
        "s", {"done": {"criteria": "evidence-ref"}}, tools=("workspace-read", "tests")
    )
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai([_report_call("done", {"criteria": "pkg::missing"})]),
            _ai([_call("test_targeted", {"path": "pkg::real"}, "t1")]),
            _ai([_report_call("done", {"criteria": "pkg::real"})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.payload == {"criteria": "pkg::real"}
    assert outcome.corrections == 1


# --- STEP-8: Step Artifacts ------------------------------------------------------------


def test_document_fields_become_named_immutable_redacted_artifacts(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-8: `document` fields land outside the tree, redacted and immutable."""
    step = _agent_step("process", {"done": {"summary": "document", "n": "integer"}})
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {"summary": f"plan with {SECRET}", "n": 1})])]
    )
    archive = tmp_path / "archive"
    deps = _deps(
        attempt, model, workspace, tmp_path,
        archive_dir=archive, redactions={"TOKEN": SECRET},
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert set(outcome.artifacts) == {"summary"}
    artifact = Path(outcome.artifacts["summary"])
    assert archive in artifact.parents
    assert workspace not in artifact.parents
    assert SECRET not in artifact.read_text(encoding="utf-8")
    assert outcome.payload is not None
    again = store_step_artifacts(
        step_id="process", seq=1, result_name="done",
        payload={"summary": outcome.payload["summary"], "n": 1},
        schema=step.agent.results["done"] if step.agent else {},
        archive_dir=archive, redactions={"TOKEN": SECRET},
    )
    assert again == outcome.artifacts


# --- STEP-9: endings without a result -----------------------------------------------------


def test_error_endings_keep_facts_but_hold_no_result_or_artifact(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-9: `error` keeps Transcript/evidence/footprint/usage, rolls nothing back."""
    step = _agent_step("s", {"done": {"summary": "document"}})
    attempt = _facts()
    (workspace / "keep.txt").write_text("stays\n", encoding="utf-8")
    model = ScriptedModel(
        [_ai([_report_call("bogus", {})]) for _ in range(MAX_REPORT_CORRECTIONS + 1)]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert outcome.result_name is None
    assert outcome.payload is None
    assert outcome.artifacts == {}
    assert set(outcome.summary) >= {
        "changed_files", "commands", "evidence", "spend",
    }
    assert "Transcript" not in json.dumps(outcome.summary)
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "stays\n"


# --- STEP-11: inputs --------------------------------------------------------------------------


def test_inputs_read_the_latest_execution_and_absent_optionals(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-11: latest wins; absent optionals are stated absent."""
    step = _agent_step(
        "consumer",
        {"done": {}},
        inputs=(
            V2InputRef(reference="process.done.summary"),
            V2InputRef(reference="never.facts", optional=True),
        ),
    )
    priors = {
        "process": PriorStepExecution(
            step_id="process", seq=2, result="done",
            payload={"summary": "second"},
            summary={"changed_files": []},
        )
    }
    resolved, missing = resolve_step_inputs(step, priors)
    assert missing is None
    assert resolved["process.done.summary"] == "second"
    assert resolved["never.facts"] is None


def test_missing_required_input_ends_error_without_a_model_call(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-11: a required input missing at run time ends `error` silently."""
    step = _agent_step(
        "consumer", {"done": {}}, inputs=(V2InputRef(reference="process.done.summary"),)
    )
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert "process.done.summary" in outcome.detail
    assert model.invocations == []


# --- STEP-12: the Execution Summary -------------------------------------------------------


def test_facts_input_after_limit_exceeded_carries_the_summary() -> None:
    """STEP-12: `<step>.facts` exists with files/commands/evidence/limit/spend."""
    step = _agent_step("b", {"done": {}}, inputs=(V2InputRef(reference="a.facts"),))
    priors = {
        "a": PriorStepExecution(
            step_id="a",
            seq=1,
            result="limit-exceeded",
            payload=None,
            summary={
                "changed_files": ["x.py"],
                "commands": [{"command": "run_command true", "result": "ok"}],
                "evidence": {"pkg::t": {"passed": True}},
                "limit": {"scope": "step", "reason": "handoff-count"},
                "spend": {"model_calls": 2},
                "snapshot_digest": "abc",
            },
        )
    }
    resolved, missing = resolve_step_inputs(step, priors)
    assert missing is None
    summary = resolved["a.facts"]
    assert isinstance(summary, dict)
    assert set(summary) >= {
        "changed_files", "commands", "evidence", "limit", "spend", "snapshot_digest",
    }
    assert "Transcript" not in json.dumps(summary)


# --- STEP-13: Sub-agents without review attribution --------------------------------------


def test_subagents_run_read_only_and_count_against_the_step(
    workspace: Path, tmp_path: Path, ledger: RunLedger
) -> None:
    """STEP-13: a spawn group runs read-only; spend counts; no review is attributed."""
    from coding_agent.runtime.graph.subagent import SUBAGENT_TOOL_NAMES

    step = _agent_step(
        "review",
        {"reviewed": {"body": "document"}},
        tools=("workspace-read", "subagents"),
        instructions=(V2Instruction(skill="code-review"),),
    )
    attempt = _facts()
    seen: dict[str, Any] = {}
    from support.scripted_model import ScriptedModel as _SM

    assert SUBAGENT_TOOL_NAMES == frozenset(
        {
            "read_file",
            "list_directory",
            "find_files",
            "search_text",
            "read_lines",
            "read_result_slice",
            "read_change",
            "report_subagent",
        }
    )

    def runner(call_id: str, description: str, instructions: str) -> str:
        seen["call"] = (call_id, description, instructions)
        return "Sub-agent report body."

    model = _SM(
        [
            _ai(
                [
                    _call(ACTIVATION_TOOL_NAME, {"skill": "code-review"}, "a1"),
                    _call(
                        "spawn_subagent",
                        {"description": "check diff", "instructions": "read the diff"},
                        "s1",
                    ),
                ]
            ),
            _ai([_report_call("reviewed", {"body": "review notes"})]),
        ]
    )
    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        skill_texts={"code-review": "# code-review\n\nBody."},
        subagent_runner=runner,
        ledger=ledger,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert seen["call"][1:] == ("check diff", "read the diff")
    tool_calls = outcome.usage["tool_calls"]
    assert isinstance(tool_calls, int) and tool_calls >= 1
    kinds = [event["kind"] for event in outcome.events]
    assert kinds == [
        "model_response",
        "subagent_queued",
        "subagent_started",
        "subagent_ended",
        "model_response",
    ]
    assert ledger.list_step_effects(attempt.attempt_id) == ()


def test_subagent_spawn_validation_errors_start_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-13: an invalid spawn call is refused with no Sub-agent event."""
    step = _agent_step(
        "s", {"done": {}}, tools=("workspace-read", "subagents")
    )
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {"description": "", "instructions": "do it"},
                        "s1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    kinds = [event["kind"] for event in outcome.events]
    assert kinds == ["model_response", "model_response"]
    assert not any(str(kind).startswith("subagent_") for kind in kinds)


# --- STEP-14: Context-Window Handoff -----------------------------------------------------------


def _handoff_snapshot(attempt_id: str = "implement#7/1") -> ContinuationSnapshot:
    return ContinuationSnapshot(
        version=1,
        attempt_id=attempt_id,
        base_revision="abc123",
        branch="agent/implement/7/1-x",
        commit="abc123",
        changed_files=("x.py",),
        commands=({"command": "true", "result": "ok"},),
        validation_evidence=None,
        unresolved_questions=(),
        next_intended_action="continue",
    )


def _calibrated_threshold(step: V2Step, deps: AgentStepDeps, slack: int = 400) -> int:
    """A usable threshold the opening fits but the opening plus one padded
    turn exceeds (§9.9): the first request is sent, the second hands off."""
    from coding_agent.runtime.context_handoff import estimate_context_tokens
    from coding_agent.runtime.graph.agent_step import build_agent_step_tools

    tools = build_agent_step_tools(step, deps)
    opening = compose_step_opening(step, deps, {})
    return estimate_context_tokens(list(opening), [], list(tools)) + slack


_BIG = "p" * 4000


def _big_tool_call(call_id: str = "big-1") -> dict[str, object]:
    return _call("read_file", {"path": "README.md"}, call_id)


def test_handoff_continues_the_same_step_execution(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-14: a handoff reopens fresh with prefix, snapshot and activations."""
    step = _agent_step(
        "process",
        {"done": {"summary": "document"}},
        instructions=(V2Instruction(skill="code-review"),),
    )
    attempt = _facts()
    skill_body = "# code-review\n\nBody."
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG),
            _ai([_report_call("done", {"summary": "after handoff"})]),
        ]
    )
    produced: list[int] = []

    def producer(messages: list[object]) -> ContinuationSnapshot:
        produced.append(len(messages))
        return _handoff_snapshot()

    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        skill_texts={"code-review": skill_body},
        handoff_producer=producer,
    )
    deps.context_window_threshold = _calibrated_threshold(step, deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    assert outcome.summary["snapshot_digest"] == _handoff_snapshot().digest
    assert outcome.usage["handoffs"] == 1
    assert produced
    fresh_invocation = " ".join(str(message.content) for message in model.invocations[1])
    assert "Continuation Snapshot" in fresh_invocation
    assert skill_body in fresh_invocation


def test_failed_handoff_ends_error(workspace: Path, tmp_path: Path) -> None:
    """STEP-14: a malformed snapshot ends the Step Execution `error`."""

    def producer(messages: list[object]) -> ContinuationSnapshot:
        raise ValueError("handoff response was not valid JSON")

    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_big_tool_call()], content=_BIG)])
    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        handoff_producer=producer,
    )
    deps.context_window_threshold = _calibrated_threshold(step, deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert "Handoff" in outcome.detail
    assert outcome.result_name is None
    assert len(model.invocations) == 1


def test_too_many_handoffs_yields_limit_exceeded(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-14: exceeding the code-owned handoff count yields `limit-exceeded`."""
    from coding_agent.runtime.context_handoff import MAX_HANDOFFS_PER_STEP

    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_big_tool_call(f"big-{i}")], content=_BIG) for i in range(MAX_HANDOFFS_PER_STEP + 1)]
    )

    def producer(messages: list[object]) -> ContinuationSnapshot:
        return _handoff_snapshot()

    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        handoff_producer=producer,
    )
    deps.context_window_threshold = _calibrated_threshold(step, deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "limit-exceeded"
    assert outcome.handoffs == MAX_HANDOFFS_PER_STEP
    assert outcome.limit_fact == {"scope": "step", "reason": "handoff-count"}


def test_prefix_that_cannot_fit_refuses_before_any_model_work(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-14/§9.9: an over-large Pinned Prefix ends `error` with no model call."""
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_report_call("done", {})])])
    deps = _deps(attempt, model, workspace, tmp_path, context_window_threshold=1)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert model.invocations == []


# --- STEP-16: one Pinned Model ---------------------------------------------------------------


def test_every_request_uses_the_one_pinned_model(
    workspace: Path, tmp_path: Path
) -> None:
    """STEP-16: main, Sub-agent and handoff requests share one model object."""
    step = _agent_step(
        "s", {"done": {}}, tools=("workspace-read", "subagents")
    )
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {"description": "look", "instructions": "read files"},
                        "s1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ]
    )
    seen_models: list[int] = []
    inner = model.bind_tools

    def runner(call_id: str, description: str, instructions: str) -> str:
        seen_models.append(id(model))
        return "report"

    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        context_window_threshold=None,
        subagent_runner=runner,
    )
    assert inner is not None
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert seen_models == [id(model)]
    assert model.invocations, "the one model answered every request"


# --- Graph integration: error routing and Execution Summary facts -----------------------


def test_graph_routes_agent_error_and_records_facts(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§8.4/§9.6: the router follows the Step Result name; ends record facts."""
    bad = _agent_step("s", {"done": {}})
    attempt = _facts(attempt_id="w#1/1", workflow_id="w")
    model = ScriptedModel(
        [_ai([_report_call("bogus", {})]) for _ in range(MAX_REPORT_CORRECTIONS + 1)]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    workflow = _workflow(
        "w",
        {"s": bad},
        "s",
        defaults_routes={
            "done": V2RouteTarget(end="completed"),
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
    )
    result = run_workflow(
        workflow=workflow, attempt_id="w#1/1", ledger=ledger, agent_deps=deps
    )
    assert result.outcome == "failed"
    executions = ledger.list_step_executions("w#1/1")
    assert [(record.step_id, record.seq, record.result) for record in executions] == [
        ("s", 1, "error")
    ]
    assert executions[0].payload is None
    assert executions[0].facts is not None
    assert set(executions[0].facts) >= {"changed_files", "commands", "spend"}


def test_graph_routes_reported_result_and_reads_latest_inputs(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """STEP-11 at the graph level: the consumer reads the latest execution."""
    first = _agent_step(
        "process",
        {"done": {"summary": "document"}},
        routes={"done": V2RouteTarget(step="consumer")},
    )
    second = _agent_step(
        "consumer",
        {"done": {}},
        inputs=(V2InputRef(reference="process.done.summary"),),
        routes={"done": V2RouteTarget(end="completed")},
    )
    attempt = _facts(attempt_id="w#2/1", workflow_id="w")
    model = ScriptedModel(
        [
            _ai([_report_call("done", {"summary": "the summary"})]),
            _ai([_report_call("done", {})]),
        ]
    )
    deps = _deps(attempt, model, workspace, tmp_path)
    workflow = _workflow(
        "w",
        {"process": first, "consumer": second},
        "process",
        defaults_routes={
            "done": V2RouteTarget(end="completed"),
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
    )
    result = run_workflow(
        workflow=workflow, attempt_id="w#2/1", ledger=ledger, agent_deps=deps
    )
    assert result.outcome == "completed"
    executions = ledger.list_step_executions("w#2/1")
    assert [record.step_id for record in executions] == ["process", "consumer"]
    assert executions[0].payload == {"summary": "the summary"}


# --- Code-review follow-ups (§9.7/§9.8/§9.9/§13.3) --------------------------------------


def test_subagent_events_carry_attempt_step_and_call_ids(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§9.8: queued/started/ended carry the Attempt id, Step Execution, call id."""
    step = _agent_step("s", {"done": {}}, tools=("workspace-read", "subagents"))
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        "spawn_subagent",
                        {"description": "look", "instructions": "read files"},
                        "s1",
                    )
                ]
            ),
            _ai([_report_call("done", {})]),
        ]
    )

    def runner(call_id: str, description: str, instructions: str) -> str:
        return "report"

    deps = _deps(
        attempt,
        model,
        workspace,
        tmp_path,
        subagent_runner=runner,
        ledger=ledger,
    )
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    kinds = [event["kind"] for event in outcome.events]
    assert kinds == [
        "model_response",
        "subagent_queued",
        "subagent_started",
        "subagent_ended",
        "model_response",
    ]
    for event in outcome.events:
        payload = event["payload"]
        assert isinstance(payload, dict)
        assert payload["attempt_id"] == attempt.attempt_id
        assert payload["step_id"] == "s"
        assert payload["seq"] == 1
        if str(event["kind"]).startswith("subagent_"):
            assert payload["call_id"] == "s1"


def test_oversized_snapshot_ends_error(
    workspace: Path, tmp_path: Path
) -> None:
    """§9.9: an over-limit continuation snapshot ends the Step Execution `error`."""
    from dataclasses import replace

    from coding_agent.runtime.context_handoff import SNAPSHOT_SIZE_LIMIT_BYTES

    base = _handoff_snapshot()
    big = replace(
        base, next_intended_action="x" * (SNAPSHOT_SIZE_LIMIT_BYTES + 1)
    )

    def producer(messages: list[object]) -> ContinuationSnapshot:
        return big

    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel([_ai([_big_tool_call()], content=_BIG)])
    deps = _deps(
        attempt, model, workspace, tmp_path, handoff_producer=producer
    )
    deps.context_window_threshold = _calibrated_threshold(step, deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "error"
    assert "Handoff" in outcome.detail
    assert outcome.result_name is None


def test_summary_cap_trims_evidence_when_still_over_cap() -> None:
    """§9.7: the size cap trims evidence entries once commands/files are gone."""
    from coding_agent.runtime.graph.agent_step import EXECUTION_SUMMARY_CAP_BYTES

    evidence = {
        f"pkg::test-{index}": {
            "passed": True,
            "tree": "tree-1",
            "note": "y" * 200,
        }
        for index in range(200)
    }
    summary = compose_execution_summary(
        changed_files=[],
        commands=[],
        evidence=evidence,
        limit=None,
        spend={"model_calls": 1, "tool_calls": 0, "handoffs": 0},
        snapshot_digest=None,
        redactions=None,
    )
    assert summary.get("truncated") is True
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= EXECUTION_SUMMARY_CAP_BYTES


def test_transcript_keeps_pre_handoff_turns(
    workspace: Path, tmp_path: Path
) -> None:
    """§13.3: the Transcript keeps pre-handoff turns plus the snapshot note."""
    step = _agent_step("s", {"done": {}})
    attempt = _facts()
    model = ScriptedModel(
        [
            _ai([_big_tool_call()], content=_BIG),
            _ai([_report_call("done", {})]),
        ]
    )

    def producer(messages: list[object]) -> ContinuationSnapshot:
        return _handoff_snapshot()

    deps = _deps(
        attempt, model, workspace, tmp_path, handoff_producer=producer
    )
    deps.context_window_threshold = _calibrated_threshold(step, deps)
    outcome = run_agent_step(step, 1, deps, {})
    assert outcome.status == "reported"
    assert outcome.handoffs == 1
    assert deps.archive_dir is not None
    lines = (deps.archive_dir / "transcript.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert lines
    records = [json.loads(line) for line in lines]
    assert {record["conversation"] for record in records} == {"step/s/1"}
    assert any(
        "Continuation Snapshot" in str(record.get("content", ""))
        for record in records
    )
    assert any(_BIG[:100] in str(record.get("content", "")) for record in records)
