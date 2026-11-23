"""Round Handoff declarations while pickup stays disabled.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (Routes) and
§2.5/§3 (declared inputs and the required-reference proof).

Issue #611. An issue Workflow may carry valid ``round-handoff`` Route
annotations and ``optional: round-handoff.note`` declarations while the
``round-handoff`` enablement mapping is omitted. The declarations validate
like any other Route/input shape, round-trip through the generated schema
and the pinned v4 Effective Configuration, and behave as ordinary
``unfinished`` Routes / absent inputs at runtime. Pull-request Workflows
reject both.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from coding_agent import cli
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.agent_step import resolve_step_inputs
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.workflow.effective import _step_record
from coding_agent.workflow.jsonschema import build_workflow_json_schema
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    InputRef,
    RoundHandoffAnnotation,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.scripted_executor import ScriptedStepExecutor
from support.workflow_config import load_config

BASE = """\
version: 4
target:
  kind: github
  repository: OWNER/REPO
  base-branch: main
  token-env: GITHUB_TOKEN
models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000
"""

DEFAULTS = """\
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
"""

CEILING = (
    "    limits: {cost-usd: 6, effective-tokens: 1500000,"
    " tool-calls: 300, wall-clock-minutes: 30}\n"
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(BASE + body, encoding="utf-8")
    return path


def _valid_issue_workflows() -> str:
    """An issue Workflow with both annotation kinds and an optional note."""
    return (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: process, not-ready: {end: blocked}}\n"
          "      process:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [{optional: round-handoff.note}]\n"
          "          results:\n"
          "            done: {summary: document}\n"
          "        routes: {done: ai-note}\n"
          "      ai-note:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [process.facts]\n"
          "          results:\n"
          "            handoff: {note: document}\n"
          "        routes: {handoff: handoff-push}\n"
          "      handoff-push:\n"
          "        operation: commit-and-push\n"
          "        routes:\n"
          "          pushed: {end: unfinished,\n"
          "            round-handoff: {kind: ai-assisted, note: ai-note.handoff.note}}\n"
          "          no-changes: {end: unfinished, round-handoff: {kind: hard}}\n"
    )


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _faults_text(faults: Any) -> str:
    return "\n".join(f"{fault.path}: {fault.detail}" for fault in faults)


# --- valid disabled-mode declarations ----------------------------------------


def test_disabled_annotations_and_note_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both annotation kinds and the optional note validate with enablement omitted."""
    _without_env(monkeypatch)
    path = _write(tmp_path, _valid_issue_workflows())

    config, faults = load_config(path)

    assert config is not None, _faults_text(faults)
    assert not faults
    push = config.workflows["implement"].steps["handoff-push"]
    assert push.routes["pushed"].end == "unfinished"
    assert push.routes["pushed"].round_handoff is not None
    assert push.routes["pushed"].round_handoff.kind == "ai-assisted"
    assert push.routes["pushed"].round_handoff.note == "ai-note.handoff.note"
    assert push.routes["no-changes"].round_handoff is not None
    assert push.routes["no-changes"].round_handoff.kind == "hard"
    assert push.routes["no-changes"].round_handoff.note is None
    process = config.workflows["implement"].steps["process"]
    assert process.agent is not None
    assert process.agent.inputs == (
        InputRef(reference="round-handoff.note", optional=True),
    )


