"""The Workflow Configuration may supply the Project Profile (ADR 0030).

Covers ``target.profile``: load resolution, the Effective Configuration
``project_profile`` section and Claim blobs, ``check-readiness`` facts,
final-checks disclosure and the Attempt-run pinned-profile helper.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coding_agent.cli import run_config_validate_command
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import archive as attempt_archives
from coding_agent.operator.store import OperatorStore
from coding_agent.profile.toolchain import (
    TOOLCHAIN_MATRIX_ENV,
    SupportedToolchainMatrix,
    load_toolchain_matrix,
)
from coding_agent.runtime.final_checks import (
    FinalChecksReport,
    candidate_changed_profile,
    final_checks_evidence_payload,
)
from coding_agent.runtime.step_operations import LocalOperationRunner, run_check_readiness
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.worker.attempt_run import pinned_profile_text
from coding_agent.worker.startup import (
    Ready,
    SkillDeploymentCheck,
    StartupRefusal,
    start,
)
from coding_agent.workflow.configuration import load_workflow_configuration
from coding_agent.workflow.effective import (
    collect_claim_blobs,
    is_full_effective_configuration,
)
from coding_agent.workflow.instance_profile import (
    InstanceProfileError,
    assert_instance_profile_toolchain,
    parse_instance_profile,
)
from coding_agent.workflow.schema import WorkflowStep
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from worker_fakes import WorkerFakes, make_worker_ports

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

MIN_CONFIG = """version: 3
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
workflows:
  w:
    model: m
    input: {type: issue, label: impl}
    limits: {cost-usd: 1.0, tokens: 10.0, tool-calls: 10.0, wall-clock-minutes: 10.0}
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


