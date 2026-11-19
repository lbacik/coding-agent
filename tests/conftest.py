import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest import mock

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.github.client import GitHubClient
from coding_agent.operator.paths import OPERATOR_SOCKET_ENV_VAR
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
def short_socket_dir() -> Iterator[Path]:
    """A short, private directory for the operator control socket.

    macOS limits ``sun_path`` to 104 bytes while ``tmp_path`` lives under
    a long ``$TMPDIR`` (``/private/var/folders/...``), so a Worker bound at
    ``<tmp_path>/state/operator/agent.sock`` fails with ``AF_UNIX path too
    long``. ``mkdtemp`` under ``/tmp`` keeps the socket path short; it
    creates the directory 0700 and owned by the user, which the listener's
    directory checks require. Removed after the test, even on failure.
    """
    directory = Path(tempfile.mkdtemp(prefix="ca-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def short_work_dir(short_socket_dir: Path) -> Path:
    """A short base directory for a Worker on the default socket layout.

    Tests that unset ``CODING_AGENT_OPERATOR_SOCKET`` bind at
    ``<state_dir>/operator/agent.sock``, so their ``state_dir`` (and the
    config, bundle and ledger files around it) must live under
    ``short_socket_dir`` to stay within the macOS 104-byte ``sun_path``
    limit. Removed with ``short_socket_dir`` after the test.
    """
    work_dir = short_socket_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


@pytest.fixture(autouse=True)
def _silent_container_log() -> Iterator[None]:
    """Keep Worker container-log lines off unless a test opts in.

    The ledger/store/turn print paths share the process with ``agentctl``
    and the offline readers, which own stdout for contract JSON; only the
    Worker Supervisor enables the log. Reset the process-wide flag around
    every test so one test enabling it never leaks lines into another.
    """
    from coding_agent.operator.runlog import set_container_log_enabled

    set_container_log_enabled(False)
    try:
        yield
    finally:
        set_container_log_enabled(False)


@pytest.fixture(autouse=True)
def _short_operator_socket(
    short_socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point both Worker and ``agentctl`` at a short operator socket path.

    ``worker_socket_path`` and ``client_socket_path`` both honour
    ``CODING_AGENT_OPERATOR_SOCKET``, so every test driving ``run_worker``
    binds and dials ``<short dir>/agent.sock`` whatever the platform's temp
    directory is. Tests exercising the default ``<state_dir>/operator``
    layout, or pointing the variable elsewhere, override this with
    ``monkeypatch.delenv``/``setenv``.
    """
    monkeypatch.setenv(OPERATOR_SOCKET_ENV_VAR, str(short_socket_dir / "agent.sock"))


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


def _commit_all(path: Path, message: str) -> str:
    """Stage everything and commit; return the new HEAD sha."""
    run_git(["add", "."], path)
    run_git(["commit", "-q", "-m", message], path)
    return run_git(["rev-parse", "HEAD"], path)


def init_origin_repo(path: Path, *, branch: str = "main") -> str:
    """A real local git repository standing in for the Target Repository in
    mirror/checkout tests — no network needed, since a local path is a
    perfectly good git remote. Returns the sha of its one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    run_git(["config", "user.email", "agent@example.test"], path)
    run_git(["config", "user.name", "agent"], path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    return _commit_all(path, "init")


MINIMAL_VALIDATION_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/all.xml"
  test_targeted: "{evidence_dir}/targeted.xml"
checks: none
"""
"""The smallest profile the readiness rules accept as ``Ready``.

Service-free (no probing) with the python/uv toolchain the worker test
matrices declare, so worker end-to-end fixtures pinning this profile get
a Validation Contract at the Attempt commit. The Attempt run requires
one: ``AgentStepDeps.profile`` has no default.
"""


def commit_validation_profile(path: Path) -> str:
    """Commit the minimal Validation Contract into an origin repo fixture.

    Writes ``docs/agents/project-profile.yml`` (the canonical location the
    Attempt run pins) and commits it. Returns the new HEAD sha.
    """
    profile_path = path / "docs" / "agents" / "project-profile.yml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(MINIMAL_VALIDATION_PROFILE, encoding="utf-8")
    return _commit_all(path, "add validation contract")
