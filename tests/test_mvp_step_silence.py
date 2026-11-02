"""M2 step contract: silence is answered, not obeyed.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 ("Exit": a response
with no tool calls and no accepted exit draws one deterministic continuation
message composed by the runtime from the unmet obligations — the criteria
lacking evidence, whether a candidate diff exists, which targeted tests ran;
budget two per work node, monotonic and never reset by progress; once
exhausted the node ends ``completion not established`` / ``undeclared``; no
continuation in the verification reserve phase or after a hard ceiling) and
§9 (``report_completion`` is the only non-exceptional exit; the only other
exits are a ceiling, the verification reserve and a classified failure), and
[ADR 0014](../../docs/adr/0014-completion-is-declared-and-substantiated-never-inferred.md).

MVP row ids: L3-IMP-24, L3-IMP-25, L3-IMP-26.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

import coding_agent.runtime.graph.step as step_module
from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.acceptance import parse_acceptance_criteria
from coding_agent.runtime.completion import (
    CriterionEvidence,
    decide_implementation_outcome,
    normalize_claim,
    report_completion_schema,
)
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.silence import (
    MAX_SILENCE_CONTINUATIONS,
    SILENCE_CONTINUATION_MARKER,
    compose_silence_continuation,
    is_silence_continuation,
)
from coding_agent.tracker.port import TrackedIssue
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

HAPPY_PROFILE = """\
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


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {"name": "activate_implement", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _silent_message(text: str = "still thinking") -> AIMessage:
    return AIMessage(content=text, tool_calls=[])


def _test_targeted_message(call_id: str = "call_test_1") -> AIMessage:
    return AIMessage(
        content="running the existing suite",
        tool_calls=[
            {
                "name": "test_targeted",
                "args": {"path": "tests/test_greet.py"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _write_and_test_message(
    write_id: str = "call_write_1", test_id: str = "call_test_1"
) -> AIMessage:
    return AIMessage(
        content="writing then testing",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "greeting.txt", "content": "hello\n"},
                "id": write_id,
                "type": "tool_call",
            },
            {
                "name": "test_targeted",
                "args": {"path": "tests/test_greet.py"},
                "id": test_id,
                "type": "tool_call",
            },
        ],
    )


def _continuations_in(messages: list[BaseMessage]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        if is_silence_continuation(message):
            content = message.content
            texts.append(content if isinstance(content, str) else str(content))
    return texts


def _last_invocation_continuations(model: ScriptedModel) -> list[str]:
    assert model.invocations, "the model was never called"
    return _continuations_in(list(model.invocations[-1]))


def _make_workspace(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    workdir = tmp_path / f"{name}-work"
    run_git(["clone", "-q", str(bare), str(workdir)], cwd=tmp_path)
    return workdir


def test_l3_imp_24_silence_after_unrelated_passing_test_is_undeclared(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-24: explore, run an unrelated passing test, then go silent.

    Exactly two continuations are sent — each naming the uncovered criteria
    and the absent diff — and the node ends ``failed`` / ``undeclared`` in the
    Run Ledger. It never becomes ``verified completion`` or
    ``already satisfied``.
    """
    workspace = _make_workspace(tmp_path, "pilot")
    ledger = RunLedger(tmp_path / "pilot.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _test_targeted_message(),
                _silent_message("still thinking"),
                _silent_message("still thinking"),
                _silent_message("still silent"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert result.outcome == "failed"
        assert result.reason == "undeclared"
        assert result.reason != "verified-completion"
        assert result.reason != "already-satisfied"
        assert result.outcome != "delivered"
        recorded = ledger.get_outcome("#7/1")
        assert recorded is not None
        assert recorded.outcome == "failed"
        assert recorded.reason == "undeclared"
        continuations = _last_invocation_continuations(model)
        assert len(continuations) == MAX_SILENCE_CONTINUATIONS == 2
        for text in continuations:
            assert SILENCE_CONTINUATION_MARKER in text
            assert "AC-1" in text
            assert "absent" in text
        bodies = [
            args[1] for name, args in tracker.calls if name == "post_terminal_comment"
        ]
        assert len(bodies) == 1
        assert "PASS" not in str(bodies[0])
    finally:
        ledger.close()


def test_silence_continuations_are_deterministic_and_runtime_composed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """The continuation bytes are a pure function of state: no model call."""
    text_one = compose_silence_continuation(
        sequence_number=1,
        acceptance_criteria=(("AC-1", "`greet` returns a greeting"),),
        uncovered_ids=("AC-1",),
        has_diff=False,
        executed_tests=(("pkg::test_ok", True),),
    )
    text_two = compose_silence_continuation(
        sequence_number=1,
        acceptance_criteria=(("AC-1", "`greet` returns a greeting"),),
        uncovered_ids=("AC-1",),
        has_diff=False,
        executed_tests=(("pkg::test_ok", True),),
    )
    assert text_one == text_two
    assert SILENCE_CONTINUATION_MARKER in text_one
    assert "AC-1" in text_one

    workspace = _make_workspace(tmp_path, "deterministic")
    ledger = RunLedger(tmp_path / "deterministic.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _silent_message(),
                _silent_message(),
                _silent_message(),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert result.reason == "undeclared"
        last = list(model.invocations[-1])
        continuations = [m for m in last if is_silence_continuation(m)]
        assert len(continuations) == 2
        # Runtime-composed: the model only ever produces AIMessage, so every
        # continuation handed to it is a HumanMessage carrying the marker.
        assert all(isinstance(m, HumanMessage) for m in continuations)
        assert all(
            SILENCE_CONTINUATION_MARKER in (
                m.content if isinstance(m.content, str) else str(m.content)
            )
            for m in continuations
        )
    finally:
        ledger.close()


def test_silence_budget_is_not_reset_by_progress(
    tmp_path: Path, skills_dir: Path
) -> None:
    """A tool-calling turn between two silences buys no extra continuation."""
    workspace = _make_workspace(tmp_path, "budget")
    ledger = RunLedger(tmp_path / "budget.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _silent_message("silence before progress"),
                _write_and_test_message(),
                _silent_message("silence after progress"),
                _silent_message("silence again"),
                _silent_message("silence once more"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert result.outcome == "failed"
        assert result.reason == "undeclared"
        recorded = ledger.get_outcome("#7/1")
        assert recorded is not None
        assert (recorded.outcome, recorded.reason) == ("failed", "undeclared")
        continuations = _last_invocation_continuations(model)
        assert len(continuations) == 2
        assert all("AC-1" in text for text in continuations)
        # The first continuation predates the write (absent diff); the second
        # follows it (present diff) — yet the budget still ran out.
        assert "absent" in continuations[0]
        assert "present" in continuations[1]
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert status != ""
    finally:
        ledger.close()


def test_l3_imp_25_only_report_completion_ceiling_reserve_or_failure_exit_loop() -> None:
    """L3-IMP-25: structurally, silence never reaches an outcome.

    A response with no tool calls has no edge to an Implementation Outcome.
    The only edges out of the tool loop are ``report_completion``, a ceiling,
    the verification reserve and a classified failure.
    """
    assert step_module.__file__ is not None
    source = Path(step_module.__file__).read_text(encoding="utf-8")
    # The silence node exists and the model routes to exactly three places.
    assert 'add_node("silence"' in source
    assert '"tools": "tools", "silence": "silence", "exit": END' in source
    assert 'Literal["tools", "silence", "exit"]' in source
    # Silence never yields an outcome on its own: the step subgraph produces
    # no Implementation Outcome and no Terminal Outcome wording of its own.
    for wording in (
        "verified completion",
        "already satisfied",
        "verified-completion",
        "already-satisfied",
        "decide_implementation_outcome",
        "DELIVERED",
        "record_terminal_outcome",
    ):
        assert wording not in source, wording
    # The only exits out of the tool loop are named in the routing contract.
    for exit_name in (
        "report_completion",
        "ceiling",
        "reserve",
        "failure",
    ):
        assert exit_name in source, exit_name


def test_l3_imp_26_criteria_enumerated_model_free_and_schema_is_exact(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-26: enumeration needs no model; the schema answers every held id.

    ``report_completion`` cannot express a claim answering fewer criteria
    than the Attempt holds, and a Target Issue naming none ends
    ``failed`` / ``missing-readiness-fact`` before any model call.
    """
    body = (
        "## Acceptance criteria\n"
        "- [ ] `greet` returns a greeting\n"
        "- [ ] `farewell` says goodbye\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert tuple(c.id for c in criteria) == ("AC-1", "AC-2")
    ids = tuple(c.id for c in criteria)
    schema = report_completion_schema(ids)
    assert set(schema["properties"]) == {"AC-1", "AC-2"}
    assert set(schema["required"]) == {"AC-1", "AC-2"}
    assert schema["additionalProperties"] is False
    # A claim answering fewer criteria cannot validate: the missing id has
    # no default and extra ids are refused.
    assert "AC-2" in schema["required"]
    claim = normalize_claim({"AC-1": ("pkg::test_ok",)}, ids)
    assert claim["AC-2"] is None
    outcome, reason = decide_implementation_outcome(
        claim, CriterionEvidence(), has_diff=True
    )
    assert outcome == "completion not established"
    assert reason == "criteria-uncovered"

    # A Target Issue naming no criteria ends before any model call.
    workspace = _make_workspace(tmp_path, "nocriteria")
    ledger = RunLedger(tmp_path / "nocriteria.sqlite3")
    try:
        tracker = FakeTracker(
            issue=TrackedIssue(
                number=7,
                title="Add a greeting",
                body="No criteria section here, only prose about `greet`.",
            )
        )
        model = ScriptedModel([])
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert result.outcome == "failed"
        assert result.reason.startswith("missing-readiness-fact:")
        assert model.invocations == []
        recorded = ledger.get_outcome("#7/1")
        assert recorded is not None
        assert recorded.outcome == "failed"
        assert recorded.reason.startswith("missing-readiness-fact:")
    finally:
        ledger.close()
