from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import Identity
from coding_agent.implement import attempt
from coding_agent.implement.ceilings import LoopCeilings, UsageTotals
from coding_agent.implement.delivery import (
    COMMITTED_AND_PUSHED,
    NO_CHANGE_PRODUCED,
    DeliverySnapshotOutcome,
)
from coding_agent.implement.git import GitFailure
from coding_agent.implement.loop import ToolLoopResult
from coding_agent.implement.result_capping import DEFAULT_RESULT_CAP_LIMIT
from coding_agent.implement.skeleton import SkeletonReport, StageResult
from coding_agent.provider.config import DEFAULT_COMPACTION_THRESHOLDS, DEFAULT_PRICE_TABLE, PINNED_MODELS
from coding_agent.validate.baseline import Regression, ValidationEvidence
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
  test_all: python3 write_junit.py {evidence_dir}/test_all.xml
  test_targeted: python3 write_junit.py {evidence_dir}/test_targeted.xml
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
"""

_WRITE_JUNIT_SCRIPT = """\
import os
import sys

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    f.write('<testsuite name="suite" tests="1"><testcase classname="pkg" name="ok"/></testsuite>')
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
    (origin / "write_junit.py").write_text(_WRITE_JUNIT_SCRIPT, encoding="utf-8")
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
        price_table=DEFAULT_PRICE_TABLE,
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(),
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
    assert report.validation is not None
    assert report.validation.clean is True
    assert attempt.implement_outcome(report) == "delivered-snapshot"

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


