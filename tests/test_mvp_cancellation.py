"""M7 cancellation Gates: a human stop ends the Attempt ``abandoned``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (cancellation at a
step-boundary Gate yields ``abandoned``), §10 (the in-tool Gate in front of
the review-snapshot push: a stop found there ends the Attempt at that Gate,
keeping whatever is already pushed) and §11 (a Gate re-reads the issue's
open state and labels through the port; a human cancels by closing the
issue or removing the Selection Label; a Gate before the Delivery Intent
that sees either stops the model loop, commits the existing edits and
pushes the branch, ending ``abandoned`` with no pull request; after the
Delivery Intent cancellation does not stop publication).

MVP row ids: P-CAN-1, P-CAN-2, W-STP-3, G-REV-3.

Every test drives the delivered happy-path shape (``invoke_happy_path`` +
``verified_flow_model`` against a local bare remote) through a tracker that
reports the issue open and labeled for the first ``open_reads`` Gate reads
and cancelled afterwards — either by dropping the Selection Label or by
closing the issue. The three pre-Intent reads in that flow are the
step-boundary check, the in-tool Gate in front of the snapshot push, and
the between-step ``gate`` node; nothing re-reads the issue after the
Delivery Intent, so arming cancellation from the fourth read on proves
P-CAN-2 structurally.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.tracker.port import IssueState
from langchain_core.messages import AIMessage
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from test_mvp_attempt_graph import (
    EFFECTIVE_CONFIGURATION,
    HAPPY_PATH_PROFILE,
    activation_message,
    completion_message,
    invoke_happy_path,
    verified_flow_model,
)


def _write_and_test_message(call_id: str = "call_write_1") -> AIMessage:
    """A working turn: the candidate diff plus passing Criterion Evidence (§9)."""
    return AIMessage(
        content="working",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "greeting.txt", "content": "hello\n"},
                "id": f"{call_id}-write",
                "type": "tool_call",
            },
            {
                "name": "test_targeted",
                "args": {"path": "tests/test_greet.py"},
                "id": f"{call_id}-test",
                "type": "tool_call",
            },
        ],
    )


def _review_message(call_id: str = "call_review_1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[
            {"name": "activate_code_review", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _cancelled_review_model() -> ScriptedModel:
    """A flow whose review never runs: the in-tool Gate stops the snapshot.

    The cancelled snapshot records no completed review, so the first
    ``report_completion`` is returned once for the missing review (P-DEL-3)
    and the second is accepted — the Attempt then ends at the stopped Gate.
    Three completions mirror the M2 triple-call shape, so a second return
    still terminates instead of exhausting the script.
    """
    return ScriptedModel(
        [
            activation_message("call_activate_1"),
            _write_and_test_message(),
            _review_message("call_review_1"),
            completion_message(covering=["pkg::test_ok"], call_id="call_completion_1"),
            completion_message(covering=["pkg::test_ok"], call_id="call_completion_2"),
            completion_message(covering=["pkg::test_ok"], call_id="call_completion_3"),
        ]
    )

TERMINAL_WRITES_WITHOUT_PR = (
    "post_terminal_comment",
    "remove_selection_label",
    "add_ready_for_human",
    "release_assignment",
)
"""The §14 terminal writes for an outcome with no pull request (C-OUT-1)."""


@dataclass
class CancellingTracker(FakeTracker):
    """A fake that reports cancellation after ``open_reads`` Gate reads (§11).

    The first ``open_reads`` ``read_issue_state`` calls report the default
    open, labeled issue; every later call reports the cancellation — a
    closed issue where ``cancel_closed`` holds, else an open issue without
    the Selection Label. Counting recorded reads (rather than flipping on a
    timer) pins the stop to one Gate: the step boundary, the in-tool Gate,
    or the between-step ``gate`` node.
    """

    open_reads: int = 2
    cancel_closed: bool = False

    def read_issue_state(self, issue_number: int) -> IssueState:
        seen = sum(1 for name, _ in self.calls if name == "read_issue_state")
        if seen < self.open_reads:
            return super().read_issue_state(issue_number)
        self._record("read_issue_state", issue_number)
        if self.cancel_closed:
            return IssueState(
                number=issue_number,
                is_open=False,
                labels=(self.selection_label,),
            )
        return IssueState(number=issue_number, is_open=True, labels=())


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(
        HAPPY_PATH_PROFILE, encoding="utf-8"
    )
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
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


def _run_cancelled(
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    bare_remote: Path,
    tracker: CancellingTracker,
    model: ScriptedModel | None = None,
) -> AttemptResult:
    """Run the happy-path flow with our assignment on record (§5, C-OUT-1).

    Pre-opening the Attempt with ``assigned_by_us=True`` is what the
    Worker's Claim records under ``agent run``; the graph's ``claim`` keeps
    the row, so ``terminal_writes`` removes the Worker's assignment last.
    """
    ledger.open_attempt(
        "#7/1",
        effective_configuration=EFFECTIVE_CONFIGURATION,
        assigned_by_us=True,
    )
    return invoke_happy_path(
        ledger,
        workspace,
        skills_dir,
        tracker,
        model if model is not None else verified_flow_model(),
        bare_remote,
    )


def _assert_abandoned_without_pr(
    ledger: RunLedger, tracker: CancellingTracker, result: AttemptResult
) -> None:
    """The shared P-CAN-1 shape: pushed work, comment, labels, no PR (§11)."""
    assert (result.outcome, result.reason) == ("abandoned", "cancelled")
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("abandoned", "cancelled")
    assert "Selection Label" in outcome.terminal_comment_body
    assert outcome.writes == (
        "terminal-comment",
        "remove-selection-label",
        "add-ready-for-human",
        "release-assignment",
    )
    assert "open_pull_request" not in tracker.call_names()
    assert "find_pull_request" not in tracker.call_names()
    delivery = ledger.get_effect("#7/1", "delivery-snapshot-push")
    assert delivery is not None
    assert delivery.status == "receipt"
    assert tracker.call_names() == [
        "fetch_issue",
        "read_issue_state",
        "read_issue_state",
        "read_issue_state",
        *TERMINAL_WRITES_WITHOUT_PR,
    ]


def test_p_can_1_label_removed_during_implementation_ends_abandoned(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-CAN-1, W-STP-3: a dropped Selection Label stops the Attempt (§11).

    The first two Gate reads (step boundary, in-tool Gate) see the open,
    labeled issue, so the review snapshot pushes; the between-step ``gate``
    sees the dropped label and ends the Attempt ``abandoned`` /
    ``cancelled``. The Delivery Snapshot still commits and pushes the work,
    the terminal comment and ``ready-for-human`` are written, the Worker's
    assignment is removed, and no pull request opens.
    """
    tracker = CancellingTracker(open_reads=2)
    result = _run_cancelled(ledger, workspace, skills_dir, bare_remote, tracker)
    _assert_abandoned_without_pr(ledger, tracker, result)
    snapshot = ledger.get_effect("#7/1", "review-snapshot-push/1")
    assert snapshot is not None
    assert snapshot.status == "receipt"


