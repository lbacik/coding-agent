"""M3 review subgraph: two isolated reviewer conversations.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (after the
review-snapshot push the review subgraph fans out the Standards and Spec
reviewer conversations, each with its own message list and read-only tool
set; the Reviewer Brief is concatenation of a Seat Assignment, the seat's
own Role Block cut byte-identical from the installed ``code-review``
``SKILL.md``, and the concrete inputs; a seat completes only through
``report_review``; the tool result carries both Review Reports or names the
seat that made none; both seats start only after the receipt is recorded;
reviewer usage counts against the Attempt-wide meters with no reserved
review budget).

MVP row ids: L3-REV-1, L3-REV-2, G-REV-1, G-REV-2, P-DEL-4, W-STP-6, C-REV-1,
P-DEL-3.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, UsageMetadata

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import EffectRecord, RunLedger
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.git import (
    Workspace,
    checkout_workspace,
    ensure_mirror,
)
from coding_agent.runtime.graph import review as review_module
from coding_agent.runtime.completion import REPORT_STEP_DONE
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.graph.step import WorkflowStep
from coding_agent.runtime.graph.review import (
    REVIEW_DIFF_INLINE_LIMIT,
    SEATS,
    SPEC_SEAT,
    STANDARDS_SEAT,
    SeatRecord,
    build_reviewer_tools,
    compose_reviewer_brief,
    prepare_review_change,
    review_diff_artifact_id,
    review_tool_result_text,
    run_review_invocation,
    run_review_seat,
    seat_assignment,
)
from coding_agent.runtime.result_capping import InMemoryArtifactStore
from coding_agent.runtime.graph.tools import _with_review_snapshot
from coding_agent.runtime.review_snapshot import (
    REVIEW_STATUS_KIND,
    review_disclosure,
    MAX_REVIEW_INVOCATIONS,
    REVIEW_SNAPSHOT_KIND_PREFIX,
    CompletedReview,
    ReviewSnapshotState,
)
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.skillbundle.activation import build_activation_tools
from coding_agent.skillbundle.verify import DEFAULT_ROLE_BLOCK_SPEC, extract_role_blocks
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import REVIEW_SKILL_TEXT

IDENTITY = Identity(account_id=999, login="coding-agent")

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


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    *,
    content: str = "",
    calls: list[dict[str, object]] | None = None,
    usage: UsageMetadata | None = None,
) -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls or []), usage_metadata=usage)


def _report(seat: str, call_id: str, findings: str) -> AIMessage:
    return _ai(
        content=f"{seat} reporting",
        calls=[_call("report_review", {"findings": findings}, call_id)],
    )


@pytest.fixture
def review_skills_dir(skills_dir: Path) -> Path:
    """The synthetic bundle with an anchored ``code-review`` skill.

    ``write_skill_bundle`` covers every bundle member; this overwrites only
    ``code-review/SKILL.md`` with text carrying the production anchors, so
    Role Block extraction cuts real slices. Never the host's skills.
    """
    (skills_dir / "code-review" / "SKILL.md").write_text(REVIEW_SKILL_TEXT, encoding="utf-8")
    return skills_dir


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    (src / "CODING_STANDARDS.md").write_text("# Standards\n", encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add standards"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def checked_out_workspace(tmp_path: Path, bare_remote: Path) -> Workspace:
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(bare_remote))
    return checkout_workspace(mirror, tmp_path / "workspace")


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _wrapped_tool(
    *,
    skills_dir: Path,
    ledger: RunLedger,
    review_workspace: Workspace,
    state: ReviewSnapshotState,
    model: ScriptedModel | None = None,
    remote_url: str | None = None,
) -> Any:
    activation_tools = build_activation_tools(skills_dir)
    (wrapped,) = tuple(
        tool
        for tool in _with_review_snapshot(
            activation_tools,
            skills_dir=skills_dir,
            ledger=ledger,
            review_workspace=review_workspace,
            attempt_id="#34/2",
            issue_number=34,
            attempt_number=2,
            issue_title="Fix the loop!",
            identity=IDENTITY,
            remote_url=remote_url,
            state=state,
            model=model,
            review_issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
            review_base_revision="base-rev",
        )
        if tool.name == "activate_code_review"
    )
    return wrapped


def test_seat_assignment_names_axis_and_exit_without_criteria() -> None:
    """G-REV-2: the assignment states the reviewer identity and the Report exit."""
    for seat in SEATS:
        assignment = seat_assignment(seat)
        assert seat in assignment
        assert "reviewer the Role Block" in assignment
        assert "Review Report" in assignment
        assert "report_review" in assignment
    forbidden = [
        "smell",
        "violation",
        "scope creep",
        "cite",
        "400",
        "hard violation",
        "judgement",
        "missing or partial",
        "looks wrong",
        "documented standard",
        "hunk",
    ]
    for seat in SEATS:
        lowered = seat_assignment(seat).lower()
        for word in forbidden:
            assert word not in lowered, f"Seat Assignment carries criteria: {word!r}"


def test_role_blocks_are_byte_identical_slices(review_skills_dir: Path) -> None:
    """L3-REV-2: each seat's Role Block is its own slice, byte-identical."""
    standards, spec = extract_role_blocks(review_skills_dir)
    text = (review_skills_dir / "code-review" / "SKILL.md").read_text(encoding="utf-8")
    anchors = DEFAULT_ROLE_BLOCK_SPEC.anchors
    positions = [text.find(anchor) for anchor in anchors]
    assert all(position >= 0 for position in positions)
    assert standards == text[positions[0] : positions[1]] + text[positions[2] : positions[3]]
    assert spec == text[positions[3] : positions[4]]
    assert "STD-SECTION-MARKER" in standards
    assert "STD-BRIEF-MARKER" in standards
    assert "SPEC-BRIEF-MARKER" not in standards
    assert "SPEC-BRIEF-MARKER" in spec
    assert "STD-SECTION-MARKER" not in spec
    assert "STD-BRIEF-MARKER" not in spec


