"""A Workflow needs a Project Profile only when it validates (ADR 0037).

Covers the ``needs_validation_contract`` property over every tool group
and operation, parse-plus-schema pinning (services never block the pin),
the deferred Target Repository read at startup, the Attempt-start
``profile-unavailable`` halt, the catch-all ``unexpected`` failure class,
and the ``config validate`` notice.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.cli import run_config_validate_command
from coding_agent.identity.startup import Identity
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import SocketServiceProber
from coding_agent.profile.toolchain import load_toolchain_matrix
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.step_operations import (
    PROFILE_RELPATH,
    LocalOperationRunner,
    ProfileUnavailable,
)
from coding_agent.runtime.workspace_git import AttemptWorkspaceGit, BareRepoRemote
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.worker.startup import (
    Ready,
    SkillDeploymentCheck,
    StartupRefusal,
    start,
)
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    Workflow,
    WorkflowStep,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from worker_fakes import WorkerFakes, make_worker_ports

IDENTITY = Identity(account_id=999, login="coding-agent")
LABEL = "agent:implement"
BODY = "Do the thing.\n"

GOOD_PROFILE = """schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "bootstrap-cmd"
  test_all: "run-test-all"
  test_targeted: "run-test-targeted {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services:
  - name: db
    url_env: DB_URL
"""

SCHEMA_BAD_PROFILE = "schema: 2\nlanguage: python\n"

FAKE_TIP = "b" * 40


# --- `needs_validation_contract` ------------------------------------------------


def _workflow_with_step(step: WorkflowStep) -> Workflow:
    """A one-step Workflow carrying ``step``."""
    return Workflow(
        id="w",
        model="m",
        input=InputDecl(type="issue", label="impl"),
        ceiling=AttemptCeiling(
            cost_usd=1.0,
            effective_tokens=1,
            tool_calls=1.0,
            wall_clock_minutes=1.0,
        ),
        start="s",
        steps={"s": step},
    )


def _agent_workflow(*tools: str) -> Workflow:
    return _workflow_with_step(
        WorkflowStep(id="s", agent=Agent(tools=tuple(tools)))
    )


def _operation_workflow(operation: str) -> Workflow:
    return _workflow_with_step(WorkflowStep(id="s", operation=operation))


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (("workspace-read",), False),
        (("workspace-write",), False),
        (("workspace-read", "workspace-write"), False),
        (("shell",), False),
        (("subagents",), False),
        (("advisor",), False),
        (("workspace-read", "workspace-write", "shell", "subagents", "advisor"), False),
        (("tests",), True),
        (("shell", "tests"), True),
        (("workspace-read", "tests"), True),
    ],
)
def test_needs_validation_contract_tool_groups(
    tools: tuple[str, ...], expected: bool
) -> None:
    """Only the ``tests`` tool group binds the Validation Contract."""
    assert _agent_workflow(*tools).needs_validation_contract is expected


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("check-readiness", True),
        ("run-validation", True),
        ("commit-and-push", False),
        ("update-from-base", False),
        ("open-pull-request", False),
        ("publish-review", False),
        ("post-comment", False),
    ],
)
def test_needs_validation_contract_operations(
    operation: str, expected: bool
) -> None:
    """Only the validation operations bind the Validation Contract."""
    assert _operation_workflow(operation).needs_validation_contract is expected


def test_needs_validation_contract_mixed_steps() -> None:
    """One validating step anywhere makes the Workflow need the contract."""
    workflow = Workflow(
        id="w",
        model="m",
        input=InputDecl(type="issue", label="impl"),
        ceiling=AttemptCeiling(
            cost_usd=1.0,
            effective_tokens=1,
            tool_calls=1.0,
            wall_clock_minutes=1.0,
        ),
        start="a",
        steps={
            "a": WorkflowStep(id="a", agent=Agent(tools=("shell",))),
            "b": WorkflowStep(id="b", operation="commit-and-push"),
        },
    )
    assert workflow.needs_validation_contract is False
    validating = Workflow(
        id="w",
        model="m",
        input=InputDecl(type="issue", label="impl"),
        ceiling=AttemptCeiling(
            cost_usd=1.0,
            effective_tokens=1,
            tool_calls=1.0,
            wall_clock_minutes=1.0,
        ),
        start="a",
        steps={
            "a": WorkflowStep(id="a", agent=Agent(tools=("shell",))),
            "b": WorkflowStep(id="b", operation="run-validation"),
        },
    )
    assert validating.needs_validation_contract is True


# --- Pin = parse + schema -------------------------------------------------------


class _NeverRunner:
    """A command runner the pin never calls."""

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        raise AssertionError("the pin runs no command")


def _pin_runner(
    tmp_path: Path,
    *,
    start_commit: str,
    profile_text_override: str | None,
) -> LocalOperationRunner:
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
    runner: CommandRunner = _NeverRunner()
    return LocalOperationRunner(
        git=AttemptWorkspaceGit(
            tmp_path / "workspace",
            "agent",
            "1+agent@users.noreply.github.com",
            BareRepoRemote(tmp_path / "workspace", tmp_path / "remote.git"),
        ),
        start_commit=start_commit,
        matrix=matrix,
        service_env={},
        prober=SocketServiceProber(),
        runner=runner,
        evidence_dir=tmp_path / "evidence",
        attempt_branch="agent/w/1/1-slug",
        input_type="issue",
        profile_text_override=profile_text_override,
    )


def test_pin_succeeds_with_unreachable_services(tmp_path: Path) -> None:
    """A valid profile declaring services pins with an empty service env.

    Service reachability is never part of the pin; it stays in
    ``check-readiness``, which reports it as ``not-ready``.
    """
    runner = _pin_runner(
        tmp_path, start_commit="0" * 40, profile_text_override=GOOD_PROFILE
    )
    profile, error = runner.pin_validation_contract()
    assert error is None
    assert isinstance(profile, ProjectProfile)


def test_pin_reports_unparsable_override(tmp_path: Path) -> None:
    """An unparsable override fails the pin with its location."""
    runner = _pin_runner(
        tmp_path, start_commit="0" * 40, profile_text_override="schema: [unclosed\n"
    )
    profile, error = runner.pin_validation_contract()
    assert profile is None
    assert error is not None
    assert "workflow-configuration" in error


def test_pin_reports_missing_repository_profile(tmp_path: Path) -> None:
    """No override and no repository profile fails naming the revision."""
    (tmp_path / "workspace").mkdir()
    runner = _pin_runner(
        tmp_path, start_commit="d" * 40, profile_text_override=None
    )
    profile, error = runner.pin_validation_contract()
    assert profile is None
    assert error is not None
    assert "d" * 40 in error
    assert PROFILE_RELPATH in error


# --- Startup: the deferred Target Repository read -------------------------------

_VALIDATING_CONFIG = """\
version: 4