def test_annotated_workflow_validates_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent config validate` accepts the disabled-mode declarations offline."""
    _without_env(monkeypatch)
    path = _write(tmp_path, _valid_issue_workflows())

    assert cli.main(["config", "validate", "--config", str(path)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


def test_optional_note_on_every_first_agent_branch_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each path's first agent step may declare the optional note."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: left, not-ready: right}\n"
          "      left:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [{optional: round-handoff.note}]\n"
          "          results: {done: {}}\n"
          "        routes: {done: {end: completed}}\n"
          "      right:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [{optional: round-handoff.note}]\n"
          "          results: {done: {}}\n"
          "        routes: {done: {end: completed}}\n"
    )
    config, faults = load_config(_write(tmp_path, body))

    assert config is not None, _faults_text(faults)
    assert not faults


# --- annotation placement -----------------------------------------------------


def _annotation_case(routes: str) -> str:
    return (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: writer, not-ready: {end: blocked}}\n"
          "      writer:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results:\n"
          "            draft: {text: document}\n"
          "        routes: {draft: handoff-push}\n"
          "      handoff-push:\n"
          "        operation: commit-and-push\n"
        + routes
    )


ANNOTATED_PUSHED = (
    "        routes:\n"
    "          pushed: {end: unfinished,\n"
    "            round-handoff: {kind: ai-assisted, note: writer.draft.text}}\n"
    "          no-changes: {end: completed}\n"
)


@pytest.mark.parametrize(
    ("routes", "expect"),
    [
        (
            "        routes:\n"
            "          pushed: {end: completed,\n"
            "            round-handoff: {kind: hard}}\n"
            "          no-changes: {end: completed}\n",
            "round-handoff",
        ),
        (
            "        routes:\n"
            "          pushed: {step: writer, round-handoff: {kind: hard}}\n"
            "          no-changes: {end: completed}\n",
            "round-handoff",
        ),
    ],
)
def test_annotation_outside_unfinished_end_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routes: str, expect: str
) -> None:
    """The annotation lives only on an `unfinished` Workflow End Route."""
    _without_env(monkeypatch)

    config, faults = load_config(_write(tmp_path, _annotation_case(routes)))

    assert config is None
    assert expect in _faults_text(faults)
    assert "round-handoff" in _faults_text(faults)


def test_annotation_on_an_agent_step_route_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a `commit-and-push` step can carry the annotation."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: first\n"
        + DEFAULTS
        + "    steps:\n"
          "      first:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results: {done: {}}\n"
          "        routes:\n"
          "          done: {end: unfinished, round-handoff: {kind: hard}}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "round-handoff" in _faults_text(faults)
    assert "commit-and-push" in _faults_text(faults)


def test_ai_assisted_note_must_be_a_document_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-`document` note field is refused like any publication body."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: writer, not-ready: {end: blocked}}\n"
          "      writer:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results:\n"
          "            draft: {count: integer}\n"
          "        routes: {draft: handoff-push}\n"
          "      handoff-push:\n"
          "        operation: commit-and-push\n"
          "        routes:\n"
          "          pushed: {end: unfinished,\n"
          "            round-handoff: {kind: ai-assisted, note: writer.draft.count}}\n"
          "          no-changes: {end: completed}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "document" in _faults_text(faults)


def test_ai_assisted_note_must_be_proven_on_every_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path-dependent note is refused by the required-reference proof."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: writer, not-ready: handoff-push}\n"
          "      writer:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results:\n"
          "            draft: {text: document}\n"
          "        routes: {draft: handoff-push}\n"
          "      handoff-push:\n"
          "        operation: commit-and-push\n"
          "        routes:\n"
          "          pushed: {end: unfinished,\n"
          "            round-handoff: {kind: ai-assisted, note: writer.draft.text}}\n"
          "          no-changes: {end: completed}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "writer.draft.text" in _faults_text(faults)
    assert "not proven" in _faults_text(faults)


@pytest.mark.parametrize("kind", ["soft", "AI-ASSISTED", "42", "null"])
def test_unknown_annotation_kind_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Only `ai-assisted` and `hard` spell the annotation kind."""
    _without_env(monkeypatch)
    note = "" if kind != "soft" else ""
    routes = (
        "        routes:\n"
        f"          pushed: {{end: unfinished, round-handoff: {{kind: {kind}{note}}}}}\n"
        "          no-changes: {end: completed}\n"
    )

    config, faults = load_config(_write(tmp_path, _annotation_case(routes)))

    assert config is None
    assert "round-handoff" in _faults_text(faults)


def test_hard_annotation_with_a_note_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hard` carries no note field."""
    _without_env(monkeypatch)
    routes = (
        "        routes:\n"
        "          pushed: {end: unfinished,\n"
        "            round-handoff: {kind: hard, note: writer.draft.text}}\n"
        "          no-changes: {end: completed}\n"
    )

    config, faults = load_config(_write(tmp_path, _annotation_case(routes)))

    assert config is None
    assert "round-handoff" in _faults_text(faults)


def test_ai_assisted_annotation_without_a_note_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ai-assisted` requires the note reference."""
    _without_env(monkeypatch)
    routes = (
        "        routes:\n"
        "          pushed: {end: unfinished, round-handoff: {kind: ai-assisted}}\n"
        "          no-changes: {end: completed}\n"
    )

    config, faults = load_config(_write(tmp_path, _annotation_case(routes)))

    assert config is None
    assert "round-handoff" in _faults_text(faults)


