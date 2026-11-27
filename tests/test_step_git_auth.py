"""``GitHubRemote``/``AttemptWorkspaceGit`` authenticate through the Attempt's helper (§18).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §18 (one
redaction registry; git authenticates through a credential helper, never
through the URL). Every remote-touching call — ``push_issue_branch``,
``push_ref`` and ``remote_sha`` (``ls-remote``) — carries the helper's
``-c credential.helper=...`` prefix while the remote URL stays the
canonical credential-free form, and every failure message passes through
the Attempt's redaction registry.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coding_agent.profile.toolchain import load_toolchain_matrix
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.redaction import (
    GitCredentialHelper,
    SecretRegistry,
    redaction_marker,
)
from coding_agent.runtime.step_operations import LocalOperationRunner
from coding_agent.runtime.workspace_git import (
    AttemptWorkspaceGit,
    BareRepoRemote,
    GitHubRemote,
)

SECRET = "super-secret-token-value"
REMOTE_URL = "https://github.com/owner/repo.git"


def _helper(tmp_path: Path) -> GitCredentialHelper:
    return GitCredentialHelper(store_file=tmp_path / "git-credentials")


def _registry() -> SecretRegistry:
    return SecretRegistry({"GITHUB_TOKEN": SECRET})


def _workspace_git(
    workspace: Path, helper: GitCredentialHelper, registry: SecretRegistry
) -> AttemptWorkspaceGit:
    """An ``AttemptWorkspaceGit`` bound to the GitHub remote with helper+registry."""
    return AttemptWorkspaceGit(
        workspace,
        "agent",
        "1+agent@users.noreply.github.com",
        GitHubRemote(workspace, REMOTE_URL, helper=helper, redact=registry),
    )


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, captured: list[list[str]], completed: _Completed
) -> None:
    def fake(
        command: list[str], **kwargs: object
    ) -> _Completed:
        captured.append(list(command))
        return completed

    monkeypatch.setattr(subprocess, "run", fake)


def test_push_issue_branch_carries_helper_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push argv carries the helper config before ``push`` (§18).

    ``push_issue_branch`` resolves the local branch ref first (plain
    ``rev-parse``, no remote touch), then pushes exactly that sha: the
    remote-touching push carries the helper prefix while the credential-free
    URL stays the remote.
    """
    helper = _helper(tmp_path)
    git = _workspace_git(tmp_path, helper, _registry())
    captured: list[list[str]] = []

    def fake(command: list[str], **kwargs: object) -> _Completed:
        captured.append(list(command))
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return _Completed(0, f"{'a' * 40}\n", "")
        return _Completed(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    git.push_issue_branch("agent/implement/7/1-slug")
    pushes = [argv for argv in captured if "push" in argv]
    assert len(pushes) == 1
    argv = pushes[0]
    assert argv[0] == "git"
    assert argv[1:3] == helper.config_args()
    assert argv.index("push") > 2
    assert REMOTE_URL in argv
    assert f"{'a' * 40}:refs/heads/agent/implement/7/1-slug" in argv
    assert all(SECRET not in " ".join(argv) for argv in captured)
    assert not any(
        "x-access-token" in part for argv in captured for part in argv
    )
    assert "--force" not in argv
    assert "--force-with-lease" not in argv
    assert "-d" not in argv
    assert "--delete" not in argv


def test_push_ref_carries_helper_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``push_ref`` authenticates through the helper, never the URL (§18)."""
    helper = _helper(tmp_path)
    git = _workspace_git(tmp_path, helper, _registry())
    captured: list[list[str]] = []
    _patch_run(monkeypatch, captured, _Completed(0, "", ""))
    git.push_ref("a" * 40, "agent/implement/7/1-slug")
    assert len(captured) == 1
    argv = captured[0]
    assert argv[1:3] == helper.config_args()
    assert "push" in argv
    assert argv.index("push") > 2
    assert REMOTE_URL in argv
    assert SECRET not in " ".join(argv)


def test_remote_sha_carries_helper_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``ls-remote`` lookup runs authenticated through the helper (§18)."""
    helper = _helper(tmp_path)
    git = _workspace_git(tmp_path, helper, _registry())
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch, captured, _Completed(0, f"{'b' * 40}\trefs/heads/br\n", "")
    )
    sha = git.remote_sha("br")
    assert sha == "b" * 40
    assert len(captured) == 1
    argv = captured[0]
    assert argv[1:3] == helper.config_args()
    assert "ls-remote" in argv
    assert REMOTE_URL in argv
    assert SECRET not in " ".join(argv)


