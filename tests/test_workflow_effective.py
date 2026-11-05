"""Semantic validation and the Effective Configuration.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table:
defaults, secrets as names, the code-owned context table), §2.2 (strict
parsing: the skill allowlist and the Selection Label rules), §2.4 (static and
offline; every refusal exits non-zero naming the YAML path; an unknown or
missing ``--workflow`` lists the available ids) and §2.5 (the Effective
Configuration with its digest: defaults filled in, names in, values never).

MVP row ids: W-CFG-5, W-CFG-6, W-CFG-9, W-CFG-10, W-CFG-11, W-CFG-12,
C-CFG-1, C-CFG-3, W-CFG-13 (semantic half: still no network call).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.workflow import (
    WorkflowConfig,
    WorkflowConfigError,
    load_workflow_config,
    resolve_effective_configuration,
    validate_workflow_semantics,
)


def _write_config(tmp_path: Path, text: str, prompt_files: dict[str, str] | None = None) -> Path:
    config = tmp_path / "workflow.yml"
    config.write_text(text, encoding="utf-8")
    for name, content in (prompt_files or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return config


def _minimal_config(**overrides: str) -> str:
    """A minimal valid file; ``overrides`` replaces whole top-level blocks."""
    blocks = {
        "version": "version: 1\n",
        "target": (
            "target:\n"
            "  kind: github\n"
            "  repository: octocat/sandbox\n"
            "  base_branch: main\n"
            "  token_env: GITHUB_TOKEN\n"
        ),
        "models": (
            "models:\n"
            "  sonnet:\n"
            "    provider: anthropic\n"
            "    model: claude-sonnet-5\n"
            "    endpoint: messages\n"
            "    effort: medium\n"
            "    api_key_env: ANTHROPIC_API_KEY\n"
            "    prices_usd_per_mtok: {input: 2.00, output: 10.00,"
            " cache_read: 0.20, cache_write: 2.50}\n"
        ),
        "workflows": (
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: implement\n"
        ),
    }
    blocks.update(overrides)
    return "".join(blocks[key] for key in ("version", "target", "models", "workflows"))


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _load(tmp_path: Path, text: str, prompts: dict[str, str] | None = None) -> WorkflowConfig:
    return load_workflow_config(_write_config(tmp_path, text, prompts))


def _validate_cli(config: Path, workflow: str | None = None) -> int:
    args = ["config", "validate", "--config", str(config)]
    if workflow is not None:
        args += ["--workflow", workflow]
    return cli.main(args)


# --- W-CFG-5: missing or unknown --workflow ------------------------------------


def test_missing_workflow_lists_the_available_ids(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _minimal_config())

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(cfg, None)

    assert excinfo.value.path == "workflows"
    assert "implement" in str(excinfo.value)


def test_unknown_workflow_lists_the_available_ids(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _minimal_config())

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(cfg, "does-not-exist")

    assert excinfo.value.path == "workflows"
    assert "does-not-exist" in str(excinfo.value)
    assert "implement" in str(excinfo.value)


def test_cli_unknown_workflow_still_lists_the_available_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    assert _validate_cli(config, "does-not-exist") == 1
    err = capsys.readouterr().err
    assert "workflows" in err
    assert "implement" in err


# --- W-CFG-6: skills -----------------------------------------------------------


def test_skill_outside_the_bundle_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: nosuchskill\n"
        )
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "workflows.implement.steps[0].skill"

    assert _validate_cli(_write_config(tmp_path, text), "implement") == 1
    assert "workflows.implement.steps[0].skill" in capsys.readouterr().err


def test_code_review_is_refused_as_outside_the_allowlist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: review\n"
            "        skill: code-review\n"
        )
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "workflows.implement.steps[0].skill"
    assert "allowlist" in str(excinfo.value)

    assert _validate_cli(_write_config(tmp_path, text), "implement") == 1
    assert "workflows.implement.steps[0].skill" in capsys.readouterr().err


# --- W-CFG-9: unset env vars ---------------------------------------------------


def test_unset_token_env_names_the_variable_and_prints_no_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-anthropic-value-123")
    # cli.main loads ./.env; an operator's local file must not refill the
    # variable this test unsets.
    monkeypatch.chdir(tmp_path)
    config = _write_config(tmp_path, _minimal_config())
    cfg = load_workflow_config(config)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(cfg, "implement")
    assert excinfo.value.path == "target.token_env"
    assert "GITHUB_TOKEN" in str(excinfo.value)
    assert "sentinel-anthropic-value-123" not in str(excinfo.value)

    assert _validate_cli(config, "implement") == 1
    err = capsys.readouterr().err
    assert "target.token_env" in err
    assert "GITHUB_TOKEN" in err
    assert "sentinel-anthropic-value-123" not in err


def test_unset_api_key_env_names_the_variable_and_prints_no_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel-github-value-456")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # cli.main loads ./.env; an operator's local file must not refill the
    # variable this test unsets.
    monkeypatch.chdir(tmp_path)
    config = _write_config(tmp_path, _minimal_config())
    cfg = load_workflow_config(config)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(cfg, "implement")
    assert excinfo.value.path == "models.sonnet.api_key_env"
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
    assert "sentinel-github-value-456" not in str(excinfo.value)

    assert _validate_cli(config, "implement") == 1
    err = capsys.readouterr().err
    assert "models.sonnet.api_key_env" in err
    assert "sentinel-github-value-456" not in err


# --- W-CFG-10: model references -------------------------------------------------


def test_unknown_model_reference_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: nosuchmodel\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: implement\n"
        )
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "workflows.implement.model"


def test_pair_outside_the_code_owned_table_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        models=(
            "models:\n"
            "  sonnet:\n"
            "    provider: anthropic\n"
            "    model: no-such-model-xyz\n"
            "    endpoint: messages\n"
            "    effort: medium\n"
            "    api_key_env: ANTHROPIC_API_KEY\n"
            "    prices_usd_per_mtok: {input: 2.00, output: 10.00,"
            " cache_read: 0.20, cache_write: 2.50}\n"
        )
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "models.sonnet.model"


# --- W-CFG-11: target.kind -------------------------------------------------------


def test_non_github_target_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        target=(
            "target:\n"
            "  kind: gitlab\n"
            "  repository: octocat/sandbox\n"
            "  base_branch: main\n"
            "  token_env: GITHUB_TOKEN\n"
        )
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "target.kind"

    assert _validate_cli(_write_config(tmp_path, text), "implement") == 1
    assert "target.kind" in capsys.readouterr().err


# --- W-CFG-12: Selection Labels ---------------------------------------------------


def _selector_workflows(label: str, extra: str = "") -> str:
    return (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    selector:\n"
        f'      label: "{label}"\n'
        f"{extra}"
        "    steps:\n"
        "      - id: implement\n"
        "        skill: implement\n"
    )


@pytest.mark.parametrize(
    ("label", "extra"),
    [
        ("ready-for-human", ""),
        ("wontfix", ""),
        ("wayfinder:design", ""),
        ("wayfinder:", ""),
        ("needs-design", "      require_all: [needs-design]\n"),
        ("needs-design", "      require_any: [needs-design]\n"),
        ("needs-design", "      exclude: [needs-design]\n"),
    ],
    ids=[
        "ready-for-human",
        "wontfix",
        "wayfinder-prefixed",
        "wayfinder-bare",
        "in-require-all",
        "in-require-any",
        "in-exclude",
    ],
)
def test_forbidden_selection_labels_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, extra: str
) -> None:
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config(workflows=_selector_workflows(label, extra)))

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "workflows.implement.selector.label"


def test_label_equal_to_a_custom_ready_for_human_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        target=(
            "target:\n"
            "  kind: github\n"
            "  repository: octocat/sandbox\n"
            "  base_branch: main\n"
            "  token_env: GITHUB_TOKEN\n"
            "  labels:\n"
            "    ready_for_human: human-done\n"
        ),
        workflows=_selector_workflows("human-done"),
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "workflows.implement.selector.label"


def test_empty_selection_label_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WorkflowConfigError) as excinfo:
        _load(tmp_path, _minimal_config(workflows=_selector_workflows("")))
    assert excinfo.value.path == "workflows.implement.selector.label"


def test_custom_ready_for_human_name_allows_the_default_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        target=(
            "target:\n"
            "  kind: github\n"
            "  repository: octocat/sandbox\n"
            "  base_branch: main\n"
            "  token_env: GITHUB_TOKEN\n"
            "  labels:\n"
            "    ready_for_human: human-done\n"
        ),
    )
    cfg = _load(tmp_path, text)

    effective = resolve_effective_configuration(
        cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
    )
    assert effective.target.ready_for_human == "human-done"
    assert effective.workflow.label == "ready-for-agent"


# --- C-CFG-1: secret-shaped values -----------------------------------------------


@pytest.mark.parametrize(
    ("field", "secret"),
    [
        ("token", "github_pat_abcdefghij1234567890"),
        ("token", "my-secret-value"),
        ("token", "s3cr et with spaces"),
        ("api", "sk-ant-abc123def456"),
        ("api", "ghp_abcdefghij1234567890"),
    ],
    ids=[
        "fine-grained-pat",
        "dashed-value",
        "spaced-value",
        "anthropic-key",
        "classic-pat",
    ],
)
def test_secret_shaped_values_are_refused_and_never_printed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    secret: str,
) -> None:
    _with_env(monkeypatch)
    if field == "token":
        text = _minimal_config(
            target=(
                "target:\n"
                "  kind: github\n"
                "  repository: octocat/sandbox\n"
                "  base_branch: main\n"
                f"  token_env: {secret}\n"
            )
        )
        expected_path = "target.token_env"
    else:
        text = _minimal_config(
            models=(
                "models:\n"
                "  sonnet:\n"
                "    provider: anthropic\n"
                "    model: claude-sonnet-5\n"
                "    endpoint: messages\n"
                "    effort: medium\n"
                f"    api_key_env: {secret}\n"
                "    prices_usd_per_mtok: {input: 2.00, output: 10.00,"
                " cache_read: 0.20, cache_write: 2.50}\n"
            )
        )
        expected_path = "models.sonnet.api_key_env"
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        resolve_effective_configuration(
            cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == expected_path
    assert secret not in str(excinfo.value)

    assert _validate_cli(_write_config(tmp_path, text), "implement") == 1
    err = capsys.readouterr().err
    assert expected_path in err
    assert secret not in err


def test_all_uppercase_name_is_a_name_not_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An all-uppercase ``token_env`` is a variable name (C-CFG-1): real
    secrets are never all-uppercase, so the shape check must not fire."""
    monkeypatch.setenv("GITHUB_PAT_TOKEN", "test-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    text = _minimal_config(
        target=(
            "target:\n"
            "  kind: github\n"
            "  repository: octocat/sandbox\n"
            "  base_branch: main\n"
            "  token_env: GITHUB_PAT_TOKEN\n"
        )
    )
    cfg = _load(tmp_path, text)

    effective = resolve_effective_configuration(cfg, "implement")

    assert effective.target.token_env == "GITHUB_PAT_TOKEN"


