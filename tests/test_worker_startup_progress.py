"""Worker startup progress reporting (issue #59).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup
in order; a failure at any step exits non-zero with no Attempt and no GitHub
write). Startup reports its own progress on stderr: an initial message before
any expensive check, one started/ok pair per phase in contract order, a
skipped marker where there is nothing to recover, a named phase on every
refusal, and the overall elapsed time on completion.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from coding_agent.identity.startup import Identity, StartupCheckFailed, StartupCheckResult
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import ProviderCapabilityRefused
from coding_agent.skillbundle.verify import VerificationReport
from coding_agent.worker import supervisor
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import write_skill_bundle

IDENTITY = Identity(account_id=999, login="coding-agent")

EXPECTED_PHASES = [
    "configuration",
    "volume-lock",
    "toolchain-matrix",
    "skill-bundle",
    "github-checks",
    "provider-capability",
    "run-ledger",
    "startup-recovery",
]


_RUN_CONFIG = """\
version: 2

target:
  kind: github
  repository: example-owner/example-repo
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: sonnet
    input: {type: issue, label: agent:implement}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      work:
        agent:
          tools: [workspace-read]
          results:
            done: {}
        routes: {done: {end: completed}}
"""


def _write_run_config(tmp_path: Path, text: str = _RUN_CONFIG) -> Path:
    """A valid v2 run configuration file (local copy, never from another test)."""
    path = tmp_path / "workflow.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_run_matrix(tmp_path: Path) -> Path:
    """A minimal toolchain matrix the startup toolchain phase accepts."""
    path = tmp_path / "toolchain-matrix.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_manager": {"name": "uv", "version": "0.12.8"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _passing_bundle_report() -> VerificationReport:
    report = VerificationReport()
    report.add("bundle", True, "verified")
    return report


class _StartupSpy:
    """Patches every startup collaborator and records the order they ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, model: ScriptedModel) -> None:
        self.order: list[str] = []
        self.model = model
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(supervisor, "verify_installed_skill_bundle", self._bundle)
        monkeypatch.setattr(supervisor, "run_startup_checks", self._github)
        monkeypatch.setattr(supervisor, "build_chat_model", self._build)
        monkeypatch.setattr(supervisor, "assert_provider_capability", self._provider)
        self.bundle_report = _passing_bundle_report()
        self.github_error: Exception | None = None
        self.provider_error: Exception | None = None

    def _bundle(self, check: supervisor.SkillBundleCheck) -> VerificationReport:
        self.order.append("skill-bundle")
        return self.bundle_report

    def _github(self, *args: object, **kwargs: object) -> StartupCheckResult:
        self.order.append("github")
        if self.github_error is not None:
            raise self.github_error
        return StartupCheckResult(
            identity=IDENTITY,
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        )

    def _build(self, pin: object, **kwargs: object) -> ScriptedModel:
        return self.model

    def _provider(
        self, pin: object, model: object, table: object, **kwargs: object
    ) -> None:
        self.order.append("provider")
        if self.provider_error is not None:
            raise self.provider_error


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    *,
    config: Path | None = None,
    max_sweeps: int | None = None,
) -> tuple[int, list[dict[str, object]]]:
    """Run the Worker offline with a recording tracker factory."""
    built: list[dict[str, object]] = []
    # The stubbed bundle verification passes, so install the synthetic
    # bundle the startup activation registry loads before the Run Ledger
    # opens.
    write_skill_bundle(tmp_path)

    def factory(**kwargs: object) -> FakeStepTracker:
        built.append(kwargs)
        return tracker

    exit_code = supervisor.run_worker(
        config=config if config is not None else _write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        tracker_factory=factory,
        sleep=lambda seconds: None,
        max_sweeps=max_sweeps,
    )
    return exit_code, built


def _order_indexes(lines: list[str], needles: list[str]) -> list[int]:
    indexes: list[int] = []
    for needle in needles:
        for pos, line in enumerate(lines):
            if needle in line:
                indexes.append(pos)
                break
        else:
            raise AssertionError(f"missing startup line: {needle!r}\n" + "\n".join(lines))
    return indexes


def test_successful_startup_reports_every_phase_in_order_with_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """All phases start and complete in contract order; completion has elapsed."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
    _StartupSpy(monkeypatch, ScriptedModel([]))
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)

    exit_code, _ = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 0
    err = capsys.readouterr().err
    lines = err.splitlines()
    needles = ["run: startup started"]
    for phase in EXPECTED_PHASES:
        needles.append(f"run: startup: {phase} started")
        if phase == "startup-recovery":
            needles.append(f"run: startup: {phase} skipped")
        else:
            needles.append(f"run: startup: {phase} ok")
    needles.append("run: startup complete in ")
    indexes = _order_indexes(lines, needles)
    assert indexes == sorted(indexes)
    assert re.search(r"run: startup complete in \d+\.\d\ds", err) is not None
    # No credential value reaches the log streams.
    assert "github_pat_testtoken" not in err
    assert "sk-ant-test-value" not in err


def test_startup_started_comes_before_any_expensive_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The initial message is emitted before the first expensive check."""
    seen: list[str] = []

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    passing = spy.bundle_report

    def _recording(check: supervisor.SkillBundleCheck) -> VerificationReport:
        seen.append(capsys.readouterr().err)
        spy.order.append("skill-bundle")
        return passing

    monkeypatch.setattr(supervisor, "verify_installed_skill_bundle", _recording)
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)

    exit_code, _ = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 0
    assert seen
    assert "run: startup started" in seen[0]
    assert "run: startup: skill-bundle started" in seen[0]


