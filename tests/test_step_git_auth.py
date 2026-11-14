"""``LiveCommitPushGitOps`` authenticates through the Attempt's helper (§18).

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
from coding_agent.runtime.step_operations import (
    LiveCommitPushGitOps,
    LocalOperationRunner,
)

SECRET = "super-secret-token-value"
REMOTE_URL = "https://github.com/owner/repo.git"


def _helper(tmp_path: Path) -> GitCredentialHelper:
    return GitCredentialHelper(store_file=tmp_path / "git-credentials")


def _registry() -> SecretRegistry:
    return SecretRegistry({"GITHUB_TOKEN": SECRET})


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
    """The push argv carries the helper config before ``push`` (§18)."""
    helper = _helper(tmp_path)
    git = LiveCommitPushGitOps(REMOTE_URL, helper=helper, redact=_registry())
    captured: list[list[str]] = []
    _patch_run(monkeypatch, captured, _Completed(0, "", ""))
    git.push_issue_branch(tmp_path, "agent/implement/7/1-slug")
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "git"
    assert argv[1:3] == helper.config_args()
    assert "push" in argv
    assert argv.index("push") > 2
    assert REMOTE_URL in argv
    assert SECRET not in " ".join(argv)
    assert not any("x-access-token" in part for part in argv)


def test_push_ref_carries_helper_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``push_ref`` authenticates through the helper, never the URL (§18)."""
    helper = _helper(tmp_path)
    git = LiveCommitPushGitOps(REMOTE_URL, helper=helper, redact=_registry())
    captured: list[list[str]] = []
    _patch_run(monkeypatch, captured, _Completed(0, "", ""))
    git.push_ref(tmp_path, "a" * 40, "agent/implement/7/1-slug")
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
    git = LiveCommitPushGitOps(REMOTE_URL, helper=helper, redact=_registry())
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
    """A failing push raises ``GitFailure`` with the secret redacted (§18)."""
    git = LiveCommitPushGitOps(REMOTE_URL, helper=_helper(tmp_path), redact=_registry())
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch,
        captured,
        _Completed(1, "", f"fatal: could not authenticate {SECRET}"),
    )
    with pytest.raises(GitFailure) as excinfo:
        git.push_issue_branch(tmp_path, "agent/implement/7/1-slug")
    assert SECRET not in str(excinfo.value)
    assert redaction_marker("GITHUB_TOKEN") in str(excinfo.value)


def test_failing_ls_remote_redacts_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``ls-remote`` raises redacted, never leaking the token (§18)."""
    git = LiveCommitPushGitOps(REMOTE_URL, helper=_helper(tmp_path), redact=_registry())
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
    git = LiveCommitPushGitOps(REMOTE_URL, helper=_helper(tmp_path), redact=_registry())
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch, captured, _Completed(1, "", f"permission denied {SECRET}")
    )
    with pytest.raises(GitFailure) as excinfo:
        git.push_ref(tmp_path, "a" * 40, "agent/implement/7/1-slug")
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
    """``LocalOperationRunner`` builds the live push with helper+registry."""
    helper = _helper(tmp_path)
    registry = _registry()
    matrix = load_toolchain_matrix(
        {
            "schema": 1,
            "toolchains": {
                "python": {
                    "version": "3.13.9",
                    "package_manager": {"name": "uv", "version": "0.6.1"},
                },
            },
        }
    )
    runner = LocalOperationRunner(
        workspace=tmp_path,
        start_commit="a" * 40,
        matrix=matrix,
        service_env={},
        prober=_FakeProber(),
        runner=_UnusedRunner(),  # type: ignore[arg-type]
        evidence_dir=tmp_path / "evidence",
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        attempt_branch="agent/implement/7/1-slug",
        input_type="issue",
        remote_url=REMOTE_URL,
        helper=helper,
        redact=registry,
    )
    ops = runner._git_ops()
    assert isinstance(ops, LiveCommitPushGitOps)
    captured: list[list[str]] = []
    _patch_run(
        monkeypatch, captured, _Completed(0, f"{'c' * 40}\trefs/heads/br\n", "")
    )
    assert ops.remote_sha("br") == "c" * 40
    assert captured and captured[0][1:3] == helper.config_args()


def test_supervisor_passes_helper_and_registry() -> None:
    """The Attempt supervisor wires the helper and registry into the push."""
    source = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "coding_agent"
        / "worker"
        / "supervisor.py"
    ).read_text(encoding="utf-8")
    squashed = re.sub(r"\s+", "", source)
    assert (
        "LiveCommitPushGitOps(canonical_remote,helper=helper,redact=registry)"
        in squashed
    )
    assert "helper=helper" in source
    assert "redact=registry" in source
