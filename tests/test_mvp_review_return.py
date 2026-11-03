"""M3 review return of a claim: once, naming the missing review.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 claim check 2 (a
claim made with a candidate diff and no completed review in the Attempt is
returned once, naming the missing review; made again without a review, it
is accepted and the Publication Gate decides) and §10 (a completed review is
one where both seats made a Review Report; the review status is recorded
per Attempt). The candidate diff is measured against the Base Revision, so
a review snapshot that committed the tree does not hide it.

MVP row ids: P-DEL-3, C-REV-1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from conftest import init_origin_repo, run_git
from coding_agent.runtime.completion import (
    MAX_CLAIM_RETURNS,
    ClaimRecord,
    CriterionEvidence,
    ReportCompletionTool,
    build_report_completion_tool,
)
from coding_agent.runtime.git import tree_differs_from_base
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.review_snapshot import CompletedReview, ReviewSnapshotState
from coding_agent.validate.junit import parse_junit_xml
from coding_agent.ledger.store import RunLedger
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

PASSING_JUNIT = (
    '<testsuite name="suite" tests="1">'
    '<testcase classname="pkg" name="test_ok"/>'
    "</testsuite>"
)

BACKED = {"AC-1": ["pkg::test_ok"]}
UNBACKED = {"AC-1": ["pkg::never_ran"]}

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

PROFILE = """\
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


def _evidence() -> CriterionEvidence:
    evidence = CriterionEvidence()
    evidence.record_junit(parse_junit_xml(PASSING_JUNIT))
    return evidence


def _tool(
    evidence: CriterionEvidence,
    gate: ReviewSnapshotState,
    *,
    has_diff: bool = True,
    record: ClaimRecord | None = None,
) -> tuple[ReportCompletionTool, ClaimRecord]:
    bound = record if record is not None else ClaimRecord()
    tool = build_report_completion_tool(
        ("AC-1",),
        {},
        criterion_evidence=evidence,
        claim_record=bound,
        review_gate=gate,
        has_candidate_diff=lambda: has_diff,
    )
    return tool, bound


def _completed_review(sha: str, *, spec: str | None = "spec: no findings") -> CompletedReview:
    return CompletedReview(
        ordinal=1,
        commit_sha=sha,
        branch_name="agent/7/1-x",
        standards_findings="standards: no findings",
        spec_findings=spec,
    )


def test_claim_with_diff_and_no_review_is_returned_once_then_accepted() -> None:
    """P-DEL-3: returned once naming the missing review; the repeat is accepted unmet."""
    gate = ReviewSnapshotState()
    tool, record = _tool(_evidence(), gate)

    first = str(tool.invoke(BACKED))
    assert "returned" in first
    assert "no completed review" in first
    assert "activate_code_review" in first
    assert "not-invoked" in first
    assert record.accepted_raw is None
    # A review return spends none of the two-return evidence budget.
    assert record.returns == 0

    second = str(tool.invoke(BACKED))
    assert second == "completion recorded"
    assert record.accepted_raw == BACKED
    assert record.force_accepted is False
    assert record.review_status == "not-invoked"
    assert gate.review_gate_met is False


def test_claim_without_a_candidate_diff_is_not_returned_for_review() -> None:
    """§9: the review return applies only to a claim made with a candidate diff."""
    gate = ReviewSnapshotState()
    tool, record = _tool(_evidence(), gate, has_diff=False)
    assert str(tool.invoke(BACKED)) == "completion recorded"
    assert gate.claim_review_returned is False
    assert record.review_status == "not-invoked"


@pytest.mark.parametrize(
    ("snapshots", "expected_status"),
    [(["sha-1"], "fresh"), (["sha-1", "sha-2"], "stale")],
)
def test_completed_review_meets_the_gate_and_the_claim_is_accepted_at_once(
    snapshots: list[str], expected_status: str
) -> None:
    """§10: a ``fresh`` or ``stale`` completed review meets the gate: no return."""
    gate = ReviewSnapshotState(invocations=len(snapshots), snapshots=list(snapshots))
    gate.record_completed_review(_completed_review("sha-1"))
    tool, record = _tool(_evidence(), gate)
    assert str(tool.invoke(BACKED)) == "completion recorded"
    assert record.review_status == expected_status


def test_invocation_with_one_seat_missing_does_not_meet_the_gate() -> None:
    """G-REV-1: an invocation one seat did not report on is no completed review."""
    gate = ReviewSnapshotState(invocations=1, snapshots=["sha-1"])
    gate.record_completed_review(_completed_review("sha-1", spec=None))
    tool, record = _tool(_evidence(), gate)
    first = str(tool.invoke(BACKED))
    assert "no completed review" in first
    assert "review status: incomplete" in first
    assert record.accepted_raw is None
    assert str(tool.invoke(BACKED)) == "completion recorded"
    assert record.review_status == "incomplete"