def test_brief_is_concatenation_of_assignment_block_and_inputs(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """L3-REV-2: the brief carries the seat's own block, not the whole skill."""
    standards, spec = extract_role_blocks(review_skills_dir)
    full = (review_skills_dir / "code-review" / "SKILL.md").read_text(encoding="utf-8")
    standards_brief = compose_reviewer_brief(
        seat=STANDARDS_SEAT,
        role_block=standards,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
        standards_sources=("CODING_STANDARDS.md",),
        commit_list="abc123 Fix the loop",
        diff="diff --git a/greeting.txt b/greeting.txt\n+hello\n",
    )
    spec_brief = compose_reviewer_brief(
        seat=SPEC_SEAT,
        role_block=spec,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
        standards_sources=("CODING_STANDARDS.md",),
        commit_list="abc123 Fix the loop",
        diff="diff --git a/greeting.txt b/greeting.txt\n+hello\n",
    )
    for brief, block, marker in (
        (standards_brief, standards, STANDARDS_SEAT),
        (spec_brief, spec, SPEC_SEAT),
    ):
        assert seat_assignment(marker) in brief
        assert block in brief
        assert "Concrete inputs." in brief
        assert "abc123" in brief
    assert "SPEC-BRIEF-MARKER" not in standards_brief
    assert "STD-SECTION-MARKER" not in spec_brief
    assert full not in standards_brief
    assert full not in spec_brief
    assert "Fix the loop" in spec_brief
    assert "CODING_STANDARDS.md" in standards_brief
    for brief in (standards_brief, spec_brief):
        assert "abc123 Fix the loop" in brief
        assert "+hello" in brief
        assert "review the change below, not the whole tree" in brief
        assert "for reference: git diff" not in brief


def test_reviewer_toolset_is_read_only_without_spawn_or_exit(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """L3-REV-1/L3-REV-2: read-only tools plus report_review; no spawn, no step exit."""
    tools = build_reviewer_tools(tmp_path, record=SeatRecord(seat=STANDARDS_SEAT))
    names = {tool.name for tool in tools}
    assert names == {
        "read_file",
        "list_directory",
        "find_files",
        "search_text",
        "read_lines",
        "read_result_slice",
        "report_review",
    }
    assert not any(name.startswith("activate_") for name in names)
    assert "report_completion" not in names
    assert "report_step" not in names
    assert not any("shell" in name or "write" in name or "edit" in name for name in names)
    assert not any(name.startswith("git_") or name == "git" for name in names)


def test_two_seats_run_with_separate_lists_and_bound_tools(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """L3-REV-1: exactly two conversations, separate lists, read-only bindings."""
    model = ScriptedModel(
        [
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=tmp_path,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert result.completed
    assert result.standards.findings == "standards findings"
    assert result.spec.findings == "spec findings"
    assert result.standards.messages is not result.spec.messages
    assert result.standards.messages[0] is not result.spec.messages[0]
    assert STANDARDS_SEAT in str(result.standards.messages[0].content)
    assert SPEC_SEAT in str(result.spec.messages[0].content)
    assert SPEC_SEAT not in str(result.standards.messages[0].content)
    assert STANDARDS_SEAT not in str(result.spec.messages[0].content)
    seat_bindings = [
        (choice, names)
        for choice, names in model.bind_history
        if "report_review" in names
    ]
    assert len(seat_bindings) == 2
    for choice, names in seat_bindings:
        assert choice is None
        assert set(names) == {
            "read_file",
            "list_directory",
            "find_files",
            "search_text",
            "read_lines",
            "read_result_slice",
            "report_review",
        }
    implementer_bindings = [
        names for choice, names in model.bind_history if "report_review" not in names
    ]
    assert implementer_bindings == []


def test_seat_without_report_is_not_completed(review_skills_dir: Path, tmp_path: Path) -> None:
    """G-REV-1: a silent seat makes no Review Report; the result names it."""
    model = ScriptedModel(
        [
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _ai(content="still thinking, no tool call"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=tmp_path,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert not result.completed
    assert result.missing_seats == (SPEC_SEAT,)
    text = review_tool_result_text(
        outcome_branch_name=result.branch_name,
        outcome_remote_sha=result.commit_sha,
        outcome_pushed=True,
        write_kind=f"{REVIEW_SNAPSHOT_KIND_PREFIX}1",
        result=result,
    )
    assert SPEC_SEAT in text
    assert "standards findings" in text
    assert "## Spec Review Report" not in text


def test_both_seats_missing_names_both_seats(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """G-REV-1: with neither seat reporting, the result names both seats."""
    model = ScriptedModel(
        [
            _ai(content="standards silent, no tool call"),
            _ai(content="spec silent, no tool call"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=tmp_path,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert not result.completed
    assert result.missing_seats == (STANDARDS_SEAT, SPEC_SEAT)
    text = review_tool_result_text(
        outcome_branch_name=result.branch_name,
        outcome_remote_sha=result.commit_sha,
        outcome_pushed=True,
        write_kind=f"{REVIEW_SNAPSHOT_KIND_PREFIX}1",
        result=result,
    )
    assert "Standards, Spec seats" in text
    assert "## Standards Review Report" not in text
    assert "## Spec Review Report" not in text


def test_blank_report_is_refused_and_seat_stays_open(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """G-REV-1: a blank report_review records nothing; a later report completes."""
    model = ScriptedModel(
        [
            _ai(
                content="empty",
                calls=[_call("report_review", {"findings": "  "}, "seat-std-blank")],
            ),
            _report(STANDARDS_SEAT, "seat-std-1", "no findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=tmp_path,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert result.completed
    assert result.standards.findings == "no findings"


def test_seat_reads_then_reports(review_skills_dir: Path, tmp_path: Path) -> None:
    """L3-REV-1: a seat may read the tree before reporting, on its own list."""
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    model = ScriptedModel(
        [
            _ai(
                content="reading first",
                calls=[_call("read_file", {"path": "notes.txt"}, "seat-std-read")],
            ),
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=tmp_path,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert result.completed
    assert any("hello" in str(message.content) for message in result.standards.messages)


def test_seats_start_only_after_receipt(
    review_skills_dir: Path,
    checked_out_workspace: Workspace,
    bare_remote: Path,
    ledger: RunLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10: the snapshot receipt is recorded before either reviewer starts."""
    events: list[str] = []

    real_receipt = ledger.record_effect_receipt

    def logging_receipt(
        attempt_id: str,
        write_kind: str,
        *,
        remote_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> EffectRecord:
        events.append("receipt")
        return real_receipt(
            attempt_id, write_kind, remote_id=remote_id, payload=payload
        )

    monkeypatch.setattr(ledger, "record_effect_receipt", logging_receipt)

    real_seat = review_module.run_review_seat

    def spying_seat(
        model: Any,
        *,
        seat: str,
        brief: str,
        workspace: Path,
        max_rounds: int = review_module.MAX_REVIEW_ROUNDS,
        artifact_store: Any = None,
    ) -> Any:
        assert ledger.get_effect(
            "#34/2", f"{REVIEW_SNAPSHOT_KIND_PREFIX}1"
        ) is not None
        events.append(f"seat-{seat}")
        return real_seat(
            model,
            seat=seat,
            brief=brief,
            workspace=workspace,
            max_rounds=max_rounds,
            artifact_store=artifact_store,
        )

    monkeypatch.setattr(review_module, "run_review_seat", spying_seat)

    state = ReviewSnapshotState()
    model = ScriptedModel(
        [
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    wrapped = _wrapped_tool(
        skills_dir=review_skills_dir,
        ledger=ledger,
        review_workspace=checked_out_workspace,
        state=state,
        model=model,
        remote_url=str(bare_remote),
    )
    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    result = str(wrapped.invoke({}))
    assert events == ["receipt", "seat-Standards", "seat-Spec"]
    assert "both Review Reports" in result
    assert "standards findings" in result
    assert "spec findings" in result
    assert state.status == "fresh"
    assert state.review_gate_met


def test_completed_review_records_usage_for_attempt_meters(
    review_skills_dir: Path,
    checked_out_workspace: Workspace,
    bare_remote: Path,
    ledger: RunLedger,
) -> None:
    """W-STP-6: reviewer spend is staged on Attempt state for the meters."""
    state = ReviewSnapshotState()
    model = ScriptedModel(
        [
            _ai(
                content="standards",
                calls=[
                    _call("report_review", {"findings": "standards findings"}, "s1")
                ],
                usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            ),
            _ai(
                content="spec",
                calls=[_call("report_review", {"findings": "spec findings"}, "s2")],
                usage={"input_tokens": 200, "output_tokens": 50, "total_tokens": 250},
            ),
        ]
    )
    wrapped = _wrapped_tool(
        skills_dir=review_skills_dir,
        ledger=ledger,
        review_workspace=checked_out_workspace,
        state=state,
        model=model,
        remote_url=str(bare_remote),
    )
    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    str(wrapped.invoke({}))
    assert len(state.completed_reviews) == 1
    completed = state.completed_reviews[0]
    assert completed.completed
    assert completed.usage.tokens == 400
    assert completed.usage.effective_tokens == 400
    assert completed.usage.tool_calls == 2
    pending = state.take_pending_review_usage()
    assert pending.tokens == 400
    assert pending.tool_calls == 2
    assert state.take_pending_review_usage() == UsageTotals()


def test_status_and_gate_across_invocations() -> None:
    """C-REV-1/G-REV-1: not-invoked, incomplete, fresh, stale; the gate needs both seats."""
    state = ReviewSnapshotState()
    assert state.status == "not-invoked"
    assert not state.review_gate_met
    state.invocations = 1
    state.snapshots.append("sha-1")
    assert state.status == "incomplete"
    state.record_completed_review(
        CompletedReview(
            ordinal=1,
            commit_sha="sha-1",
            branch_name="agent/34/2-x",
            standards_findings="standards findings",
            spec_findings=None,
        )
    )
    # One seat made no report: no completed review, so still incomplete.
    assert state.status == "incomplete"
    assert not state.review_gate_met
    assert not state.completed_reviews[-1].completed
    state.snapshots.append("sha-2")
    assert state.status == "incomplete"
    assert not state.review_gate_met
    state.invocations = 2
    state.record_completed_review(
        CompletedReview(
            ordinal=2,
            commit_sha="sha-2",
            branch_name="agent/34/2-x",
            standards_findings="standards findings",
            spec_findings="spec findings",
        )
    )
    assert state.status == "fresh"
    assert state.review_gate_met
    # A later invocation one seat did not report on leaves the completed
    # review of sha-2 the latest: stale, and the gate stays met.
    state.invocations = 3
    state.snapshots.append("sha-3")
    state.record_completed_review(
        CompletedReview(
            ordinal=3,
            commit_sha="sha-3",
            branch_name="agent/34/2-x",
            standards_findings=None,
            spec_findings="spec findings",
        )
    )
    assert state.status == "stale"
    assert state.review_gate_met


def test_status_is_stale_after_a_later_change_with_no_new_invocation() -> None:
    """C-REV-1: a change after the latest completed review makes it stale."""
    changed = {"sha-1": False}
    state = ReviewSnapshotState(
        invocations=1,
        snapshots=["sha-1"],
        changed_since=lambda sha: changed[sha],
    )
    state.record_completed_review(
        CompletedReview(
            ordinal=1,
            commit_sha="sha-1",
            branch_name="agent/34/2-x",
            standards_findings="standards findings",
            spec_findings="spec findings",
        )
    )
    assert state.status == "fresh"
    changed["sha-1"] = True
    assert state.status == "stale"
    assert state.review_gate_met


def test_a_workspace_change_after_review_makes_the_status_stale(
    review_skills_dir: Path,
    checked_out_workspace: Workspace,
    bare_remote: Path,
    ledger: RunLedger,
) -> None:
    """C-REV-1: an edit after the reviewed snapshot, with no new invocation, is stale."""
    state = ReviewSnapshotState()
    model = ScriptedModel(
        [
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    wrapped = _wrapped_tool(
        skills_dir=review_skills_dir,
        ledger=ledger,
        review_workspace=checked_out_workspace,
        state=state,
        model=model,
        remote_url=str(bare_remote),
    )
    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    assert "both Review Reports" in str(wrapped.invoke({}))
    assert state.status == "fresh"
    (checked_out_workspace.path / "changed.txt").write_text("changed again\n", encoding="utf-8")
    assert state.status == "stale"
    assert state.review_gate_met
    assert state.invocations == 1


def test_fourth_invocation_refused_with_reviewers(
    review_skills_dir: Path,
    checked_out_workspace: Workspace,
    bare_remote: Path,
    ledger: RunLedger,
) -> None:
    """P-DEL-4: the fourth invocation is refused and pushes nothing."""
    state = ReviewSnapshotState()
    model = ScriptedModel(
        [
            _report(seat, f"{seat}-{ordinal}", f"{seat} findings")
            for ordinal in (1, 2, 3)
            for seat in SEATS
        ]
    )
    wrapped = _wrapped_tool(
        skills_dir=review_skills_dir,
        ledger=ledger,
        review_workspace=checked_out_workspace,
        state=state,
        model=model,
        remote_url=str(bare_remote),
    )
    for _ in range(MAX_REVIEW_INVOCATIONS):
        (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
        result = str(wrapped.invoke({}))
        assert "both Review Reports" in result
    limited = str(wrapped.invoke({}))
    assert "invocation limit" in limited
    assert state.invocations == MAX_REVIEW_INVOCATIONS
    assert len(state.completed_reviews) == MAX_REVIEW_INVOCATIONS


def _completion(call_id: str) -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"AC-1": ["pkg::test_ok"]},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _attempt_repository(tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote carrying the JUnit profile, and a workspace cloned from it."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(JUNIT_PROFILE, encoding="utf-8")
    (src / "CODING_STANDARDS.md").write_text("# Standards\n", encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    workspace = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workspace)], cwd=tmp_path)
    return bare, workspace


def test_attempt_runs_reviewers_and_charges_their_spend(
    tmp_path: Path, review_skills_dir: Path
) -> None:
    """End to end: the snapshot push precedes both seats, and their tool
    calls land on the Attempt-wide meters (a Gate the implementer alone
    could not reach)."""
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        model = ScriptedModel(
            [
                AIMessage(
                    content="activating",
                    tool_calls=[
                        {
                            "name": "activate_implement",
                            "args": {},
                            "id": "call_activate_1",
                            "type": "tool_call",
                        }
                    ],
                ),
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
                AIMessage(
                    content="reviewing",
                    tool_calls=[
                        {
                            "name": "activate_code_review",
                            "args": {},
                            "id": "call_review_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                _ai(
                    content="standards",
                    calls=[
                        _call(
                            "report_review",
                            {"findings": "standards: no findings"},
                            "seat-std-1",
                        )
                    ],
                    usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                ),
                _ai(
                    content="spec",
                    calls=[
                        _call(
                            "report_review",
                            {"findings": "spec: no findings"},
                            "seat-spec-1",
                        )
                    ],
                    usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                ),
                AIMessage(
                    content="addressing review feedback",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "path": "greeting.txt",
                                "content": "hello, reviewed\n",
                            },
                            "id": "call_write_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                _completion("call_completion_1"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=review_skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
            ceilings=LoopCeilings(max_tool_calls=9),
        )
        kinds = [record.kind for record in ledger.records("#7/1")]
    finally:
        ledger.close()

    assert result.outcome == "delivered"
    assert result.reason == "verified-completion"
    # Five implementer requests plus one per reviewer seat. The completed
    # review meets the review gate, so the claim is accepted at once even
    # though the follow-up write left the review stale (P-DEL-3, C-REV-1).
    assert len(model.invocations) == 7
    seat_bindings = [
        names for choice, names in model.bind_history if "report_review" in names
    ]
    assert len(seat_bindings) == 2
    implementer_bindings = [
        names for choice, names in model.bind_history if "report_review" not in names
    ]
    assert implementer_bindings != []
    assert all("write_file" in names for names in implementer_bindings)
    # Six implementer tool calls cannot reach 70% of nine; with the two
    # reviewer calls folded in, the Attempt-wide meter trips the Gate.
    assert "gate" in kinds


def test_claim_after_a_completed_review_is_verified_with_no_return(
    tmp_path: Path, review_skills_dir: Path
) -> None:
    """P-DEL-3: a completed review meets the gate, so the claim is not returned.

    No work follows the review, so the snapshot commit leaves the working
    tree clean against ``HEAD``. The candidate diff is measured against the
    Base Revision, so the claim still earns ``verified completion``, not
    ``already satisfied``.
    """
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        model = ScriptedModel(
            [
                _ai(content="activating", calls=[_call("activate_implement", {}, "call_act_1")]),
                _ai(
                    content="working",
                    calls=[
                        _call(
                            "write_file",
                            {"path": "greeting.txt", "content": "hello\n"},
                            "call_write_1",
                        ),
                        _call("test_targeted", {"path": "tests/test_greet.py"}, "call_test_1"),
                    ],
                ),
                _ai(content="reviewing", calls=[_call("activate_code_review", {}, "call_rev_1")]),
                _ai(
                    content="standards",
                    calls=[_call("report_review", {"findings": "no findings"}, "seat-std-1")],
                ),
                _ai(
                    content="spec",
                    calls=[_call("report_review", {"findings": "no findings"}, "seat-spec-1")],
                ),
                _completion("call_completion_1"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=review_skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
        )
    finally:
        ledger.close()

    assert run_git(["status", "--porcelain"], workspace) == ""
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    # Four implementer requests plus one per seat: the claim was not returned.
    assert len(model.invocations) == 6


def _review_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A git repo with a base commit plus one change commit.

    Returns ``(repo, base_sha, head_sha)``: the Base Revision and the
    reviewed commit the seats must see the change between.
    """
    repo = tmp_path / "review-repo"
    base = init_origin_repo(repo)
    (repo / "greeting.txt").write_text("hello\n", encoding="utf-8")
    run_git(["add", "."], repo)
    run_git(["commit", "-q", "-m", "Greet the world"], repo)
    return repo, base, run_git(["rev-parse", "HEAD"], repo)


def test_seat_briefs_carry_commit_list_and_diff(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """§10: each seat's brief carries the change itself, not just its commands."""
    repo, base, head = _review_repo(tmp_path)
    model = ScriptedModel(
        [
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=repo,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha=head,
        base_revision=base,
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert result.completed
    assert result.standards.messages and result.spec.messages
    for seat_result in (result.standards, result.spec):
        brief = str(seat_result.messages[0].content)
        assert "Greet the world" in brief
        assert "+hello" in brief
        assert "review the change below, not the whole tree" in brief
        assert "for reference: git diff" not in brief


def test_large_diff_is_bounded_and_reachable(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """§10: an over-bound diff is capped inline and stays reachable in full."""
    repo = tmp_path / "big-repo"
    base = init_origin_repo(repo)
    big_line = "x" * (REVIEW_DIFF_INLINE_LIMIT + 1000)
    (repo / "big.txt").write_text(big_line + "\n", encoding="utf-8")
    run_git(["add", "."], repo)
    run_git(["commit", "-q", "-m", "Add a big file"], repo)
    head = run_git(["rev-parse", "HEAD"], repo)
    store = InMemoryArtifactStore()
    change = prepare_review_change(
        workspace=repo, base_revision=base, commit_sha=head, ordinal=1, store=store
    )
    assert "[capped:" in change.diff
    assert review_diff_artifact_id(1) in change.diff
    assert len(change.diff) < REVIEW_DIFF_INLINE_LIMIT + len(big_line) // 2
    assert big_line in store.read(review_diff_artifact_id(1))
    standards_block, _ = extract_role_blocks(review_skills_dir)
    brief = compose_reviewer_brief(
        seat=STANDARDS_SEAT,
        role_block=standards_block,
        branch_name="agent/34/2-big",
        commit_sha=head,
        base_revision=base,
        issue_number=34,
        issue_title="Big!",
        issue_body="## Acceptance criteria\n- [ ] Big\n",
        standards_sources=(),
        commit_list=change.commit_list,
        diff=change.diff,
    )
    model = ScriptedModel(
        [
            _ai(
                content="reading the full diff",
                calls=[
                    _call(
                        "read_result_slice",
                        {
                            "artifact_id": review_diff_artifact_id(1),
                            "start": 0,
                            "end": 10_000_000,
                        },
                        "slice-1",
                    )
                ],
            ),
            _report(STANDARDS_SEAT, "seat-std-1", "no findings"),
        ]
    )
    seat_result = run_review_seat(
        model, seat=STANDARDS_SEAT, brief=brief, workspace=repo, artifact_store=store
    )
    assert seat_result.completed
    assert any(
        big_line[:100] in str(message.content) for message in seat_result.messages
    )


def test_unreadable_change_range_still_completes(
    review_skills_dir: Path, tmp_path: Path
) -> None:
    """§10: where the range cannot be read, the brief says so and seats report."""
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    model = ScriptedModel(
        [
            _report(STANDARDS_SEAT, "seat-std-1", "standards findings"),
            _report(SPEC_SEAT, "seat-spec-1", "spec findings"),
        ]
    )
    result = run_review_invocation(
        model,
        skills_dir=review_skills_dir,
        workspace=tmp_path,
        ordinal=1,
        branch_name="agent/34/2-fix-the-loop",
        commit_sha="abc123",
        base_revision="base-rev",
        issue_number=34,
        issue_title="Fix the loop!",
        issue_body="## Acceptance criteria\n- [ ] Fix the loop\n",
    )
    assert result.completed
    assert "change unavailable" in str(result.standards.messages[0].content)
    assert "change unavailable" in str(result.spec.messages[0].content)


def test_long_commit_list_is_bounded_with_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10: the inline commit list names its total instead of cutting silently."""
    entries = "\n".join(f"{index:07d} subject {index}" for index in range(60))
    monkeypatch.setattr(review_module, "commit_list_since_base", lambda *args, **kwargs: entries)
    monkeypatch.setattr(
        review_module, "diff_since_base", lambda *args, **kwargs: "diff --git a/x b/x\n"
    )
    change = prepare_review_change(
        workspace=tmp_path,
        base_revision="base-rev",
        commit_sha="abc123",
        ordinal=3,
        store=InMemoryArtifactStore(),
    )
    assert "60 commits total; showing the first 50" in change.commit_list
    assert change.diff == "diff --git a/x b/x\n"


def _work_turns(write_id: str, test_id: str) -> list[AIMessage]:
    return [
        _ai(content="activating", calls=[_call("activate_implement", {}, f"act-{write_id}")]),
        _ai(
            content="working",
            calls=[
                _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id),
                _call("test_targeted", {"path": "tests/test_greet.py"}, test_id),
            ],
        ),
    ]


def _review_turns(ordinal: int) -> list[AIMessage]:
    return [
        _ai(content="reviewing", calls=[_call("activate_code_review", {}, f"call_rev_{ordinal}")]),
        _report(STANDARDS_SEAT, f"seat-std-{ordinal}", "standards: no findings"),
        _report(SPEC_SEAT, f"seat-spec-{ordinal}", "spec: no findings"),
    ]


def test_reviewer_spend_crosses_the_ceiling_inside_the_invoking_node(
    tmp_path: Path, review_skills_dir: Path
) -> None:
    """W-STP-6: the seats' tool calls count before the node's next model call.

    Four implementer calls stay under a five-call ceiling; the two seats'
    ``report_review`` calls take the Attempt to six, so the node stops right
    after the review with no claim. Folded in only after the node, the
    implementer would have reached an accepted claim first.
    """
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        model = ScriptedModel(
            [*_work_turns("call_write_1", "call_test_1"), *_review_turns(1), _completion("call_completion_1")]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=review_skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
            ceilings=LoopCeilings(max_tool_calls=5),
        )
    finally:
        ledger.close()

    assert (result.outcome, result.reason) == ("failed", "saved-partial-work")
    # Three implementer requests plus one per seat: no claim request followed.
    assert len(model.invocations) == 5


def test_review_limit_is_counted_across_steps(
    tmp_path: Path, review_skills_dir: Path
) -> None:
    """W-STP-6, P-DEL-4: three invocations in step one leave none for step two.

    The fourth invocation, the first in step two, is refused with a tool
    result and no push or reviewer runs. The completed reviews from step
    one still meet the review gate, so the claim is accepted at once.
    """
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    steps = (
        WorkflowStep(step_id="s1", skill="implement"),
        WorkflowStep(step_id="s2", skill="implement"),
    )
    try:
        model = ScriptedModel(
            [
                *_work_turns("call_write_1", "call_test_1"),
                *_review_turns(1),
                *_review_turns(2),
                *_review_turns(3),
                _ai(
                    content="step done",
                    calls=[
                        _call(
                            "report_step",
                            {"status": REPORT_STEP_DONE, "justification": "reviewed three times"},
                            "call_step_1",
                        )
                    ],
                ),
                _ai(content="activating", calls=[_call("activate_implement", {}, "call_act_2")]),
                _ai(content="reviewing", calls=[_call("activate_code_review", {}, "call_rev_4")]),
                _completion("call_completion_1"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=review_skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
            steps=steps,
        )
        snapshot_kinds = [
            effect.write_kind
            for effect in ledger.list_effects("#7/1")
            if effect.write_kind.startswith(REVIEW_SNAPSHOT_KIND_PREFIX)
        ]
    finally:
        ledger.close()

    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert snapshot_kinds == [f"{REVIEW_SNAPSHOT_KIND_PREFIX}{n}" for n in (1, 2, 3)]
    # The fourth invocation's result is the refusal, read by the claim request.
    claim_request = model.invocations[-1]
    assert "invocation limit" in str(claim_request[-1].content)
    # No seat ran for the refused invocation: three pairs of seat requests.
    seat_requests = [names for _, names in model.bind_history if "report_review" in names]
    assert len(seat_requests) == 6


def test_review_disclosure_holds_status_count_and_every_report() -> None:
    """P-DEL-5: the disclosure carries the status, the count and each invocation's reports."""
    state = ReviewSnapshotState(invocations=2, snapshots=["sha-1", "sha-2"])
    state.record_completed_review(
        CompletedReview(
            ordinal=1,
            commit_sha="sha-1",
            branch_name="agent/34/2-x",
            standards_findings="standards one",
            spec_findings=None,
        )
    )
    state.record_completed_review(
        CompletedReview(
            ordinal=2,
            commit_sha="sha-2",
            branch_name="agent/34/2-x",
            standards_findings="standards two",
            spec_findings="spec two",
        )
    )
    assert review_disclosure(state) == {
        "status": "fresh",
        "invocations": 2,
        "review_gate_met": True,
        "latest_completed_ordinal": 2,
        "reviews": [
            {
                "ordinal": 1,
                "commit_sha": "sha-1",
                "branch_name": "agent/34/2-x",
                "completed": False,
                "standards_report": "standards one",
                "spec_report": None,
            },
            {
                "ordinal": 2,
                "commit_sha": "sha-2",
                "branch_name": "agent/34/2-x",
                "completed": True,
                "standards_report": "standards two",
                "spec_report": "spec two",
            },
        ],
    }


BLOCKING_STANDARDS = "CRITICAL (blocking): SQL injection in greeting.txt; must not merge"
BLOCKING_SPEC = "HIGH: AC-1 is not met; reject this change"


def test_findings_of_any_severity_change_neither_outcome_nor_status(
    tmp_path: Path, review_skills_dir: Path
) -> None:
    """P-DEL-5: blocking-worded findings leave the outcome and status as they are.

    The review status, the invocation count and both Review Reports are
    recorded in the Run Ledger for disclosure, with the Attempt-wide budget.
    """
    bare, workspace = _attempt_repository(tmp_path)
    ceilings = LoopCeilings(max_tool_calls=50)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3", ceilings=ceilings)
    try:
        model = ScriptedModel(
            [
                *_work_turns("call_write_1", "call_test_1"),
                _ai(content="reviewing", calls=[_call("activate_code_review", {}, "call_rev_1")]),
                _report(STANDARDS_SEAT, "seat-std-1", BLOCKING_STANDARDS),
                _report(SPEC_SEAT, "seat-spec-1", BLOCKING_SPEC),
                _completion("call_completion_1"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=review_skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
            ceilings=ceilings,
        )
        records = [r for r in ledger.records("#7/1") if r.kind == REVIEW_STATUS_KIND]
    finally:
        ledger.close()

    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert len(records) == 1
    disclosure = records[0].payload
    assert disclosure["status"] == "fresh"
    assert disclosure["invocations"] == 1
    assert disclosure["review_gate_met"] is True
    reviews = disclosure["reviews"]
    assert isinstance(reviews, list) and len(reviews) == 1
    assert reviews[0]["standards_report"] == BLOCKING_STANDARDS
    assert reviews[0]["spec_report"] == BLOCKING_SPEC
    # Attempt-wide spend: five implementer calls plus two reviewer calls.
    assert records[0].remaining_budget["tool_calls"] == 50 - 7


def test_attempt_without_review_records_not_invoked(
    tmp_path: Path, review_skills_dir: Path
) -> None:
    """P-DEL-5: an Attempt that never invoked review still records its status."""
    bare, workspace = _attempt_repository(tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        model = ScriptedModel(
            [
                *_work_turns("call_write_1", "call_test_1"),
                _completion("call_completion_1"),
                _completion("call_completion_2"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=review_skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
        )
        records = [r for r in ledger.records("#7/1") if r.kind == REVIEW_STATUS_KIND]
    finally:
        ledger.close()

    assert result.outcome == "delivered"
    assert len(records) == 1
    assert records[0].payload["status"] == "not-invoked"
    assert records[0].payload["invocations"] == 0
    assert records[0].payload["review_gate_met"] is False
    assert records[0].payload["reviews"] == []
