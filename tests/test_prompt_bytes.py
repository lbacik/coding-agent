"""Prompt files are raw bytes: validation refuses bad files, the model sees exact bytes.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.8 (paths
resolve against the YAML directory and stay inside it after symlink
resolution; prompt content is used verbatim), §2.9 (every validation
failure exits non-zero naming the YAML path) and §9.3 (instructions reach
the model verbatim).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    StepAttemptFacts,
    compose_step_opening,
)
from coding_agent.workflow import validate_config_file
from coding_agent.workflow.v2_schema import V2Instruction
from coding_agent.worker.supervisor import load_prompt_texts


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _minimal_config_with_prompts(*prompt_lines: str) -> str:
    instructions = "\n".join(f"            - prompt: {line}" for line in prompt_lines)
    return (
        "version: 3\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
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
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: review\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      review:\n"
        "        agent:\n"
        "          instructions:\n"
        f"{instructions}\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            reviewed: {body: document}\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _facts() -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id="review#7/1",
        workflow_id="review",
        input_type="pull-request",
        input_number=7,
        title="t",
        body="b",
        base_revision="abc123",
        current_revision="abc123",
    )


# --- Phase-API seam: validate_config_file --------------------------------------


def test_missing_prompt_is_refused_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config_with_prompts("prompts/missing.md"))

    cfg, faults = validate_config_file(config)

    assert cfg is None
    assert faults
    text = " ".join(str(fault) for fault in faults)
    assert "workflows.review.steps.review.agent.instructions[0]" in text
    assert "prompts/missing.md" in text
    assert "does not exist" in text


def test_invalid_utf8_prompt_is_refused_without_echoing_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    secret = "SECRET-BYTES-MUST-NOT-APPEAR-9f3a"
    prompt = tmp_path / "bad.md"
    prompt.write_bytes(secret.encode("utf-8") + b"\xff\xfe")
    config = _write_config(tmp_path, _minimal_config_with_prompts("bad.md"))

    cfg, faults = validate_config_file(config)

    assert cfg is None
    assert faults
    text = " ".join(str(fault) for fault in faults)
    assert "workflows.review.steps.review.agent.instructions[0]" in text
    assert "bad.md" in text
    assert "UTF-8" in text
    assert secret not in text


def test_unreadable_prompt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    prompt = tmp_path / "secret.md"
    prompt.write_text("operator bytes", encoding="utf-8")
    config = _write_config(tmp_path, _minimal_config_with_prompts("secret.md"))

    def _boom(self: Path, *args: object, **kwargs: object) -> bytes:
        raise PermissionError("denied by operator")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    cfg, faults = validate_config_file(config)

    assert cfg is None
    assert faults
    text = " ".join(str(fault) for fault in faults)
    assert "workflows.review.steps.review.agent.instructions[0]" in text
    assert "secret.md" in text
    assert "operator bytes" not in text


def test_contained_symlink_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    real = tmp_path / "real.md"
    real.write_text("contained bytes", encoding="utf-8")
    (tmp_path / "link.md").symlink_to(real)
    config = _write_config(tmp_path, _minimal_config_with_prompts("link.md"))

    cfg, faults = validate_config_file(config)

    assert not faults
    assert cfg is not None


def test_escaping_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "evil.md"
    outside.write_text("evil", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "link.md").symlink_to(outside)
    inner_config = sub / "workflows.yaml"
    inner_config.write_text(
        _minimal_config_with_prompts("link.md"), encoding="utf-8"
    )

    cfg, faults = validate_config_file(inner_config)

    assert cfg is None
    assert faults
    assert any("escapes the configuration directory" in str(fault) for fault in faults)


def test_every_workflow_is_read_even_when_validating_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    (tmp_path / "good.md").write_text("good", encoding="utf-8")
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe")
    text = (
        "version: 3\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
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
        "workflows:\n"
        "  goodflow:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: review\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      review:\n"
        "        agent:\n"
        "          instructions:\n"
        "            - prompt: good.md\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            reviewed: {body: document}\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
        "  badflow:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:other}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: review\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      review:\n"
        "        agent:\n"
        "          instructions:\n"
        "            - prompt: bad.md\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            reviewed: {body: document}\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )
    config = _write_config(tmp_path, text)

    cfg, faults = validate_config_file(config, "goodflow")

    assert cfg is None
    assert faults
    assert any("bad.md" in str(fault) for fault in faults)


def test_cli_reports_prompt_fault_with_exit_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config_with_prompts("prompts/missing.md"))

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    err = capsys.readouterr().err
    assert "workflows.review.steps.review.agent.instructions[0]" in err
    assert "prompts/missing.md" in err


# --- Worker-loop seam: load_prompt_texts + model-visible content --------------


def test_load_prompt_texts_preserves_crlf_and_non_ascii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    raw = "line one\r\nline two\r\ncafé naïve — ✓\r\n".encode("utf-8")
    (tmp_path / "policy.md").write_bytes(raw)
    config = _write_config(tmp_path, _minimal_config_with_prompts("policy.md"))

    cfg, faults = validate_config_file(config)
    assert not faults
    assert cfg is not None
    texts = load_prompt_texts(config, cfg.workflows["review"])

    assert texts["policy.md"] == raw.decode("utf-8")
    assert "\r\n" in texts["policy.md"]
    assert "café" in texts["policy.md"]


def test_load_prompt_texts_rechecks_containment_every_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    (tmp_path / "policy.md").write_text("ok", encoding="utf-8")
    config = _write_config(tmp_path, _minimal_config_with_prompts("policy.md"))
    cfg, faults = validate_config_file(config)
    assert cfg is not None
    workflow = cfg.workflows["review"]
    # Swap the validated file for an escaping symlink after validation.
    (tmp_path / "policy.md").unlink()
    outside = tmp_path.parent / "escaped-prompt.md"
    outside.write_text("evil", encoding="utf-8")
    try:
        (tmp_path / "policy.md").symlink_to(outside)
        with pytest.raises(OSError) as excinfo:
            load_prompt_texts(config, workflow)
    finally:
        if outside.exists():
            outside.unlink()
    message = str(excinfo.value)
    assert "workflows.review.steps.review.agent.instructions[0]" in message
    assert "policy.md" in message
    assert "evil" not in message


def test_load_prompt_texts_refuses_invalid_utf8_without_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    import dataclasses

    secret = "SECRET-BYTES-MUST-NOT-APPEAR-4b1c"
    (tmp_path / "policy.md").write_bytes(secret.encode("utf-8") + b"\xff")
    (tmp_path / "ok.md").write_text("ok", encoding="utf-8")
    ok_config = tmp_path / "ok.yaml"
    ok_config.write_text(_minimal_config_with_prompts("ok.md"), encoding="utf-8")
    cfg_ok, faults_ok = validate_config_file(ok_config)
    assert cfg_ok is not None
    assert not faults_ok
    workflow = cfg_ok.workflows["review"]
    step = workflow.steps["review"]
    assert step.agent is not None
    bad_instructions = tuple(
        V2Instruction(prompt="policy.md") if item.prompt == "ok.md" else item
        for item in step.agent.instructions
    )
    bad_agent = dataclasses.replace(step.agent, instructions=bad_instructions)
    bad_step = dataclasses.replace(step, agent=bad_agent)
    bad_steps = dict(workflow.steps)
    bad_steps["review"] = bad_step
    bad_workflow = dataclasses.replace(workflow, steps=bad_steps)

    with pytest.raises(OSError) as excinfo:
        load_prompt_texts(ok_config, bad_workflow)
    message = str(excinfo.value)
    assert "workflows.review.steps.review.agent.instructions[0]" in message
    assert "policy.md" in message
    assert secret not in message


def test_prompt_file_bytes_reach_model_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: real file bytes through validation and loading to the model."""
    from coding_agent.workflow.v2_schema import V2Agent, V2Step

    _with_env(monkeypatch)
    raw = "first\r\nsecond\r\ncafé ✓\r\n".encode("utf-8")
    (tmp_path / "policy.md").write_bytes(raw)
    config = _write_config(tmp_path, _minimal_config_with_prompts("policy.md"))
    cfg, faults = validate_config_file(config)
    assert not faults
    assert cfg is not None
    texts = load_prompt_texts(config, cfg.workflows["review"])
    assert texts["policy.md"] == raw.decode("utf-8")

    step = V2Step(
        id="review",
        agent=V2Agent(
            instructions=(V2Instruction(prompt="policy.md"),),
            tools=("workspace-read",),
            inputs=(),
            results={"reviewed": {"body": "document"}},
        ),
        routes={},
    )
    deps = AgentStepDeps(
        attempt=_facts(),
        model=None,  # type: ignore[arg-type]
        workspace=tmp_path,
        tree_id="tree",
        prompt_texts=texts,
    )
    opening = compose_step_opening(step, deps, {})
    joined = "\n".join(str(message.content) for message in opening)
    assert raw.decode("utf-8") in joined
    assert "first\r\nsecond" in joined


def test_crlf_reaches_model_byte_for_byte(tmp_path: Path) -> None:
    from coding_agent.workflow.v2_schema import V2Agent, V2Step

    body = "first\r\nsecond\r\ncafé ✓\r\n"
    step = V2Step(
        id="review",
        agent=V2Agent(
            instructions=(V2Instruction(prompt="prompts/policy.md"),),
            tools=("workspace-read",),
            inputs=(),
            results={"reviewed": {"body": "document"}},
        ),
        routes={},
    )
    deps = AgentStepDeps(
        attempt=_facts(),
        model=None,  # type: ignore[arg-type]
        workspace=tmp_path,
        tree_id="tree",
        prompt_texts={"prompts/policy.md": body},
    )

    opening = compose_step_opening(step, deps, {})

    texts = [str(message.content) for message in opening]
    assert any(body in text for text in texts)
    joined = "\n".join(texts)
    assert "first\r\nsecond" in joined
    assert "café ✓" in joined
