"""Local git operations: the mirror, the workspace, and the snapshot pushes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (every Attempt
builds its workspace deterministically from the mirror), §10 (the review
snapshot commits the current tree and pushes the branch before either
reviewer starts), §13 (one branch per Attempt, ``agent/<issue>/<n>-<slug>``;
fast-forward only, never force; the remote ref is looked up with
``ls-remote`` and the branch is pushed only when it is absent or behind)
and §18 (author and committer are the id-based noreply address; every
commit carries the ``Attempt: #<issue>/<n>`` trailer; commits are unsigned
and never squashed; no branch is ever deleted).

MVP row ids: C-WSP-1, L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitFailure(Exception):
    """A git operation exited non-zero. The message has any credential the
    caller asked to protect already redacted, so it is safe to print or log."""


def _run(args: list[str], *, cwd: Path | None = None, redact: str | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        message = f"{' '.join(args)!r} could not be executed: {exc}"
        if redact:
            message = message.replace(redact, "***")
        raise GitFailure(message) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"{' '.join(args)!r} failed: {detail}"
        if redact:
            message = message.replace(redact, "***")
        raise GitFailure(message)
    return result.stdout.strip()


def _upstream_default_branch(remote_url: str, *, redact: str | None = None) -> str:
    """Return the upstream's current default branch name.

    `git remote update --prune` does not move a mirror's ``HEAD`` on older
    git versions, so the mirror's ``HEAD`` is synced explicitly from
    ``git ls-remote --symref <remote_url> HEAD`` instead of relying on the
    refresh to do it.
    """
    output = _run(["git", "ls-remote", "--symref", remote_url, "HEAD"], redact=redact)
    for line in output.splitlines():
        if line.startswith("ref:"):
            rest = line[len("ref:") :].strip()
            target = rest.split()[0] if rest else ""
            if target.startswith("refs/heads/"):
                branch = target.removeprefix("refs/heads/")
                if branch:
                    return branch
    message = "'git ls-remote --symref <remote> HEAD' could not determine the upstream HEAD"
    if output:
        message += f": {output!r}"
    if redact:
        message = message.replace(redact, "***")
    raise GitFailure(message)


def ensure_mirror(mirror_dir: Path, remote_url: str, *, redact: str | None = None) -> None:
    """A local mirror of the Target Repository: `git clone --mirror` on
    first use, `git remote update --prune` (never a re-clone) once one is
    already there."""
    if (mirror_dir / "HEAD").exists():
        _run(["git", "remote", "update", "--prune"], cwd=mirror_dir, redact=redact)
        branch = _upstream_default_branch(remote_url, redact=redact)
        _run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=mirror_dir)
        return
    mirror_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--mirror", remote_url, str(mirror_dir)], redact=redact)


@dataclass(frozen=True)
class Workspace:
    path: Path
    base_branch: str
    base_revision: str


def checkout_workspace(
    mirror_dir: Path, workspace_dir: Path, *, origin_url: str | None = None
) -> Workspace:
    """A fresh checkout from the mirror, at the mirror's current default
    branch head — the Base Revision.

    The mirror is only a local cache. When a canonical Target Repository URL
    is supplied, retain it as the workspace's ``origin`` so ordinary Git
    commands do not accidentally operate on the cache.
    """
    head_ref = _run(["git", "symbolic-ref", "HEAD"], cwd=mirror_dir)
    base_branch = head_ref.removeprefix("refs/heads/")

    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)

    _run(["git", "clone", "--branch", base_branch, "--", str(mirror_dir), str(workspace_dir)])
    if origin_url is not None:
        _run(["git", "remote", "set-url", "origin", origin_url], cwd=workspace_dir)
    base_revision = _run(["git", "rev-parse", "HEAD"], cwd=workspace_dir)
    return Workspace(path=workspace_dir, base_branch=base_branch, base_revision=base_revision)


def has_uncommitted_changes(workspace: Workspace) -> bool:
    """`L3-IMP-10`: the workspace was checked out clean at the Base
    Revision, so any uncommitted change in the tree now *is* the diff from
    it — no need to diff by sha."""
    return bool(_run(["git", "status", "--porcelain"], cwd=workspace.path))


def candidate_diff_signature(workspace: Workspace) -> str:
    """Return a stable digest of the candidate tree relative to its Base Revision.

    Git's ordinary diff omits untracked files, so status and the content of
    every untracked file are included beside the binary diff. The digest is a
    reference for a Run Ledger progress event, not a replacement for the
    Delivery Snapshot.
    """
    status = _run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace.path)
    diff = _run(["git", "diff", "--binary", workspace.base_revision], cwd=workspace.path)
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=workspace.path)
    untracked_content: list[bytes] = []
    for raw_path in untracked.split("\0"):
        if not raw_path:
            continue
        path = workspace.path / raw_path
        try:
            untracked_content.append(raw_path.encode("utf-8") + b"\0" + path.read_bytes())
        except OSError:
            untracked_content.append(raw_path.encode("utf-8") + b"\0<unreadable>")
    return hashlib.sha256(
        status.encode("utf-8")
        + b"\0"
        + diff.encode("utf-8")
        + b"\0"
        + b"\0".join(untracked_content)
    ).hexdigest()


def create_attempt_branch(workspace: Workspace, branch_name: str) -> None:
    """`agent/<issue>/<n>-<slug>` (`L3-DEL-19`), created locally before the
    Delivery Snapshot is committed onto it."""
    _run(["git", "checkout", "-q", "-b", branch_name], cwd=workspace.path)


def commit_delivery_snapshot(
    workspace: Workspace, *, message: str, author_name: str, author_email: str
) -> str:
    """Stages every change and commits it as the Delivery Snapshot under
    the given identity, for both author and committer. Callers check
    `has_uncommitted_changes` first (`L3-IMP-10`) — this always commits.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (author and
    committer are the id-based noreply address; the ``Attempt:
    #<issue>/<n>`` trailer is present; unsigned and never squashed).

    MVP row ids: L3-DEL-20.
    """
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
            "--no-gpg-sign",
            "-m",
            message,
        ],
        cwd=workspace.path,
    )
    return _run(["git", "rev-parse", "HEAD"], cwd=workspace.path)


WORKFLOWS_PATH_PREFIX = ".github/workflows/"
WORKFLOWS_PATH_ROOT = ".github/workflows"


def _status_paths(status_output: str) -> list[str]:
    """Parse ``git status --porcelain`` output into repository-relative paths."""
    paths: list[str] = []
    for line in status_output.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.strip()
        # Handle rename/copy entries of the form ``old -> new``.
        for part in rest.split(" -> "):
            candidate = part.strip().strip('"')
            if candidate:
                paths.append(candidate)
    return paths


def _is_workflows_path(path: str) -> bool:
    """Whether ``path`` is the workflows directory or a file under it (§13)."""
    normalized = path.strip().strip('"')
    return normalized == WORKFLOWS_PATH_ROOT or normalized.startswith(WORKFLOWS_PATH_PREFIX)


def tree_touches_workflows_path(workspace_path: Path) -> bool:
    """Whether the working tree touches ``.github/workflows/`` (§13, L3-ERR-9).

    Classified by write kind and target path, never by error string: the
    caller refuses the push itself where this is true. Covers staged,
    unstaged and untracked entries via ``git status --porcelain``.
    """
    output = _run(["git", "status", "--porcelain"], cwd=workspace_path)
    return any(_is_workflows_path(path) for path in _status_paths(output))


def commit_touches_workflows_path(workspace_path: Path, revision: str = "HEAD") -> bool:
    """Whether ``revision`` touches ``.github/workflows/`` (§13, L3-ERR-9)."""
    output = _run(
        ["git", "show", "--name-only", "--pretty=format:", revision],
        cwd=workspace_path,
    )
    return any(
        _is_workflows_path(line.strip())
        for line in output.splitlines()
        if line.strip()
    )


def local_branch_exists(workspace_path: Path, branch_name: str) -> bool:
    """Whether ``branch_name`` already exists in the workspace repository."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_attempt_branch(workspace: Workspace, branch_name: str) -> None:
    """Check out ``branch_name``, creating it where it does not yet exist.

    Contract §13 and §18 (one branch per Attempt, never deleted, never
    force-moved): this only moves ``HEAD`` onto the Attempt's own branch and
    never deletes a branch.
    """
    if current_branch(workspace.path) == branch_name:
        return
    if local_branch_exists(workspace.path, branch_name):
        _run(["git", "checkout", "-q", branch_name], cwd=workspace.path)
        return
    _run(["git", "checkout", "-q", "-b", branch_name], cwd=workspace.path)


def checkout_base_revision_worktree(workspace: Workspace, worktree_dir: Path) -> Path:
    """A throwaway `git worktree`, detached at `workspace.base_revision`, added
    into `workspace`'s own repository rather than a second full clone (issue
    #36): the Base Revision commit is already there, since `workspace` was
    cloned by branch rather than shallow. Callers pair this with
    `remove_worktree` once the comparison is done."""
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "worktree", "add", "--detach", str(worktree_dir), workspace.base_revision],
        cwd=workspace.path,
    )
    return worktree_dir


