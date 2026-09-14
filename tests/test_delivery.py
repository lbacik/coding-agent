from __future__ import annotations

from pathlib import Path

from coding_agent.identity.startup import Identity
from coding_agent.implement.delivery import COMMITTED_AND_PUSHED, NO_CHANGE_PRODUCED, deliver_snapshot
from coding_agent.implement.git import Workspace, checkout_workspace, ensure_mirror
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
