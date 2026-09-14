import subprocess
from pathlib import Path

import pytest

from coding_agent.github.client import GitHubClient

TEST_BASE_URL = "https://api.github.test"


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL)


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def init_origin_repo(path: Path, *, branch: str = "main") -> str:
    """A real local git repository standing in for the Target Repository in
    mirror/checkout tests — no network needed, since a local path is a
    perfectly good git remote. Returns the sha of its one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    run_git(["config", "user.email", "agent@example.test"], path)
    run_git(["config", "user.name", "agent"], path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(["add", "."], path)
    run_git(["commit", "-q", "-m", "init"], path)
    return run_git(["rev-parse", "HEAD"], path)
