"""M2 step contract: `run_step` runs an ordered Workflow Step list.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (a fresh step
subgraph per step with its own message list; two step kinds — ``skill:
implement`` opens with the forced ``activate_implement``, a ``prompt_file``
step opens with a forced ``activate_step`` whose result is the prompt file's
bytes verbatim and exempt from result capping; positional exit tools —
``report_step`` in every non-final step and never in the final one,
``report_completion`` only in the final step; the step boundary composes a
Continuation Snapshot with the handoff mechanism carrying the justification
as a bounded field; the working tree carries over and the work-node budgets
are restored; stopping the list — a non-final ``cannot-continue`` ends
``completion not established`` / ``step-cannot-continue``, a non-final
silence after its continuations ends ``completion not established`` /
``undeclared``, a provider failure after its retries in any step ends
``failed`` / ``infrastructure-failure`` class ``provider``; later steps never
start and the Attempt continues to the fixed tail; a Context-Window Handoff
inside a ``prompt_file`` step reopens it with a forced ``activate_step``
carrying identical bytes) and §9 (every Implementation Outcome is produced
by at least one scripted run across the M2 tests).

MVP row ids: W-STP-1, W-STP-2, W-STP-4, W-STP-5, W-STP-6, G-STP-1, C-STP-1,
C-ERR-1.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from langchain_core.exceptions import ModelAPIError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import LedgerRecord, RunLedger
from coding_agent.provider.retry import DEFAULT_MAX_ATTEMPTS, invoke_with_retry
import coding_agent.provider.retry as retry_module
import coding_agent.runtime.graph.step as step_module
from coding_agent.runtime.completion import (
    MAX_STEP_JUSTIFICATION_CHARS,
    REPORT_STEP_CANNOT_CONTINUE,
    REPORT_STEP_DONE,
    StepReportRecord,
    build_report_step_tool,
)
from coding_agent.runtime.context_handoff import (
    ContinuationSnapshot,
    ContextWindowConfig,
    handoff_trace,
)
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.graph.step import (
    DEFAULT_STEPS,
    FORCED_ACTIVATION_STEP_TOOL,
    FORCED_ACTIVATION_TOOL,
    WorkflowStep,
)
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

JUNIT_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_targeted.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

# A roomy usable threshold mirroring tests/test_step_handoff.py: one padded
# assistant turn pushes the crossing request far over every fitting request.
ROOMY_THRESHOLD = 1500
PAD = "p" * 4000
STEP_ONE_MARKER = "step-one-marker-9f2c"
JUSTIFICATION_ONE = "phase one puts the header in place"


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    *,
    content: str = "",
    calls: list[dict[str, object]] | None = None,
) -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls or []))


def _activate(call_id: str, *, content: str = "activating") -> AIMessage:
    return _ai(content=content, calls=[_call("activate_implement", {}, call_id)])


def _activate_step(call_id: str, *, content: str = "activating step") -> AIMessage:
    return _ai(content=content, calls=[_call("activate_step", {}, call_id)])


def _report_step(call_id: str, status: str, justification: str) -> AIMessage:
    return _ai(
        content=f"step {status}",
        calls=[_call("report_step", {"status": status, "justification": justification}, call_id)],
    )


def _report_completion(call_id: str, test_id: str = "pkg::test_ok") -> AIMessage:
    return _ai(
        content="the change is complete",
        calls=[_call("report_completion", {"AC-1": [test_id]}, call_id)],
    )


def _write_and_test(write_id: str = "call-write-1", test_id: str = "call-test-1") -> AIMessage:
    return _ai(
        content="writing then testing",
        calls=[
            _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id),
            _call("test_targeted", {"path": "tests/test_greet.py"}, test_id),
        ],
    )


def _write_only(write_id: str = "call-write-1") -> AIMessage:
    return _ai(
        content="writing only",
        calls=[_call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id)],
    )


def _silent(text: str = "still thinking") -> AIMessage:
    return _ai(content=text)


def _snapshot_response(*, next_action: str = "run the targeted test") -> AIMessage:
    payload: dict[str, object] = {
        "version": 1,
        "attempt_id": "#7/1",
        "base_revision": "base-rev-abc",
        "branch": "agent/issue-7/attempt-1",
        "commit": "head-sha-def",
        "changed_files": ["src/greet.py"],
        "commands": [{"command": "test_targeted tests/test_greet.py::test_greet", "result": "passed"}],
        "validation_evidence": [{"test_id": "tests/test_greet.py::test_greet", "passed": True}],
        "unresolved_questions": ["which greeting wording?"],
        "next_intended_action": next_action,
    }
    return _ai(content=json.dumps(payload))


def _wire_text(message: object) -> str:
    """Message content with Anthropic cache breakpoints stripped."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(JUNIT_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _run_steps(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    model: ScriptedModel,
    steps: Sequence[WorkflowStep],
    context_window: ContextWindowConfig | None = None,
    ceilings: LoopCeilings | None = None,
) -> AttemptResult:
    return run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=FakeTracker(),
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
        context_window=context_window,
        steps=steps,
        ceilings=ceilings,
    )


