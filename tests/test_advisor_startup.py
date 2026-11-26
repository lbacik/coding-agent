"""Startup asserts the Advisor pin from a real `advisor` declaration.

Contract: step-workflow contract §4.1 (the Provider Capability Assertion
runs for every distinct pin the selected Workflow references) and §9.11
(each step's Advisor pin joins the referenced-pin set). Unlike the
foundation-slice tests in ``test_referenced_pins.py`` — which monkeypatch
``Workflow.referenced_pins`` because no YAML could express two pins — these
tests drive a real ``agent.advisor`` declaration: the Advisor pin is
asserted as well as the Workflow's pin, and the status map shows both. A
Workflow without ``advisor`` probes and reports one pin.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from coding_agent.operator import offline as offline_reader
from coding_agent.skillbundle.deployment import (
    RunVerificationRefusal,
    VerifiedRunInputs,
    _prompt_locators,
    _read_prompt_raw_bytes,
)
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration
from support.scripted_model import ScriptedModel
from test_worker_startup_progress import _fakes, _run
from worker_fakes import WorkerFakes

_ADVISOR_CONFIG = """\
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
  opus:
    provider: openai
    model: gpt-5.6-luna
    endpoint: responses
    effort: high
    api-key-env: OPENAI_API_KEY
    prices-usd-per-mtok: {input: 5.00, output: 25.00, cache-read: 0.50, cache-write: 6.25}
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 200000

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
__AGENT_BLOCK__
          results:
            done: {}
        routes: {done: {end: completed}}
"""

def _advisor_agent_block(advisor_model: str | None) -> str:
    """The declaring step's agent block, with or without a distinct pin."""
    if advisor_model is None:
        return (
            "          tools: [workspace-read, advisor]\n"
            "          advisor: {prompt: advisor-prompt.md}\n"
        )
    return (
        "          tools: [workspace-read, advisor]\n"
        f"          advisor: {{prompt: advisor-prompt.md, model: {advisor_model}}}\n"
    )

_PLAIN_AGENT_BLOCK = """\
          tools: [workspace-read]
"""

_ADVISOR_PROMPT_TEXT = "Review the question under the operator role.\n"


def _write_advisor_config(
    tmp_path: Path, *, with_advisor: bool, advisor_model: str | None = "opus"
) -> Path:
    """A two-``models`` configuration, with or without an advisor step."""
    if with_advisor:
        agent_block = _advisor_agent_block(advisor_model)
    else:
        agent_block = _PLAIN_AGENT_BLOCK
    path = tmp_path / "workflow.yml"
    path.write_text(
        _ADVISOR_CONFIG.replace("__AGENT_BLOCK__", agent_block), encoding="utf-8"
    )
    (tmp_path / "advisor-prompt.md").write_text(
        _ADVISOR_PROMPT_TEXT, encoding="utf-8"
    )
    return path


def _tracker() -> Any:
    from support.fake_step_tracker import FakeStepTracker

    from coding_agent.identity.startup import Identity

    identity = Identity(account_id=999, login="coding-agent")
    return FakeStepTracker(
        agent_login=identity.login, agent_id=identity.account_id
    )


def _advisor_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fakes: WorkerFakes,
    capsys: pytest.CaptureFixture[str],
    *,
    with_advisor: bool,
    advisor_model: str | None = "opus",
) -> tuple[int, Path]:
    """Run the Worker over the advisor config; return exit and state dir."""
    config = _write_advisor_config(
        tmp_path, with_advisor=with_advisor, advisor_model=advisor_model
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    # The fake skill-bundle port returns no prompt bytes, so collect them
    # through the production seam: every `_prompt_locators` locator —
    # including `advisor.prompt` — read as raw bytes. A regression that
    # drops the advisor locator surfaces here as the startup `KeyError`
    # production would hit.
    real_verify = fakes.skills.verify_run_inputs

    def _verify_with_prompts(
        *,
        config: LoadedWorkflowConfiguration,
        policy: Path,
        bundle_dir: Path,
        home: Path,
    ) -> VerifiedRunInputs | RunVerificationRefusal:
        result = real_verify(
            config=config, policy=policy, bundle_dir=bundle_dir, home=home
        )
        if isinstance(result, RunVerificationRefusal):
            return result
        base = config.path.parent.resolve()
        raws: dict[str, bytes] = {}
        for locator in _prompt_locators(config.config):
            raw, problem = _read_prompt_raw_bytes(base, locator)
            assert raw is not None, problem
            raws[locator] = raw
        return dataclasses.replace(result, prompt_raws=raws)

    monkeypatch.setattr(fakes.skills, "verify_run_inputs", _verify_with_prompts)
    exit_code, _ = _run(
        tmp_path,
        monkeypatch,
        _tracker(),
        config=config,
        max_sweeps=1,
        fakes=fakes,
    )
    # Drain stderr: this helper asserts on the ledger and status map, not
    # on startup log lines.
    capsys.readouterr()
    return exit_code, tmp_path / "state"


def test_startup_asserts_the_advisor_pin_from_a_real_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Advisor pin is probed after the Workflow pin; both are reported."""
    fakes = _fakes(monkeypatch, _tracker(), ScriptedModel([]))
    exit_code, state_dir = _advisor_run(
        tmp_path, monkeypatch, fakes, capsys, with_advisor=True
    )

    assert exit_code == 0
    assert [pin.model for pin in fakes.model.build_calls] == [
        "claude-sonnet-5",
        "gpt-5.6-luna",
    ]
    result = offline_reader.load_offline_status(
        state_dir / "run-ledger.sqlite3", state_dir
    )
    capability = result["provider_capability"]
    assert isinstance(capability, dict)
    assert set(capability) == {"sonnet", "opus"}
    assert capability["sonnet"]["pin_key"] == "anthropic:claude-sonnet-5@messages"
    assert capability["opus"]["pin_key"] == "openai:gpt-5.6-luna@responses"


def test_startup_without_advisor_probes_and_reports_one_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Workflow without ``advisor`` probes and reports one pin."""
    fakes = _fakes(monkeypatch, _tracker(), ScriptedModel([]))
    exit_code, state_dir = _advisor_run(
        tmp_path, monkeypatch, fakes, capsys, with_advisor=False
    )

    assert exit_code == 0
    assert [pin.model for pin in fakes.model.build_calls] == ["claude-sonnet-5"]
    result = offline_reader.load_offline_status(
        state_dir / "run-ledger.sqlite3", state_dir
    )
    capability = result["provider_capability"]
    assert isinstance(capability, dict)
    assert set(capability) == {"sonnet"}


def test_startup_advisor_without_model_probes_and_reports_one_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ``advisor`` without ``model`` uses the Pinned Model: one probe."""
    fakes = _fakes(monkeypatch, _tracker(), ScriptedModel([]))
    exit_code, state_dir = _advisor_run(
        tmp_path, monkeypatch, fakes, capsys, with_advisor=True, advisor_model=None
    )

    assert exit_code == 0
    assert [pin.model for pin in fakes.model.build_calls] == ["claude-sonnet-5"]
    result = offline_reader.load_offline_status(
        state_dir / "run-ledger.sqlite3", state_dir
    )
    capability = result["provider_capability"]
    assert isinstance(capability, dict)
    assert set(capability) == {"sonnet"}