def remove_worktree(workspace: Workspace, worktree_dir: Path) -> None:
    """Discards a worktree `checkout_base_revision_worktree` added, freeing
    `workspace`'s repository of it."""
    _run(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=workspace.path)


def push_branch(
    workspace: Workspace, remote_url: str, branch_name: str, *, redact: str | None = None
) -> None:
    """Pushes the workspace's current `HEAD` to `branch_name` on
    `remote_url` directly — the same explicit-URL push `ensure_mirror` uses,
    since the workspace was cloned from the local mirror rather than from
    the credentialed remote.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (fast-forward
    only, never force) and §18 (no branch is ever deleted).

    MVP row ids: L3-DEL-19.

    Fast-forward only: no ``--force``, ``--force-with-lease`` or delete
    flag is ever passed, and the capability is never held.
    """
    _run(
        [
            "git",
            "push",
            "-q",
            remote_url,
            f"HEAD:refs/heads/{branch_name}",
        ],
        cwd=workspace.path,
        redact=redact,
    )


def remote_ref_sha(
    remote_url: str, branch_name: str, *, redact: str | None = None
) -> str | None:
    """Return the remote sha of ``branch_name``, or ``None`` where absent (§13).

    Looks the remote ref up with ``ls-remote`` (step 3 of the write
    sequence) so the caller pushes only when it is absent or behind. A
    missing ref is ``None``; a transport failure raises :class:`GitFailure`.
    """
    args = [
        "git",
        "ls-remote",
        remote_url,
        f"refs/heads/{branch_name}",
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"{' '.join(args)!r} failed: {detail}"
        if redact:
            message = message.replace(redact, "***")
        raise GitFailure(message)
    output = result.stdout.strip()
    if not output:
        return None
    first = output.splitlines()[0].split()[0]
    return first or None


def remote_branch_exists(remote_url: str, branch_name: str, *, redact: str | None = None) -> bool:
    """Return whether ``branch_name`` already exists on ``remote_url``.

    ``git ls-remote`` reports a missing ref as empty output, separately
    from a transport failure, allowing delivery to preserve an existing
    attempt branch without treating the collision as a failed push.
    """
    return remote_ref_sha(remote_url, branch_name, redact=redact) is not None


def rebuild_workspace(workspace_path: Path, *, ref: str) -> None:
    """Rebuild a workspace deterministically from the local mirror's history.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (every Attempt
    builds its workspace deterministically: ``reset --hard`` to its start
    commit, then ``clean -fdx``).

    MVP row ids: C-WSP-1.
    """
    _run(["git", "reset", "-q", "--hard", ref], cwd=workspace_path)
    _run(["git", "clean", "-q", "-fdx"], cwd=workspace_path)


def head_revision(workspace_path: Path) -> str:
    """Return the workspace's current ``HEAD`` sha (the Base Revision at
    `readiness` time, before any Attempt commit exists)."""
    return _run(["git", "rev-parse", "HEAD"], cwd=workspace_path)


def current_branch(workspace_path: Path) -> str:
    """Return the workspace's current branch name, or ``HEAD`` when detached."""
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace_path)


def remote_url(owner: str, repo: str, token: str) -> str:
    """The credentialed push/fetch URL for the Target Repository."""
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def target_repository_url(owner: str, repo: str) -> str:
    """The credential-free canonical URL retained in a workspace's Git config."""
    return f"https://github.com/{owner}/{repo}.git"


def read_file_at_revision(workspace_path: Path, revision: str, relpath: str) -> str | None:
    """Read ``relpath`` as committed at ``revision``, never from the working
    tree.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (the Project
    Profile is read at the Base Revision and pinned as the Validation
    Contract: an edit to the profile on the Attempt's branch never changes
    the pinned contract).

    Returns ``None`` where the path does not exist at that revision. Only
    the working-tree read is avoided — no prose file is consulted here.
    """
    try:
        return _run(["git", "show", f"{revision}:{relpath}"], cwd=workspace_path)
    except GitFailure as exc:
        message = str(exc)
        if "does not exist" in message or "exists on disk, but not in" in message:
            return None
        raise