def _step_boundary_records(ledger: RunLedger) -> list[LedgerRecord]:
    return [record for record in ledger.records("#7/1") if record.kind == "step_boundary"]


def test_two_steps_share_no_message_list(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """G-STP-1: the second step's first request holds prefix plus snapshot, no old turns."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1", content=STEP_ONE_MARKER),
            _report_step("call-step-1", REPORT_STEP_DONE, JUSTIFICATION_ONE),
            _activate("call-act-2"),
            _write_and_test(),
            _report_completion("call-rep-1"),
            # Repeated after the one review return (P-DEL-3).
            _report_completion("call-rep-2"),
        ]
    )
    result = _run_steps(ledger=ledger, workspace=workspace, skills_dir=skills_dir, model=model, steps=steps)
    assert result.outcome == "delivered"
    assert result.reason == "verified-completion"
    # Step one took two requests; the second step's first request is next.
    assert len(model.invocations) == 6
    first_opening = model.invocations[0]
    step_two_opening = model.invocations[2]
    assert len(first_opening) == 2
    assert len(step_two_opening) == 3
    assert [_wire_text(message) for message in step_two_opening[:2]] == [
        _wire_text(message) for message in first_opening[:2]
    ]
    assert isinstance(step_two_opening[2], HumanMessage)
    assert _wire_text(step_two_opening[2]).startswith("# Continuation Snapshot")
    assert STEP_ONE_MARKER not in json.dumps(
        [str(message.content) for message in step_two_opening]
    )
    # No handoff ran: the boundary is a step boundary, not a context handoff.
    assert handoff_trace(ledger, "#7/1").count == 0


def test_positional_exit_tools_on_the_bound_tool_set(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """W-STP-2: `report_step` without `report_completion` in non-final binds and vice versa."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            _report_step("call-step-1", REPORT_STEP_DONE, JUSTIFICATION_ONE),
            _activate("call-act-2"),
            _write_and_test(),
            _report_completion("call-rep-1"),
        ]
    )
    _run_steps(ledger=ledger, workspace=workspace, skills_dir=skills_dir, model=model, steps=steps)
    # One forced plus one unforced binding per work node; no handoff ran.
    assert len(model.bind_history) == 4
    assert model.tool_choices == ["activate_implement", None, "activate_implement", None]
    for tool_choice, names in model.bind_history[:2]:
        assert tool_choice in ("activate_implement", None)
        assert "report_step" in names
        assert "report_completion" not in names
    for tool_choice, names in model.bind_history[2:]:
        assert tool_choice in ("activate_implement", None)
        assert "report_completion" in names
        assert "report_step" not in names


