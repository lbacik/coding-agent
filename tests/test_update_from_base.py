"""``update-from-base`` and explicit-parent ``commit-and-push`` (§10.4, §10.8).

ADR 0034: a ``pull-request`` Workflow takes its base only through a runtime
merge of the pinned Base Tip. ``update-from-base`` merges that tip without
committing (``merged`` / ``conflicted`` / ``up-to-date``); the next
``commit-and-push`` records a real merge commit whose first parent is the
Input Head and whose second is the Base Tip.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.services import SocketServiceProber
from coding_agent.profile.toolchain import load_toolchain_matrix
from coding_agent.runtime.git import GitFailure, merge_base_of
from coding_agent.runtime.graph.agent_step import (
    StepAttemptFacts,
    compose_attempt_header,
)
from coding_agent.runtime.graph.subagent import build_read_change_tool
from coding_agent.runtime.step_operations import (
    CommitPushGitOps,
    LiveCommitPushGitOps,
    LocalOperationRunner,
    run_commit_and_push,
    run_update_from_base,
)
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.workflow.schema import WorkflowStep

ATTEMPT_ID = "implement#7/1"
AUTHOR_NAME = "agent"
AUTHOR_EMAIL = "1+agent@users.noreply.github.com"
HEAD_BRANCH = "feature"


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _new_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git("init", "-qb", "main", cwd=repo)
    _git("config", "user.email", "agent@example.com", cwd=repo)
    _git("config", "user.name", "agent", cwd=repo)
    return repo


def _seed_conflict(root: Path) -> dict[str, Path | str]:
    """Base and PR change the same lines: the merge stops conflicted."""
    repo = _new_repo(root)
    (repo / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git("checkout", "-qb", HEAD_BRANCH, cwd=repo)
    (repo / "file.txt").write_text("line1\npr line\nline3\n", encoding="utf-8")
    head = _commit(repo, "the pull request")
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "file.txt").write_text("line1\nbase line\nline3\n", encoding="utf-8")
    tip = _commit(repo, "base moves on")
    _git("checkout", "-q", HEAD_BRANCH, cwd=repo)
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    return {"repo": repo, "base": base, "head": head, "tip": tip}


def _seed_clean(root: Path) -> dict[str, Path | str]:
    """Base touches another file: the merge applies cleanly."""
    repo = _new_repo(root)
    (repo / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git("checkout", "-qb", HEAD_BRANCH, cwd=repo)
    (repo / "file.txt").write_text("line1\npr line\nline3\n", encoding="utf-8")
    head = _commit(repo, "the pull request")
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "other.txt").write_text("base only\n", encoding="utf-8")
    tip = _commit(repo, "base moves on")
    _git("checkout", "-q", HEAD_BRANCH, cwd=repo)
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    return {"repo": repo, "base": base, "head": head, "tip": tip}


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "ledger.sqlite3")
    yield handle
    handle.close()


class _FakeGitOps(CommitPushGitOps):
    """A ``CommitPushGitOps`` fake over a real repo with a dict remote."""

    def __init__(self, remote: dict[str, str] | None = None) -> None:
        self.remote: dict[str, str] = dict(remote or {})
        self.race_to: str | None = None

    def is_clean(self, workspace: Path) -> bool:
        return not _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )

    def touches_workflows(self, workspace: Path) -> bool:
        from coding_agent.runtime.git import tree_touches_workflows_path

        return tree_touches_workflows_path(workspace)

    def head_sha(self, workspace: Path) -> str:
        return _git("rev-parse", "HEAD", cwd=workspace)

    def stage_all(self, workspace: Path) -> None:
        _git("add", "-A", cwd=workspace)

    def clear_merge_state(self, workspace: Path) -> None:
        from coding_agent.runtime.git import clear_merge_state

        clear_merge_state(workspace)

    def write_tree(self, workspace: Path) -> str:
        return _git("write-tree", cwd=workspace)

    def create_commit(
        self,
        workspace: Path,
        tree: str,
        parents: list[str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        from coding_agent.runtime.git import create_commit_with_parents

        return create_commit_with_parents(
            workspace,
            tree,
            parents,
            message,
            author_name=author_name,
            author_email=author_email,
        )

    def move_branch(self, workspace: Path, branch: str, sha: str) -> None:
        from coding_agent.runtime.git import update_branch_ref

        update_branch_ref(workspace, branch, sha)

    def advance_head(self, workspace: Path, sha: str) -> None:
        _git("update-ref", "HEAD", sha, cwd=workspace)

    def commit_tree(self, workspace: Path, revision: str) -> str:
        return _git("rev-parse", f"{revision}^{{tree}}", cwd=workspace)

    def diff_names(self, workspace: Path, old: str, new: str) -> list[str]:
        from coding_agent.runtime.git import diff_names_between

        return diff_names_between(workspace, old, new)

    def read_blob(
        self, workspace: Path, revision: str, path: str
    ) -> bytes | None:
        from coding_agent.runtime.git import read_blob_at_revision

        return read_blob_at_revision(workspace, revision, path)

    def read_staged_blob(self, workspace: Path, path: str) -> bytes | None:
        completed = subprocess.run(
            ["git", "show", f":{path}"],
            cwd=workspace,
            capture_output=True,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout

    def object_exists(self, workspace: Path, revision: str) -> bool:
        from coding_agent.runtime.git import object_exists

        return object_exists(workspace, revision)

    def fetch_revision(self, workspace: Path, revision: str) -> None:
        return None

    def is_ancestor(
        self, workspace: Path, ancestor: str, descendant: str
    ) -> bool:
        from coding_agent.runtime.git import is_ancestor

        return is_ancestor(workspace, ancestor, descendant)

    def merge_tip(
        self,
        workspace: Path,
        tip: str,
        *,
        author_name: str,
        author_email: str,
    ) -> int:
        from coding_agent.runtime.git import merge_tip_no_commit

        return merge_tip_no_commit(
            workspace, tip, author_name=author_name, author_email=author_email
        )

    def unmerged_paths(self, workspace: Path) -> list[str]:
        from coding_agent.runtime.git import unmerged_paths

        return unmerged_paths(workspace)

    def remote_sha(self, branch: str) -> str | None:
        return self.remote.get(branch)

    def push_issue_branch(self, workspace: Path, branch: str) -> None:
        try:
            local_sha = _git("rev-parse", "--verify", branch, cwd=workspace)
        except AssertionError:
            local_sha = self.head_sha(workspace)
        self.remote[branch] = local_sha

    def push_ref(self, workspace: Path, local_sha: str, branch: str) -> None:
        if self.race_to is not None:
            self.remote[branch] = self.race_to
            raise GitFailure("'git push' failed: non-fast-forward")
        self.remote[branch] = local_sha


def _pr_kwargs(
    seed: dict[str, Path | str],
    git: CommitPushGitOps,
    ledger: RunLedger,
) -> dict[str, object]:
    return {
        "workspace": seed["repo"],
        "attempt_id": ATTEMPT_ID,
        "input_type": "pull-request",
        "branch": HEAD_BRANCH,
        "expected_head": seed["head"],
        "author_name": AUTHOR_NAME,
        "author_email": AUTHOR_EMAIL,
        "start_commit": seed["head"],
        "git": git,
        "ledger": ledger,
    }


def _parents(repo: Path, revision: str) -> list[str]:
    return _git("rev-list", "--parents", "-n", "1", revision, cwd=repo).split()[1:]


# --- The headline end-to-end -----------------------------------------------


def test_conflicted_resolve_push_against_a_bare_remote(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Conflicted PR + base merged at the pinned tip, resolved, pushed (§10.8).

    The pushed commit has exactly two parents (the old Input Head and the
    Base Tip), fast-forwards the remote head branch, and moves the merge
    base with the Base Tip onto the Base Tip itself.
    """
    seed = _seed_conflict(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "main", HEAD_BRANCH, cwd=repo)

    class _NeverRunner(CommandRunner):
        def run(
            self,
            command: str,
            *,
            cwd: Path,
            timeout_seconds: float | None = None,
        ) -> ExecutedCommand:
            raise AssertionError("no commands run here")

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    runner = LocalOperationRunner(
        workspace=repo,
        start_commit=head,
        matrix=load_toolchain_matrix(
            {
                "schema": 2,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_managers": {"uv": {"version": "0.6.1"}},
                    },
                },
            }
        ),
        service_env={},
        prober=SocketServiceProber(),
        runner=_NeverRunner(),
        evidence_dir=evidence_dir,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        attempt_branch=HEAD_BRANCH,
        input_type="pull-request",
        head_branch=HEAD_BRANCH,
        expected_head=head,
        base_tip=tip,
        input_number=7,
        git=LiveCommitPushGitOps(str(origin)),
    )
    update = runner.run(
        WorkflowStep(id="sync", operation="update-from-base", routes={}),
        1,
        ATTEMPT_ID,
        ledger,
    )
    assert update.result == "conflicted"
    assert update.facts["conflicted_paths"] == ["file.txt"]
    assert update.facts["base_tip"] == tip

    (repo / "file.txt").write_text("line1\nresolved\nline3\n", encoding="utf-8")
    push = runner.run(
        WorkflowStep(id="push", operation="commit-and-push", routes={}),
        1,
        ATTEMPT_ID,
        ledger,
    )
    assert push.result == "pushed"
    revision = str(push.payload["revision"])

    assert _parents(repo, revision) == [head, tip]
    remote_head = _git(
        "rev-parse", f"refs/heads/{HEAD_BRANCH}", cwd=origin
    )
    assert remote_head == revision
    assert _git("merge-base", revision, tip, cwd=repo) == tip


