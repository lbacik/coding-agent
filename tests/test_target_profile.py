"""The Workflow Configuration may supply the Project Profile (ADR 0030).

Covers ``target.profile``: load resolution, the Effective Configuration
``project_profile`` section and Claim blobs, ``check-readiness`` facts,
final-checks disclosure and the Attempt-run pinned-profile helper.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coding_agent.profile.toolchain import (
    SupportedToolchainMatrix,
    load_toolchain_matrix,
)
from coding_agent.runtime.final_checks import (
    FinalChecksReport,
    candidate_changed_profile,
    final_checks_evidence_payload,
)
from coding_agent.runtime.step_operations import run_check_readiness
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
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
            "schema": 1,
            "toolchains": {
                "python": {
                    "version": "3.13.9",
                    "package_manager": {"name": "uv", "version": "0.6.1"},
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
            "schema": 1,
            "toolchains": {
                "python": {
                    "version": "9.99.9",
                    "package_manager": {"name": "uv", "version": "0.6.1"},
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
    from coding_agent.ledger.store import RunLedger
    from coding_agent.worker.attempt_run import pinned_profile_text

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