def test_step_justification_reaches_the_next_snapshot(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """W-STP-5: the `report_step` justification is in the next step's Continuation Snapshot."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            _report_step("call-step-1", REPORT_STEP_DONE, JUSTIFICATION_ONE),
            _activate("call-act-2"),
            _write_and_test(),
            _report_completion("call-rep-1"),
        ]
    )
    _run_steps(ledger=ledger, workspace=workspace, skills_dir=skills_dir, model=model, steps=steps)
    step_two_opening = model.invocations[2]
    embedded = ContinuationSnapshot.from_json(
        _wire_text(step_two_opening[2]).split("\n\n", 1)[1]
    )
    assert embedded.step_justification == JUSTIFICATION_ONE
    records = _step_boundary_records(ledger)
    assert len(records) == 1
    payload = records[0].payload
    assert payload["step_id"] == "s1"
    assert payload["next_step_id"] == "s2"
    assert payload["step_justification"] == JUSTIFICATION_ONE
    assert records[0].artifact_ref is not None


def test_step_justification_persists_in_the_boundary_artifact(
    ledger: RunLedger, workspace: Path, skills_dir: Path, tmp_path: Path
) -> None:
    """W-STP-5 (artifact): the persisted boundary snapshot carries the justification."""
    from coding_agent.runtime.result_capping import InMemoryArtifactStore  # noqa: PLC0415

    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            _report_step("call-step-1", REPORT_STEP_DONE, JUSTIFICATION_ONE),
            _activate("call-act-2"),
            _write_and_test(),
            _report_completion("call-rep-1"),
        ]
    )
    store = InMemoryArtifactStore()
    run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=FakeTracker(),
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
        artifact_store=store,
        steps=steps,
    )
    records = _step_boundary_records(ledger)
    assert len(records) == 1
    assert records[0].artifact_ref is not None
    stored = ContinuationSnapshot.from_json(store.read(records[0].artifact_ref or ""))
    assert stored.step_justification == JUSTIFICATION_ONE
    assert stored.next_intended_action == "continue with Workflow Step s2"


def test_cannot_continue_stops_the_list(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """W-STP-1: a non-final `cannot-continue` stops the list; the diff stays for delivery."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            _write_only(),
            _report_step("call-step-1", REPORT_STEP_CANNOT_CONTINUE, "blocked: needs a human"),
        ]
    )
    result = _run_steps(ledger=ledger, workspace=workspace, skills_dir=skills_dir, model=model, steps=steps)
    assert result.outcome == "failed"
    assert result.reason == "step-cannot-continue"
    # The second step never started: one forced plus one unforced binding only.
    assert model.tool_choices == ["activate_implement", None]
    assert len(model.invocations) == 3
    assert len(model.bind_history) == 2
    assert _step_boundary_records(ledger) == []
    assert handoff_trace(ledger, "#7/1").count == 0
    # The working tree carries over: the diff stays for `delivery_snapshot` (M4).
    assert (workspace / "greeting.txt").exists()


