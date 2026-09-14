from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.implement.git import (
    GitFailure,
    Workspace,
    checkout_base_revision_worktree,
    checkout_workspace,
    commit_delivery_snapshot,
    create_attempt_branch,
    ensure_mirror,
    has_uncommitted_changes,
    push_branch,
    remove_worktree,
)
from conftest import init_origin_repo, run_git


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace = checkout_workspace(mirror, tmp_path / "workspace")
    return origin, workspace.path


def test_has_uncommitted_changes_is_false_on_a_fresh_checkout(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace = checkout_workspace(mirror, tmp_path / "workspace")
    assert has_uncommitted_changes(workspace) is False


def test_has_uncommitted_changes_is_true_after_a_file_edit(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace = checkout_workspace(mirror, tmp_path / "workspace")
    (workspace.path / "new-file.txt").write_text("hi\n", encoding="utf-8")
    assert has_uncommitted_changes(workspace) is True


def test_commit_delivery_snapshot_commits_under_the_given_identity(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace = checkout_workspace(mirror, tmp_path / "workspace")
    (workspace.path / "new-file.txt").write_text("hi\n", encoding="utf-8")

    sha = commit_delivery_snapshot(
        workspace,
        message="Implement #34: do the thing\n\nAttempt: #34/1",
        author_name="coding-agent",
        author_email="1+coding-agent@users.noreply.github.com",
    )

    assert sha == run_git(["rev-parse", "HEAD"], workspace.path)
    assert run_git(["log", "-1", "--format=%an"], workspace.path) == "coding-agent"
    assert (
        run_git(["log", "-1", "--format=%ae"], workspace.path)
        == "1+coding-agent@users.noreply.github.com"
    )
    assert run_git(["log", "-1", "--format=%cn"], workspace.path) == "coding-agent"
    assert run_git(["log", "-1", "--format=%B"], workspace.path).endswith("Attempt: #34/1")


def test_push_branch_creates_the_branch_on_the_remote(tmp_path: Path) -> None:
    origin, workspace_path = _workspace(tmp_path)
    workspace = Workspace(path=workspace_path, base_branch="main", base_revision="ignored")
    (workspace.path / "new-file.txt").write_text("hi\n", encoding="utf-8")
    create_attempt_branch(workspace, "agent/34/1-do-the-thing")
    sha = commit_delivery_snapshot(
        workspace,
        message="Implement #34: do the thing\n\nAttempt: #34/1",
        author_name="coding-agent",
        author_email="1+coding-agent@users.noreply.github.com",
    )

    push_branch(workspace, str(origin), "agent/34/1-do-the-thing")

    assert run_git(["rev-parse", "agent/34/1-do-the-thing"], origin) == sha


def test_push_branch_redacts_the_credential_on_failure(tmp_path: Path) -> None:
    origin, workspace_path = _workspace(tmp_path)
    workspace = Workspace(path=workspace_path, base_branch="main", base_revision="ignored")
    secret = "s3cr3t-token"
    bogus_remote = str(tmp_path / f"{secret}-does-not-exist")

    with pytest.raises(GitFailure) as excinfo:
        push_branch(workspace, bogus_remote, "agent/34/1-x", redact=secret)

    assert secret not in str(excinfo.value)


def test_checkout_base_revision_worktree_reads_the_tree_at_the_base_revision(
    tmp_path: Path,
) -> None:
    origin, workspace_path = _workspace(tmp_path)
    base_revision = run_git(["rev-parse", "HEAD"], workspace_path)
    workspace = Workspace(path=workspace_path, base_branch="main", base_revision=base_revision)
    create_attempt_branch(workspace, "agent/34/1-do-the-thing")
    (workspace.path / "new-file.txt").write_text("hi\n", encoding="utf-8")
    commit_delivery_snapshot(
        workspace,
        message="Implement #34: do the thing\n\nAttempt: #34/1",
        author_name="coding-agent",
        author_email="1+coding-agent@users.noreply.github.com",
    )

    worktree_dir = tmp_path / "base-revision-worktree"
    checkout_base_revision_worktree(workspace, worktree_dir)

    assert run_git(["rev-parse", "HEAD"], worktree_dir) == base_revision
    assert not (worktree_dir / "new-file.txt").exists()
    assert (worktree_dir / "README.md").exists()


def test_checkout_base_revision_worktree_replaces_a_stale_directory(tmp_path: Path) -> None:
    origin, workspace_path = _workspace(tmp_path)
    base_revision = run_git(["rev-parse", "HEAD"], workspace_path)
    workspace = Workspace(path=workspace_path, base_branch="main", base_revision=base_revision)

    worktree_dir = tmp_path / "base-revision-worktree"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "stale.txt").write_text("leftover\n", encoding="utf-8")

    checkout_base_revision_worktree(workspace, worktree_dir)

    assert not (worktree_dir / "stale.txt").exists()
    assert run_git(["rev-parse", "HEAD"], worktree_dir) == base_revision


def test_remove_worktree_discards_it_from_the_repository(tmp_path: Path) -> None:
    origin, workspace_path = _workspace(tmp_path)
    base_revision = run_git(["rev-parse", "HEAD"], workspace_path)
    workspace = Workspace(path=workspace_path, base_branch="main", base_revision=base_revision)

    worktree_dir = tmp_path / "base-revision-worktree"
    checkout_base_revision_worktree(workspace, worktree_dir)
    remove_worktree(workspace, worktree_dir)

    assert not worktree_dir.exists()
    assert str(worktree_dir) not in run_git(["worktree", "list"], workspace_path)