def test_evidence_returns_come_first_and_the_review_return_is_separate() -> None:
    """§9: an unbacked claim gets the evidence return; the review return follows once."""
    gate = ReviewSnapshotState()
    tool, record = _tool(_evidence(), gate)

    evidence_return = str(tool.invoke(UNBACKED))
    assert "not backed by Criterion Evidence" in evidence_return
    assert gate.claim_review_returned is False
    assert record.returns == 1

    review_return = str(tool.invoke(BACKED))
    assert "no completed review" in review_return
    assert record.returns == 1

    assert str(tool.invoke(BACKED)) == "completion recorded"
    assert record.force_accepted is False


def test_force_accepted_claim_is_not_returned_for_review() -> None:
    """§9: the third unbacked call is force-accepted, review or not."""
    gate = ReviewSnapshotState()
    tool, record = _tool(_evidence(), gate)
    for _ in range(MAX_CLAIM_RETURNS):
        assert "not backed" in str(tool.invoke(UNBACKED))
    forced = str(tool.invoke(UNBACKED))
    assert "force-accepted" in forced
    assert record.force_accepted is True
    assert record.review_status == "not-invoked"
    assert gate.claim_review_returned is False


def test_malformed_returns_come_first_and_a_forced_malformed_claim_skips_review() -> None:
    """§9: a malformed claim gets the shared-budget return, never the review return.

    Once the budget runs out the malformed claim is force-accepted, not
    returned for review, and the review status at acceptance is recorded.
    """
    gate = ReviewSnapshotState()
    tool, record = _tool(_evidence(), gate)
    malformed = {"AC-1": '["pkg::test_ok"]'}
    for _ in range(MAX_CLAIM_RETURNS):
        assert "malformed" in str(tool.invoke(malformed))
        assert gate.claim_review_returned is False
    forced = str(tool.invoke(malformed))
    assert "force-accepted" in forced
    assert record.force_accepted is True
    assert record.review_status == "not-invoked"
    assert gate.claim_review_returned is False


def test_review_return_happens_once_per_attempt_across_fresh_claim_records() -> None:
    """P-DEL-3: a handoff's fresh ``ClaimRecord`` does not re-arm the review return."""
    evidence = _evidence()
    gate = ReviewSnapshotState()
    before_handoff, _ = _tool(evidence, gate)
    assert "no completed review" in str(before_handoff.invoke(BACKED))

    after_handoff, record = _tool(evidence, gate, record=ClaimRecord())
    assert str(after_handoff.invoke(BACKED)) == "completion recorded"
    assert record.review_status == "not-invoked"


def test_review_gate_without_a_diff_check_is_refused() -> None:
    with pytest.raises(ValueError, match="has_candidate_diff"):
        ReportCompletionTool(
            ("AC-1",),
            {},
            criterion_evidence=_evidence(),
            claim_record=ClaimRecord(),
            review_gate=ReviewSnapshotState(),
        )


def test_candidate_diff_is_measured_against_the_base_revision(tmp_path: Path) -> None:
    """§9, §10: a commit on top of the Base Revision is still the candidate diff."""
    repo = tmp_path / "repo"
    base = init_origin_repo(repo)
    assert tree_differs_from_base(repo, base) is False

    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    assert tree_differs_from_base(repo, base) is True

    run_git(["add", "."], repo)
    run_git(["commit", "-q", "-m", "review snapshot"], repo)
    assert run_git(["status", "--porcelain"], repo) == ""
    assert tree_differs_from_base(repo, base) is True
    # Without a Base Revision only uncommitted changes count.
    assert tree_differs_from_base(repo, None) is False

    run_git(["rm", "-q", "untracked.txt"], repo)
    run_git(["commit", "-q", "-m", "revert"], repo)
    assert tree_differs_from_base(repo, base) is False


def test_unreadable_base_revision_assumes_a_candidate_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_origin_repo(repo)
    assert tree_differs_from_base(repo, "0" * 40) is True


def test_attempt_returns_the_unreviewed_claim_once_and_then_verifies(
    tmp_path: Path, skills_dir: Path
) -> None:
    """P-DEL-3 end to end: one return naming the review, then ``verified completion``."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    (src / "docs" / "agents").mkdir(parents=True)
    (src / "docs" / "agents" / "project-profile.yml").write_text(PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    workspace = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workspace)], cwd=tmp_path)

    def completion(call_id: str) -> AIMessage:
        return AIMessage(
            content="done",
            tool_calls=[
                {"name": "report_completion", "args": BACKED, "id": call_id, "type": "tool_call"}
            ],
        )

    model = ScriptedModel(
        [
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
            completion("call_completion_1"),
            completion("call_completion_2"),
        ]
    )
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
    finally:
        ledger.close()

    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert len(model.invocations) == 3
    returned = [
        message
        for message in model.invocations[2]
        if isinstance(message, ToolMessage) and message.tool_call_id == "call_completion_1"
    ]
    assert len(returned) == 1
    assert "no completed review" in str(returned[0].content)
    assert "activate_code_review" in str(returned[0].content)