# --- update-from-base results -----------------------------------------------


def test_merged_clean_base_records_pending(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A non-conflicting base merges cleanly and records the parent (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps(remote={HEAD_BRANCH: str(seed["head"])})
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=str(seed["tip"]),
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "merged"
    assert outcome.facts["base_tip"] == seed["tip"]
    assert (repo / "other.txt").read_text(encoding="utf-8") == "base only\n"
    pending = ledger.get_merge_pending(ATTEMPT_ID)
    assert pending is not None
    assert pending.base_tip == seed["tip"]
    assert list(pending.conflicted_paths) == []


def test_commit_after_merged_is_a_two_parent_merge_commit(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """The pending parent becomes the merge commit's second parent (§10.4)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    merged = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=tip,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert merged.result == "merged"
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert _parents(repo, outcome.revision) == [head, tip]
    assert git.remote[HEAD_BRANCH] == outcome.revision
    assert ledger.get_merge_pending(ATTEMPT_ID) is None


def test_up_to_date_changes_nothing(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """An already-merged Base Tip ends ``up-to-date`` with no pending (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps(remote={HEAD_BRANCH: str(seed["head"])})
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=str(seed["base"]),
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "up-to-date"
    assert _git("rev-parse", "HEAD", cwd=repo) == seed["head"]
    assert _git("status", "--porcelain", cwd=repo) == ""
    assert ledger.get_merge_pending(ATTEMPT_ID) is None


def test_second_update_while_pending_ends_error(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A pending parent must be consumed before another merge (§10.8)."""
    seed = _seed_conflict(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps(remote={HEAD_BRANCH: str(seed["head"])})
    first = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=str(seed["tip"]),
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert first.result == "conflicted"
    second = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=str(seed["tip"]),
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert second.result == "error"
    assert "merge already pending" in second.detail


def test_issue_workflow_update_from_base_ends_error(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """``update-from-base`` is a ``pull-request``-only operation (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps()
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="issue",
        base_tip=str(seed["tip"]),
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "error"
    assert "pull-request" in outcome.detail


def test_missing_base_tip_ends_error(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A Base Tip the workspace cannot serve ends ``error`` (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps()
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip="f" * 40,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "error"
    assert ledger.get_merge_pending(ATTEMPT_ID) is None


def test_post_claim_base_commit_is_never_merged(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Only the pinned tip merges; later base commits stay out (§10.8)."""
    seed = _seed_conflict(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "later.txt").write_text("after claim\n", encoding="utf-8")
    _commit(repo, "base moves after claim")
    _git("checkout", "-q", HEAD_BRANCH, cwd=repo)

    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=tip,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "conflicted"
    assert not (repo / "later.txt").exists()
    (repo / "file.txt").write_text("line1\nresolved\nline3\n", encoding="utf-8")
    push_kwargs = _pr_kwargs(seed, git, ledger)
    assert isinstance(push_kwargs["workspace"], Path)
    pushed = run_commit_and_push(**push_kwargs)  # type: ignore[arg-type]
    assert pushed.result == "pushed"
    assert pushed.revision is not None
    assert _parents(repo, pushed.revision) == [head, tip]
    assert not (repo / "later.txt").exists()


# --- commit-and-push guards --------------------------------------------------


def test_strict_rule_names_paths_outside_the_conflict(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Edits beyond the conflicted paths refuse the merge commit (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps(remote={HEAD_BRANCH: str(seed["head"])})
    assert (
        run_update_from_base(
            workspace=repo,
            attempt_id=ATTEMPT_ID,
            input_type="pull-request",
            base_tip=str(seed["tip"]),
            author_name=AUTHOR_NAME,
            author_email=AUTHOR_EMAIL,
            git=git,
            ledger=ledger,
        )
    ).result == "merged"
    (repo / "extra.txt").write_text("a test fix\n", encoding="utf-8")
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "error"
    assert outcome.facts["paths"] == ["extra.txt"]
    assert "outside the conflicted paths" in outcome.detail
    assert outcome.revision is not None
    assert git.remote[HEAD_BRANCH] == seed["head"]


def test_unresolved_markers_end_error_and_keep_the_parent(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Marker lines refuse the commit; the parent survives (§10.8)."""
    seed = _seed_conflict(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps(remote={HEAD_BRANCH: str(seed["head"])})
    assert (
        run_update_from_base(
            workspace=repo,
            attempt_id=ATTEMPT_ID,
            input_type="pull-request",
            base_tip=str(seed["tip"]),
            author_name=AUTHOR_NAME,
            author_email=AUTHOR_EMAIL,
            git=git,
            ledger=ledger,
        )
    ).result == "conflicted"
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "error"
    assert outcome.revision is None
    assert outcome.facts["conflicted_paths"] == ["file.txt"]
    assert git.remote[HEAD_BRANCH] == seed["head"]
    pending = ledger.get_merge_pending(ATTEMPT_ID)
    assert pending is not None
    assert pending.base_tip == seed["tip"]


def test_pending_survives_stale_head(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A ``stale-head`` creates no commit, so the parent survives (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    assert (
        run_update_from_base(
            workspace=repo,
            attempt_id=ATTEMPT_ID,
            input_type="pull-request",
            base_tip=tip,
            author_name=AUTHOR_NAME,
            author_email=AUTHOR_EMAIL,
            git=git,
            ledger=ledger,
        )
    ).result == "merged"
    # The remote moved before any commit exists: the early compare-and-swap
    # check ends `stale-head` with no commit, so the parent survives.
    git.remote[HEAD_BRANCH] = "e" * 40
    stale = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert stale.result == "stale-head"
    assert stale.revision == head
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    pending = ledger.get_merge_pending(ATTEMPT_ID)
    assert pending is not None
    assert pending.base_tip == tip
    git.remote[HEAD_BRANCH] = head
    pushed = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert pushed.result == "pushed"
    assert pushed.revision is not None
    assert _parents(repo, pushed.revision) == [head, tip]


def test_shell_merge_head_never_becomes_a_parent(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Merge state left by ``shell`` is cleared, never a parent (§10.4)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    merge = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", tip],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert _parents(repo, outcome.revision) == [head]
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert (repo / "other.txt").read_text(encoding="utf-8") == "base only\n"


def test_single_parent_pr_commit_behaves_as_before(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Without a pending parent a PR commit keeps its single parent (§10.4)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert _parents(repo, outcome.revision) == [head]


# --- .github/workflows/ authorship ------------------------------------------


def _seed_workflows_base(root: Path) -> dict[str, Path | str]:
    """The base adds a workflows file the PR never touches."""
    repo = _new_repo(root)
    (repo / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git("checkout", "-qb", HEAD_BRANCH, cwd=repo)
    (repo / "file.txt").write_text("line1\npr line\nline3\n", encoding="utf-8")
    head = _commit(repo, "the pull request")
    _git("checkout", "-q", "main", cwd=repo)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  test: {}\n", encoding="utf-8")
    tip = _commit(repo, "base adds a workflow")
    _git("checkout", "-q", HEAD_BRANCH, cwd=repo)
    return {"repo": repo, "base": base, "head": head, "tip": tip}


def test_base_workflows_change_merges_without_refusal(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Base content taken verbatim from a parent is not a refusal (§10.4)."""
    seed = _seed_workflows_base(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    assert (
        run_update_from_base(
            workspace=repo,
            attempt_id=ATTEMPT_ID,
            input_type="pull-request",
            base_tip=tip,
            author_name=AUTHOR_NAME,
            author_email=AUTHOR_EMAIL,
            git=git,
            ledger=ledger,
        )
    ).result == "merged"
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert _parents(repo, outcome.revision) == [head, tip]


def test_hand_edited_workflows_file_is_still_refused(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A hand edit under ``.github/workflows/`` refuses the push (§10.4)."""
    seed = _seed_workflows_base(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    git = _FakeGitOps(remote={HEAD_BRANCH: str(seed["head"])})
    assert (
        run_update_from_base(
            workspace=repo,
            attempt_id=ATTEMPT_ID,
            input_type="pull-request",
            base_tip=str(seed["tip"]),
            author_name=AUTHOR_NAME,
            author_email=AUTHOR_EMAIL,
            git=git,
            ledger=ledger,
        )
    ).result == "merged"
    workflow_file = repo / ".github" / "workflows" / "ci.yml"
    workflow_file.write_text(
        workflow_file.read_text(encoding="utf-8") + "# hand edit\n",
        encoding="utf-8",
    )
    outcome = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert outcome.result == "error"
    assert outcome.facts["paths"] == [".github/workflows/ci.yml"]
    assert outcome.revision is not None
    assert git.remote[HEAD_BRANCH] == seed["head"]


def test_hand_resolved_workflows_conflict_is_still_refused(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A hand-resolved conflict in a workflows file is not verbatim (§10.4)."""
    repo = _new_repo(tmp_path)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  test: old\n", encoding="utf-8")
    (repo / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _commit(repo, "base")
    _git("checkout", "-qb", HEAD_BRANCH, cwd=repo)
    (workflows / "ci.yml").write_text("jobs:\n  test: pr\n", encoding="utf-8")
    head = _commit(repo, "the pull request")
    _git("checkout", "-q", "main", cwd=repo)
    (workflows / "ci.yml").write_text("jobs:\n  test: base\n", encoding="utf-8")
    tip = _commit(repo, "base moves on")
    _git("checkout", "-q", HEAD_BRANCH, cwd=repo)

    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=tip,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "conflicted"
    assert outcome.facts["conflicted_paths"] == [".github/workflows/ci.yml"]
    (workflows / "ci.yml").write_text("jobs:\n  test: merged\n", encoding="utf-8")
    seed = {"repo": repo, "base": "", "head": head, "tip": tip}
    refused = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert refused.result == "error"
    assert refused.facts["paths"] == [".github/workflows/ci.yml"]
    assert git.remote[HEAD_BRANCH] == head


# --- Reader, facts and configuration ----------------------------------------


def test_reader_comparison_point_follows_the_merge(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """After the merge the reader compares against the Base Tip (§9.4)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    base = str(seed["base"])
    head = str(seed["head"])
    tip = str(seed["tip"])
    assert merge_base_of(repo, head, tip) == base
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    assert (
        run_update_from_base(
            workspace=repo,
            attempt_id=ATTEMPT_ID,
            input_type="pull-request",
            base_tip=tip,
            author_name=AUTHOR_NAME,
            author_email=AUTHOR_EMAIL,
            git=git,
            ledger=ledger,
        )
    ).result == "merged"
    pushed = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert pushed.result == "pushed"
    assert pushed.revision is not None
    assert merge_base_of(repo, pushed.revision, tip) == tip

    tool = build_read_change_tool(repo, base, base_tip=tip)
    text = str(tool.invoke({}))
    assert f"Change against Base Tip {tip}." in text
    assert "pr line" in text
    assert "base only" not in text


def test_reader_without_base_tip_compares_against_base_revision(
    tmp_path: Path,
) -> None:
    """Issue Attempts keep the Base Revision comparison point (§9.4)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    base = str(seed["base"])
    tool = build_read_change_tool(repo, base)
    text = str(tool.invoke({}))
    assert f"Change against Base Revision {base}." in text
    assert "pr line" in text


def _facts(**overrides: object) -> StepAttemptFacts:
    values: dict[str, object] = {
        "attempt_id": ATTEMPT_ID,
        "workflow_id": "fix",
        "input_type": "pull-request",
        "input_number": 7,
        "title": "Fix the bug",
        "body": "body",
        "base_revision": "b" * 40,
        "current_revision": "d" * 40,
    }
    values.update(overrides)
    return StepAttemptFacts(**values)  # type: ignore[arg-type]


def test_attempt_header_carries_the_base_tip_for_pull_requests() -> None:
    """The Attempt facts given to steps include the Base Tip (§7.2, §7.4)."""
    header = compose_attempt_header(_facts(base_tip="e" * 40))
    assert "Base Tip: " + "e" * 40 in header


def test_attempt_header_omits_the_base_tip_for_issue_steps() -> None:
    """An issue step's facts carry no Base Tip (§7.2, §7.4)."""
    header = compose_attempt_header(_facts(input_type="issue"))
    assert "Base Tip:" not in header


def test_config_validate_rejects_update_from_base_in_issue_workflow(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config validate`` refuses the operation in an issue Workflow (§3)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "workflows"
        / "invalid"
        / "68-update-from-base-in-issue-workflow.yaml"
    )
    assert cli.main(["config", "validate", "--config", str(fixture)]) != 0
    assert "available only in `pull-request` Workflows" in capsys.readouterr().err


# --- Without any ambient git identity ----------------------------------------


def _strip_repo_identity(repo: Path) -> None:
    """Remove the repo-local identity so only an explicit ``-c`` flag can work."""
    _git("config", "--unset", "user.name", cwd=repo)
    _git("config", "--unset", "user.email", cwd=repo)
    for key in ("user.name", "user.email"):
        probe = subprocess.run(
            ["git", "config", "--local", key],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert probe.returncode != 0, key


def _no_git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Leave the test with no usable git identity at any level (§10.8).

    ``HOME`` points at an empty directory, the global config at an empty
    file, the system config is ignored, and ``user.useConfigOnly=true``
    (forced through the environment, which the ``_isolated_git_identity``
    fixture cannot mask) stops git from guessing an identity from the
    hostname — so a ``git merge`` without an explicit ``-c user.name/...``
    fails with exit 128.
    """
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir(exist_ok=True)
    empty_global = tmp_path / "empty-gitconfig"
    empty_global.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")


def _worker_authorship(repo: Path, revision: str) -> list[str]:
    """The author/committer name and email of ``revision``."""
    return _git(
        "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", revision, cwd=repo
    ).split("\x00")


def test_merged_without_any_git_identity(
    tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``merged`` runs under the Worker identity, not ambient config (§10.8)."""
    seed = _seed_clean(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    _strip_repo_identity(repo)
    _no_git_identity(monkeypatch, tmp_path)
    git = _FakeGitOps(remote={HEAD_BRANCH: head})
    outcome = run_update_from_base(
        workspace=repo,
        attempt_id=ATTEMPT_ID,
        input_type="pull-request",
        base_tip=tip,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        git=git,
        ledger=ledger,
    )
    assert outcome.result == "merged"
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    pending = ledger.get_merge_pending(ATTEMPT_ID)
    assert pending is not None
    assert pending.base_tip == tip

    pushed = run_commit_and_push(**_pr_kwargs(seed, git, ledger))  # type: ignore[arg-type]
    assert pushed.result == "pushed"
    assert pushed.revision is not None
    assert _parents(repo, pushed.revision) == [head, tip]
    assert _worker_authorship(repo, pushed.revision) == [
        AUTHOR_NAME,
        AUTHOR_EMAIL,
        AUTHOR_NAME,
        AUTHOR_EMAIL,
    ]


def test_conflicted_without_any_git_identity(
    tmp_path: Path, ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``conflicted`` plus the resolving push need no ambient identity (§10.8).

    Drives the ``LocalOperationRunner`` ``update-from-base`` branch (the
    ``pr-review`` sync step from the incident) with a production
    ``LiveCommitPushGitOps`` under the sterile environment.
    """
    seed = _seed_conflict(tmp_path)
    repo = seed["repo"]
    assert isinstance(repo, Path)
    head = str(seed["head"])
    tip = str(seed["tip"])
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "main", HEAD_BRANCH, cwd=repo)
    _strip_repo_identity(repo)
    _no_git_identity(monkeypatch, tmp_path)

    class _NeverRunner(CommandRunner):
        def run(
            self,
            command: str,
            *,
            cwd: Path,
            timeout_seconds: float | None = None,
        ) -> ExecutedCommand:
            raise AssertionError("no commands run here")

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    runner = LocalOperationRunner(
        workspace=repo,
        start_commit=head,
        matrix=load_toolchain_matrix(
            {
                "schema": 2,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_managers": {"uv": {"version": "0.6.1"}},
                    },
                },
            }
        ),
        service_env={},
        prober=SocketServiceProber(),
        runner=_NeverRunner(),
        evidence_dir=evidence_dir,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        attempt_branch=HEAD_BRANCH,
        input_type="pull-request",
        head_branch=HEAD_BRANCH,
        expected_head=head,
        base_tip=tip,
        input_number=7,
        git=LiveCommitPushGitOps(str(origin)),
    )
    update = runner.run(
        WorkflowStep(id="sync", operation="update-from-base", routes={}),
        1,
        ATTEMPT_ID,
        ledger,
    )
    assert update.result == "conflicted"
    assert update.facts["conflicted_paths"] == ["file.txt"]
    assert update.facts["base_tip"] == tip
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    pending = ledger.get_merge_pending(ATTEMPT_ID)
    assert pending is not None
    assert pending.base_tip == tip

    (repo / "file.txt").write_text("line1\nresolved\nline3\n", encoding="utf-8")
    push = runner.run(
        WorkflowStep(id="push", operation="commit-and-push", routes={}),
        1,
        ATTEMPT_ID,
        ledger,
    )
    assert push.result == "pushed"
    revision = str(push.payload["revision"])
    assert _parents(repo, revision) == [head, tip]
    assert _worker_authorship(repo, revision) == [
        AUTHOR_NAME,
        AUTHOR_EMAIL,
        AUTHOR_NAME,
        AUTHOR_EMAIL,
    ]
