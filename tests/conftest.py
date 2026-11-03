import os
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest import mock

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.github.client import GitHubClient
from support.skill_bundle import write_skill_bundle

TEST_BASE_URL = "https://api.github.test"


class FakeChatModel:
    """A scripted `InvokableToolModel`: each `invoke` pops the next queued
    `AIMessage`, recording every message list it was called with. Shared by
    every test that drives `runtime.graph.step.run_implement_step` or a
    command built on it, so the fake's shape tracks ADR 0010's contract in
    one place.

    `tests/test_provider.py`'s own `FakeChatModel` is deliberately separate:
    it also injects raised exceptions and a `bind_tools` failure, for
    `invoke_with_retry`/capability-assertion tests this one has no need of.
    """

    def __init__(self, outcomes: list[AIMessage]) -> None:
        self._outcomes = list(outcomes)
        self.invocations: list[tuple[BaseMessage, ...]] = []

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> "FakeChatModel":
        _ = tool_choice
        return self

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        assert isinstance(input, Sequence)
        self.invocations.append(tuple(input))  # type: ignore[arg-type]
        return self._outcomes.pop(0)


@pytest.fixture(scope="session")
def _test_gitconfig(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("git-home") / "gitconfig"
    path.write_text("[user]\n\tname = agent\n\temail = agent@example.test\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_environ() -> Iterator[None]:
    """Every test gets a private copy of the process environment.

    Helpers under test (``load_dotenv``, ``load_required_env_file`` and
    ``load_application_environment`` via ``cli.main``) write straight into
    ``os.environ`` with ``setdefault``, so without isolation a value loaded
    from one test's env file survives into every later test module. In
    particular ``monkeypatch.delenv(name, raising=False)`` on an unset
    variable records no undo entry, so the leaked value is never restored.
    ``mock.patch.dict`` snapshots ``os.environ`` on entry and restores it on
    exit however the test ends, which a manual per-variable teardown at the
    end of the test cannot guarantee.
    """
    with mock.patch.dict(os.environ):
        yield


@pytest.fixture(autouse=True)
def _isolated_git_identity(_test_gitconfig: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git process a test starts reads this file in place of the
    developer's or CI runner's `~/.gitconfig`, so a `git commit` in any
    repository a test creates or clones has an identity even where the host
    has none (a GitHub Actions runner fails such a commit with exit 128).

    This is the global level, the lowest one, so a repository's local config
    and the Worker's own `git -c user.name=... -c user.email=...` still win
    over it. `GIT_AUTHOR_*`/`GIT_COMMITTER_*` would override those, which is
    why they are not set here."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_test_gitconfig))


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """A synthetic Skill Bundle under ``tmp_path``; never the host's
    ``~/.agents/skills``, which CI runners do not have."""
    return write_skill_bundle(tmp_path)


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
