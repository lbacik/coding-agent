"""M2 evidence returns: an unbacked claim is returned, never inferred.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (a claim naming a
test identifier that is absent from Criterion Evidence, that never
executed, or whose latest recorded run did not pass is returned as a tool
result naming every failing entry; a return is not a progress event; after
two returns the third ``report_completion`` call is force-accepted —
``implemented but unverified`` with a candidate diff,
``completion not established`` / ``claims-unverified`` without one; an
explicit ``uncovered`` criterion is accepted, never returned) and §1 (the
model's toolset holds no shell tool and file tools cannot reach the
runtime-owned ``{evidence_dir}``: a JUnit file the model writes through its
own tools can never become Criterion Evidence).

MVP row ids: C-IMP-1, L3-IMP-28, L3-IMP-29.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.runtime.completion import (
    MAX_CLAIM_RETURNS,
    ClaimRecord,
    CriterionEvidence,
    ReportCompletionTool,
    build_report_completion_tool,
    decide_implementation_outcome,
    find_evidence_failures,
    find_malformed_claim_entries,
    normalize_claim,
)
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.graph.tools import build_implement_step_tools
from coding_agent.validate.junit import parse_junit_xml
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

PASSING_JUNIT = (
    '<testsuite name="suite" tests="1">'
    '<testcase classname="pkg" name="test_ok"/>'
    "</testsuite>"
)

FAILING_JUNIT = (
    '<testsuite name="suite" tests="1">'
    '<testcase classname="pkg" name="test_flaky">'
    '<failure message="boom"/>'
    "</testcase>"
    "</testsuite>"
)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
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


def _completion_message(
    covering: object = ("pkg::test_ok",), call_id: str = "call_completion_1"
) -> AIMessage:
    args: dict[str, object] = {"AC-1": covering}
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {"name": "report_completion", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {"name": "activate_implement", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _write_message(call_id: str = "call_write_1") -> AIMessage:
    return AIMessage(
        content="working",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "greeting.txt", "content": "hello\n"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _bound_tool(
    criteria_ids: tuple[str, ...] = ("AC-1",),
    evidence: CriterionEvidence | None = None,
) -> tuple[ReportCompletionTool, ClaimRecord, CriterionEvidence]:
    """A ``report_completion`` tool bound to fresh evidence and a record."""
    store = evidence if evidence is not None else CriterionEvidence()
    record = ClaimRecord()
    sink: dict[str, object] = {}
    tool = build_report_completion_tool(
        criteria_ids, sink, criterion_evidence=store, claim_record=record
    )
    return tool, record, store


def test_absent_test_is_returned_naming_the_entry() -> None:
    """C-IMP-1: a claim naming a never-recorded test is returned, not recorded."""
    tool, record, _evidence = _bound_tool()
    result = str(tool.invoke({"AC-1": ["pkg::never_ran"]}))
    assert "pkg::never_ran" in result
    assert "AC-1" in result
    assert "absent from Criterion Evidence" in result
    assert "completion recorded" not in result
    assert record.returns == 1
    assert record.accepted_raw is None
    assert record.force_accepted is False


def test_latest_failed_run_is_returned_naming_the_entry() -> None:
    """C-IMP-1: a test whose latest recorded run failed is returned by name."""
    evidence = CriterionEvidence()
    evidence.record_junit(parse_junit_xml(PASSING_JUNIT))
    evidence.record_junit(parse_junit_xml(FAILING_JUNIT))
    assert evidence.latest_result("pkg::test_flaky") is False
    tool, record, _store = _bound_tool(evidence=evidence)
    result = str(tool.invoke({"AC-1": ["pkg::test_flaky"]}))
    assert "pkg::test_flaky" in result
    assert "AC-1" in result
    assert "latest recorded run did not pass" in result
    assert record.returns == 1
    assert record.accepted_raw is None


def test_return_names_every_failing_entry_and_skips_uncovered() -> None:
    """C-IMP-1: the return names each failing entry; ``uncovered`` is accepted."""
    evidence = CriterionEvidence()
    evidence.record_junit(parse_junit_xml(PASSING_JUNIT))
    claim = normalize_claim(
        {"AC-1": ["pkg::test_ok", "pkg::never_ran"], "AC-2": "uncovered"},
        ("AC-1", "AC-2"),
    )
    failures = find_evidence_failures(claim, evidence)
    assert [(failure.criterion_id, failure.test_id) for failure in failures] == [
        ("AC-1", "pkg::never_ran")
    ]
    tool, record, _store = _bound_tool(
        criteria_ids=("AC-1", "AC-2"), evidence=evidence
    )
    result = str(
        tool.invoke({"AC-1": ["pkg::test_ok", "pkg::never_ran"], "AC-2": "uncovered"})
    )
    assert "pkg::never_ran" in result
    assert "AC-1" in result
    assert record.returns == 1
    assert record.accepted_raw is None


def test_third_call_is_force_accepted_after_two_returns() -> None:
    """C-IMP-1: after two returns the third call is force-accepted."""
    tool, record, _evidence = _bound_tool()
    first = str(tool.invoke({"AC-1": ["pkg::never_ran"]}))
    second = str(tool.invoke({"AC-1": ["pkg::never_ran"]}))
    assert "pkg::never_ran" in first
    assert "pkg::never_ran" in second
    assert record.returns == 2
    assert record.accepted_raw is None
    third = str(tool.invoke({"AC-1": ["pkg::never_ran"]}))
    assert "force-accepted" in third
    assert record.force_accepted is True
    assert record.accepted_raw == {"AC-1": ["pkg::never_ran"]}
    assert record.returns == 3


def test_returns_are_not_progress_events_and_third_call_ends_claims_unverified(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """C-IMP-1 end to end: returns name entries, record no progress event.

    The unbacked claim is returned twice and the third call is
    force-accepted; with no candidate diff the Attempt ends ``failed`` /
    ``claims-unverified``. The ledger holds the baseline and terminal
    rows only — a return never records a ``progress_event``.
    """
    tracker = FakeTracker()
    model = ScriptedModel(
        [
            _completion_message(["pkg::never_ran"], "call_completion_1"),
            _completion_message(["pkg::never_ran"], "call_completion_2"),
            _completion_message(["pkg::never_ran"], "call_completion_3"),
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
    assert result.reason == "claims-unverified"
    # Two returns plus the force-accepted call: three model turns.
    assert len(model.invocations) == 3
    # The first return reached the model as a tool result naming the entry.
    first_return = model.invocations[1][-1]
    assert "pkg::never_ran" in str(getattr(first_return, "content", ""))
    kinds = {record.kind for record in ledger.records("#7/1")}
    assert "progress_event" not in kinds


def test_force_accepted_claim_with_a_diff_is_implemented_but_unverified(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """C-IMP-1 end to end: a forced claim with a diff keeps the diff."""
    tracker = FakeTracker()
    model = ScriptedModel(
        [
            _write_message(),
            _completion_message(["pkg::never_ran"], "call_completion_1"),
            _completion_message(["pkg::never_ran"], "call_completion_2"),
            _completion_message(["pkg::never_ran"], "call_completion_3"),
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
    assert result.reason == "unverified"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status != "", "the candidate diff must survive force-accept"


def test_backed_claim_is_verified_on_its_first_call_with_no_return(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """§9: a claim the evidence backs earns ``verified completion`` at once."""
    tracker = FakeTracker()
    model = ScriptedModel(
        [
            _activation_message(),
            AIMessage(
                content="working",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "greeting.txt", "content": "hello\n"},
                        "id": "call_write_1",
                        "type": "tool_call",
                    },
                    {
                        "name": "test_targeted",
                        "args": {"path": "tests/test_greet.py"},
                        "id": "call_test_1",
                        "type": "tool_call",
                    },
                ],
            ),
            _completion_message(["pkg::test_ok"]),
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
    assert result.outcome == "delivered"
    assert result.reason == "verified-completion"
    # No return happened: exactly the activation, work and claim turns.
    assert len(model.invocations) == 3


def test_bound_toolset_has_no_shell_and_reaches_no_evidence_dir(
    workspace: Path, skills_dir: Path, tmp_path: Path
) -> None:
    """L3-IMP-28, structurally: no shell tool, and file tools stop at the root.

    The model's JUnit write can only go through its workspace-scoped file
    tools, which refuse any path outside the workspace — so nothing the
    model writes can land in the runtime-owned ``{evidence_dir}`` or become
    Criterion Evidence. Asserted on the bound toolset and the directory
    layout, never on the model's account.
    """
    from coding_agent.profile.parser import parse_profile_yaml
    from coding_agent.profile.schema import ProjectProfile

    parsed = parse_profile_yaml(HAPPY_PROFILE)
    assert isinstance(parsed, ProjectProfile)
    evidence_dir = tmp_path / "evidence"
    evidence = CriterionEvidence()
    tools = build_implement_step_tools(
        workspace,
        skills_dir=skills_dir,
        profile=parsed,
        evidence_dir=evidence_dir,
        criterion_evidence=evidence,
        criteria_ids=("AC-1",),
    )
    names = {tool.name for tool in tools}
    assert "test_targeted" in names
    assert "report_completion" in names
    for name in names:
        lowered = name.lower()
        assert "shell" not in lowered
        assert "exec" not in lowered
        assert "bash" not in lowered
        assert "terminal" not in lowered
    by_name = {tool.name: tool for tool in tools}
    write_tool = by_name["write_file"]
    target = evidence_dir / "test_targeted.xml"
    write_result = str(
        write_tool.invoke(
            {
                "path": str(target),
                "content": "<testsuite><testcase classname='pkg' name='fake'/></testsuite>",
            }
        )
    )
    assert "outside the workspace" in write_result
    assert not target.exists()
    read_result = str(by_name["read_file"].invoke({"path": str(target)}))
    assert "outside the workspace" in read_result
    # Nothing the model wrote became Criterion Evidence.
    assert evidence.history() == ()
    assert evidence.executed_ids() == frozenset()


def test_evidence_dir_inside_the_workspace_is_refused(
    workspace: Path, skills_dir: Path
) -> None:
    """L3-IMP-28: the runtime-owned ``{evidence_dir}`` must live outside the tree."""
    from coding_agent.profile.parser import parse_profile_yaml
    from coding_agent.profile.schema import ProjectProfile

    parsed = parse_profile_yaml(HAPPY_PROFILE)
    assert isinstance(parsed, ProjectProfile)
    with pytest.raises(ValueError, match="outside the workspace"):
        build_implement_step_tools(
            workspace,
            skills_dir=skills_dir,
            profile=parsed,
            evidence_dir=workspace / "evidence",
            criterion_evidence=CriterionEvidence(),
            criteria_ids=("AC-1",),
        )


def test_uncovered_claim_is_accepted_and_keeps_the_diff(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """L3-IMP-29: ``uncovered`` is accepted, never returned; the diff stays."""
    tracker = FakeTracker()
    model = ScriptedModel(
        [
            _write_message(),
            _completion_message("uncovered", "call_completion_1"),
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
    assert result.reason == "criteria-uncovered"
    # Accepted on the first call: no return turn happened.
    assert len(model.invocations) == 2
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status != "", "the candidate diff must stay in the workspace"


def test_backed_claim_is_accepted_on_the_first_call() -> None:
    """C-IMP-1: passing Criterion Evidence accepts the claim with no return."""
    evidence = CriterionEvidence()
    evidence.record_junit(parse_junit_xml(PASSING_JUNIT))
    tool, record, _store = _bound_tool(evidence=evidence)
    result = str(tool.invoke({"AC-1": ["pkg::test_ok"]}))
    assert result == "completion recorded"
    assert record.returns == 0
    assert record.accepted_raw == {"AC-1": ["pkg::test_ok"]}
    assert record.force_accepted is False
    assert MAX_CLAIM_RETURNS == 2


def test_json_encoded_string_claim_is_returned_as_malformed_not_uncovered() -> None:
    """§9: a JSON-encoded string is malformed, never ``uncovered``.

    Live-run shape (lbacik/coding-agent-sandbox#11): the model sent each
    covering list as a JSON-encoded string. It must be returned with the
    criterion named and the expected shape stated, not silently accepted as
    ``uncovered`` — even though the named test did pass through
    ``test_targeted``.
    """
    evidence = CriterionEvidence()
    evidence.record_junit(parse_junit_xml(PASSING_JUNIT))
    assert evidence.latest_result("pkg::test_ok") is True
    tool, record, _store = _bound_tool(evidence=evidence)
    encoded = '["pkg::test_ok"]'
    result = str(tool.invoke({"AC-1": encoded}))
    assert "AC-1" in result
    assert "non-empty list" in result
    assert "uncovered" in result
    assert "completion recorded" not in result
    assert record.returns == 1
    assert record.accepted_raw is None
    assert record.force_accepted is False
    malformed = find_malformed_claim_entries({"AC-1": encoded}, ("AC-1",))
    assert [entry.criterion_id for entry in malformed] == ["AC-1"]


def test_normalize_claim_keeps_none_only_for_explicit_uncovered() -> None:
    """§9: only an explicit ``uncovered`` (or missing entry) normalizes to None."""
    assert normalize_claim({"AC-1": "uncovered"}, ("AC-1",)) == {"AC-1": None}
    assert normalize_claim({}, ("AC-1",)) == {"AC-1": None}
    assert normalize_claim({"AC-1": ["pkg::test_ok"]}, ("AC-1",)) == {
        "AC-1": ("pkg::test_ok",)
    }
    malformed_claim = normalize_claim({"AC-1": '["pkg::test_ok"]'}, ("AC-1",))
    assert malformed_claim["AC-1"] is not None
    assert find_malformed_claim_entries(
        {"AC-1": '["pkg::test_ok"]'}, ("AC-1",)
    ) != []
    assert find_malformed_claim_entries({"AC-1": "uncovered"}, ("AC-1",)) == []
    assert find_malformed_claim_entries(
        {"AC-1": ["pkg::test_ok"]}, ("AC-1",)
    ) == []


def test_malformed_returns_share_the_two_return_evidence_budget() -> None:
    """§9: malformed-claim returns count against the same two-return budget."""
    tool, record, _store = _bound_tool()
    first = str(tool.invoke({"AC-1": '["pkg::never_ran"]'}))
    assert "AC-1" in first
    assert record.returns == 1
    assert record.accepted_raw is None
    second = str(tool.invoke({"AC-1": ["pkg::never_ran"]}))
    assert "pkg::never_ran" in second
    assert record.returns == 2
    assert record.accepted_raw is None
    third = str(tool.invoke({"AC-1": '["pkg::never_ran"]'}))
    assert "force-accepted" in third
    assert record.force_accepted is True
    assert record.returns == 3


def test_malformed_claim_is_never_verified_at_decision_time() -> None:
    """§9: a malformed value reaching decision counts as uncovered, never verified."""
    evidence = CriterionEvidence()
    evidence.record_junit(parse_junit_xml(PASSING_JUNIT))
    claim = normalize_claim({"AC-1": '["pkg::test_ok"]'}, ("AC-1",))
    outcome, reason = decide_implementation_outcome(claim, evidence, has_diff=True)
    assert outcome == "completion not established"
    assert reason == "criteria-uncovered"