def test_silence_in_a_non_final_step_stops_the_list(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """C-STP-1: a non-final step ending in silence after its continuations stops the list."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            _silent("quiet once"),
            _silent("quiet twice"),
            _silent("quiet thrice"),
        ]
    )
    result = _run_steps(ledger=ledger, workspace=workspace, skills_dir=skills_dir, model=model, steps=steps)
    assert result.outcome == "failed"
    assert result.reason == "undeclared"
    # Two continuations, then the node ended: the second step never started.
    assert len(model.invocations) == 4
    assert model.tool_choices == ["activate_implement", None]
    assert _step_boundary_records(ledger) == []


def test_handoff_in_a_prompt_file_step_reopens_with_identical_bytes(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """W-STP-4: a handoff inside a `prompt_file` step reopens with an identical `activate_step`."""
    prompt_text = "do the thing: write greeting.txt, run the targeted test, then report"
    steps = (WorkflowStep(step_id="only", prompt_bytes=prompt_text.encode("utf-8")),)
    model = ScriptedModel(
        [
            _activate_step("call-act-1"),
            _ai(content=PAD),
            _snapshot_response(),
            _activate_step("call-act-2"),
            _report_completion("call-rep-1", test_id="pkg::phantom"),
            _report_completion("call-rep-2", test_id="pkg::phantom"),
            _report_completion("call-rep-3", test_id="pkg::phantom"),
        ]
    )
    result = _run_steps(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        steps=steps,
        context_window=ContextWindowConfig(
            threshold_tokens=ROOMY_THRESHOLD,
            safety_margin_tokens=0,
            request_overhead_tokens=0,
            handoff_token_cap=2000,
        ),
    )
    assert result.outcome == "failed"
    assert result.reason == "claims-unverified"
    # Exactly one bounded handoff; every forced choice is the step's own tool.
    assert len(model.invocations) == 7
    assert model.tool_choices == ["activate_step", None, "activate_step", None]
    # Two distinct `activate_step` tool results with bytes identical to the prompt.
    # `_wire_text` unwraps the Anthropic cache-breakpoint copy (`list` content)
    # applied to the tail message of each request, so both the copied and the
    # stored (plain `str`) shapes count; the `tool_call_id` set proves two
    # distinct activations rather than one object reappearing in history.
    results = [
        message
        for invocation in model.invocations
        for message in invocation
        if isinstance(message, ToolMessage) and _wire_text(message) == prompt_text
    ]
    assert {message.tool_call_id for message in results} == {"call-act-1", "call-act-2"}


class _ProviderFailingModel(ScriptedModel):
    """A scripted model that raises a provider error from one invoke onward."""

    def __init__(
        self,
        outcomes: list[AIMessage],
        *,
        fail_from: int,
        error: Exception,
    ) -> None:
        super().__init__(outcomes)
        self._fail_from = fail_from
        self._error = error
        self.model_ids: list[int] = []

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        self.model_ids.append(id(self))
        if len(self.invocations) >= self._fail_from:
            assert isinstance(input, Sequence)
            self.invocations.append(tuple(input))  # type: ignore[arg-type]
            raise self._error
        return super().invoke(input)


def test_provider_failure_in_a_later_step_stops_the_list(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """C-ERR-1: retries hit the same Pinned Model; the list stops as provider infrastructure-failure."""
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = _ProviderFailingModel(
        [
            _activate("call-act-1"),
            _report_step("call-step-1", REPORT_STEP_DONE, JUSTIFICATION_ONE),
        ],
        fail_from=2,
        error=ModelAPIError("synthetic 503 overload"),
    )
    result = _run_steps(ledger=ledger, workspace=workspace, skills_dir=skills_dir, model=model, steps=steps)
    assert result.outcome == "failed"
    assert "infrastructure-failure" in result.reason
    assert "provider" in result.reason
    # Step one took two requests; step two retried the same model to exhaustion.
    assert len(model.invocations) == 2 + DEFAULT_MAX_ATTEMPTS
    assert set(model.model_ids) == {id(model)}
    assert len(model.model_ids) == len(model.invocations)
    # No model swap and no automatic new Attempt: the retry takes exactly one
    # model, and nothing in the retry or the step loop wires a fallback model.
    assert list(inspect.signature(invoke_with_retry).parameters) == [
        "model",
        "input",
        "max_attempts",
    ]
    combined_sources = inspect.getsource(retry_module) + inspect.getsource(step_module)
    assert "with_fallbacks" not in combined_sources
    assert "fallback_model" not in combined_sources
    # Step one completed before step two failed, so its boundary persists: the
    # list stops without starting any later step, but the composed snapshot is
    # not rolled back (contract §8, W-STP-5).
    boundary_records = _step_boundary_records(ledger)
    assert len(boundary_records) == 1
    assert boundary_records[0].payload["step_id"] == "s1"
    assert boundary_records[0].payload["next_step_id"] == "s2"
    assert boundary_records[0].payload["step_justification"] == JUSTIFICATION_ONE


def test_workflow_step_validation() -> None:
    """Unit: a Workflow Step sets exactly one of skill or prompt bytes."""
    assert DEFAULT_STEPS == (WorkflowStep(step_id="implement", skill="implement"),)
    assert WorkflowStep(step_id="s", skill="implement").forced_tool == FORCED_ACTIVATION_TOOL
    prompt_step = WorkflowStep(step_id="p", prompt_bytes=b"do it")
    assert prompt_step.forced_tool == FORCED_ACTIVATION_STEP_TOOL
    with pytest.raises(ValueError):
        WorkflowStep(step_id="", skill="implement")
    with pytest.raises(ValueError):
        WorkflowStep(step_id="s")
    with pytest.raises(ValueError):
        WorkflowStep(step_id="s", skill="implement", prompt_bytes=b"do it")
    with pytest.raises(ValueError):
        WorkflowStep(step_id="s", skill="tdd")
    with pytest.raises(ValueError):
        WorkflowStep(step_id="s", prompt_bytes=b"")


def test_report_step_truncates_and_refuses_gracefully() -> None:
    """Unit: the justification is bounded; a bad status is a tool error, never an exit."""
    record = StepReportRecord()
    tool = build_report_step_tool(record)
    long_justification = "j" * (MAX_STEP_JUSTIFICATION_CHARS + 100)
    assert tool.invoke({"status": REPORT_STEP_DONE, "justification": long_justification}) == "step done recorded"
    assert record.report is not None
    assert len(record.report.justification) == MAX_STEP_JUSTIFICATION_CHARS

    fresh = StepReportRecord()
    bad_tool = build_report_step_tool(fresh)
    outcome = bad_tool.invoke({"status": "maybe", "justification": "x"})
    assert isinstance(outcome, str) and outcome.startswith("Error:")
    assert fresh.report is None

    non_string = StepReportRecord()
    non_string_tool = build_report_step_tool(non_string)
    outcome = non_string_tool.invoke({"status": REPORT_STEP_DONE, "justification": 42})
    assert isinstance(outcome, str) and outcome.startswith("Error:")
    assert non_string.report is None


def test_run_attempt_validates_its_step_list(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """Unit: `run_attempt` refuses an empty or ambiguous step list before any model call."""
    model = ScriptedModel([_activate("call-act-1")])
    with pytest.raises(ValueError):
        run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            steps=[],
        )
    with pytest.raises(ValueError):
        run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            steps=[
                WorkflowStep(step_id="dup", skill="implement"),
                WorkflowStep(step_id="dup", skill="implement"),
            ],
        )
    assert model.invocations == []


def test_meters_run_attempt_wide_across_steps(
    tmp_path: Path, workspace: Path, skills_dir: Path
) -> None:
    """W-STP-6: a later step's work node sees the earlier step's spend.

    Step one makes three tool calls, under the Gate of a five-call ceiling.
    Step two's activation is the fourth call Attempt-wide: the Gate and the
    reserve are recorded against four calls, not the node's one, and the
    write-and-test turn crosses the ceiling, ending the list before any
    claim. Counted per node, step two would have reached a verified claim.
    """
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    model = ScriptedModel(
        [
            _activate("call-act-1"),
            _write_only(),
            _report_step("call-step-1", REPORT_STEP_DONE, JUSTIFICATION_ONE),
            _activate("call-act-2"),
            _write_and_test("call-write-2", "call-test-2"),
            _report_completion("call-rep-1"),
            _report_completion("call-rep-2"),
        ]
    )
    ceilings = LoopCeilings(max_tool_calls=5)
    ledger = RunLedger(tmp_path / "metered" / "run-ledger.sqlite3", ceilings=ceilings)
    try:
        result = _run_steps(
            ledger=ledger,
            workspace=workspace,
            skills_dir=skills_dir,
            model=model,
            steps=steps,
            ceilings=ceilings,
        )
        records = ledger.records("#7/1")
        boundary = _step_boundary_records(ledger)[0]
    finally:
        ledger.close()
    assert (result.outcome, result.reason) == ("failed", "saved-partial-work")
    # No claim was made: the ceiling stopped step two after write-and-test.
    assert len(model.invocations) == 5
    assert boundary.remaining_budget["tool_calls"] == 2
    # Step two's activation is the fourth call Attempt-wide: the Gate and the
    # reserve are recorded there, with one call left, not four.
    gate = next(record for record in records if record.kind == "gate")
    assert gate.remaining_budget["tool_calls"] == 1
