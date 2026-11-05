"""Worker startup progress reporting (issue #59).

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup in order;
a failure at any step exits non-zero with no Attempt and no GitHub write).
Startup reports its own progress on stderr: an initial message before any
expensive check, one started/ok pair per phase in contract order, a skipped
marker where there is nothing to recover, a named phase on every refusal,
and the overall elapsed time on completion.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from coding_agent.identity.startup import StartupCheckFailed
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import ProviderCapabilityRefused
from coding_agent.skillbundle.verify import VerificationReport
from coding_agent.worker import supervisor
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from test_mvp_attempt_graph import IDENTITY  # noqa: F401  (fixtures reused)
from test_worker_run import (
    _RUN_CONFIG,
    _StartupSpy,
    _run,
    _write_run_config,
    _write_run_matrix,
)

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
    tracker = FakeTracker(agent_login=IDENTITY.login)

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
    tracker = FakeTracker(agent_login=IDENTITY.login)

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
    tracker = FakeTracker(agent_login=IDENTITY.login)

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
    bad = _write_run_config(tmp_path, _RUN_CONFIG.replace("skill: implement", "skill: tdd"))
    exit_code, built = _run(tmp_path, monkeypatch, FakeTracker(), config=bad, max_sweeps=1)

    assert exit_code == 1
    assert spy.order == []
    assert built == []
    err = capsys.readouterr().err
    assert "run: startup started" in err
    assert "run: startup: configuration started" in err
    assert "run: startup refused at configuration after " in err
    assert "run: startup: volume-lock started" not in err


def _seed_unfinished_ledger(state_dir: Path) -> None:
    record: dict[str, object] = {
        "target": {
            "kind": "github",
            "repository": "example-owner/example-repo",
            "base_branch": "main",
            "token_env": "GITHUB_TOKEN",
            "ready_for_human": "ready-for-human",
        },
        "workflow": {"id": "implement", "selector": {"label": "ready-for-agent"}},
    }
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration=record)
    finally:
        ledger.close()


def _tracker_factory_for(tracker: FakeTracker) -> Any:
    def factory(**kwargs: object) -> FakeTracker:
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
    tracker = FakeTracker(agent_login=IDENTITY.login)
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)

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
    assert "run: the Run Ledger holds unfinished Attempts: #7/1" in err
    assert "run: startup complete in " in err
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        assert ledger.get_attempt("#7/1").state == "open"
    finally:
        ledger.close()


def test_run_ledger_refusal_names_run_ledger_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing pinned record refuses at run-ledger with no GitHub write."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")
    _StartupSpy(monkeypatch, ScriptedModel([]))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration={})
    finally:
        ledger.close()
    tracker = FakeTracker(agent_login=IDENTITY.login)
    config = _write_run_config(tmp_path)
    matrix = _write_run_matrix(tmp_path)

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

    exit_code, _ = _run(tmp_path, monkeypatch, FakeTracker(), max_sweeps=1)

    assert exit_code == 0
    assert "preflight" not in capsys.readouterr().err
