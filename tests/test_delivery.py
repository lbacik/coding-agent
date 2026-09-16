from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.identity.startup import Identity
from coding_agent.implement import delivery
from coding_agent.implement.delivery import (
    COMMITTED_AND_PUSHED,
    NO_CHANGE_PRODUCED,
    REMOTE_BRANCH_COLLISION,
    deliver_snapshot,
)
from coding_agent.implement.git import Workspace, checkout_workspace, ensure_mirror, push_branch
from conftest import init_origin_repo, run_git

IDENTITY = Identity(account_id=999, login="coding-agent")


def _checked_out_workspace(tmp_path: Path) -> tuple[Path, Workspace]:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace = checkout_workspace(mirror, tmp_path / "workspace")
    return origin, workspace


def test_deliver_snapshot_reports_no_change_produced_and_pushes_nothing(tmp_path: Path) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)

    outcome = deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Do the thing",
        attempt_number=1,
    )

    assert outcome.kind == NO_CHANGE_PRODUCED
    assert outcome.branch_name is None
    assert outcome.commit_sha is None
    assert run_git(["branch", "-a"], origin) == "* main"


def test_deliver_snapshot_commits_and_pushes_when_the_tree_changed(tmp_path: Path) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")

    outcome = deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Fix the loop!",
        attempt_number=2,
    )

    assert outcome.kind == COMMITTED_AND_PUSHED
    assert outcome.branch_name == "agent/34/2-fix-the-loop"
    assert outcome.commit_sha is not None

    remote_sha = run_git(["rev-parse", "agent/34/2-fix-the-loop"], origin)
    assert remote_sha == outcome.commit_sha
    assert run_git(["log", "-1", "--format=%an", remote_sha], origin) == "coding-agent"
    assert (
        run_git(["log", "-1", "--format=%ae", remote_sha], origin)
        == "999+coding-agent@users.noreply.github.com"
    )
    assert run_git(["log", "-1", "--format=%B", remote_sha], origin).strip().endswith(
        "Attempt: #34/2"
    )


def test_deliver_snapshot_reports_an_existing_remote_attempt_branch_without_committing(
    tmp_path: Path,
) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)
    branch_name = "agent/34/2-fix-the-loop"
    run_git(["branch", branch_name], origin)
    remote_sha = run_git(["rev-parse", branch_name], origin)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")

    outcome = deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Fix the loop!",
        attempt_number=2,
    )

    assert outcome.kind == REMOTE_BRANCH_COLLISION
    assert outcome.branch_name == branch_name
    assert outcome.commit_sha is None
    assert outcome.next_action == "choose a new attempt number or inspect the existing remote branch"
    assert run_git(["rev-parse", branch_name], origin) == remote_sha
    assert run_git(["rev-parse", "HEAD"], workspace.path) == workspace.base_revision
    assert run_git(["status", "--porcelain"], workspace.path) == "?? changed.txt"


def test_deliver_snapshot_reports_a_branch_created_while_pushing_as_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)
    branch_name = "agent/34/2-fix-the-loop"
    remote_sha = run_git(["rev-parse", "HEAD"], origin)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    def competing_push(
        active_workspace: Workspace, remote_url: str, active_branch_name: str, *, redact: str | None = None
    ) -> None:
        run_git(["branch", active_branch_name], origin)
        push_branch(active_workspace, remote_url, active_branch_name, redact=redact)

    monkeypatch.setattr("coding_agent.implement.delivery.push_branch", competing_push)

    outcome = delivery.deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Fix the loop!",
        attempt_number=2,
    )

    assert outcome.kind == REMOTE_BRANCH_COLLISION
    assert outcome.branch_name == branch_name
    assert outcome.commit_sha is not None
    assert run_git(["rev-parse", branch_name], origin) == remote_sha