def test_run_implement_attempt_reports_a_stage_failure_when_the_base_revision_worktree_fails(
    client: GitHubClient, requests_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `git worktree` failure while comparing against the Base Revision
    ends the Attempt on a printed `StageResult`, like every other
    git-touching stage (`ensure_mirror`, `checkout_workspace`) — never an
    uncaught `GitFailure` propagating past `run_implement_attempt`."""
    origin = _origin_with_profile(tmp_path)
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    from coding_agent.implement import skeleton as skeleton_module

    monkeypatch.setattr(skeleton_module, "remote_url", lambda owner, repo, token: str(origin))
    _mock_issue(requests_mock)
    _mock_user(requests_mock)

    def _fail_worktree_checkout(*args: object, **kwargs: object) -> Path:
        raise GitFailure("worktree add failed: boom")

    monkeypatch.setattr(attempt, "checkout_base_revision_worktree", _fail_worktree_checkout)

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
        price_table=DEFAULT_PRICE_TABLE,
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(),
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    assert report.ok is False
    assert report.validation is None
    stage = next(r for r in report.results if r.name == "validation")
    assert stage.passed is False
    assert "boom" in stage.detail
    assert attempt.implement_outcome(report) is None


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
        price_table=DEFAULT_PRICE_TABLE,
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(),
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    assert report.ok is True, report.results
    assert report.delivery is not None
    assert report.delivery.kind == NO_CHANGE_PRODUCED
    assert report.validation is None
    assert attempt.implement_outcome(report) == "no-change-produced"
    assert run_git(["branch", "-a"], origin) == "* main"


def test_run_implement_attempt_tags_requests_with_cache_breakpoints_on_the_anthropic_pin(
    client: GitHubClient, requests_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _origin_with_profile(tmp_path)
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    from coding_agent.implement import skeleton as skeleton_module

    monkeypatch.setattr(skeleton_module, "remote_url", lambda owner, repo, token: str(origin))
    _mock_issue(requests_mock)
    _mock_user(requests_mock)

    model = FakeChatModel([AIMessage(content="nothing to change here", tool_calls=[])])

    attempt.run_implement_attempt(
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
        pin=PINNED_MODELS["anthropic"],
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
        price_table=DEFAULT_PRICE_TABLE,
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(),
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    first_request = model.invocations[0]
    tagged = first_request[-1].content
    assert isinstance(tagged, list)
    last_block = tagged[-1]
    assert isinstance(last_block, dict)
    assert last_block["cache_control"] == {"type": "ephemeral"}


def test_run_implement_attempt_leaves_requests_untagged_on_the_openai_pin(
    client: GitHubClient, requests_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _origin_with_profile(tmp_path)
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    from coding_agent.implement import skeleton as skeleton_module

    monkeypatch.setattr(skeleton_module, "remote_url", lambda owner, repo, token: str(origin))
    _mock_issue(requests_mock)
    _mock_user(requests_mock)

    model = FakeChatModel([AIMessage(content="nothing to change here", tool_calls=[])])

    attempt.run_implement_attempt(
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
        pin=PINNED_MODELS["openai"],
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
        price_table=DEFAULT_PRICE_TABLE,
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(),
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    first_request = model.invocations[0]
    assert isinstance(first_request[-1].content, str)


def test_run_implement_attempt_refuses_before_any_model_call_when_the_pin_has_no_price(
    client: GitHubClient, requests_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _origin_with_profile(tmp_path)
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    from coding_agent.implement import skeleton as skeleton_module

    monkeypatch.setattr(skeleton_module, "remote_url", lambda owner, repo, token: str(origin))
    _mock_issue(requests_mock)

    def _never_invoke(*args: object, **kwargs: object) -> AIMessage:
        raise AssertionError("the model must never be called once the Price Table refuses")

    model = FakeChatModel([])
    monkeypatch.setattr(model, "invoke", _never_invoke)

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
        price_table={},
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(),
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    assert report.ok is False
    assert report.tool_loop is None
    assert any(r.name == "price table entry" and not r.passed for r in report.results)
    assert attempt.implement_outcome(report) is None


def test_run_implement_attempt_reports_the_tool_loop_stage_as_failed_when_a_ceiling_stops_it(
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
                content="nothing to change here",
                tool_calls=[],
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
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
        price_table=DEFAULT_PRICE_TABLE,
        result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
        ceilings=LoopCeilings(max_tokens=1),
        model=model,
        attempt_number=1,
        token_env="GITHUB_TOKEN",
    )

    assert report.tool_loop is not None
    assert report.tool_loop.stopped_by == "tokens"
    stage = next(r for r in report.results if r.name == "tool loop completed")
    assert stage.passed is False
    assert "tokens" in stage.detail
    assert report.ok is False
    assert report.validation is None
    assert attempt.implement_outcome(report) == "failed-limit"


# --- implement_outcome: unit-level, one AttemptReport shape per outcome ----


def _skeleton_stopping_at(name: str, *, passed: bool, detail: str = "") -> SkeletonReport:
    report = SkeletonReport()
    report.add(StageResult(name, passed, detail))
    return report


def _ok_skeleton() -> SkeletonReport:
    return _skeleton_stopping_at("seam set confirmed", passed=True, detail="a_thing")


def test_implement_outcome_maps_an_unconfirmed_seam() -> None:
    report = attempt.AttemptReport(
        skeleton=_skeleton_stopping_at(
            "seam set confirmed", passed=False, detail="no Seam Set derivable"
        )
    )
    assert attempt.implement_outcome(report) == "seam-not-confirmed"


def test_implement_outcome_is_none_for_an_earlier_unnamed_stage_failure() -> None:
    report = attempt.AttemptReport(
        skeleton=_skeleton_stopping_at("mirror updated", passed=False, detail="unreachable")
    )
    assert attempt.implement_outcome(report) is None


def test_implement_outcome_maps_a_validation_regression() -> None:
    validation = ValidationEvidence(
        commands=(),
        passed=(),
        baseline_failures=(),
        regressions=(Regression("test_all", frozenset({"pkg::new"})),),
        unclaimable=(),
        missing_evidence=(),
    )
    report = attempt.AttemptReport(
        skeleton=_ok_skeleton(),
        tool_loop=ToolLoopResult(conversation=(), tool_call_count=1, usage=UsageTotals(), stopped_by=None),
        delivery=DeliverySnapshotOutcome(kind=COMMITTED_AND_PUSHED, branch_name="agent/1/1-x", commit_sha="abc"),
        validation=validation,
    )
    assert attempt.implement_outcome(report) == "validation-failed"