def test_failing_push_redacts_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing push raises ``GitFailure`` with the secret redacted (§18).

    The local branch-ref resolution succeeds; only the remote push fails,
    so the redaction assertion covers the push failure it names.
    """
    git = _workspace_git(tmp_path, _helper(tmp_path), _registry())
    captured: list[list[str]] = []

    def fake(command: list[str], **kwargs: object) -> _Completed:
        captured.append(list(command))
        if "push" in command:
            return _Completed(1, "", f"fatal: could not authenticate {SECRET}")
        if command[:2] == ["git", "rev-parse"]:
            return _Completed(0, f"{'a' * 40}\n", "")
        return _Completed(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(GitFailure) as excinfo:
        git.push_issue_branch("agent/implement/7/1-slug")
    assert SECRET not in str(excinfo.value)
    assert redaction_marker("GITHUB_TOKEN") in str(excinfo.value)


def test_failing_ls_remote_redacts_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``ls-remote`` raises redacted, never leaking the token (§18)."""
    git = _workspace_git(tmp_path, _helper(tmp_path), _registry())
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch,
        captured,
        _Completed(1, "", f"fatal: unable to access {SECRET}: denied"),
    )
    with pytest.raises(GitFailure) as excinfo:
        git.remote_sha("agent/implement/7/1-slug")
    assert SECRET not in str(excinfo.value)
    assert redaction_marker("GITHUB_TOKEN") in str(excinfo.value)


def test_failing_push_ref_redacts_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``push_ref`` raises ``GitFailure`` with the secret redacted."""
    git = _workspace_git(tmp_path, _helper(tmp_path), _registry())
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch, captured, _Completed(1, "", f"permission denied {SECRET}")
    )
    with pytest.raises(GitFailure) as excinfo:
        git.push_ref("a" * 40, "agent/implement/7/1-slug")
    assert SECRET not in str(excinfo.value)
    assert redaction_marker("GITHUB_TOKEN") in str(excinfo.value)


class _FakeProber:
    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        return True


class _UnusedRunner:
    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> object:
        raise AssertionError("unused")


def test_runner_fallback_forwards_helper_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LocalOperationRunner`` carries the helper+registry on its bound git."""
    helper = _helper(tmp_path)
    registry = _registry()
    matrix = load_toolchain_matrix(
        {
            "schema": 2,
            "toolchains": {
                "python": {
                    "version": "3.13.9",
                    "package_managers": {"uv": {"version": "0.6.1"}},
                },
            },
        }
    )
    runner = LocalOperationRunner(
        git=_workspace_git(tmp_path, helper, registry),
        start_commit="a" * 40,
        matrix=matrix,
        service_env={},
        prober=_FakeProber(),
        runner=_UnusedRunner(),  # type: ignore[arg-type]
        evidence_dir=tmp_path / "evidence",
        attempt_branch="agent/implement/7/1-slug",
        input_type="issue",
    )
    assert isinstance(runner.git, AttemptWorkspaceGit)
    assert isinstance(runner.git.remote, GitHubRemote)
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch, captured, _Completed(0, f"{'c' * 40}\trefs/heads/br\n", "")
    )
    assert runner.git.remote_sha("br") == "c" * 40
    assert captured and captured[0][1:3] == helper.config_args()


def test_supervisor_passes_helper_and_registry() -> None:
    """The Attempt run wires the helper and registry into the bound git."""
    worker_dir = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "coding_agent"
        / "worker"
    )
    source = (worker_dir / "attempt_run.py").read_text(encoding="utf-8")
    squashed = re.sub(r"\s+", "", source)
    assert (
        "GitHubRemote(workspace.path,canonical_remote,helper=helper,redact=registry)"
        in squashed
    )
    assert "AttemptWorkspaceGit(" in source
    assert "helper=helper" in source
    assert "redact=registry" in source
    supervisor_source = (worker_dir / "supervisor.py").read_text(encoding="utf-8")
    assert "run_attempt" in supervisor_source


def test_commits_carry_the_construction_bound_identity(tmp_path: Path) -> None:
    """A commit built through the bound object ignores ambient config (§18)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    initialised = subprocess.run(
        ["git", "init", "-q"], cwd=repo, capture_output=True, text=True
    )
    assert initialised.returncode == 0, initialised.stderr
    for key, value in (
        ("user.name", "ambient"),
        ("user.email", "ambient@example.com"),
    ):
        configured = subprocess.run(
            ["git", "config", key, value],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert configured.returncode == 0, configured.stderr
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    staged = subprocess.run(
        ["git", "add", "-A"], cwd=repo, capture_output=True, text=True
    )
    assert staged.returncode == 0, staged.stderr
    seeded = subprocess.run(
        ["git", "commit", "-qm", "seed"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert seeded.returncode == 0, seeded.stderr
    git = AttemptWorkspaceGit(
        repo,
        "agent",
        "1+agent@users.noreply.github.com",
        BareRepoRemote(repo, tmp_path / "unused.git"),
    )
    (repo / "next.txt").write_text("next\n", encoding="utf-8")
    git.stage_all()
    tree = git.write_tree()
    head = git.head_sha()
    rev = git.create_commit(tree, [head], "bound commit")
    git.advance_head(rev)
    logged = subprocess.run(
        ["git", "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert logged.returncode == 0, logged.stderr
    assert logged.stdout.strip().split("\x00") == [
        "agent",
        "1+agent@users.noreply.github.com",
        "agent",
        "1+agent@users.noreply.github.com",
    ]
    assert git.workspace is repo