def test_annotation_in_defaults_routes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`defaults.routes` holds scalar step ids and Workflow Ends only."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: first\n"
        + "    defaults:\n"
          "      max-executions: 1\n"
          "      routes: {error: {end: unfinished, round-handoff: {kind: hard}},\n"
          " limit-exceeded: {end: unfinished}}\n"
        + "    steps:\n"
          "      first:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results: {done: {}}\n"
          "        routes: {done: {end: completed}}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "defaults.routes" in _faults_text(faults)


# --- the special input --------------------------------------------------------


def test_required_note_reference_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note is optional-only: a required reference is refused."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: first\n"
        + DEFAULTS
        + "    steps:\n"
          "      first:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [round-handoff.note]\n"
          "          results: {done: {}}\n"
          "        routes: {done: {end: completed}}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "round-handoff.note" in _faults_text(faults)
    assert "optional" in _faults_text(faults)


def test_note_on_a_later_agent_step_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only possible first agent steps may declare the note."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: first\n"
        + DEFAULTS
        + "    steps:\n"
          "      first:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results: {done: {}}\n"
          "        routes: {done: second}\n"
          "      second:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [{optional: round-handoff.note}]\n"
          "          results: {done: {}}\n"
          "        routes: {done: {end: completed}}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "round-handoff.note" in _faults_text(faults)
    assert "first" in _faults_text(faults)


def test_note_as_an_operation_body_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note never backs an operation body."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + "    start: first\n"
        + DEFAULTS
        + "    steps:\n"
          "      first:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results:\n"
          "            done: {summary: document}\n"
          "        routes: {done: comment}\n"
          "      comment:\n"
          "        operation: post-comment\n"
          "        with: {body: round-handoff.note}\n"
          "        routes: {posted: {end: completed}}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    assert "round-handoff.note" in _faults_text(faults)


# --- pull-request Workflows ----------------------------------------------------


def _pr_workflows(steps: str) -> str:
    return (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        + CEILING
        + "    start: preflight\n"
        + DEFAULTS
        + steps
    )


def test_annotation_is_rejected_for_pull_request_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull-request Workflows never publish Round Handoffs."""
    _without_env(monkeypatch)
    steps = (
        "    steps:\n"
        "      preflight:\n"
        "        operation: check-readiness\n"
        "        routes: {ready: handoff-push, not-ready: {end: blocked}}\n"
        "      handoff-push:\n"
        "        operation: commit-and-push\n"
        "        routes:\n"
        "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
        "          no-changes: {end: completed}\n"
        "          stale-head: {end: failed}\n"
    )

    config, faults = load_config(_write(tmp_path, _pr_workflows(steps)))

    assert config is None
    assert "round-handoff" in _faults_text(faults)
    assert "pull-request" in _faults_text(faults)


def test_special_input_is_rejected_for_pull_request_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull-request Workflows never declare the note input."""
    _without_env(monkeypatch)
    steps = (
        "    steps:\n"
        "      preflight:\n"
        "        operation: check-readiness\n"
        "        routes: {ready: process, not-ready: {end: blocked}}\n"
        "      process:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          inputs: [{optional: round-handoff.note}]\n"
        "          results: {done: {}}\n"
        "        routes: {done: {end: completed}}\n"
    )

    config, faults = load_config(_write(tmp_path, _pr_workflows(steps)))

    assert config is None
    assert "round-handoff.note" in _faults_text(faults)
    assert "pull-request" in _faults_text(faults)


# --- schema round-trip ----------------------------------------------------------


def _validator() -> Draft202012Validator:
    return Draft202012Validator(build_workflow_json_schema())


def test_annotated_file_passes_the_generated_schema(tmp_path: Path) -> None:
    """The disabled-mode declarations are structural schema shapes."""
    path = _write(tmp_path, _valid_issue_workflows())
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert _validator().is_valid(data), list(_validator().iter_errors(data))


def test_unknown_annotation_kind_fails_the_generated_schema(tmp_path: Path) -> None:
    """A kind outside the closed vocabulary is structurally rejected."""
    path = _write(tmp_path, _valid_issue_workflows())
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    push_routes = data["workflows"]["implement"]["steps"]["handoff-push"]["routes"]
    push_routes["pushed"]["round-handoff"]["kind"] = "soft"

    assert not _validator().is_valid(data)