@pytest.mark.parametrize(
    ("refusal", "phase"),
    [
        ("skill-bundle", "skill-bundle"),
        ("github", "github-checks"),
        ("provider", "provider-capability"),
    ],
)
def test_startup_refusal_names_its_phase_with_elapsed_and_stops_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refusal: str,
    phase: str,
) -> None:
    """A refusal names the in-progress phase and never runs a later one."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    if refusal == "skill-bundle":
        spy.bundle_report = VerificationReport()
        spy.bundle_report.add("closure", False, "missing /tdd")
    elif refusal == "github":
        spy.github_error = StartupCheckFailed("GET /user returned 401: Bad credentials")
    else:
        spy.provider_error = ProviderCapabilityRefused("no tool call arrived")
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)

    exit_code, built = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 1
    assert built == []
    err = capsys.readouterr().err
    assert f"run: startup refused at {phase} after " in err
    assert re.search(r"run: startup refused at \S+ after \d+\.\d\ds: ", err) is not None
    assert f"run: startup: {phase} started" in err
    assert f"run: startup: {phase} ok" not in err
    phase_pos = EXPECTED_PHASES.index(phase)
    for later in EXPECTED_PHASES[phase_pos + 1 :]:
        assert f"run: startup: {later} started" not in err
    for earlier in EXPECTED_PHASES[:phase_pos]:
        assert f"run: startup: {earlier} ok" in err
    assert "run: startup complete" not in err
    assert "github_pat_testtoken" not in err
    assert "sk-ant-test-value" not in err


def test_invalid_configuration_refuses_at_configuration_before_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 1 refuses before any later phase starts (W-CFG-13)."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    bad = _write_run_config(tmp_path, _RUN_CONFIG.replace("model: sonnet", "model: nope"))
    exit_code, built = _run(tmp_path, monkeypatch, FakeStepTracker(), config=bad, max_sweeps=1)

    assert exit_code == 1
    assert spy.order == []
    assert built == []
    err = capsys.readouterr().err
    assert "run: startup started" in err
    assert "run: startup: configuration started" in err
    assert "run: startup refused at configuration after " in err
    assert "run: startup: volume-lock started" not in err


def _seed_unfinished_ledger(state_dir: Path) -> None:
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "implement"})
    finally:
        ledger.close()


def _tracker_factory_for(tracker: FakeStepTracker) -> Callable[..., FakeStepTracker]:
    def factory(**kwargs: object) -> FakeStepTracker:
        return tracker

    return factory


def test_startup_recovery_with_unfinished_attempt_is_pending_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unfinished Attempt keeps recovery visible and preserves behavior."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _StartupSpy(monkeypatch, ScriptedModel([]))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _seed_unfinished_ledger(state_dir)
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)
    write_skill_bundle(tmp_path)

    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        toolchain_matrix_path=matrix,
        tracker_factory=_tracker_factory_for(tracker),
        sleep=lambda seconds: None,
        max_sweeps=0,
    )

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "run: startup: startup-recovery started" in err
    assert "run: startup: startup-recovery skipped" not in err
    assert "run: startup: startup-recovery ok" in err
    assert "run: the Run Ledger holds unfinished Attempts: implement#7/1" in err
    assert "run: startup complete in " in err
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        assert ledger.get_attempt("implement#7/1").state == "open"
    finally:
        ledger.close()


def test_run_ledger_refusal_names_run_ledger_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupted Effective Configuration cell refuses at run-ledger with
    no GitHub write (REC-CFG-2)."""
    import sqlite3 as _sqlite3

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
    _StartupSpy(monkeypatch, ScriptedModel([]))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    finally:
        ledger.close()
    connection = _sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        connection.execute(
            "UPDATE attempts SET effective_configuration = 'not-json'"
            " WHERE attempt_id = 'implement#7/1'"
        )
        connection.commit()
    finally:
        connection.close()
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)
    write_skill_bundle(tmp_path)

    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        toolchain_matrix_path=matrix,
        tracker_factory=_tracker_factory_for(tracker),
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    assert tracker.calls == []
    err = capsys.readouterr().err
    assert "run: startup refused at run-ledger after " in err
    assert "run: startup: startup-recovery started" not in err
    assert "github_pat_testtoken" not in err
    assert "sk-ant-test-value" not in err


def test_worker_startup_never_runs_the_write_probing_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agent preflight` stays an operator command, never a startup action."""
    import coding_agent.preflight.probes as probes

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("preflight must not run during `agent run` startup")

    monkeypatch.setattr(probes, "run_preflight", _forbidden)
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _StartupSpy(monkeypatch, ScriptedModel([]))

    exit_code, _ = _run(tmp_path, monkeypatch, FakeStepTracker(), max_sweeps=1)

    assert exit_code == 0
    assert "preflight" not in capsys.readouterr().err