def _write_config(tmp_path: Path, *, profile: str | None) -> Path:
    text = MIN_CONFIG
    if profile is not None:
        text = text.replace(
            "  token-env: GITHUB_TOKEN\n",
            f"  token-env: GITHUB_TOKEN\n  profile: {profile}\n",
        )
    path = tmp_path / "workflow.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _matrix() -> SupportedToolchainMatrix:
    return load_toolchain_matrix(
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


class _Prober:
    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        return True


class _Runner(CommandRunner):
    def __init__(self, evidence_dir: Path) -> None:
        self._evidence_dir = evidence_dir
        self.calls: list[str] = []

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        self.calls.append(command)
        return ExecutedCommand(
            command=command, exit_code=0, stdout="ok", stderr=""
        )


def test_absent_profile_loads_with_no_text(tmp_path: Path) -> None:
    path = _write_config(tmp_path, profile=None)
    loaded, faults = load_workflow_configuration(path)
    assert faults == []
    assert loaded is not None
    assert loaded.config.target.profile is None
    assert loaded.profile_text is None


def test_present_profile_resolves_text(tmp_path: Path) -> None:
    (tmp_path / "project-profile.yml").write_text(GOOD_PROFILE, encoding="utf-8")
    path = _write_config(tmp_path, profile="project-profile.yml")
    loaded, faults = load_workflow_configuration(path)
    assert faults == []
    assert loaded is not None
    assert loaded.config.target.profile == "project-profile.yml"
    assert loaded.profile_text == GOOD_PROFILE


def test_escaping_profile_refuses_naming_target_profile(tmp_path: Path) -> None:
    path = _write_config(tmp_path, profile="../escape.yml")
    loaded, faults = load_workflow_configuration(path)
    assert loaded is None
    assert any(fault.path == "target.profile" for fault in faults)


def test_missing_profile_refuses_naming_target_profile(tmp_path: Path) -> None:
    path = _write_config(tmp_path, profile="missing.yml")
    loaded, faults = load_workflow_configuration(path)
    assert loaded is None
    assert any(fault.path == "target.profile" for fault in faults)


def test_invalid_utf8_profile_refuses(tmp_path: Path) -> None:
    (tmp_path / "bad.yml").write_bytes(b"\xff\xfe not utf-8\n")
    path = _write_config(tmp_path, profile="bad.yml")
    loaded, faults = load_workflow_configuration(path)
    assert loaded is None
    assert any(fault.path == "target.profile" for fault in faults)


def test_unparsable_profile_refuses_in_config_validate_parser() -> None:
    with pytest.raises(InstanceProfileError, match="target.profile"):
        parse_instance_profile("schema: [unclosed\n")


def test_unsupported_toolchain_refuses_naming_target_profile() -> None:
    parsed = parse_instance_profile(GOOD_PROFILE)
    matrix = load_toolchain_matrix(
        {
            "schema": 2,
            "toolchains": {
                "python": {
                    "version": "9.99.9",
                    "package_managers": {"uv": {"version": "0.6.1"}},
                },
            },
        }
    )
    with pytest.raises(InstanceProfileError, match="target.profile"):
        assert_instance_profile_toolchain(parsed, matrix)


def test_check_readiness_supplied_profile_records_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    outcome = run_check_readiness(
        workspace=workspace,
        profile_text=GOOD_PROFILE,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=_Prober(),
        runner=_Runner(evidence_dir),
        evidence_dir=evidence_dir,
        profile_source="workflow-configuration",
        profile_origin="workflow-configuration",
        repository_profile_ignored=True,
    )
    assert outcome.result == "ready"
    assert outcome.facts["profile_source"] == "workflow-configuration"
    assert outcome.facts["repository_profile_ignored"] is True


def test_check_readiness_repository_profile_records_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence_dir = tmp_path / "evidence"
    outcome = run_check_readiness(
        workspace=workspace,
        profile_text=GOOD_PROFILE,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=_Prober(),
        runner=_Runner(evidence_dir),
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "ready"
    assert outcome.facts["profile_source"] == "target-repository"
    assert "repository_profile_ignored" not in outcome.facts


def test_candidate_changed_profile_ignored_for_supplied_source(tmp_path: Path) -> None:
    assert (
        candidate_changed_profile(
            tmp_path,
            base_revision="a" * 40,
            head_revision="b" * 40,
            profile_source="workflow-configuration",
        )
        is False
    )


def test_final_checks_payload_switches_disclosure() -> None:
    supplied = FinalChecksReport(
        results=(),
        green=True,
        dirty_files=(),
        working_area="clean",
        base_revision="a" * 40,
        profile_changed=True,
        profile_source="workflow-configuration",
        profile_sha256="f" * 64,
    )
    payload = final_checks_evidence_payload(supplied)
    assert payload["validation_contract_source"] == "workflow-configuration"
    assert payload["validation_contract_sha256"] == "f" * 64
    assert "profile_changed_in_candidate" not in payload

    repo = FinalChecksReport(
        results=(),
        green=True,
        dirty_files=(),
        working_area="clean",
        base_revision="a" * 40,
        profile_changed=True,
    )
    repo_payload = final_checks_evidence_payload(repo)
    assert repo_payload["profile_changed_in_candidate"] is True


def test_claim_blobs_include_profile_bytes() -> None:
    blobs = collect_claim_blobs(
        prompt_texts={},
        activation_deliveries={},
        activation_components={},
        profile_text=GOOD_PROFILE,
    )
    assert (
        blobs[hashlib.sha256(GOOD_PROFILE.encode("utf-8")).hexdigest()]
        == GOOD_PROFILE.encode("utf-8")
    )


def test_old_record_without_project_profile_still_full() -> None:
    record: dict[str, object] = {
        "version": 2,
        "workflow": {
            "id": "w",
            "input": {"type": "issue", "label": "impl"},
            "outcomes": {},
        },
        "file": {"target": {"base_branch": "main"}},
        "prompts": {},
        "skill_bundle": {"members": {}},
        "activations": {},
        "pinned_model": {
            "prices": {
                "input": 1.0,
                "output": 1.0,
                "cache_read": 1.0,
                "cache_write": 1.0,
            },
            "output_cap_tokens": 1,
            "context_window": {
                "threshold_tokens": 1,
                "safety_margin_tokens": 1,
                "request_overhead_tokens": 1,
                "handoff_token_cap": 1,
            },
        },
    }
    assert is_full_effective_configuration(record) is True
    bad = dict(record)
    bad["project_profile"] = {"locator": "", "sha256": "x", "size_bytes": -1}
    assert is_full_effective_configuration(bad) is False


def test_pinned_profile_text_none_without_key(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.db")
    try:
        record: dict[str, object] = {
            "version": 2,
            "workflow": {
                "id": "w",
                "model": "m",
                "input": {
                    "type": "issue",
                    "label": "impl",
                    "require_all": [],
                    "require_any": [],
                    "exclude": [],
                    "drafts": "exclude",
                    "skip_open_agent_pr": False,
                },
                "outcomes": {},
                "start": "s",
                "steps": {},
            },
            "file": {"target": {"base_branch": "main"}},
            "prompts": {},
            "skill_bundle": {"members": {}},
            "activations": {},
            "pinned_model": {
                "prices": {
                    "input": 1.0,
                    "output": 1.0,
                    "cache_read": 1.0,
                    "cache_write": 1.0,
                },
                "output_cap_tokens": 1,
                "context_window": {
                    "threshold_tokens": 1,
                    "safety_margin_tokens": 1,
                    "request_overhead_tokens": 1,
                    "handoff_token_cap": 1,
                },
            },
        }
        ledger.open_attempt(
            attempt_id="w#1/1",
            effective_configuration=record,
            fingerprint=json.dumps({"x": 1}),
            base_revision="a" * 40,
        )
        assert pinned_profile_text(ledger, "w#1/1") is None
    finally:
        ledger.close()


# --- `config validate` with `target.profile` -----------------------------------

EDITED_PROFILE = GOOD_PROFILE.replace("bootstrap-cmd", "edited-bootstrap-cmd")
"""A second valid profile, observably different from :data:`GOOD_PROFILE`."""

SCHEMA_VIOLATING_PROFILE = "schema: 2\nlanguage: python\n"
"""Parses as YAML but violates the Project Profile schema (missing facts)."""


def _write_profile_config(
    tmp_path: Path, profile_text: str, *, profile_name: str = "project-profile.yml"
) -> Path:
    """A Workflow Configuration naming ``target.profile`` with this text."""
    (tmp_path / profile_name).write_text(profile_text, encoding="utf-8")
    return _write_config(tmp_path, profile=profile_name)


def _write_json_matrix(
    tmp_path: Path, *, python_version: str, filename: str = "toolchain-matrix.json"
) -> Path:
    """A Supported Toolchain Matrix file carrying this python version."""
    path = tmp_path / filename
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "toolchains": {
                    "python": {
                        "version": python_version,
                        "package_managers": {"uv": {"version": "0.6.1"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_config_validate_rejects_unparsable_target_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unparsable ``target.profile`` refuses naming the YAML path."""
    config = _write_profile_config(tmp_path, "schema: [unclosed\n")

    assert run_config_validate_command(config) == 1
    err = capsys.readouterr().err
    assert "config validate: FAILED: target.profile:" in err


def test_config_validate_rejects_schema_violating_target_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A schema-violating ``target.profile`` refuses before any matrix read."""
    config = _write_profile_config(tmp_path, SCHEMA_VIOLATING_PROFILE)

    assert run_config_validate_command(config) == 1
    err = capsys.readouterr().err
    assert "config validate: FAILED: target.profile:" in err


def test_config_validate_without_matrix_notices_and_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No matrix at the resolved path: a NOTICE, no toolchain check, exit 0."""
    config = _write_profile_config(tmp_path, GOOD_PROFILE)
    monkeypatch.setenv(TOOLCHAIN_MATRIX_ENV, str(tmp_path / "missing-matrix.json"))

    assert run_config_validate_command(config) == 0
    out = capsys.readouterr().out
    assert "config validate: NOTICE: no Supported Toolchain Matrix at " in out
    assert "skipping target.profile toolchain check" in out
    assert "config validate: OK" in out


def test_config_validate_malformed_matrix_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed matrix refuses naming ``target.profile``."""
    config = _write_profile_config(tmp_path, GOOD_PROFILE)
    matrix = tmp_path / "toolchain-matrix.json"
    matrix.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(TOOLCHAIN_MATRIX_ENV, str(matrix))

    assert run_config_validate_command(config) == 1
    err = capsys.readouterr().err
    assert "config validate: FAILED: target.profile:" in err
    assert "cannot load the Supported Toolchain Matrix" in err


def test_config_validate_unreadable_matrix_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable matrix refuses naming ``target.profile``."""
    config = _write_profile_config(tmp_path, GOOD_PROFILE)
    matrix = _write_json_matrix(tmp_path, python_version="3.13.9")
    monkeypatch.setenv(TOOLCHAIN_MATRIX_ENV, str(matrix))

    def _boom(path: Path) -> SupportedToolchainMatrix:
        raise OSError(f"cannot read {path}")

    monkeypatch.setattr(
        "coding_agent.profile.toolchain.load_supported_toolchain_matrix", _boom
    )

    assert run_config_validate_command(config) == 1
    err = capsys.readouterr().err
    assert "config validate: FAILED: target.profile:" in err
    assert "cannot load the Supported Toolchain Matrix" in err


def test_config_validate_rejects_unsupported_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile toolchain outside the loaded matrix refuses."""
    config = _write_profile_config(tmp_path, GOOD_PROFILE)
    monkeypatch.setenv(
        TOOLCHAIN_MATRIX_ENV, str(_write_json_matrix(tmp_path, python_version="9.99.9"))
    )

    assert run_config_validate_command(config) == 1
    err = capsys.readouterr().err
    assert "config validate: FAILED: target.profile: unsupported toolchain:" in err


def test_config_validate_accepts_supported_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile toolchain inside the loaded matrix validates cleanly."""
    config = _write_profile_config(tmp_path, GOOD_PROFILE)
    monkeypatch.setenv(
        TOOLCHAIN_MATRIX_ENV, str(_write_json_matrix(tmp_path, python_version="3.13.9"))
    )

    assert run_config_validate_command(config) == 0
    captured = capsys.readouterr()
    assert "config validate: OK" in captured.out
    assert "FAILED" not in captured.err
    assert "NOTICE" not in captured.out


# --- Startup phase `project-profile` -------------------------------------------

_STARTUP_CONFIG = """\
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


def _write_startup_config(tmp_path: Path, *, profile: str | None) -> Path:
    """A runnable startup config, optionally naming ``target.profile``."""
    text = _STARTUP_CONFIG
    if profile is not None:
        text = text.replace(
            "  token-env: GITHUB_TOKEN\n",
            f"  token-env: GITHUB_TOKEN\n  profile: {profile}\n",
        )
    path = tmp_path / "workflow.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_startup_matrix(tmp_path: Path, *, python_version: str = "3.13.9") -> Path:
    """A Supported Toolchain Matrix file startup accepts for :data:`GOOD_PROFILE`."""
    return _write_json_matrix(tmp_path, python_version=python_version)


def _startup_fakes(monkeypatch: pytest.MonkeyPatch) -> WorkerFakes:
    """Fake startup ports handing out a scripted model and tracker."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    return make_worker_ports(model=ScriptedModel([]), tracker=tracker)


def _deployment(tmp_path: Path) -> SkillDeploymentCheck:
    """The Skill Bundle deployment triple startup verifies (faked in tests)."""
    return SkillDeploymentCheck(
        policy=tmp_path / "skill-policy.json",
        bundle_dir=tmp_path / "bundle",
        home=tmp_path / "home",
    )


def _startup_check_rows(state_dir: Path) -> list[tuple[str, str]]:
    """The ``(check, result)`` of every ``startup_check`` event, in order."""
    connection = sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        rows = connection.execute(
            "SELECT check_name, result FROM operator_events"
            " WHERE type = 'startup_check' ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [(str(check), str(result)) for check, result in rows]


def _phase_indexes(lines: list[str], needle: str) -> int:
    for pos, line in enumerate(lines):
        if needle in line:
            return pos
    raise AssertionError(f"missing startup line: {needle!r}\n" + "\n".join(lines))


def test_startup_skips_project_profile_without_target_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``target.profile``: the phase skips and startup continues past it."""
    fakes = _startup_fakes(monkeypatch)
    config = _write_startup_config(tmp_path, profile=None)
    state_dir = tmp_path / "state"

    result = start(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_startup_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, Ready)
    try:
        err = capsys.readouterr().err
        assert (
            "run: startup: project-profile skipped (profile from Target Repository)"
            in err
        )
        lines = err.splitlines()
        assert _phase_indexes(lines, "run: startup: toolchain-matrix ok") < _phase_indexes(
            lines, "run: startup: project-profile skipped"
        )
        assert _phase_indexes(lines, "run: startup: project-profile skipped") < _phase_indexes(
            lines, "run: startup: skill-bundle started"
        )
        assert fakes.order[0] == "skill-bundle"
    finally:
        result.__exit__(None, None, None)


def test_startup_refuses_unparsable_target_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unparsable ``target.profile`` refuses at ``project-profile``."""
    fakes = _startup_fakes(monkeypatch)
    (tmp_path / "project-profile.yml").write_text("schema: [unclosed\n", encoding="utf-8")
    config = _write_startup_config(tmp_path, profile="project-profile.yml")

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_startup_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, StartupRefusal)
    assert result.phase == "project-profile"
    assert str(config) in result.message
    assert "target.profile" in result.message
    err = capsys.readouterr().err
    assert "run: startup refused at project-profile after " in err
    assert fakes.order == []


def test_startup_refuses_toolchain_outside_the_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile toolchain outside the loaded matrix refuses startup."""
    fakes = _startup_fakes(monkeypatch)
    (tmp_path / "project-profile.yml").write_text(GOOD_PROFILE, encoding="utf-8")
    config = _write_startup_config(tmp_path, profile="project-profile.yml")

    result = start(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_startup_matrix(tmp_path, python_version="9.99.9"),
        ports=fakes.ports,
    )

    assert isinstance(result, StartupRefusal)
    assert result.phase == "project-profile"
    assert str(config) in result.message
    assert "target.profile" in result.message
    assert "unsupported toolchain" in result.message
    err = capsys.readouterr().err
    assert "run: startup refused at project-profile after " in err
    assert fakes.order == []


def test_startup_accepts_valid_target_profile_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid ``target.profile`` passes between the matrix and the bundle."""
    fakes = _startup_fakes(monkeypatch)
    (tmp_path / "project-profile.yml").write_text(GOOD_PROFILE, encoding="utf-8")
    config = _write_startup_config(tmp_path, profile="project-profile.yml")
    state_dir = tmp_path / "state"

    result = start(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=_deployment(tmp_path),
        toolchain_matrix_path=_write_startup_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, Ready)
    try:
        assert result.claim_config.profile_text == GOOD_PROFILE
        err = capsys.readouterr().err
        lines = err.splitlines()
        assert _phase_indexes(lines, "run: startup: toolchain-matrix ok") < _phase_indexes(
            lines, "run: startup: project-profile ok"
        )
        assert _phase_indexes(lines, "run: startup: project-profile ok") < _phase_indexes(
            lines, "run: startup: skill-bundle started"
        )
        assert fakes.order == ["skill-bundle", "github", "provider"]
    finally:
        result.__exit__(None, None, None)
    assert _startup_check_rows(state_dir) == [
        ("toolchain-matrix", "started"),
        ("project-profile", "started"),
        ("skill-bundle", "started"),
        ("credentials", "started"),
        ("github-checks", "started"),
        ("provider-capability", "started"),
    ]


# --- The pin survives a profile edit after Claim --------------------------------


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _init_repo_with_profile(path: Path, profile_text: str) -> str:
    """A workspace repo whose committed profile carries this text."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "agent@example.com", cwd=path)
    _git("config", "user.name", "agent", cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    profile_path = path / "docs" / "agents" / "project-profile.yml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile_text, encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "add profile", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _ledger_with_instance(state_dir: Path, repo: str) -> tuple[RunLedger, OperatorStore]:
    """A ledger whose instance row resolves an Attempt Archive under ``state_dir``."""
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    store = OperatorStore(state_dir / "run-ledger.sqlite3")
    store.write_instance_metadata(
        target_repository=repo,
        process_started_at=datetime.now(timezone.utc),
    )
    return ledger, store


def _pinning_record(profile_text: str | None) -> dict[str, object]:
    """A minimal Effective Configuration record, pinned with this profile text."""
    record: dict[str, object] = {
        "version": 2,
        "workflow": {
            "id": "w",
            "model": "m",
            "input": {
                "type": "issue",
                "label": "impl",
                "require_all": [],
                "require_any": [],
                "exclude": [],
                "drafts": "exclude",
                "skip_open_agent_pr": False,
            },
            "outcomes": {},
            "start": "s",
            "steps": {},
        },
        "file": {"target": {"base_branch": "main"}},
        "prompts": {},
        "skill_bundle": {"members": {}},
        "activations": {},
        "pinned_model": {
            "prices": {
                "input": 1.0,
                "output": 1.0,
                "cache_read": 1.0,
                "cache_write": 1.0,
            },
            "output_cap_tokens": 1,
            "context_window": {
                "threshold_tokens": 1,
                "safety_margin_tokens": 1,
                "request_overhead_tokens": 1,
                "handoff_token_cap": 1,
            },
        },
    }
    if profile_text is not None:
        raw = profile_text.encode("utf-8")
        record["project_profile"] = {
            "locator": "project-profile.yml",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    return record


def _store_pinned_blob(state_dir: Path, repo: str, attempt_id: str, raw: bytes) -> None:
    """Archive the pinned profile bytes the way Claim archives them."""
    archive_dir = attempt_archives.archive_dir_for(state_dir, repo, attempt_id)
    attempt_archives.store_content_blobs(
        archive_dir, {hashlib.sha256(raw).hexdigest(): raw}
    )


def test_pinned_profile_text_survives_config_edit_after_claim(
    tmp_path: Path,
) -> None:
    """Recovery runs the archived bytes, not the edited file beside the config."""
    profile_path = tmp_path / "project-profile.yml"
    profile_path.write_text(GOOD_PROFILE, encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ledger, store = _ledger_with_instance(state_dir, "o/r")
    try:
        ledger.open_attempt("w#1/1", effective_configuration=_pinning_record(GOOD_PROFILE))
        _store_pinned_blob(state_dir, "o/r", "w#1/1", GOOD_PROFILE.encode("utf-8"))
        profile_path.write_text(EDITED_PROFILE, encoding="utf-8")

        assert pinned_profile_text(ledger, "w#1/1") == GOOD_PROFILE
    finally:
        ledger.close()
        store.close()


def test_pinned_override_runs_ahead_of_the_repository_profile(
    tmp_path: Path,
) -> None:
    """The pinned bytes drive the Attempt even where the repository moved on."""
    repo = tmp_path / "repo"
    head = _init_repo_with_profile(repo, EDITED_PROFILE)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    command_runner = _Runner(evidence_dir)
    runner = LocalOperationRunner(
        workspace=repo,
        start_commit=head,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=_Prober(),
        runner=command_runner,
        evidence_dir=evidence_dir,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        attempt_branch="agent/w/1/1-slug",
        input_type="issue",
        profile_text_override=GOOD_PROFILE,
    )
    ledger = RunLedger(tmp_path / "ledger.db")
    try:
        execution = runner.run(
            WorkflowStep(id="preflight", operation="check-readiness", routes={}),
            1,
            "w#1/1",
            ledger,
        )
        assert execution.result == "ready"
        assert execution.facts["profile_source"] == "workflow-configuration"
        assert command_runner.calls and command_runner.calls[0] == "bootstrap-cmd"
    finally:
        ledger.close()


def test_unpinned_attempt_reads_the_repository_profile(
    tmp_path: Path,
) -> None:
    """Without ``project_profile`` the Attempt still comes from the repository.

    The current configuration may name ``target.profile`` with different
    text; an Attempt pinned before that change keeps reading the Target
    Repository profile at its start commit.
    """
    config = _write_profile_config(tmp_path, EDITED_PROFILE)
    loaded, faults = load_workflow_configuration(config)
    assert faults == []
    assert loaded is not None
    assert loaded.profile_text == EDITED_PROFILE

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ledger, store = _ledger_with_instance(state_dir, "o/r")
    try:
        ledger.open_attempt("w#1/1", effective_configuration=_pinning_record(None))
        assert pinned_profile_text(ledger, "w#1/1") is None
    finally:
        ledger.close()
        store.close()

    repo = tmp_path / "repo"
    head = _init_repo_with_profile(repo, GOOD_PROFILE)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    inner = RunLedger(tmp_path / "ledger.db")
    try:
        command_runner = _Runner(evidence_dir)
        runner = LocalOperationRunner(
            workspace=repo,
            start_commit=head,
            matrix=_matrix(),
            service_env={"DB_URL": "tcp://db.local:5432"},
            prober=_Prober(),
            runner=command_runner,
            evidence_dir=evidence_dir,
            author_name="agent",
            author_email="1+agent@users.noreply.github.com",
            attempt_branch="agent/w/1/1-slug",
            input_type="issue",
            profile_text_override=None,
        )
        execution = runner.run(
            WorkflowStep(id="preflight", operation="check-readiness", routes={}),
            1,
            "w#1/1",
            inner,
        )
        assert execution.result == "ready"
        assert execution.facts["profile_source"] == "target-repository"
        assert command_runner.calls and command_runner.calls[0] == "bootstrap-cmd"
    finally:
        inner.close()