target:
  kind: github
  repository: o/r
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  m:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: low
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 1.0, output: 1.0, cache-read: 1.0, cache-write: 1.0}
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000

workflows:
  implement:
    model: m
    input: {type: issue, label: impl}
    limits: {cost-usd: 1.0, effective-tokens: 1500000, tool-calls: 10.0, wall-clock-minutes: 10.0}
    start: s
    steps:
      s:
        operation: check-readiness
        routes:
          ready: {end: completed}
          not-ready: {end: blocked}
          error: {end: failed}
          limit-exceeded: {end: failed}
"""

_NON_VALIDATING_CONFIG = _VALIDATING_CONFIG.replace(
    """    steps:
      s:
        operation: check-readiness
        routes:
          ready: {end: completed}
          not-ready: {end: blocked}
          error: {end: failed}
          limit-exceeded: {end: failed}
""",
    """    steps:
      s:
        agent:
          tools: [workspace-read, workspace-write, shell, subagents]
          results:
            done: {}
        routes:
          done: {end: completed}
          error: {end: failed}
          limit-exceeded: {end: failed}
""",
)


def _contents_url() -> str:
    return (
        "https://api.github.com/repos/o/r/contents/"
        f"{PROFILE_RELPATH}?ref={FAKE_TIP}"
    )


def _contents_body(text: str) -> dict[str, str]:
    return {
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }


def _write_matrix(tmp_path: Path) -> Path:
    path = tmp_path / "toolchain-matrix.json"
    path.write_text(
        json.dumps(
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
        encoding="utf-8",
    )
    return path


def _startup_fakes(monkeypatch: pytest.MonkeyPatch) -> WorkerFakes:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    return make_worker_ports(model=ScriptedModel([]), tracker=tracker)


def _deployment(tmp_path: Path) -> SkillDeploymentCheck:
    return SkillDeploymentCheck(
        policy=tmp_path / "skill-policy.json",
        bundle_dir=tmp_path / "bundle",
        home=tmp_path / "home",
    )


def _halt_row(state_dir: Path) -> tuple[str, str]:
    connection = sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        row = connection.execute(
            "SELECT reason, detail FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0]), str(row[1])


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _no_attempt_opened(state_dir: Path) -> None:
    ledger_path = state_dir / "run-ledger.sqlite3"
    if not ledger_path.exists():
        return
    connection = sqlite3.connect(str(ledger_path))
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "attempts" in tables:
            count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()
            assert count is not None and int(count[0]) == 0
    finally:
        connection.close()


def test_startup_refuses_missing_repository_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``target.profile`` and no repository profile: Startup Refusal."""
    fakes = _startup_fakes(monkeypatch)
    requests_mock.get(_contents_url(), status_code=404)
    config = _write_config(tmp_path, _VALIDATING_CONFIG)

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, StartupRefusal)
    assert result.phase == "project-profile"
    assert PROFILE_RELPATH in result.message
    assert FAKE_TIP in result.message
    assert fakes.order == ["skill-bundle", "github"]
    tracker = fakes.github.tracker
    assert isinstance(tracker, FakeStepTracker)
    assert set(tracker.call_names()) <= {"fetch_repository_owner_id", "fetch_base_tip"}
    _no_attempt_opened(tmp_path / "state")
    reason, detail = _halt_row(tmp_path / "state")
    assert reason == "startup-check-refused"
    assert PROFILE_RELPATH in detail
    assert "run: startup refused at project-profile after " in capsys.readouterr().err


