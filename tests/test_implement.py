from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.implement import skeleton
from coding_agent.implement.fingerprint import compute_fingerprint
from coding_agent.implement.git import GitFailure, checkout_workspace, ensure_mirror
from conftest import init_origin_repo, run_git

TEST_BASE_URL = "https://api.github.test"


def _commit_more(path: Path, filename: str) -> str:
    (path / filename).write_text("more\n", encoding="utf-8")
    run_git(["add", "."], path)
    run_git(["commit", "-q", "-m", f"add {filename}"], path)
    return run_git(["rev-parse", "HEAD"], path)


# --- compute_fingerprint -----------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    assert compute_fingerprint("t", "b") == compute_fingerprint("t", "b")


def test_fingerprint_changes_with_title() -> None:
    assert compute_fingerprint("t1", "b") != compute_fingerprint("t2", "b")


def test_fingerprint_changes_with_body() -> None:
    assert compute_fingerprint("t", "b1") != compute_fingerprint("t", "b2")


def test_fingerprint_does_not_collide_across_the_title_body_boundary() -> None:
    assert compute_fingerprint("a", "bc") != compute_fingerprint("ab", "c")


# --- ensure_mirror ------------------------------------------------------------


def test_ensure_mirror_clones_on_first_use(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    sha = init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"

    ensure_mirror(mirror, str(origin))

    assert (mirror / "HEAD").exists()
    assert run_git(["rev-parse", "refs/heads/main"], mirror) == sha


def test_ensure_mirror_updates_rather_than_re_cloning(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))

    # A sentinel git wouldn't touch: proves the directory survives, rather
    # than being deleted and re-cloned into, on the second call.
    sentinel = mirror / "coding-agent-test-sentinel"
    sentinel.write_text("still here\n", encoding="utf-8")

    sha2 = _commit_more(origin, "second.txt")
    ensure_mirror(mirror, str(origin))

    assert sentinel.exists()
    assert run_git(["rev-parse", "refs/heads/main"], mirror) == sha2


def test_ensure_mirror_follows_the_upstream_default_branch(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin, branch="main")
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    assert run_git(["symbolic-ref", "HEAD"], mirror) == "refs/heads/main"

    run_git(["checkout", "-q", "-b", "develop"], origin)
    _commit_more(origin, "on-develop.txt")
    run_git(["symbolic-ref", "HEAD", "refs/heads/develop"], origin)
    run_git(["branch", "-m", "main", "old-main"], origin)

    ensure_mirror(mirror, str(origin))

    assert run_git(["symbolic-ref", "HEAD"], mirror) == "refs/heads/develop"


def test_ensure_mirror_raises_and_redacts_the_credential_on_failure(tmp_path: Path) -> None:
    secret = "s3cr3t-token"
    bogus_remote = str(tmp_path / f"{secret}-does-not-exist")
    mirror = tmp_path / "mirror.git"

    with pytest.raises(GitFailure) as excinfo:
        ensure_mirror(mirror, bogus_remote, redact=secret)

    assert secret not in str(excinfo.value)


# --- checkout_workspace -------------------------------------------------------


def test_checkout_workspace_checks_out_the_mirrors_base_head(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    sha = init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace_dir = tmp_path / "workspace"

    workspace = checkout_workspace(mirror, workspace_dir)

    assert workspace.base_branch == "main"
    assert workspace.base_revision == sha
    assert (workspace_dir / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_checkout_workspace_replaces_a_stale_workspace(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "stray.txt").write_text("leftover from a previous run\n", encoding="utf-8")

    checkout_workspace(mirror, workspace_dir)

    assert not (workspace_dir / "stray.txt").exists()
    assert (workspace_dir / "README.md").exists()


# --- run_implement_skeleton: the full orchestration --------------------------


@pytest.fixture
def origin_repo(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin"
    sha = init_origin_repo(origin)
    return origin, sha


def test_run_implement_skeleton_happy_path(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    origin_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, sha = origin_repo
    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": "a body"},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is True
    assert [r.name for r in report.results] == [
        "issue fetched",
        "mirror updated",
        "workspace checked out",
        "fingerprint computed",
    ]
    assert all(r.passed for r in report.results)
    assert report.issue is not None
    assert report.issue.title == "a title"
    assert report.workspace is not None
    assert report.workspace.base_revision == sha
    assert report.fingerprint == compute_fingerprint("a title", "a body")


def test_run_implement_skeleton_stops_after_a_missing_issue(
    client: GitHubClient, requests_mock: Any, tmp_path: Path
) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        999,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is False
    assert [r.name for r in report.results] == ["issue fetched"]
    assert not (tmp_path / "mirror.git").exists()


def test_run_implement_skeleton_stops_after_an_unreachable_mirror(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        skeleton, "remote_url", lambda owner, repo, token: str(tmp_path / "no-such-repo")
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": "a body"},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is False
    assert [r.name for r in report.results] == ["issue fetched", "mirror updated"]
    assert not (tmp_path / "workspace").exists()