# --- C-CFG-3: defaults -------------------------------------------------------------


def test_omitted_limits_worker_and_labels_record_every_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())

    effective = resolve_effective_configuration(
        cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
    )

    assert effective.workflow.limits.wall_clock_minutes == 60
    assert effective.workflow.limits.cost_usd == 20
    assert effective.workflow.limits.tokens == 4_000_000
    assert effective.workflow.limits.tool_calls == 600
    assert effective.workflow.limits.max_parallel_subagents == 2
    assert effective.worker.poll_interval_seconds == 60
    assert effective.worker.shutdown_grace_seconds == 30
    assert effective.target.ready_for_human == "ready-for-human"


def test_declared_limits_and_worker_survive_normalisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = (
        "version: 1\n"
        "target:\n"
        "  kind: github\n"
        "  repository: octocat/sandbox\n"
        "  base_branch: main\n"
        "  token_env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api_key_env: ANTHROPIC_API_KEY\n"
        "    prices_usd_per_mtok: {input: 2.00, output: 10.00,"
        " cache_read: 0.20, cache_write: 2.50}\n"
        "worker:\n"
        "  poll_interval_seconds: 30\n"
        "  shutdown_grace_seconds: 10\n"
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    selector:\n"
        "      label: ready-for-agent\n"
        "    limits:\n"
        "      wall_clock_minutes: 90\n"
        "      cost_usd: 5\n"
        "      tokens: 1000\n"
        "      tool_calls: 50\n"
        "    steps:\n"
        "      - id: implement\n"
        "        skill: implement\n"
    )
    cfg = _load(tmp_path, text)

    effective = resolve_effective_configuration(
        cfg, "implement", env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
    )

    assert effective.workflow.limits.wall_clock_minutes == 90
    assert effective.workflow.limits.cost_usd == 5
    assert effective.workflow.limits.tokens == 1000
    assert effective.workflow.limits.tool_calls == 50
    assert effective.workflow.limits.max_parallel_subagents == 2
    assert effective.worker.poll_interval_seconds == 30
    assert effective.worker.shutdown_grace_seconds == 10