def test_startup_refuses_unparsable_repository_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
) -> None:
    """A repository profile that does not parse refuses startup."""
    fakes = _startup_fakes(monkeypatch)
    requests_mock.get(_contents_url(), json=_contents_body("schema: [unclosed\n"))
    config = _write_config(tmp_path, _VALIDATING_CONFIG)

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, StartupRefusal)
    assert result.phase == "project-profile"
    assert PROFILE_RELPATH in result.message
    assert FAKE_TIP in result.message
    _no_attempt_opened(tmp_path / "state")


def test_startup_refuses_schema_bad_repository_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
) -> None:
    """A repository profile failing the schema refuses startup."""
    fakes = _startup_fakes(monkeypatch)
    requests_mock.get(_contents_url(), json=_contents_body(SCHEMA_BAD_PROFILE))
    config = _write_config(tmp_path, _VALIDATING_CONFIG)

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, StartupRefusal)
    assert result.phase == "project-profile"
    assert PROFILE_RELPATH in result.message
    assert FAKE_TIP in result.message
    _no_attempt_opened(tmp_path / "state")


def test_startup_accepts_valid_repository_profile_after_github_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid repository profile passes between `github-checks` and the bundle."""
    fakes = _startup_fakes(monkeypatch)
    requests_mock.get(_contents_url(), json=_contents_body(GOOD_PROFILE))
    config = _write_config(tmp_path, _VALIDATING_CONFIG)

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, Ready)
    try:
        assert result.claim_config.profile_text is None
        err = capsys.readouterr().err
        lines = err.splitlines()
        github_ok = next(
            pos for pos, line in enumerate(lines) if "github-checks ok" in line
        )
        profile_ok = next(
            pos
            for pos, line in enumerate(lines)
            if "project-profile ok" in line
        )
        assert github_ok < profile_ok
        tracker = fakes.github.tracker
        assert isinstance(tracker, FakeStepTracker)
        assert set(tracker.call_names()) <= {
            "fetch_repository_owner_id",
            "fetch_base_tip",
        }
        assert fakes.order == ["skill-bundle", "github", "provider"]
    finally:
        result.__exit__(None, None, None)


def test_startup_skips_without_reading_for_a_non_validating_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Workflow that needs no contract skips without any repository read."""
    fakes = _startup_fakes(monkeypatch)
    config = _write_config(tmp_path, _NON_VALIDATING_CONFIG)

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, Ready)
    try:
        err = capsys.readouterr().err
        assert (
            "run: startup: project-profile skipped (profile from Target Repository)"
            in err
        )
        # No registered mock: any repository read would raise NoMockAddress.
        assert requests_mock.called is False
    finally:
        result.__exit__(None, None, None)


