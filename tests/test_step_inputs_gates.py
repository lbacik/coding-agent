"""Gates, Drift and withdrawal (contract §7.3).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §7 (the pins)
and §7.3 (Gates before every Step Execution and every external write;
Drift and withdrawal endings; base-branch commits disclosed, never Drift;
draft changes ignored).

Row ids: INP-1 … INP-3, INP-6, INP-7, INP-14 … INP-18.
"""

from __future__ import annotations

from dataclasses import replace

from coding_agent.inputs.gates import check_gate
from coding_agent.inputs.pinning import PinnedInput, pin_from_readback
from coding_agent.inputs.port import TrackedInput


def _issue(
    *,
    number: int = 42,
    title: str = "Add a greeting",
    body: str = "a body",
    is_open: bool = True,
    labels: tuple[str, ...] = ("agent:implement",),
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=title,
        body=body,
        is_open=is_open,
        labels=labels,
        assignees=("coding-agent",),
        created_at="2026-10-01T00:00:00Z",
    )


def _pull(
    *,
    number: int = 42,
    title: str = "Add a greeting",
    body: str = "a body",
    is_open: bool = True,
    merged: bool = False,
    head_sha: str = "h" * 40,
    head_branch: str = "feature",
    base_branch: str = "main",
    labels: tuple[str, ...] = ("agent:review",),
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="pull-request",
        title=title,
        body=body,
        is_open=is_open,
        labels=labels,
        assignees=("coding-agent",),
        created_at="2026-10-01T00:00:00Z",
        draft=False,
        merged=merged,
        head_sha=head_sha,
        head_branch=head_branch,
        head_repo="octocat/sandbox",
        base_branch=base_branch,
    )


def _issue_pin() -> PinnedInput:
    return pin_from_readback(
        _issue(), workflow_id="implement", base_tip="t" * 40
    )


def _pull_pin() -> PinnedInput:
    return pin_from_readback(
        _pull(), workflow_id="review", base_tip="t" * 40, merge_base="m" * 40
    )


def test_clean_gate_passes() -> None:
    """§7.3: an unchanged input passes the Gate."""
    assert check_gate(_issue(), _issue_pin(), selection_label="agent:implement").status == "ok"
    assert check_gate(_pull(), _pull_pin(), selection_label="agent:review").status == "ok"


def test_fingerprint_is_the_readback_title_and_body() -> None:
    """INP-14: the Fingerprint is the read-back's title and body digest."""
    from coding_agent.runtime.fingerprint import compute_fingerprint

    pin = _issue_pin()
    assert pin.fingerprint == compute_fingerprint("Add a greeting", "a body")
    assert pin.base_revision == "t" * 40


def test_pull_request_pins_head_and_merge_base() -> None:
    """INP-1: the read-back pins the Input Head and the merge-base Base Revision."""
    pin = _pull_pin()
    assert pin.input_head == "h" * 40
    assert pin.base_revision == "m" * 40
    assert pin.base_branch == "main"


def test_issue_body_edit_is_drift() -> None:
    """INP-7, INP-16: an issue body edited mid-run ends the Attempt ``drift``."""
    changed = replace(_issue(), body="a different body")
    result = check_gate(changed, _issue_pin(), selection_label="agent:implement")
    assert (result.status, result.reason) == ("drift", "content-changed")


def test_foreign_push_is_drift() -> None:
    """INP-3, INP-17: a foreign push ends the Attempt ``drift`` at the next Gate."""
    changed = replace(_pull(), head_sha="f" * 40)
    result = check_gate(changed, _pull_pin(), selection_label="agent:review")
    assert (result.status, result.reason) == ("drift", "head-changed")


def test_own_push_advance_is_not_drift() -> None:
    """INP-2: the Attempt's own compare-and-swap push advances the Input Head."""
    changed = replace(_pull(), head_sha="n" * 40)
    result = check_gate(
        changed, _pull_pin(), selection_label="agent:review", expected_head="n" * 40
    )
    assert result.status == "ok"


def test_base_branch_change_is_drift() -> None:
    """§7.3: a change of base branch is Drift."""
    changed = replace(_pull(), base_branch="other")
    result = check_gate(changed, _pull_pin(), selection_label="agent:review")
    assert (result.status, result.reason) == ("drift", "base-branch-changed")


def test_label_removal_is_withdrawal() -> None:
    """INP-6: removing the Selection Label mid-run is a withdrawal."""
    changed = replace(_issue(), labels=())
    result = check_gate(changed, _issue_pin(), selection_label="agent:implement")
    assert (result.status, result.reason) == ("withdrawn", "selection-label-removed")


def test_closure_is_withdrawal() -> None:
    """INP-6: closing the input mid-run is a withdrawal."""
    changed = replace(_issue(), is_open=False)
    result = check_gate(changed, _issue_pin(), selection_label="agent:implement")
    assert (result.status, result.reason) == ("withdrawn", "closed")


def test_merge_is_withdrawal() -> None:
    """INP-6: merging the pull request mid-run is a withdrawal."""
    changed = replace(_pull(), merged=True, is_open=False)
    result = check_gate(changed, _pull_pin(), selection_label="agent:review")
    assert result.status == "withdrawn"


def test_withdrawal_wins_over_drift() -> None:
    """§7.3: a Gate finding both reports the withdrawal."""
    changed = replace(_issue(), is_open=False, body="edited too")
    result = check_gate(changed, _issue_pin(), selection_label="agent:implement")
    assert result.status == "withdrawn"


def test_base_commits_are_disclosed_not_drift() -> None:
    """INP-18: new commits on the base branch are disclosed, never Drift."""
    result = check_gate(
        _issue(),
        _issue_pin(),
        selection_label="agent:implement",
        base_tip_now="n" * 40,
    )
    assert result.status == "ok"
    assert result.base_moved is True
    same = check_gate(
        _issue(),
        _issue_pin(),
        selection_label="agent:implement",
        base_tip_now="t" * 40,
    )
    assert same.base_moved is False
