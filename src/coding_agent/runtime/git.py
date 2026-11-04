"""Local git operations: the mirror, the workspace, and the snapshot pushes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (every Attempt
builds its workspace deterministically from the mirror), §10 (the review
snapshot commits the current tree and pushes the branch before either
reviewer starts), §13 (one branch per Attempt, ``agent/<issue>/<n>-<slug>``;
fast-forward only, never force; the remote ref is looked up with
``ls-remote`` and the branch is pushed only when it is absent or behind),
§15 (the recovery comment carries the last persisted Continuation Snapshot
only if its commit is an ancestor of the pushed head)
and §18 (author and committer are the id-based noreply address; every
commit carries the ``Attempt: #<issue>/<n>`` trailer; commits are unsigned
and never squashed; no branch is ever deleted; credentials reach git
through a credential helper rather than a remote URL, and every failure
message passes through the redaction registry).

MVP row ids: C-WSP-1, L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10, P-CMD-2,
L3-ERR-11, REC-INT-5.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from coding_agent.runtime.redaction import (
    GitCredentialHelper,
    Redactable,
    registry_from,
)


class GitFailure(Exception):
    """A git operation exited non-zero. The message has every registered
    secret replaced through the redaction registry, so it is safe to print
    or log."""


def _command_args(args: Sequence[str], helper: GitCredentialHelper | None) -> list[str]:
    """The argv for a git command: the helper config first (§18).

    Authentication rides on ``-c credential.helper=...``, never in the
    remote URL, so neither the argv, the logs nor ``.git/config`` ever
    carry the token.
    """
    command = list(args)
    if helper is not None:
        command[1:1] = helper.config_args()
    return command


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> str:
    command = _command_args(args, helper)
    registry = registry_from(redact)
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        message = registry.redact(f"{' '.join(command)!r} could not be executed: {exc}")
        raise GitFailure(message) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = registry.redact(f"{' '.join(command)!r} failed: {detail}")
        raise GitFailure(message)
    return result.stdout.strip()


def _upstream_default_branch(
    remote_url: str,
    *,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> str:
    """Return the upstream's current default branch name.

    `git remote update --prune` does not move a mirror's ``HEAD`` on older
    git versions, so the mirror's ``HEAD`` is synced explicitly from
    ``git ls-remote --symref <remote_url> HEAD`` instead of relying on the
    refresh to do it.
    """
    output = _run(
        ["git", "ls-remote", "--symref", remote_url, "HEAD"],
        redact=redact,
        helper=helper,
    )
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
    raise GitFailure(registry_from(redact).redact(message))


def ensure_mirror(
    mirror_dir: Path,
    remote_url: str,
    *,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> None:
    """A local mirror of the Target Repository: `git clone --mirror` on
    first use, `git remote update --prune` (never a re-clone) once one is
    already there."""
    if (mirror_dir / "HEAD").exists():
        _run(
            ["git", "remote", "update", "--prune"],
            cwd=mirror_dir,
            redact=redact,
            helper=helper,
        )
        branch = _upstream_default_branch(remote_url, redact=redact, helper=helper)
        _run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=mirror_dir)
        return
    mirror_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "clone", "--mirror", remote_url, str(mirror_dir)],
        redact=redact,
        helper=helper,
    )


@dataclass(frozen=True)
class Workspace:
    path: Path
    base_branch: str
    base_revision: str


def checkout_workspace(
    mirror_dir: Path,
    workspace_dir: Path,
    *,
    origin_url: str | None = None,
    branch: str | None = None,
) -> Workspace:
    """A fresh checkout from the mirror, at the mirror's current default
    branch head — the Base Revision.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (the Project
    Profile is read at the Base Revision of the configured base branch;
    nothing assumes ``main``).

    MVP row ids: P-CMD-2.

    Where ``branch`` names the Workflow Configuration's ``target.base_branch``,
    the workspace is checked out at that branch's head instead, so the Base
    Revision ``readiness`` resolves and the profile it pins come from the
    configured branch. ``None`` keeps the mirror's default branch.

    The mirror is only a local cache. When a canonical Target Repository URL
    is supplied, retain it as the workspace's ``origin`` so ordinary Git
    commands do not accidentally operate on the cache.
    """
    if branch is not None:
        base_branch = branch
    else:
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


def tree_differs_from_base(workspace_path: Path, base_revision: str | None) -> bool:
    """Whether the working tree differs from the Base Revision (the candidate diff).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (a candidate
    diff decides ``verified completion`` against ``already satisfied``, and
    whether a claim without a completed review is returned) and §10 (the
    review snapshot commits the tree, so ``HEAD`` moves off the Base
    Revision while the candidate diff stays). The diff is measured against
    ``base_revision``, not ``HEAD``: tracked changes, committed or not,
    through ``git diff --quiet <base>``, plus any untracked, non-ignored
    file. Without a Base Revision the uncommitted changes are the diff,
    which is right only until the first commit. An unreadable tree state
    never reports a clean tree: it assumes a candidate diff, so an unknown
    state cannot become ``already satisfied``.

    MVP row ids: P-DEL-3, L3-IMP-10.
    """
    try:
        if not base_revision:
            return bool(_run(["git", "status", "--porcelain"], cwd=workspace_path))
        tracked = subprocess.run(
            ["git", "diff", "--quiet", base_revision, "--"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
        )
        if tracked.returncode != 0:
            # 1 is a tracked difference; anything else is an unreadable state.
            return True
        untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace_path
        )
        return bool(untracked)
    except (GitFailure, OSError):
        return True


def commit_list_since_base(workspace_path: Path, base_revision: str, commit_sha: str) -> str:
    """The oneline commit list between the Base Revision and ``commit_sha``.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (each reviewer
    seat's concrete inputs carry the change itself: the commit list computed
    at call time against the Base Revision, so no seat has to reconstruct the
    change from the tree alone).

    MVP row ids: G-REV-2, L3-REV-1, L3-REV-2.

    Runs ``git log <base>..<commit> --oneline`` in the workspace. Returns
    ``""`` where the reviewed commit adds no commit on top of the base.
    Raises :class:`GitFailure` where the range cannot be read.
    """
    return _run(
        ["git", "log", f"{base_revision}..{commit_sha}", "--oneline", "--"],
        cwd=workspace_path,
    )


def diff_since_base(workspace_path: Path, base_revision: str, commit_sha: str) -> str:
    """The unified diff between the Base Revision and ``commit_sha``.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (each reviewer
    seat's concrete inputs carry the diff computed at call time against the
    Base Revision; the caller bounds it inline and keeps the full text behind
    a range-readable artifact).

    MVP row ids: G-REV-2, L3-REV-1, L3-REV-2.

    Runs ``git diff <base>...<commit>`` in the workspace. Returns ``""``
    where the reviewed tree matches the base. Raises :class:`GitFailure`
    where the range cannot be read.
    """
    return _run(
        ["git", "diff", f"{base_revision}...{commit_sha}", "--"],
        cwd=workspace_path,
    )


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
    unstaged and untracked entries via ``git status --porcelain`` with
    ``--untracked-files=all`` so a wholly untracked ``.github/`` directory
    (reported as ``?? .github/`` in the default collapsed form) is still
    seen as the individual ``.github/workflows/`` file it contains.

    MVP row ids: L3-ERR-9.
    """
    output = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workspace_path,
    )
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
    workspace: Workspace,
    remote_url: str,
    branch_name: str,
    *,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> None:
    """Pushes the workspace's current `HEAD` to `branch_name` on
    `remote_url` directly — the same explicit-URL push `ensure_mirror` uses,
    since the workspace was cloned from the local mirror rather than from
    the remote.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (fast-forward
    only, never force) and §18 (no branch is ever deleted; the credential
    rides on the helper, never in the remote URL).

    MVP row ids: L3-DEL-19, L3-ERR-11.

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
        helper=helper,
    )


def remote_ref_sha(
    remote_url: str,
    branch_name: str,
    *,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> str | None:
    """Return the remote sha of ``branch_name``, or ``None`` where absent (§13).

    Looks the remote ref up with ``ls-remote`` (step 3 of the write
    sequence) so the caller pushes only when it is absent or behind. A
    missing ref is ``None``; a transport failure raises :class:`GitFailure`
    with every registered secret replaced through the registry (§18,
    L3-ERR-11).
    """
    output = _run(
        [
            "git",
            "ls-remote",
            remote_url,
            f"refs/heads/{branch_name}",
        ],
        redact=redact,
        helper=helper,
    )
    if not output:
        return None
    first = output.splitlines()[0].split()[0]
    return first or None


def remote_branch_exists(
    remote_url: str,
    branch_name: str,
    *,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> bool:
    """Return whether ``branch_name`` already exists on ``remote_url``.

    ``git ls-remote`` reports a missing ref as empty output, separately
    from a transport failure, allowing delivery to preserve an existing
    attempt branch without treating the collision as a failed push.
    """
    return remote_ref_sha(remote_url, branch_name, redact=redact, helper=helper) is not None


def attempt_branch_sha(workspace_path: Path, branch_name: str) -> str | None:
    """Return the local sha of ``branch_name``, or ``None`` where absent (§17).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (recovery
    preserves pushed local commits). Reads the branch ref directly, without
    touching the checkout, so the interrupted finalization can push commits
    that live on the Attempt branch while the dirty tree stays uncommitted.

    MVP row ids: REC-SIG-1, REC-INT-1.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch_name}"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def push_ref(
    workspace_path: Path,
    remote_url: str,
    local_sha: str,
    branch_name: str,
    *,
    redact: Redactable = None,
    helper: GitCredentialHelper | None = None,
) -> None:
    """Push one local sha to the Attempt branch without touching the checkout.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (fast-forward
    only, never force) and §17 (unpushed local commits are pushed
    fast-forward; a foreign remote head means no push). The explicit
    ``<sha>:refs/heads/<branch>`` refspec pushes exactly ``local_sha``: the
    dirty tree is never committed, and the receiver still rejects a
    non-fast-forward update because no force flag is ever passed.

    MVP row ids: P-REC-4, REC-SIG-1, REC-INT-1, REC-INT-3.
    """
    _run(
        ["git", "push", "-q", remote_url, f"{local_sha}:refs/heads/{branch_name}"],
        cwd=workspace_path,
        redact=redact,
        helper=helper,
    )