# --- Attempt start: `profile-unavailable` and the `unexpected` catch-all -------


def _issue_input(number: int, created_at: str) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=f"Add a greeting {number}",
        body=BODY,
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeStepTracker:
    tracker = FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = _issue_input(number, created_at)
        tracker.labeled_events[(number, LABEL)] = LabeledEvent(
            label=LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at="2026-10-01T00:00:00Z",
        )
    return tracker


def _loop_workflow() -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="work",
        outcomes={},
        steps={
            "work": WorkflowStep(
                id="work", agent=Agent(results={"done": {}})
            )
        },
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _loop_config(workflow: Workflow | None = None) -> WorkerLoopConfig:
    selected = workflow if workflow is not None else _loop_workflow()
    return WorkerLoopConfig(
        claim_config=ClaimConfig(
            workflow_id="implement",
            input_type="issue",
            selection_label=LABEL,
            base_branch="main",
            agent_login=IDENTITY.login,
            agent_id=IDENTITY.account_id,
            effective_configuration=full_effective_configuration(selected),
        ),
        workflow=selected,
        poll_interval_seconds=60.0,
    )


def _halt_reason(state_dir: Path) -> tuple[str, str] | None:
    connection = sqlite3.connect(str(state_dir / "run-ledger.sqlite3"))
    try:
        row = connection.execute(
            "SELECT reason, detail FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def test_profile_unavailable_ends_attempt_and_halts(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """A validating Workflow with no pinnable contract ends `internal-failure`.

    The Attempt is finished through finalisation (never left open at
    `claim-confirmed`) and the Worker halts `profile-unavailable`.
    """
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))
    workflow = _operation_workflow("check-readiness")

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise ProfileUnavailable(
            f"cannot run Attempt {claimed.attempt_id!r}: "
            f"no Project Profile at {'c' * 40}:{PROFILE_RELPATH}"
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(workflow),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    stored = ledger.get_outcome("implement#31/1")
    assert stored is not None
    assert stored.outcome == "internal-failure"
    assert PROFILE_RELPATH in stored.reason
    assert ledger.get_attempt("implement#31/1").state == "finished"
    assert not ledger.has_unfinished_attempt()
    halt = _halt_reason(tmp_path / "state")
    assert halt is not None
    assert halt[0] == "profile-unavailable"


def test_unexpected_escape_ends_attempt_and_keeps_polling(
    ledger: RunLedger,
) -> None:
    """An arbitrary exception after the Claim ends that Attempt, not the Worker."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"), (32, "2026-10-02T00:00:00Z")
    )
    ran: list[int] = []

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        raise ValueError("boom")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=2,
    )

    assert exit_code == 0
    assert ran == [31, 32]
    for number in (31, 32):
        stored = ledger.get_outcome(f"implement#{number}/1")
        assert stored is not None
        assert stored.outcome == "internal-failure"
        assert stored.reason.startswith("infrastructure-failure: unexpected")
        assert ledger.get_attempt(f"implement#{number}/1").state == "finished"


def test_three_unexpected_escapes_halt(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """Three consecutive `unexpected` failures halt after the third Attempt."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
    )

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise ValueError("boom")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    for number in (31, 32, 33):
        stored = ledger.get_outcome(f"implement#{number}/1")
        assert stored is not None
        assert stored.outcome == "internal-failure"
    halt = _halt_reason(tmp_path / "state")
    assert halt is not None
    assert halt[0] == "infrastructure-failure"


# --- `config validate` ----------------------------------------------------------


def test_config_validate_notices_repository_profile_for_validating_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A validating Workflow without `target.profile`: notice, still exit 0."""
    config = _write_config(tmp_path, _VALIDATING_CONFIG)

    assert run_config_validate_command(config) == 0
    out = capsys.readouterr().out
    assert "config validate: NOTICE" in out
    assert PROFILE_RELPATH in out
    assert "config validate: OK" in out


def test_config_validate_silent_for_a_non_validating_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Workflow that needs no contract draws no notice."""
    config = _write_config(tmp_path, _NON_VALIDATING_CONFIG)

    assert run_config_validate_command(config) == 0
    captured = capsys.readouterr()
    assert "NOTICE" not in captured.out
    assert "config validate: OK" in captured.out