def test_closed_issue_behaves_like_label_removal(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-CAN-1: closing the issue cancels exactly like dropping the label."""
    tracker = CancellingTracker(open_reads=2, cancel_closed=True)
    result = _run_cancelled(ledger, workspace, skills_dir, bare_remote, tracker)
    _assert_abandoned_without_pr(ledger, tracker, result)
    snapshot = ledger.get_effect("#7/1", "review-snapshot-push/1")
    assert snapshot is not None
    assert snapshot.status == "receipt"
    assert tracker.read_issue_state(7).is_open is False


def test_g_rev_3_in_tool_gate_stop_keeps_pushed_work(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """G-REV-3: a stop at the in-tool Gate ends the Attempt at that Gate.

    Only the step-boundary read sees the open issue; the in-tool Gate in
    front of the review-snapshot push sees the dropped label, so the
    snapshot never pushes and no review runs. The Attempt still ends
    ``abandoned`` / ``cancelled`` with no pull request, and the Delivery
    Snapshot still commits and pushes the candidate diff.
    """
    tracker = CancellingTracker(open_reads=1)
    result = _run_cancelled(
        ledger, workspace, skills_dir, bare_remote, tracker, _cancelled_review_model()
    )
    _assert_abandoned_without_pr(ledger, tracker, result)
    assert [
        effect.write_kind
        for effect in ledger.list_effects("#7/1")
        if effect.write_kind.startswith("review-snapshot-push/")
    ] == []


def test_p_can_2_post_intent_cancellation_does_not_stop_publication(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-CAN-2: cancellation after the Delivery Intent stops nothing (§11).

    All three pre-Intent Gate reads see the open, labeled issue, so the
    Attempt delivers and the pull request opens. The tracker is armed to
    report the dropped label from the fourth read on — nothing after the
    Intent ever asks, which the trailing read proves: it reports the
    cancellation the Attempt ignored.
    """
    tracker = CancellingTracker(open_reads=3)
    result = invoke_happy_path(
        ledger, workspace, skills_dir, tracker, verified_flow_model(), bare_remote
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert "open_pull_request" in tracker.call_names()
    assert "add_ready_for_human" not in tracker.call_names()
    assert tracker.call_names() == [
        "fetch_issue",
        "read_issue_state",
        "read_issue_state",
        "read_issue_state",
        "fetch_issue",
        "find_pull_request",
        "open_pull_request",
        "post_terminal_comment",
        "remove_selection_label",
    ]
    late = tracker.read_issue_state(7)
    assert late.is_open is True
    assert tracker.selection_label not in late.labels
