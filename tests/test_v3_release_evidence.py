"""Local CLI release evidence for Workflow Configuration v3 (issue #404).

Contract: the phase-API seam proves the same guarantees the release claims,
without Docker — ``config validate`` (secret-free), ``prepare-skill-bundle
--mode installed`` (one completed generation), ``verify-skill-bundle``
(offline, read-only) and ``run`` startup (full offline re-verification
before any secret or live check) against real deployment roots and a real
managed store under ``tmp_path``. The version pins below are the release
record in ``docs/plan/v3-release-evidence.md``: changing any of them without
updating that record fails this test on purpose.

Matrix row ids: SKV3-20 (secret-free build phases), SKV3-21 (happy-path
startup the selected-secret refusals presuppose), SKV3-17 (verbatim
deliveries for a populated bundle), SKV3-33 (local-CLI half).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.identity.startup import Identity, StartupCheckResult
from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION
from coding_agent.skillbundle.deployment import (
    VerifiedRunInputs,
    verify_run_inputs,
)
from coding_agent.workflow.configuration import load_workflow_configuration
from coding_agent.skillbundle.installer import (
    FAKE_INSTALLER_VERSION,
    MIN_INSTALLER_VERSION,
    REAL_INSTALL_REPORT_SCHEMA_VERSION,
    SUPPORTED_INSTALL_REPORT_SCHEMA_VERSIONS,
    SUPPORTED_SKILL_HASH_ALGORITHMS,
    SKILL_TREE_ALGORITHM_V2,
    base_store_for,
    write_fake_store,
)
from coding_agent.skillbundle.policy import POLICY_KIND, POLICY_SCHEMA_VERSION
from coding_agent.skillbundle.records import SCHEMA_VERSION, WORKFLOW_CONFIG_VERSION
from coding_agent.worker import supervisor
from coding_agent.workflow.effective import EFFECTIVE_CONFIGURATION_VERSION
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    fake_store_spec_for,
    skills_entries_for,
    write_policy_file,
)
from test_worker_run import (
    _assert_no_attempt_opened,
    _tracker_with,
    _write_run_matrix,
)


def _without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run with the credential names unset, isolated from the ambient env.

    A bare ``delenv`` on an already-unset variable records nothing to
    restore, leaking later ``setenv`` calls into other tests; setting a
    placeholder first keeps the isolation hermetic either way.
    """
    for var in (
        "GITHUB_TOKEN",
        "SANDBOX_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LITELLM_PROXY_API_KEY",
    ):
        monkeypatch.setenv(var, "test-placeholder")
        monkeypatch.delenv(var)


def _run_config() -> str:
    """A runnable ``version: 3`` config with an empty Skill Bundle."""
    return (
        "version: 3\n"
        "target:\n"
        "  kind: github\n"
        "  repository: example-owner/example-repo\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "worker:\n"
        "  poll-interval-seconds: 5\n"
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: work\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      work:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: {end: completed}}\n"
    )


def _stub_live_collaborators(monkeypatch: pytest.MonkeyPatch) -> ScriptedModel:
    """Stub the live checks; the deployment verification stays real."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    model = ScriptedModel([])

    def _github(*args: object, **kwargs: object) -> StartupCheckResult:
        return StartupCheckResult(
            identity=Identity(account_id=999, login="coding-agent"),
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        )

    def _build(pin: object, **kwargs: object) -> ScriptedModel:
        return model

    def _provider(pin: object, model_arg: object, table: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(supervisor, "run_startup_checks", _github)
    monkeypatch.setattr(supervisor, "build_chat_model", _build)
    monkeypatch.setattr(supervisor, "assert_provider_capability", _provider)
    return model


def test_local_cli_phases_against_a_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``validate`` → ``prepare`` → ``verify`` → ``run`` startup, no Docker.

    Every phase runs against real deployment roots (configuration, prompts,
    policy, bundle directory, managed store) under ``tmp_path`` with no
    Worker GitHub or model secrets set for the build phases. ``run``
    startup then repeats the full offline verification before the stubbed
    live checks and reaches readiness with no Attempt to work.
    """
    _without_secrets(monkeypatch)
    config = tmp_path / "workflow.yml"
    config.write_text(_run_config(), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert "config validate: OK" in capsys.readouterr().out

    assert (
        cli.main(
            [
                "prepare-skill-bundle",
                "--mode",
                "installed",
                "--config",
                str(config),
                "--policy",
                str(policy),
                "--bundle-dir",
                str(bundle_dir),
                "--home",
                str(home),
            ]
        )
        == 0
    )
    capsys.readouterr()
    for generated in ("requirements.json", "manifest.json", "validation.json"):
        record = json.loads((bundle_dir / generated).read_bytes().decode("utf-8"))
        assert record["schemaVersion"] == 1, generated
    assert (bundle_dir / "requirements.json").read_bytes()
    assert (bundle_dir / "manifest.json").read_bytes()
    assert (bundle_dir / "validation.json").read_bytes()

    assert (
        cli.main(
            [
                "verify-skill-bundle",
                "--config",
                str(config),
                "--policy",
                str(policy),
                "--bundle-dir",
                str(bundle_dir),
                "--home",
                str(home),
            ]
        )
        == 0
    )
    assert "verified" in capsys.readouterr().out

    _stub_live_collaborators(monkeypatch)
    tracker: FakeStepTracker = _tracker_with()
    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=policy, bundle_dir=bundle_dir, home=home
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        tracker_factory=lambda **kwargs: tracker,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )
    assert exit_code == 0
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")


