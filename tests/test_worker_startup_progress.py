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
from pathlib import Path

import pytest

from coding_agent.identity.startup import Identity, StartupCheckFailed
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import ProviderCapabilityRefused
from coding_agent.skillbundle.deployment import (
    DeploymentFault,
    RunVerificationRefusal,
    VerifiedRunInputs,
)
import coding_agent.worker.supervisor as supervisor
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from worker_fakes import WorkerFakes, make_worker_ports

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
version: 4

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
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: sonnet
    input: {type: issue, label: agent:implement}
    limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
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
                "schema": 2,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_managers": {"uv": {"version": "0.12.8"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _fakes(
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    model: ScriptedModel,
    *,
    skill_refusal: RunVerificationRefusal | None = None,
    github_error: Exception | None = None,
    provider_error: Exception | None = None,
) -> WorkerFakes:
    """Fake startup ports handing out ``tracker`` and ``model``."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return make_worker_ports(
        model=model,
        tracker=tracker,
        skill_refusal=skill_refusal,
        github_error=github_error,
        provider_error=provider_error,
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    *,
    config: Path | None = None,
    max_sweeps: int | None = None,
    fakes: WorkerFakes | None = None,
) -> tuple[int, WorkerFakes]:
    """Run the Worker offline with fake startup ports."""
    active = (
        fakes
        if fakes is not None
        else _fakes(monkeypatch, tracker, ScriptedModel([]))
    )
    exit_code = supervisor.run_worker(
        config=config if config is not None else _write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=active.ports,
        sleep=lambda seconds: None,
        max_sweeps=max_sweeps,
    )
    return exit_code, active


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
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")

    exit_code, _ = _run(tmp_path, monkeypatch, tracker, max_sweeps=1, fakes=fakes)

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

    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    real_verify = fakes.skills.verify_run_inputs

    def _recording(
        *,
        config: LoadedWorkflowConfiguration,
        policy: Path,
        bundle_dir: Path,
        home: Path,
    ) -> VerifiedRunInputs | RunVerificationRefusal:
        seen.append(capsys.readouterr().err)
        result: VerifiedRunInputs | RunVerificationRefusal = real_verify(
            config=config, policy=policy, bundle_dir=bundle_dir, home=home
        )
        return result

    monkeypatch.setattr(fakes.skills, "verify_run_inputs", _recording)

    exit_code, _ = _run(tmp_path, monkeypatch, tracker, max_sweeps=1, fakes=fakes)

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
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    if refusal == "skill-bundle":
        skill_refusal: RunVerificationRefusal | None = RunVerificationRefusal(
            faults=(
                DeploymentFault(phase="bundle", path="", detail="missing"),
            ),
        )
        github_error: Exception | None = None
        provider_error: Exception | None = None
    elif refusal == "github":
        skill_refusal = None
        github_error = StartupCheckFailed("GET /user returned 401: Bad credentials")
        provider_error = None
    else:
        skill_refusal = None
        github_error = None
        provider_error = ProviderCapabilityRefused("no tool call arrived")
    fakes = _fakes(
        monkeypatch,
        tracker,
        ScriptedModel([]),
        skill_refusal=skill_refusal,
        github_error=github_error,
        provider_error=provider_error,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")

    exit_code, fakes = _run(tmp_path, monkeypatch, tracker, max_sweeps=1, fakes=fakes)

    assert exit_code == 1
    if refusal == "provider":
        # The owner lookup runs inside github-checks, before
        # provider-capability, so the tracker is already built.
        assert len(fakes.github.tracker_calls) == 1
        assert tracker.call_names() == ["fetch_repository_owner_id"]
    else:
        assert fakes.github.tracker_calls == []
        assert tracker.calls == []
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
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    bad = _write_run_config(tmp_path, _RUN_CONFIG.replace("model: sonnet", "model: nope"))
    exit_code, fakes = _run(
        tmp_path, monkeypatch, tracker, config=bad, max_sweeps=1, fakes=fakes
    )

    assert exit_code == 1
    assert fakes.order == []
    assert fakes.github.tracker_calls == []
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


def test_startup_recovery_with_unfinished_attempt_is_pending_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unfinished Attempt keeps recovery visible and preserves behavior."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _seed_unfinished_ledger(state_dir)
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)

    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=matrix,
        ports=fakes.ports,
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
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)

    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=matrix,
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    # The owner lookup runs inside github-checks, before the Run Ledger
    # opens, so only the lookup has run when the ledger refuses.
    assert tracker.call_names() == ["fetch_repository_owner_id"]
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
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))

    exit_code, _ = _run(tmp_path, monkeypatch, tracker, max_sweeps=1, fakes=fakes)

    assert exit_code == 0
    assert "preflight" not in capsys.readouterr().err


def test_failing_owner_lookup_refuses_at_github_checks_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing owner lookup refuses in github-checks with no ready.

    The owner lookup and identity-mode detection run inside the
    ``github-checks`` phase, before the Provider Capability Assertion and
    before the Run Ledger opens: no ``ready`` event follows, the ledger is
    never constructed and ``github-checks ok`` is never logged.
    """
    import sqlite3 as _sqlite3

    from coding_agent.ledger.store import RunLedger as _RealRunLedger
    from coding_agent.runtime.ceilings import LoopCeilings as _LoopCeilings
    from coding_agent.runtime.redaction import Redactable as _Redactable

    tracker = FakeStepTracker(
        agent_login=IDENTITY.login,
        agent_id=IDENTITY.account_id,
        fail_on="fetch_repository_owner_id",
    )
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    ledger_constructions: list[Path] = []
    real_init = _RealRunLedger.__init__

    def _recording_init(
        self: _RealRunLedger,
        path: Path,
        *,
        ceilings: _LoopCeilings | None = None,
        registry: _Redactable = None,
    ) -> None:
        ledger_constructions.append(path)
        real_init(self, path, ceilings=ceilings, registry=registry)

    monkeypatch.setattr(_RealRunLedger, "__init__", _recording_init)
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)

    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=matrix,
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    # The tracker is built once, inside github-checks, and the lookup fails.
    assert len(fakes.github.tracker_calls) == 1
    assert tracker.call_names() == ["fetch_repository_owner_id"]
    # The Provider Capability Assertion never runs.
    assert fakes.order == ["skill-bundle", "github"]
    # The Run Ledger never opens (the operator store shares its SQLite file,
    # so the construction call itself is the assertion).
    assert ledger_constructions == []
    err = capsys.readouterr().err
    assert "run: startup refused at github-checks after " in err
    assert "could not resolve the repository owner" in err
    assert "run: startup: github-checks started" in err
    assert "run: startup: github-checks ok" not in err
    assert "run: startup: provider-capability started" not in err
    connection = _sqlite3.connect(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        event_types = [
            str(row[0])
            for row in connection.execute("SELECT type FROM operator_events ORDER BY id").fetchall()
        ]
        halt = connection.execute(
            "SELECT reason, detail FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert "ready" not in event_types
    assert "halted" in event_types
    assert halt is not None
    assert str(halt[0]) == "startup-check-refused"
    assert "could not resolve the repository owner" in str(halt[1])
