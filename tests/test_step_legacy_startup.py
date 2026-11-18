"""Legacy Attempt startup refusal (contract §16.4, REC-11, S10).

The step-workflow Worker refuses to start on a state volume holding any
Attempt written by the MVP runtime that is not ``finished`` or ``released``.
Finished legacy Attempts remain read-only history. Active from S10 only.
"""

from __future__ import annotations

import json
from pathlib import Path

from coding_agent.attempt_id import AttemptId
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import (
    RunLedger,
    legacy_unfinished_attempts,
    malformed_unfinished_attempts,
)
from coding_agent.worker import supervisor
from coding_agent.worker.run import HALT_LEGACY_ATTEMPT, HALT_MALFORMED_ATTEMPT
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from worker_fakes import make_worker_ports

IDENTITY = Identity(account_id=999, login="coding-agent")


def test_legacy_attempt_id_distinguishes_mvp_from_step_ids() -> None:
    """MVP ``#N/n`` is a Legacy Attempt Id; a step id is not (§16.4)."""
    parsed_legacy = AttemptId.try_parse("#7/1")
    assert parsed_legacy is not None and parsed_legacy.is_legacy is True
    parsed_step = AttemptId.try_parse("implement#7/1")
    assert parsed_step is not None and parsed_step.is_legacy is False
    parsed_other = AttemptId.try_parse("review#42/2")
    assert parsed_other is not None and parsed_other.is_legacy is False
    assert AttemptId.try_parse("garbage") is None


def test_legacy_unfinished_attempts_ignores_finished_history(
    tmp_path: Path,
) -> None:
    """Only unfinished legacy Attempts refuse startup; finished stay history."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration={"workflow": "x"})
        ledger.open_attempt("#8/1", effective_configuration={"workflow": "x"})
        ledger.open_attempt(
            "implement#9/1", effective_configuration={"workflow": "x"}
        )
        ledger.set_attempt_state("#8/1", "finished")
        ledger.set_attempt_state("implement#9/1", "finished")
        unfinished = ledger.unfinished_attempts()
        legacy = legacy_unfinished_attempts(unfinished)
        assert [attempt.attempt_id for attempt in legacy] == ["#7/1"]
    finally:
        ledger.close()


def test_startup_halts_on_unfinished_legacy_attempt(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    """REC-11: startup halts, telling the operator to drain with the old release."""
    from _pytest.monkeypatch import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    config_text = """\
version: 3

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
    config_path = tmp_path / "workflow.yml"
    config_path.write_text(config_text, encoding="utf-8")
    matrix_path = tmp_path / "toolchain-matrix.json"
    matrix_path.write_text(
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

    state_dir = tmp_path / "state"
    seed = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        seed.open_attempt("#7/1", effective_configuration={"workflow": "x"})
    finally:
        seed.close()

    tracker = FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    fakes = make_worker_ports(model=ScriptedModel([]), tracker=tracker)

    exit_code = supervisor.run_worker(
        config=config_path,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=matrix_path,
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )
    assert exit_code == 1
    assert HALT_LEGACY_ATTEMPT == "legacy-attempt"
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "drain" in err
    assert "#7/1" in err

    check = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        rows = check.unfinished_attempts()
        assert [attempt.attempt_id for attempt in rows] == ["#7/1"]
    finally:
        check.close()


def test_startup_halts_on_malformed_attempt_id(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    """A ledger row that is not an Attempt Id refuses with its own reason.

    The refusal names the id and never uses the unfinished-MVP message.
    """
    from _pytest.monkeypatch import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    config_text = """\
version: 3

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
    config_path = tmp_path / "workflow.yml"
    config_path.write_text(config_text, encoding="utf-8")
    matrix_path = tmp_path / "toolchain-matrix.json"
    matrix_path.write_text(
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

    state_dir = tmp_path / "state"
    seed = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        seed.open_attempt("garbage", effective_configuration={"workflow": "x"})
    finally:
        seed.close()

    tracker = FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    fakes = make_worker_ports(model=ScriptedModel([]), tracker=tracker)

    exit_code = supervisor.run_worker(
        config=config_path,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=matrix_path,
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )
    assert exit_code == 1
    assert HALT_MALFORMED_ATTEMPT == "malformed-attempt"
    assert HALT_MALFORMED_ATTEMPT != HALT_LEGACY_ATTEMPT
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "not an Attempt Id" in err
    assert "garbage" in err
    assert "unfinished MVP" not in err

    check = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        rows = check.unfinished_attempts()
        assert [attempt.attempt_id for attempt in rows] == ["garbage"]
    finally:
        check.close()