def test_release_version_pins() -> None:
    """The exact versions ``docs/plan/v3-release-evidence.md`` records.

    Bumping the installer, a report format, the hash algorithm, the
    generated-record schema, the policy schema, the Effective Configuration
    record, the delivery framing or the Workflow Configuration version
    without updating the release record fails here on purpose.
    """
    assert MIN_INSTALLER_VERSION == "0.8.0"
    assert REAL_INSTALL_REPORT_SCHEMA_VERSION == 3
    assert SUPPORTED_INSTALL_REPORT_SCHEMA_VERSIONS == (3,)
    assert SKILL_TREE_ALGORITHM_V2 == "agent-installer-tree-v2"
    assert SUPPORTED_SKILL_HASH_ALGORITHMS == (SKILL_TREE_ALGORITHM_V2,)
    assert FAKE_INSTALLER_VERSION == "fake-installer-1"
    assert SCHEMA_VERSION == 1
    assert WORKFLOW_CONFIG_VERSION == 3
    assert POLICY_SCHEMA_VERSION == 1
    assert POLICY_KIND == "coding-agent.skill-bundle-policy"
    assert EFFECTIVE_CONFIGURATION_VERSION == 2
    assert DELIVERY_FRAMING_VERSION == 1


def _skills_yaml(members: list[DeclaredMember]) -> str:
    """Render the ``skills:`` declaration for the declared members."""
    lines = ["skills:"]
    for entry in skills_entries_for(members):
        source = entry["source"]
        assert isinstance(source, str)
        lines.append(f"  - source: {source}")
        lines.append("    include:")
        include = entry["include"]
        assert isinstance(include, list)
        for name in include:
            lines.append(f"      - {name}")
        if "ref" in entry:
            lines.append(f"    ref: {entry['ref']}")
    return "\n".join(lines) + "\n"


def test_local_cli_phases_with_declared_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local sequence with a populated bundle: two declared members on
    distinct sources go through ``config validate``, ``prepare --mode
    installed`` and ``verify-skill-bundle``, and the run seam collects both
    members' bodies verbatim from the real managed store (SKV3-17, SKV3-33
    local-CLI half with content)."""
    _without_secrets(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(name="beta", source=SOURCE_B, commit=COMMIT_B),
    ]
    config = tmp_path / "workflow.yml"
    config.write_text(_run_config() + _skills_yaml(members), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert (
        cli.main(
            [
                "prepare-skill-bundle",
                "--mode",
                "installed",
                "--config",
                str(config),
                "--policy",
                str(policy),
                "--bundle-dir",
                str(bundle_dir),
                "--home",
                str(home),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "verify-skill-bundle",
                "--config",
                str(config),
                "--policy",
                str(policy),
                "--bundle-dir",
                str(bundle_dir),
                "--home",
                str(home),
            ]
        )
        == 0
    )

    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(fault) for fault in faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs), (
        [str(fault) for fault in result.faults] if not isinstance(result, VerifiedRunInputs) else ""
    )
    assert tuple(m.name for m in result.members) == ("alpha", "beta")
    for member in members:
        expected = (base_store_for(home) / member.name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert result.registry.bodies[member.name] == expected