# --- Digest and pinning (§2.5) ------------------------------------------------------


def _prompt_config() -> str:
    return _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: survey\n"
            "        prompt_file: prompts/survey.md\n"
            "      - id: implement\n"
            "        skill: implement\n"
        )
    )


def test_digest_is_stable_for_the_same_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config = _write_config(tmp_path, _prompt_config(), {"prompts/survey.md": "# survey\n"})
    first = resolve_effective_configuration(load_workflow_config(config), "implement")
    second = resolve_effective_configuration(load_workflow_config(config), "implement")

    assert first.digest == second.digest
    assert len(first.digest) == 64


def test_a_prompt_file_shared_by_two_steps_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two steps may activate the same ``prompt_file`` (§2.5): both see its
    bytes, recorded under one entry."""
    _with_env(monkeypatch)
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: survey\n"
            "        prompt_file: prompts/survey.md\n"
            "      - id: resurvey\n"
            "        prompt_file: prompts/survey.md\n"
        )
    )
    config = _write_config(tmp_path, text, {"prompts/survey.md": "# survey\n"})

    effective = resolve_effective_configuration(load_workflow_config(config), "implement")

    assert [step.prompt_content for step in effective.workflow.steps] == [b"# survey\n"] * 2
    assert [(entry.path, entry.content) for entry in effective.prompts] == [
        ("prompts/survey.md", b"# survey\n")
    ]


def test_digest_changes_when_a_prompt_files_bytes_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config = _write_config(tmp_path, _prompt_config(), {"prompts/survey.md": "# survey\n"})
    before = resolve_effective_configuration(load_workflow_config(config), "implement")

    (tmp_path / "prompts" / "survey.md").write_text("# changed\n", encoding="utf-8")
    after = resolve_effective_configuration(load_workflow_config(config), "implement")

    assert before.digest != after.digest
    assert before.prompts[0].content == b"# survey\n"
    assert after.prompts[0].content == b"# changed\n"


def test_no_environment_variable_value_is_in_the_effective_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel-github-value-789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-anthropic-value-789")
    config = _write_config(tmp_path, _prompt_config(), {"prompts/survey.md": "# survey\n"})

    effective = resolve_effective_configuration(load_workflow_config(config), "implement")

    assert effective.target.token_env == "GITHUB_TOKEN"
    assert effective.model.api_key_env == "ANTHROPIC_API_KEY"
    assert "sentinel-github-value-789" not in repr(effective)
    assert "sentinel-anthropic-value-789" not in repr(effective)
    assert "sentinel-github-value-789" not in effective.digest
    assert "sentinel-anthropic-value-789" not in effective.digest


def test_changing_an_env_value_leaves_the_digest_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path, _prompt_config(), {"prompts/survey.md": "# survey\n"})
    first = resolve_effective_configuration(
        load_workflow_config(config),
        "implement",
        env={"GITHUB_TOKEN": "first-value", "ANTHROPIC_API_KEY": "first-key"},
    )
    second = resolve_effective_configuration(
        load_workflow_config(config),
        "implement",
        env={"GITHUB_TOKEN": "second-value", "ANTHROPIC_API_KEY": "second-key"},
    )

    assert first.digest == second.digest


def test_cli_prints_the_digest_for_a_selected_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    assert _validate_cli(config, "implement") == 0
    out = capsys.readouterr().out
    assert "workflow 'implement'" in out
    assert "effective configuration digest: " in out
    expected = resolve_effective_configuration(load_workflow_config(config), "implement")
    assert expected.digest in out


# --- validate-all and offline behaviour ----------------------------------------------


def test_validate_all_checks_every_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: implement\n"
            "  second:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: review\n"
            "        skill: code-review\n"
        )
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        validate_workflow_semantics(
            cfg, env={"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}
        )
    assert excinfo.value.path == "workflows.second.steps[0].skill"


def test_semantic_refusals_make_no_network_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_env(monkeypatch)
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: review\n"
            "        skill: code-review\n"
        )
    )
    config = _write_config(tmp_path, text)

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert _validate_cli(config, "implement") == 1
    assert "workflows.implement.steps[0].skill" in capsys.readouterr().err


# --- Handoff document -------------------------------------------------------------------


def test_handoff_document_exists_and_links_contract_section_2() -> None:
    doc = Path(__file__).resolve().parent.parent / "docs" / "ops" / "workflow-configuration.md"

    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    assert "mvp-runtime-contract" in text
    assert "never a second source of truth" in text