def test_hard_annotation_with_a_note_fails_the_generated_schema(tmp_path: Path) -> None:
    """`hard` carries no note, structurally as well as statically."""
    path = _write(tmp_path, _valid_issue_workflows())
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    push_routes = data["workflows"]["implement"]["steps"]["handoff-push"]["routes"]
    push_routes["no-changes"]["round-handoff"]["note"] = "ai-note.handoff.note"

    assert not _validator().is_valid(data)


# --- effective configuration -----------------------------------------------------


def test_annotation_round_trips_through_the_effective_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical record preserves the annotation kind and note reference."""
    _without_env(monkeypatch)
    config, faults = load_config(_write(tmp_path, _valid_issue_workflows()))
    assert config is not None, _faults_text(faults)

    routes = _step_record(config.workflows["implement"].steps["handoff-push"])["routes"]
    assert isinstance(routes, dict)
    assert routes["pushed"] == {
        "end": "unfinished",
        "round_handoff": {"kind": "ai-assisted", "note": "ai-note.handoff.note"},
    }
    assert routes["no-changes"] == {"end": "unfinished", "round_handoff": {"kind": "hard"}}


def test_unannotated_routes_keep_their_effective_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workflows without the declarations pin byte-identical route records."""
    _without_env(monkeypatch)
    config, faults = load_config(_write(tmp_path, _valid_issue_workflows()))
    assert config is not None, _faults_text(faults)

    preflight = _step_record(config.workflows["implement"].steps["preflight"])["routes"]
    assert preflight == {"ready": {"step": "process"}, "not-ready": {"end": "blocked"}}
    process = _step_record(config.workflows["implement"].steps["process"])
    assert isinstance(process, dict)
    agent = process["agent"]
    assert isinstance(agent, dict)
    assert agent["inputs"] == [
        {"reference": "round-handoff.note", "optional": True}
    ]


# --- disabled runtime behavior -----------------------------------------------------


def test_optional_note_is_absent_without_enablement() -> None:
    """With enablement omitted the optional note resolves to absent, never an error."""
    step = WorkflowStep(
        id="process",
        agent=Agent(
            tools=("workspace-read",),
            inputs=(InputRef(reference="round-handoff.note", optional=True),),
            results={"done": {}},
        ),
        routes={"done": RouteTarget(end="completed")},
    )

    resolved, reason = resolve_step_inputs(step, {})

    assert reason is None
    assert resolved == {"round-handoff.note": None}


def test_annotated_route_keeps_ordinary_unfinished_behavior(tmp_path: Path) -> None:
    """An annotated Route ends `unfinished` like any ordinary Workflow End."""
    push = WorkflowStep(
        id="push",
        operation="commit-and-push",
        routes={
            "pushed": RouteTarget(
                end="unfinished",
                round_handoff=RoundHandoffAnnotation(kind="hard"),
            ),
            "no-changes": RouteTarget(end="unfinished"),
        },
    )
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:implement"),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="push",
        defaults_routes={},
        steps={"push": push},
    )
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        result = run_workflow(
            workflow=workflow,
            attempt_id="implement#7/1",
            ledger=ledger,
            step_executor=ScriptedStepExecutor({"push": ["pushed"]}),
        )
    finally:
        ledger.close()

    assert result.outcome == "unfinished"
    assert result.ending_step == "push"
    assert result.step_result == "pushed"


def test_cli_refusal_names_the_annotation_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent config validate` names the offending annotation path."""
    _without_env(monkeypatch)
    routes = (
        "        routes:\n"
        "          pushed: {end: unfinished, round-handoff: {kind: ai-assisted}}\n"
        "          no-changes: {end: completed}\n"
    )
    path = _write(tmp_path, _annotation_case(routes))

    assert cli.main(["config", "validate", "--config", str(path)]) == 1
    err = capsys.readouterr().err
    assert "round-handoff" in err
    assert "workflows.implement.steps.handoff-push.routes.pushed" in err


def test_committed_schema_covers_the_annotation() -> None:
    """The generated schema text carries the new annotation shape."""
    from coding_agent.workflow.jsonschema import dump_workflow_json_schema

    text = dump_workflow_json_schema()
    assert "round-handoff" in text
    assert "ai-assisted" in text
    assert copy.deepcopy(yaml.safe_load(text)) is not None
