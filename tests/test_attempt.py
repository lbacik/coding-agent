from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import Identity
from coding_agent.implement import attempt
from coding_agent.implement.delivery import COMMITTED_AND_PUSHED, NO_CHANGE_PRODUCED
from coding_agent.provider.config import DEFAULT_COMPACTION_THRESHOLDS, PINNED_MODELS
from conftest import FakeChatModel, init_origin_repo, run_git

TEST_BASE_URL = "https://api.github.test"
_PIN = PINNED_MODELS["anthropic"]
IDENTITY = Identity(account_id=999, login="coding-agent")

_IMPLEMENT_SKILL_MD = "# implement\n\nWork the ticket.\n"
_TDD_SKILL_MD = "# tdd\n\nRed, green, refactor.\n"
_CODEBASE_DESIGN_SKILL_MD = "# codebase-design\n\nDeep modules.\n"
_TDD_TESTS_MD = "# tests.md\n"
_TDD_MOCKING_MD = "# mocking.md\n"
_CODEBASE_DESIGN_DEEPENING_MD = "# DEEPENING.md\n"
_CODEBASE_DESIGN_DESIGN_IT_TWICE_MD = "# DESIGN-IT-TWICE.md\n"

_PROFILE_YAML = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true"
evidence:
  format: junit-xml
  test_all: evidence.xml
  test_targeted: evidence.xml
checks: none
"""


def _write_skills_dir(base: Path) -> Path:
    skills_dir = base / ".agents" / "skills"
    (skills_dir / "implement").mkdir(parents=True)
    (skills_dir / "implement" / "SKILL.md").write_text(_IMPLEMENT_SKILL_MD, encoding="utf-8")
    (skills_dir / "tdd").mkdir(parents=True)
    (skills_dir / "tdd" / "SKILL.md").write_text(_TDD_SKILL_MD, encoding="utf-8")
    (skills_dir / "tdd" / "tests.md").write_text(_TDD_TESTS_MD, encoding="utf-8")
    (skills_dir / "tdd" / "mocking.md").write_text(_TDD_MOCKING_MD, encoding="utf-8")
    (skills_dir / "codebase-design").mkdir(parents=True)
    (skills_dir / "codebase-design" / "SKILL.md").write_text(
        _CODEBASE_DESIGN_SKILL_MD, encoding="utf-8"
    )
    (skills_dir / "codebase-design" / "DEEPENING.md").write_text(
        _CODEBASE_DESIGN_DEEPENING_MD, encoding="utf-8"
    )
    (skills_dir / "codebase-design" / "DESIGN-IT-TWICE.md").write_text(
        _CODEBASE_DESIGN_DESIGN_IT_TWICE_MD, encoding="utf-8"
    )
    return skills_dir


def _origin_with_profile(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    (origin / "docs" / "agents").mkdir(parents=True)
    (origin / "docs" / "agents" / "project-profile.yml").write_text(
        _PROFILE_YAML, encoding="utf-8"
    )
    run_git(["add", "."], origin)
    run_git(["commit", "-q", "-m", "add project profile"], origin)
    return origin


def _tool_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL)


def _mock_issue(requests_mock: Any, *, number: int = 34) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{number}",
        json={
            "number": number,
            "title": "Fix `thing`",
            "body": "The `thing` function needs fixing.\n",
        },
    )


def _mock_user(requests_mock: Any) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        json={"id": IDENTITY.account_id, "login": IDENTITY.login},
        headers={"github-authentication-token-expiration": "2099-01-01 00:00:00 UTC"},
    )


def test_run_implement_attempt_commits_and_pushes_when_the_model_edits_a_file(
    client: GitHubClient, requests_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _origin_with_profile(tmp_path)
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    from coding_agent.implement import skeleton as skeleton_module

    monkeypatch.setattr(skeleton_module, "remote_url", lambda owner, repo, token: str(origin))
    _mock_issue(requests_mock)
    _mock_user(requests_mock)

    model = FakeChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "write_file",
                        {"path": "thing.py", "content": "def thing():\n    return 42\n"},
                        "call-1",
                    )
                ],
            ),
            AIMessage(content="done", tool_calls=[]),
        ]
    )

    report = attempt.run_implement_attempt(
        client,
        "octocat",
        "sandbox",
        34,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
        skills_dir=_write_skills_dir(tmp_path),
        evidence_dir=tmp_path / "evidence",
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    assert report.ok is True, report.results
    assert report.tool_loop is not None
    assert report.tool_loop.tool_call_count == 1
    assert report.delivery is not None
    assert report.delivery.kind == COMMITTED_AND_PUSHED
    assert report.delivery.branch_name == "agent/34/1-fix-thing"

    pushed_sha = run_git(["rev-parse", "agent/34/1-fix-thing"], origin)
    assert pushed_sha == report.delivery.commit_sha
    assert run_git(["log", "-1", "--format=%an", pushed_sha], origin) == "coding-agent"
    assert (
        run_git(["log", "-1", "--format=%ae", pushed_sha], origin)
        == "999+coding-agent@users.noreply.github.com"
    )
    assert run_git(["log", "-1", "--format=%B", pushed_sha], origin).strip().endswith(
        "Attempt: #34/1"
    )
    assert run_git(["show", f"{pushed_sha}:thing.py"], origin) == "def thing():\n    return 42"


def test_run_implement_attempt_reports_no_change_produced_and_pushes_nothing(
    client: GitHubClient, requests_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _origin_with_profile(tmp_path)
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    from coding_agent.implement import skeleton as skeleton_module

    monkeypatch.setattr(skeleton_module, "remote_url", lambda owner, repo, token: str(origin))
    _mock_issue(requests_mock)
    _mock_user(requests_mock)

    model = FakeChatModel([AIMessage(content="nothing to change here", tool_calls=[])])

    report = attempt.run_implement_attempt(
        client,
        "octocat",
        "sandbox",
        34,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
        skills_dir=_write_skills_dir(tmp_path),
        evidence_dir=tmp_path / "evidence",
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    assert report.ok is True, report.results
    assert report.delivery is not None
    assert report.delivery.kind == NO_CHANGE_PRODUCED
    assert run_git(["branch", "-a"], origin) == "* main"
