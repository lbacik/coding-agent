"""M4 tail bodies: the §15 pull-request body and terminal comment.

Contract: ``docs/contract/mvp-runtime-contract.md`` §14 ("Recording before
publishing": both bodies are rendered once, when the outcome is recorded, and
stored in ``outcomes`` so a retried or recovered write sends the same bytes;
the recorded writes follow the §14 order) and §15 (the pull-request body
order, the red-history disclosure, the base-movement note, the review
disclosure, the working-area and profile-edited notes, exactly one Attempt
Marker of the right kind per body, truncation with an explicit marker and a
log pointer, and the deterministic terminal Continuation Snapshot for an
Attempt ending without a pull request).

MVP row ids: P-DEL-1, P-DEL-5, G-TDD-1, L3-DEL-5, L3-REV-10, L2-17, L2-18,
C-OUT-1.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.publication import (
    ADD_READY_FOR_HUMAN_WRITE_KIND,
    RELEASE_ASSIGNMENT_WRITE_KIND,
    REMOVE_SELECTION_LABEL_WRITE_KIND,
    TERMINAL_COMMENT_WRITE_KIND,
    attempt_marker,
    baseline_detail_for_comment,
    render_pull_request_body,
    render_terminal_comment,
    terminal_writes_for_outcome,
)
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

HAPPY_PATH_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare git remote holding the pinned Project Profile."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PATH_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    """A workspace checked out from the local bare remote."""
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    """A real Run Ledger file on a temporary state volume."""
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    """One scripted tool call in a model message."""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _pr_body(**overrides: object) -> str:
    """Render a pull-request body with valid defaults, overridden per test."""
    kwargs: dict[str, object] = {
        "attempt_id": "#7/1",
        "issue_number": 7,
        "workflow_id": "implement",
        "configuration_digest": "abc123",
        "base_revision": "base-sha",
        "base_head": "base-sha",
        "base_moved": False,
        "acceptance_criteria": [{"id": "AC-1", "text": "`greet` returns a greeting"}],
        "claimed_tests": {"AC-1": ["pkg::test_ok"]},
        "criterion_results": {"AC-1": "pass"},
        "ever_failed": {"AC-1": False},
        "validation_commands": [
            {"name": "test_all", "exit_code": 0, "executed": 1, "failure_ids": []}
        ],
        "review_status": "reviewed",
        "review_invocations": 1,
        "review_reports": [
            {
                "ordinal": 1,
                "standards_report": "standards: no findings",
                "spec_report": "spec: no findings",
            }
        ],
        "seam_set": ["tdd"],
        "working_area": ".",
        "profile_changed": False,
    }
    kwargs.update(overrides)
    return render_pull_request_body(**kwargs)  # type: ignore[arg-type]


def test_g_tdd_1_red_history_is_disclosed_per_claimed_test() -> None:
    """G-TDD-1: the body states whether each claimed test ever failed."""
    red = _pr_body(ever_failed={"AC-1": True})
    assert "observed failing before this Attempt" in red
    green = _pr_body(ever_failed={"AC-1": False})
    assert "observed failing" not in green


def test_l3_del_5_base_movement_names_the_verified_revision() -> None:
    """L3-DEL-5: a moved base names the verified revision; no rebase happens."""
    moved = _pr_body(
        base_revision="base-sha",
        base_head="new-head-sha",
        base_moved=True,
    )
    assert "`base-sha`" in moved
    assert "verified against `base-sha`" in moved
    assert "No rebase or merge was performed." in moved
    still = _pr_body(base_moved=False)
    assert "Base-branch movement: none." in still


def test_l3_rev_10_oversized_report_is_truncated_with_a_log_pointer() -> None:
    """L3-REV-10: an oversized Review Report is truncated, never silent."""
    oversized = "x" * 9000
    body = _pr_body(
        review_reports=[
            {
                "ordinal": 1,
                "standards_report": oversized,
                "spec_report": "spec: no findings",
            }
        ]
    )
    assert oversized not in body
    assert "[...truncated:" in body
    assert "run-ledger review_status event" in body


def test_p_del_5_review_status_invocations_and_reports_appear() -> None:
    """P-DEL-5: the body carries the review status, count and full reports."""
    body = _pr_body(
        review_status="reviewed",
        review_invocations=2,
        review_reports=[
            {
                "ordinal": 2,
                "standards_report": "standards: blocking wording",
                "spec_report": "spec: blocking wording",
            }
        ],
    )
    assert "reviewed" in body
    assert "2 invocation(s)" in body
    assert "<details>" in body
    assert "standards: blocking wording" in body
    assert "spec: blocking wording" in body


def test_l2_17_l2_18_working_area_and_profile_notes() -> None:
    """L2-17 / L2-18: the declared area and the profile-edited note appear."""
    body = _pr_body(working_area="apps/one", profile_changed=True)
    assert "`apps/one`" in body
    assert "project-profile.yml" in body
    plain = _pr_body(working_area=".", profile_changed=False)
    assert "project-profile.yml" not in plain


def test_every_body_carries_exactly_one_marker_of_the_right_kind() -> None:
    """§15: one Attempt Marker per body, with the right kind."""
    pr_body = _pr_body()
    assert pr_body.count(attempt_marker("#7/1", kind="delivery")) == 1
    assert "kind=terminal" not in pr_body
    comment = render_terminal_comment(
        attempt_id="#7/1",
        outcome="failed",
        reason="drift",
        pull_request_url=None,
        branch_name="agent/7/1-add-a-greeting",
        workflow_id="implement",
        configuration_digest="abc123",
    )
    assert comment.count(attempt_marker("#7/1", kind="terminal")) == 1
    assert "kind=delivery" not in comment


def test_c_out_1_terminal_writes_follow_the_section_14_order() -> None:
    """C-OUT-1: comment, label removal, ready-for-human, assignment release."""
    assert terminal_writes_for_outcome(outcome="delivered") == (
        TERMINAL_COMMENT_WRITE_KIND,
        REMOVE_SELECTION_LABEL_WRITE_KIND,
        RELEASE_ASSIGNMENT_WRITE_KIND,
    )
    assert terminal_writes_for_outcome(outcome="failed") == (
        TERMINAL_COMMENT_WRITE_KIND,
        REMOVE_SELECTION_LABEL_WRITE_KIND,
        ADD_READY_FOR_HUMAN_WRITE_KIND,
        RELEASE_ASSIGNMENT_WRITE_KIND,
    )


def test_baseline_detail_names_failing_commands_and_failures() -> None:
    """§15: a red-baseline comment names the failing baseline commands."""
    detail = baseline_detail_for_comment(
        {
            "commands": [
                {
                    "name": "test_all",
                    "exit_code": 1,
                    "failure_ids": ["pkg::test_bad"],
                }
            ]
        }
    )
    assert detail is not None
    assert "test_all" in detail
    assert "pkg::test_bad" in detail


def test_failed_outcome_records_bodies_before_posting_the_same_bytes(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """§14: bodies and writes are in ``outcomes`` before any tracker write.

    An explicit ``uncovered`` claim ends ``failed`` / ``criteria-uncovered``
    with no pull request: the recorded row carries the rendered terminal
    comment (with its Continuation Snapshot) and the §14 writes, and the
    posted comment sends those same bytes.
    """
    seen: dict[str, Any] = {}

    def on_first_write(name: str) -> None:
        seen["call"] = name
        seen["outcome"] = ledger.get_outcome("#7/1")

    tracker = FakeTracker(on_first_write=on_first_write)
    model = ScriptedModel(
        [
            AIMessage(
                content="activating",
                tool_calls=[_call("activate_implement", {}, "call-act-1")],
            ),
            AIMessage(
                content="writing only",
                tool_calls=[
                    _call(
                        "write_file",
                        {"path": "greeting.txt", "content": "hello\n"},
                        "call-write-1",
                    )
                ],
            ),
            AIMessage(
                content="the change is complete",
                tool_calls=[
                    _call("report_completion", {"AC-1": ["uncovered"]}, "call-rep-1")
                ],
            ),
            AIMessage(
                content="the change is complete",
                tool_calls=[
                    _call("report_completion", {"AC-1": ["uncovered"]}, "call-rep-2")
                ],
            ),
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
    assert (result.outcome, result.reason) == ("failed", "criteria-uncovered")
    assert seen["call"] == "post_terminal_comment"
    recorded = seen["outcome"]
    assert recorded is not None
    assert recorded.outcome == "failed"
    assert recorded.pull_request_body == ""
    assert recorded.writes == (
        TERMINAL_COMMENT_WRITE_KIND,
        REMOVE_SELECTION_LABEL_WRITE_KIND,
        ADD_READY_FOR_HUMAN_WRITE_KIND,
        RELEASE_ASSIGNMENT_WRITE_KIND,
    )
    posted = str(tracker.calls[1][1][1])
    assert posted == recorded.terminal_comment_body
    assert posted.count(attempt_marker("#7/1", kind="terminal")) == 1
    assert "PASS" not in posted
    assert "## Continuation Snapshot" in posted
    assert "Next:" in posted
    assert tracker.call_names() == [
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ]