def is_ancestor(
    workspace_path: Path,
    ancestor_sha: str,
    descendant_sha: str,
    *,
    redact: Redactable = None,
) -> bool:
    """Whether ``ancestor_sha`` is an ancestor of ``descendant_sha`` (§15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (at recovery the
    comment carries the last persisted Continuation Snapshot only if its
    commit is an ancestor of the pushed head; otherwise it carries none).

    MVP row ids: REC-INT-5.

    A ``merge-base --is-ancestor`` exit of 1 is a plain ``False``; any other
    failure raises :class:`GitFailure`, and the caller degrades to no
    snapshot rather than failing recovery.
    """
    registry = registry_from(redact)
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            cwd=workspace_path,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        message = registry.redact(f"'git merge-base --is-ancestor' failed: {exc}")
        raise GitFailure(message) from exc
    if result.returncode in (0, 1):
        return result.returncode == 0
    detail = (result.stderr or result.stdout).strip()
    message = registry.redact(f"'git merge-base --is-ancestor' failed: {detail}")
    raise GitFailure(message)


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


def target_repository_url(owner: str, repo: str) -> str:
    """The canonical credential-free URL for the Target Repository (§18).

    The only remote URL the Worker ever builds (L3-ERR-11): git
    authenticates through a :class:`GitCredentialHelper`, never through a
    token embedded in the URL, so no log, no ``.git/config`` and no failure
    message ever carries the token.
    """
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
