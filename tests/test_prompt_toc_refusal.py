"""A prompt changed after the single read refuses at the skill-bundle phase.

Contract: run verification carries the raw bytes of every prompt locator it
hashed; a prompt file mutated after preparation no longer matches the
validation record, so ``verify_run_inputs`` refuses with a ``binding``-phase
fault and ``run_worker`` refuses startup at ``skill-bundle`` — never later at
``run-ledger``. Self-contained: the prepare helpers below mirror the
Phase-API seam pattern without importing private helpers from other tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.skillbundle.deployment import (
    RunVerificationRefusal,
    prepare_skill_bundle,
    verify_run_inputs,
)
from coding_agent.skillbundle.installer import FakeInstallerAdapter
from coding_agent.worker import supervisor
from coding_agent.worker.ports import ProductionSkillBundlePort, WorkerPorts
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from worker_fakes import make_worker_ports
from support.skill_bundle import build_policy_document, write_policy_file

PROMPT_LOCATOR = "prompts/greet.md"
PROMPT_BEFORE = "Greet prompt bytes.\n"
PROMPT_AFTER = "Mutated greet prompt bytes.\n"


def _config_text() -> str:
    """A runnable ``version: 3`` config whose workflow names one prompt."""
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
        "          instructions:\n"
        f"            - prompt: {PROMPT_LOCATOR}\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: {end: completed}}\n"
    )


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _load_config(config: Path) -> LoadedWorkflowConfiguration:
    """Load the Workflow Configuration through the single loader."""
    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(fault) for fault in faults]
    return loaded


def _prepare_with_prompt(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Prepare the empty deployment binding one prompt; returns roots."""
    config = tmp_path / "workflow.yml"
    config.write_text(_config_text(), encoding="utf-8")
    prompt_file = tmp_path / PROMPT_LOCATOR
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(PROMPT_BEFORE, encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    prepared = prepare_skill_bundle(
        config=_load_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    return config, policy, bundle_dir, home


def _write_matrix(tmp_path: Path) -> Path:
    """A minimal Supported Toolchain Matrix for the startup matrix phase."""
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


def test_mutated_prompt_refuses_with_a_binding_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt changed after the single read refuses verification: the
    validation record no longer binds the current prompt bytes."""
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _prepare_with_prompt(tmp_path)
    (tmp_path / PROMPT_LOCATOR).write_text(PROMPT_AFTER, encoding="utf-8")

    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )

    assert isinstance(result, RunVerificationRefusal)
    assert result.error is None
    binding = [fault for fault in result.faults if fault.phase == "binding"]
    assert binding
    assert any(
        "does not bind the current prompt bytes" in fault.detail for fault in binding
    )


def test_mutated_prompt_refuses_startup_at_skill_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the mutated prompt refuses startup at ``skill-bundle``
    with no Attempt and no ``run-ledger`` phase."""
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _prepare_with_prompt(tmp_path)
    (tmp_path / PROMPT_LOCATOR).write_text(PROMPT_AFTER, encoding="utf-8")
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=bundle_dir, home=home
    )

    fakes = make_worker_ports(
        model=ScriptedModel([]), tracker=FakeStepTracker()
    )
    ports = WorkerPorts(
        github=fakes.github,
        model=fakes.model,
        skills=ProductionSkillBundlePort(),
    )
    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=deployment,
        toolchain_matrix_path=_write_matrix(tmp_path),
        ports=ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "startup refused at skill-bundle" in err
    assert "run-ledger started" not in err
