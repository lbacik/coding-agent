"""The Attempt workspace git: one bound object per Attempt, one narrow remote seam.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.4
(``commit-and-push``: stage as ``git add -A``, commit with explicit parents
under the Agent Identity with the Attempt trailer, fast-forward pushes only,
compare-and-swap to a pull-request head with ``stale-head``, the
``.github/workflows/`` authorship refusal), §10.8 (``update-from-base``:
merge the pinned Base Tip without committing) and §18 (author and committer
are the id-based noreply address; the credential rides on a helper, never in
the remote URL; every failure message passes through the redaction
registry).

``AttemptWorkspaceGit`` is built once per Attempt with the Attempt
workspace, the Worker commit identity and the remote already bound in. Every
local operation is real git against the bound workspace, so no method takes
a workspace path or an author name/email: a commit built through this type
always carries the construction-bound identity — there is no code path that
can supply a different identity per call. The only seam is the remote: the
:py:class:`GitRemote` port with exactly three primitives, with two adapters —
:py:class:`GitHubRemote` (the production remote, owning the credential
helper and the redaction registry) and :py:class:`BareRepoRemote` (a bare
repository in tmp, with no credential concept).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

from coding_agent.runtime.git import (
    GitFailure,
    attempt_branch_sha,
    clear_merge_state,
    commit_tree_sha,
    create_commit_with_parents,
    diff_names_between,
    fetch_revision as _fetch_revision,
    head_branch,
    head_revision,
    is_ancestor as _is_ancestor,
    merge_tip_no_commit,
    object_exists as _object_exists,
    push_ref as _push_ref,
    read_blob_at_revision,
    remote_ref_sha,
    restore_detached_head,
    tree_touches_workflows_path,
    unmerged_paths as _unmerged_paths,
    update_branch_ref,
    write_staged_tree,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable


class GitRemote(Protocol):
    """The narrow remote seam: three primitives against the bound remote.

    Every method operates against the already-bound remote, so none takes a
    workspace, credential or redaction argument: the adapter bound those at
    construction. Resolving a local branch name to a sha and pushing it is
    not part of this port; it is the composed
    :meth:`AttemptWorkspaceGit.push_issue_branch` method.
    """

    def remote_sha(self, branch: str) -> str | None:
        """The remote sha of ``branch``, or ``None`` where absent."""
        ...

    def fetch_revision(self, revision: str) -> None:
        """Fetch one revision from the remote into the bound workspace."""
        ...

    def push_ref(self, local_sha: str, branch: str) -> None:
        """Push one local sha to one branch, fast-forward only."""
        ...


class GitHubRemote:
    """The production :class:`GitRemote`: the GitHub remote (§10.4, §18).

    Bound with the canonical credential-free remote URL plus the Attempt's
    credential helper and redaction registry. Every remote-touching call
    carries the helper's ``-c credential.helper=...`` prefix while the URL
    stays credential-free, and every failure message passes through the
    registry. Pushes are fast-forward only: no ``--force``,
    ``--force-with-lease`` or delete flag is ever passed.
    """

    def __init__(
        self,
        workspace: Path,
        remote_url: str,
        helper: GitCredentialHelper | None = None,
        redact: Redactable = None,
    ) -> None:
        self._workspace = workspace
        self._remote_url = remote_url
        self._helper = helper
        self._redact = redact

    @property
    def workspace(self) -> Path:
        """The bound Attempt workspace the remote is fetched into / pushed from."""
        return self._workspace

    @property
    def remote_url(self) -> str:
        """The canonical credential-free remote URL."""
        return self._remote_url

    def remote_sha(self, branch: str) -> str | None:
        """Look the remote ``branch`` up with ``ls-remote`` (§13)."""
        return remote_ref_sha(
            self._remote_url, branch, redact=self._redact, helper=self._helper
        )

    def fetch_revision(self, revision: str) -> None:
        """Fetch one revision from the remote into the workspace (§8)."""
        _fetch_revision(
            self._workspace,
            self._remote_url,
            revision,
            redact=self._redact,
            helper=self._helper,
        )

    def push_ref(self, local_sha: str, branch: str) -> None:
        """Push one local sha to one branch, fast-forward only (§13)."""
        _push_ref(
            self._workspace,
            self._remote_url,
            local_sha,
            branch,
            redact=self._redact,
            helper=self._helper,
        )


class BareRepoRemote:
    """The standalone :class:`GitRemote`: a bare repository in tmp.

    Bound with the bare-repository path only; implements the same three
    primitives with real git and no credential concept. The workspace the
    remote is fetched into / pushed from is bound alongside, exactly as for
    :class:`GitHubRemote`.
    """

    def __init__(self, workspace: Path, bare_repo: Path | str) -> None:
        self._workspace = workspace
        self._bare_repo = str(bare_repo)

    @property
    def workspace(self) -> Path:
        """The bound workspace the remote is fetched into / pushed from."""
        return self._workspace

    @property
    def bare_repo(self) -> str:
        """The bare repository serving as the remote."""
        return self._bare_repo

    def remote_sha(self, branch: str) -> str | None:
        """Look ``branch`` up in the bare repository (§13)."""
        return remote_ref_sha(self._bare_repo, branch)

    def fetch_revision(self, revision: str) -> None:
        """Fetch one revision from the bare repository (§8)."""
        _fetch_revision(self._workspace, self._bare_repo, revision)

    def push_ref(self, local_sha: str, branch: str) -> None:
        """Push one local sha to one branch, fast-forward only (§13)."""
        _push_ref(self._workspace, self._bare_repo, local_sha, branch)


class AttemptWorkspaceGit:
    """The Attempt's git: real local git with identity and remote bound in.

    Built once per Attempt with the Attempt workspace, the Worker commit
    identity (author name/email) and the remote. Local operations run real
    git against the bound workspace; the three remote operations delegate to
    the bound port. Commits are built with explicit parents (``commit-tree``,
    never the ambient merge state), so ``MERGE_HEAD`` left by ``shell`` is
    cleared and never becomes a parent.
    """

    def __init__(
        self,
        workspace: Path,
        author_name: str,
        author_email: str,
        remote: GitRemote,
    ) -> None:
        self._workspace = workspace
        self._author_name = author_name
        self._author_email = author_email
        self._remote = remote

    @property
    def workspace(self) -> Path:
        """The bound Attempt workspace every operation runs against."""
        return self._workspace

    @property
    def remote(self) -> GitRemote:
        """The bound remote port."""
        return self._remote

    def is_clean(self) -> bool:
        """Whether the working tree has no staged, unstaged or untracked change."""
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=self._workspace,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return False
        return not completed.stdout.strip()

    def touches_workflows(self) -> bool:
        """Whether the working tree touches ``.github/workflows/`` (§13)."""
        return tree_touches_workflows_path(self._workspace)

    def head_sha(self) -> str:
        """The workspace's current ``HEAD`` sha."""
        return head_revision(self._workspace)

    def stage_all(self) -> None:
        """Stage the workspace as ``git add -A`` would (§10.4)."""
        completed = subprocess.run(
            ["git", "add", "-A"],
            cwd=self._workspace,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise GitFailure(f"'git add -A' failed: {(completed.stderr or completed.stdout).strip()}")

    def clear_merge_state(self) -> None:
        """Clear git's own merge state without touching the working tree (§10.4, §10.8)."""
        clear_merge_state(self._workspace)

    def write_tree(self) -> str:
        """The tree object for the current index (§10.8)."""
        return write_staged_tree(self._workspace)

    def create_commit(self, tree: str, parents: list[str], message: str) -> str:
        """Create a commit object for ``tree`` with exactly ``parents`` (§10.4).

        The author and committer are the construction-bound Worker identity;
        there is no per-call identity.
        """
        return create_commit_with_parents(
            self._workspace,
            tree,
            parents,
            message,
            author_name=self._author_name,
            author_email=self._author_email,
        )

    def move_branch(self, branch: str, sha: str) -> None:
        """Move ``refs/heads/<branch>`` to ``sha`` with plumbing only."""
        update_branch_ref(self._workspace, branch, sha)

    def advance_head(self, sha: str) -> None:
        """Advance ``HEAD`` to ``sha``, restoring a detached head by ref too."""
        if head_branch(self._workspace) is None:
            restore_detached_head(self._workspace, sha=sha)
            return
        completed = subprocess.run(
            ["git", "update-ref", "HEAD", sha],
            cwd=self._workspace,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise GitFailure(
                f"'git update-ref HEAD {sha}' failed: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )

    def commit_tree(self, revision: str) -> str:
        """The tree object a revision points at (§10.4)."""
        return commit_tree_sha(self._workspace, revision)

    def diff_names(self, old: str, new: str) -> list[str]:
        """Repository-relative paths differing between two trees (§10.4)."""
        return diff_names_between(self._workspace, old, new)

    def read_blob(self, revision: str, path: str) -> bytes | None:
        """The bytes of ``path`` as committed at ``revision`` (§10.4)."""
        return read_blob_at_revision(self._workspace, revision, path)

    def read_staged_blob(self, path: str) -> bytes | None:
        """The bytes of ``path`` as staged in the index, if any (§10.8)."""
        result = subprocess.run(
            ["git", "show", f":{path}"],
            cwd=self._workspace,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def object_exists(self, revision: str) -> bool:
        """Whether ``revision`` names an object present in the workspace."""
        return _object_exists(self._workspace, revision)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ``ancestor`` is an ancestor of ``descendant`` (§15)."""
        return _is_ancestor(self._workspace, ancestor, descendant)

    def merge_tip(self, tip: str) -> int:
        """Merge ``tip`` without committing, under the bound identity (§10.8).

        Returns the merge exit code: 0 where the merge applied cleanly, 1
        where it stopped on conflicts.
        """
        return merge_tip_no_commit(
            self._workspace,
            tip,
            author_name=self._author_name,
            author_email=self._author_email,
        )

    def unmerged_paths(self) -> list[str]:
        """Repository-relative paths with unmerged index entries (§10.8)."""
        return _unmerged_paths(self._workspace)

    def remote_sha(self, branch: str) -> str | None:
        """The remote sha of ``branch``, or ``None`` where absent (§13)."""
        return self._remote.remote_sha(branch)

    def fetch_revision(self, revision: str) -> None:
        """Fetch one revision from the remote into the workspace (§8)."""
        self._remote.fetch_revision(revision)

    def push_ref(self, local_sha: str, branch: str) -> None:
        """Push one local sha to one branch, fast-forward only (§13)."""
        self._remote.push_ref(local_sha, branch)

    def push_issue_branch(self, branch: str) -> None:
        """Push the local ``branch`` ref, falling back to ``HEAD`` (§13).

        Resolves the local branch name to a sha first — pushing ``HEAD``
        where the local branch ref does not exist yet — then pushes exactly
        that sha through the remote port. Implemented once here, never per
        adapter.
        """
        local_sha = attempt_branch_sha(self._workspace, branch)
        if local_sha is None:
            local_sha = head_revision(self._workspace)
        self._remote.push_ref(local_sha, branch)
