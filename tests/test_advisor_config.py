"""Static validation of the `advisor` tool group and `agent.advisor` mapping.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7, §2.8,
§3, §9.4 and §11.4 (the `advisor` group is valid exactly with an
`agent.advisor` mapping; `prompt` is one trusted prompt file; `model` names
a `models` id; the Advisor pin joins the referenced-pin set and the
per-step maxima).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support.workflow_config import load_config

ADVISOR_PROMPT = "Review the question under the operator role.\n"


def _write_prompt(tmp_path: Path) -> None:
    (tmp_path / "advisor-prompt.md").write_text(ADVISOR_PROMPT, encoding="utf-8")


def _config(
    agent_extra: str = "",
    *,
    step_limits: str = "",
    second_model: bool = False,
) -> str:
    models = (
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "    context-window-tokens: 1000000\n"
        "    context-threshold-tokens: 80000\n"
        "    output-cap-tokens: 32000\n"
    )
    if second_model:
        models += (
            "  opus:\n"
            "    provider: openai\n"
            "    model: gpt-5.6-luna\n"
            "    endpoint: responses\n"
            "    effort: high\n"
            "    api-key-env: OPENAI_API_KEY\n"
            "    prices-usd-per-mtok: {input: 15.00, output: 75.00,"
            " cache-read: 1.50, cache-write: 18.75}\n"
            "    context-window-tokens: 1000000\n"
            "    context-threshold-tokens: 80000\n"
            "    output-cap-tokens: 200000\n"
        )
    limits = f"\n        limits: {step_limits}" if step_limits else ""
    return (
        "version: 4\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        + models
        + "workflows:\n"
        "  triage:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:triage}\n"
        "    limits: {cost-usd: 60, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: triage\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      triage:\n"
        "        agent:\n"
        f"{agent_extra}"
        "          results:\n"
        "            done: {summary: document}\n"
        f"{limits}\n"
        "        routes: {done: {end: completed}}\n"
    )


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _load(tmp_path: Path, text: str) -> tuple[object, list[object]]:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return load_config(config)  # type: ignore[return-value]


def test_advisor_prompt_only_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _without_env(monkeypatch)
    _write_prompt(tmp_path)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: advisor-prompt.md}\n"
    )
    cfg, faults = _load(tmp_path, _config(agent))
    assert not faults, [str(fault) for fault in faults]
    assert cfg is not None
    step = cfg.workflows["triage"].steps["triage"]
    assert step.agent is not None
    assert step.agent.advisor is not None
    assert step.agent.advisor.prompt == "advisor-prompt.md"
    assert step.agent.advisor.model is None
    assert cfg.workflows["triage"].referenced_pins == ("sonnet",)


def test_advisor_with_model_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _without_env(monkeypatch)
    _write_prompt(tmp_path)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: advisor-prompt.md, model: opus}\n"
    )
    cfg, faults = _load(tmp_path, _config(agent, second_model=True))
    assert not faults, [str(fault) for fault in faults]
    assert cfg is not None
    step = cfg.workflows["triage"].steps["triage"]
    assert step.agent is not None
    assert step.agent.advisor is not None
    assert step.agent.advisor.model == "opus"
    assert cfg.workflows["triage"].referenced_pins == ("sonnet", "opus")


def test_advisor_group_without_mapping_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    agent = "          tools: [workspace-read, advisor]\n"
    _, faults = _load(tmp_path, _config(agent))
    assert faults
    assert any("agent.advisor" in str(fault) for fault in faults)


def test_advisor_mapping_without_group_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    _write_prompt(tmp_path)
    agent = (
        "          tools: [workspace-read]\n"
        "          advisor: {prompt: advisor-prompt.md}\n"
    )
    _, faults = _load(tmp_path, _config(agent))
    assert faults
    assert any("agent.tools" in str(fault) for fault in faults)


def test_advisor_unknown_model_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    _write_prompt(tmp_path)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: advisor-prompt.md, model: unknown}\n"
    )
    _, faults = _load(tmp_path, _config(agent))
    assert faults
    assert any("agent.advisor.model" in str(fault) for fault in faults)


def test_advisor_missing_prompt_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: advisor-prompt.md}\n"
    )
    _, faults = _load(tmp_path, _config(agent))
    assert faults
    assert any("agent.advisor.prompt" in str(fault) for fault in faults)


def test_advisor_untrusted_prompt_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: ../advisor-prompt.md}\n"
    )
    _, faults = _load(tmp_path, _config(agent))
    assert faults
    assert any("agent.advisor.prompt" in str(fault) for fault in faults)


def test_advisor_unknown_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    _write_prompt(tmp_path)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: advisor-prompt.md, instructions: [x]}\n"
    )
    _, faults = _load(tmp_path, _config(agent))
    assert faults
    assert any("agent.advisor" in str(fault) for fault in faults)


def test_advisor_per_step_model_stays_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    raw = _config("          tools: [workspace-read]\n")
    raw = raw.replace(
        "          results:\n            done: {summary: document}\n",
        "          model: sonnet\n          results:\n            done: {summary: document}\n",
        1,
    )
    _, faults = _load(tmp_path, raw)
    assert faults
    assert any("agent" in str(fault) for fault in faults)


def test_advisor_allowance_floor_covers_advisor_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_env(monkeypatch)
    _write_prompt(tmp_path)
    agent = (
        "          tools: [workspace-read, advisor]\n"
        "          advisor: {prompt: advisor-prompt.md, model: opus}\n"
    )
    # 150000 tokens holds the main pin's maximal hold (110000) but not the
    # Advisor pin's (78000 usable + 200000 output cap).
    cfg_text = _config(agent, step_limits="{effective-tokens: 150000}", second_model=True)
    _, faults = _load(tmp_path, cfg_text)
    assert faults
    assert any("steps.triage.limits.effective-tokens" in str(fault) for fault in faults)
