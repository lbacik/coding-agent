from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitFailure(Exception):
    """A git operation exited non-zero. The message has any credential the
    caller asked to protect already redacted, so it is safe to print or log."""


def _run(args: list[str], *, cwd: Path | None = None, redact: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"{' '.join(args)!r} failed: {detail}"
        if redact:
            message = message.replace(redact, "***")
        raise GitFailure(message)
    return result.stdout.strip()


def ensure_mirror(mirror_dir: Path, remote_url: str, *, redact: str | None = None) -> None:
    """A local mirror of the Target Repository: `git clone --mirror` on
    first use, `git remote update --prune` (never a re-clone) once one is
    already there."""
    if (mirror_dir / "HEAD").exists():
        _run(["git", "remote", "update", "--prune"], cwd=mirror_dir, redact=redact)
        return
    mirror_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--mirror", remote_url, str(mirror_dir)], redact=redact)


@dataclass(frozen=True)
class Workspace:
    path: Path
    base_branch: str
    base_revision: str


def checkout_workspace(mirror_dir: Path, workspace_dir: Path) -> Workspace:
    """A fresh checkout from the mirror, at the mirror's current default
    branch head — the Base Revision."""
    head_ref = _run(["git", "symbolic-ref", "HEAD"], cwd=mirror_dir)
    base_branch = head_ref.removeprefix("refs/heads/")

    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)

    _run(["git", "clone", "--branch", base_branch, "--", str(mirror_dir), str(workspace_dir)])
    base_revision = _run(["git", "rev-parse", "HEAD"], cwd=workspace_dir)
    return Workspace(path=workspace_dir, base_branch=base_branch, base_revision=base_revision)


def has_uncommitted_changes(workspace: Workspace) -> bool:
    """`L3-IMP-10`: the workspace was checked out clean at the Base
    Revision, so any uncommitted change in the tree now *is* the diff from
    it — no need to diff by sha."""
    return bool(_run(["git", "status", "--porcelain"], cwd=workspace.path))


def create_attempt_branch(workspace: Workspace, branch_name: str) -> None:
    """`agent/<issue>/<n>-<slug>` (`L3-DEL-19`), created locally before the
    Delivery Snapshot is committed onto it."""
    _run(["git", "checkout", "-q", "-b", branch_name], cwd=workspace.path)


def commit_delivery_snapshot(
    workspace: Workspace, *, message: str, author_name: str, author_email: str
) -> str:
    """Stages every change and commits it as the Delivery Snapshot under
    the given identity, for both author and committer. Callers check
    `has_uncommitted_changes` first (`L3-IMP-10`) — this always commits."""
    _run(["git", "add", "-A"], cwd=workspace.path)
    _run(
        [
            "git",
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=workspace.path,
    )
    return _run(["git", "rev-parse", "HEAD"], cwd=workspace.path)


def push_branch(
    workspace: Workspace, remote_url: str, branch_name: str, *, redact: str | None = None
) -> None:
    """Pushes the workspace's current `HEAD` to `branch_name` on
    `remote_url` directly — the same explicit-URL push `ensure_mirror` uses,
    since the workspace was cloned from the local mirror rather than from
    the credentialed remote."""
    _run(
        ["git", "push", "-q", remote_url, f"HEAD:refs/heads/{branch_name}"],
        cwd=workspace.path,
        redact=redact,
    )
