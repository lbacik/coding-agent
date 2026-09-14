import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.github.client import GitHubClient

TEST_BASE_URL = "https://api.github.test"


class FakeChatModel:
    """A scripted `InvokableToolModel`: each `invoke` pops the next queued
    `AIMessage`, recording every message list it was called with. Shared by
    every test that drives `implement.loop.run_tool_loop` or a command built
    on it, so the fake's shape tracks ADR 0010's contract in one place.

    `tests/test_provider.py`'s own `FakeChatModel` is deliberately separate:
    it also injects raised exceptions and a `bind_tools` failure, for
    `invoke_with_retry`/capability-assertion tests this one has no need of.
    """

    def __init__(self, outcomes: list[AIMessage]) -> None:
        self._outcomes = list(outcomes)
        self.invocations: list[tuple[BaseMessage, ...]] = []

    def bind_tools(self, tools: Sequence[BaseTool]) -> "FakeChatModel":
        return self

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        assert isinstance(input, Sequence)
        self.invocations.append(tuple(input))  # type: ignore[arg-type]
        return self._outcomes.pop(0)


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
