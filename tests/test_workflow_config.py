"""Strict Workflow Configuration parsing and `agent config validate`.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table),
§2.2 (strict parsing), §2.3 (trust and paths), §2.4 (static and offline;
every refusal exits non-zero naming the YAML path).

MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-7, W-CFG-8, C-CFG-2,
W-CFG-13 (validate half: ``agent config validate`` makes no network call).
"""

import socket
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.workflow import WorkflowConfigError, load_workflow_config

CONTRACT_EXAMPLE = """\
version: 1

target:
  kind: github
  repository: lbacik/coding-agent-sandbox
  base_branch: main
  token_env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api_key_env: ANTHROPIC_API_KEY
    prices_usd_per_mtok: {input: 2.00, output: 10.00, cache_read: 0.20, cache_write: 2.50}

worker:
  poll_interval_seconds: 60
  shutdown_grace_seconds: 30

workflows:
  implement:
    model: sonnet
    selector:
      label: ready-for-agent
    steps:
      - id: implement
        skill: implement

  survey-then-implement:
    model: sonnet
    selector:
      label: ready-for-agent-surveyed
      exclude: [needs-design]
    limits:
      wall_clock_minutes: 90
      tool_calls: 800
    steps:
      - id: survey
        prompt_file: prompts/survey.md
      - id: implement
        skill: implement
"""


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


def _validate_cli(config: Path, workflow: str | None = None) -> int:
    args = ["config", "validate", "--config", str(config)]
    if workflow is not None:
        args += ["--workflow", workflow]
    return cli.main(args)


# --- the contract §2.1 example validates -------------------------------------


def test_contract_section_2_1_example_validates(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path, CONTRACT_EXAMPLE, {"prompts/survey.md": "# survey\n"}
    )

    cfg = load_workflow_config(config)

    assert cfg.version == 1
    assert cfg.target.repository == "lbacik/coding-agent-sandbox"
    assert cfg.target.labels.ready_for_human is None
    assert set(cfg.models) == {"sonnet"}
    assert set(cfg.workflows) == {"implement", "survey-then-implement"}
    survey = cfg.workflows["survey-then-implement"]
    assert [step.id for step in survey.steps] == ["survey", "implement"]
    assert survey.steps[0].prompt_file == "prompts/survey.md"
    assert survey.limits is not None and survey.limits.wall_clock_minutes == 90
    assert cfg.worker is not None and cfg.worker.poll_interval_seconds == 60


def test_contract_example_validates_through_the_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = _write_config(
        tmp_path, CONTRACT_EXAMPLE, {"prompts/survey.md": "# survey\n"}
    )

    assert _validate_cli(config) == 0
    assert "config validate: OK" in capsys.readouterr().out


def test_validate_selects_a_single_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = _write_config(
        tmp_path, CONTRACT_EXAMPLE, {"prompts/survey.md": "# survey\n"}
    )

    assert _validate_cli(config, "implement") == 0
    assert "workflow 'implement'" in capsys.readouterr().out


def test_validate_unknown_workflow_lists_the_available_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(
        tmp_path, CONTRACT_EXAMPLE, {"prompts/survey.md": "# survey\n"}
    )

    assert _validate_cli(config, "does-not-exist") == 1
    err = capsys.readouterr().err
    assert "workflows" in err
    assert "implement" in err
    assert "survey-then-implement" in err


# --- W-CFG-1: version ---------------------------------------------------------


@pytest.mark.parametrize(
    "version_block",
    ["", "version: 2\n", "version: one\n", "version: '1'\n", "version: true\n"],
    ids=["missing", "unknown-number", "unknown-string", "quoted-one", "boolean"],
)
def test_bad_version_is_refused_naming_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version_block: str
) -> None:
    config = _write_config(tmp_path, _minimal_config(version=version_block))

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "version"

    assert _validate_cli(config) == 1
    assert "version" in capsys.readouterr().err


# --- W-CFG-2: unknown fields --------------------------------------------------


@pytest.mark.parametrize(
    ("field_path", "overrides"),
    [
        ("bogus", {}),
        (
            "target.bogus",
            {
                "target": (
                    "target:\n"
                    "  kind: github\n"
                    "  repository: octocat/sandbox\n"
                    "  base_branch: main\n"
                    "  token_env: GITHUB_TOKEN\n"
                    "  bogus: 1\n"
                )
            },
        ),
        (
            "models.sonnet.bogus",
            {
                "models": (
                    "models:\n"
                    "  sonnet:\n"
                    "    provider: anthropic\n"
                    "    bogus: 1\n"
                )
            },
        ),
        (
            "workflows.implement.bogus",
            {
                "workflows": (
                    "workflows:\n"
                    "  implement:\n"
                    "    model: sonnet\n"
                    "    bogus: 1\n"
                )
            },
        ),
        (
            "workflows.implement.steps[0].bogus",
            {
                "workflows": (
                    "workflows:\n"
                    "  implement:\n"
                    "    model: sonnet\n"
                    "    selector:\n"
                    "      label: ready-for-agent\n"
                    "    steps:\n"
                    "      - id: implement\n"
                    "        skill: implement\n"
                    "        bogus: 1\n"
                )
            },
        ),
    ],
    ids=["top-level", "target", "model", "workflow", "step"],
)
def test_unknown_field_is_refused_naming_its_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field_path: str,
    overrides: dict[str, str],
) -> None:
    text = _minimal_config() + ("bogus: 1\n" if field_path == "bogus" else "")
    if overrides:
        text = _minimal_config(**overrides)
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == field_path

    assert _validate_cli(config) == 1
    assert field_path in capsys.readouterr().err


# --- W-CFG-3: duplicate keys --------------------------------------------------


def test_duplicate_top_level_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _minimal_config() + "version: 1\n"
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "version"

    assert _validate_cli(config) == 1
    assert "version" in capsys.readouterr().err


def test_duplicate_nested_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: implement\n"
        )
    )
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "workflows.implement.model"

    assert _validate_cli(config) == 1
    assert "workflows.implement.model" in capsys.readouterr().err


# --- W-CFG-4: anchors, aliases, merge keys, custom tags -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\ntarget: &t\n  kind: github\n",
        "version: 1\ntarget:\n  kind: github\nlabels: *t\n",
        "version: 1\ntarget:\n  kind: github\n  <<: {repository: x}\n",
        "version: !custom 1\n",
        "version: 1\ntarget:\n  kind: !custom github\n",
    ],
    ids=["anchor", "alias", "merge-key", "custom-tag-scalar", "custom-tag-value"],
)
def test_yaml_tricks_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError):
        load_workflow_config(config)

    assert _validate_cli(config) == 1
    assert capsys.readouterr().err != ""


def test_merge_key_names_its_path(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path, "version: 1\ntarget:\n  kind: github\n  <<: {repository: x}\n"
    )

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "target.<<"


# --- W-CFG-7: step shape ------------------------------------------------------


def test_step_with_both_skill_and_prompt_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "        prompt_file: prompts/survey.md\n"
        )
    )
    config = _write_config(
        tmp_path, text, {"prompts/survey.md": "# survey\n"}
    )

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "workflows.implement.steps[0]"

    assert _validate_cli(config) == 1
    assert "workflows.implement.steps[0]" in capsys.readouterr().err


def test_step_with_neither_skill_nor_prompt_file_is_refused(tmp_path: Path) -> None:
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
        )
    )
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "workflows.implement.steps[0]"


def test_empty_steps_is_refused(tmp_path: Path) -> None:
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps: []\n"
        )
    )
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "workflows.implement.steps"


def test_duplicated_step_id_is_refused(tmp_path: Path) -> None:
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
            "      - id: implement\n"
            "        skill: implement\n"
        )
    )
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "workflows.implement.steps[1].id"


# --- W-CFG-8: prompt files ----------------------------------------------------


def test_missing_prompt_file_is_refused(tmp_path: Path) -> None:
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
        )
    )
    config = _write_config(tmp_path, text)

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)
    assert excinfo.value.path == "workflows.implement.steps[0].prompt_file"


@pytest.mark.parametrize("escape", ["../outside.md", "prompts/../../outside.md"])
def test_prompt_file_escaping_through_dotdot_is_refused(
    tmp_path: Path, escape: str
) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    try:
        text = _minimal_config(
            workflows=(
                "workflows:\n"
                "  implement:\n"
                "    model: sonnet\n"
                "    selector:\n"
                "      label: ready-for-agent\n"
                f"    steps:\n"
                f"      - id: survey\n"
                f"        prompt_file: {escape}\n"
            )
        )
        config = _write_config(tmp_path, text)

        with pytest.raises(WorkflowConfigError) as excinfo:
            load_workflow_config(config)
        assert excinfo.value.path == "workflows.implement.steps[0].prompt_file"
    finally:
        outside.unlink(missing_ok=True)


def test_prompt_file_escaping_through_a_symlink_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-symlink.md"
    outside.write_text("# outside\n", encoding="utf-8")
    try:
        link = tmp_path / "prompts" / "linked.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        text = _minimal_config(
            workflows=(
                "workflows:\n"
                "  implement:\n"
                "    model: sonnet\n"
                "    selector:\n"
                "      label: ready-for-agent\n"
                "    steps:\n"
                "      - id: survey\n"
                "        prompt_file: prompts/linked.md\n"
            )
        )
        config = _write_config(tmp_path, text)

        with pytest.raises(WorkflowConfigError) as excinfo:
            load_workflow_config(config)
        assert excinfo.value.path == "workflows.implement.steps[0].prompt_file"
    finally:
        outside.unlink(missing_ok=True)


def test_prompt_file_inside_a_symlinked_directory_validates(tmp_path: Path) -> None:
    real = tmp_path / "real-prompts"
    real.mkdir()
    (real / "survey.md").write_text("# survey\n", encoding="utf-8")
    (tmp_path / "prompts").symlink_to(real, target_is_directory=True)
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
        )
    )
    config = _write_config(tmp_path, text)

    cfg = load_workflow_config(config)
    assert cfg.workflows["implement"].steps[0].prompt_file == "prompts/survey.md"


# --- C-CFG-2: ids --------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["Bad_Name", "has space", "UPPER", "under_score", ""])
def test_bad_model_workflow_and_step_ids_are_refused(
    tmp_path: Path, bad_id: str
) -> None:
    model_text = _minimal_config(
        models=(
            "models:\n"
            f'  "{bad_id}":\n'
            "    provider: anthropic\n"
            "    model: claude-sonnet-5\n"
            "    endpoint: messages\n"
            "    effort: medium\n"
            "    api_key_env: ANTHROPIC_API_KEY\n"
            "    prices_usd_per_mtok: {input: 2.00, output: 10.00,"
            " cache_read: 0.20, cache_write: 2.50}\n"
        )
    )
    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(_write_config(tmp_path, model_text))
    assert excinfo.value.path == f"models.{bad_id}"

    workflow_text = _minimal_config(
        workflows=(
            "workflows:\n"
            f'  "{bad_id}":\n'
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: implement\n"
        )
    )
    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(_write_config(tmp_path, workflow_text))
    assert excinfo.value.path == f"workflows.{bad_id}"

    step_text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            f'      - id: "{bad_id}"\n'
            "        skill: implement\n"
        )
    )
    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(_write_config(tmp_path, step_text))
    assert excinfo.value.path == "workflows.implement.steps[0].id"


def test_cli_refusal_names_the_path_for_a_bad_step_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _minimal_config(
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: Bad_Id\n"
            "        skill: implement\n"
        )
    )
    config = _write_config(tmp_path, text)

    assert _validate_cli(config) == 1
    assert "workflows.implement.steps[0].id" in capsys.readouterr().err


# --- W-CFG-13 (validate half): no network call --------------------------------


def test_validate_makes_no_network_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent config validate`` is static and offline (§2.4): with every
    socket refused, the contract example still validates."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = _write_config(
        tmp_path, CONTRACT_EXAMPLE, {"prompts/survey.md": "# survey\n"}
    )

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert _validate_cli(config) == 0
    assert "config validate: OK" in capsys.readouterr().out


def test_validate_refusal_makes_no_network_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path, _minimal_config(version=""))

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)

    assert _validate_cli(config) == 1
    assert "version" in capsys.readouterr().err


# --- CLI wiring -----------------------------------------------------------------


def test_main_dispatches_to_config_validate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Path, str | None]] = []

    def fake_run_config_validate_command(config: Path, workflow: str | None = None) -> int:
        calls.append((config, workflow))
        return 0

    monkeypatch.setattr(cli, "run_config_validate_command", fake_run_config_validate_command)

    assert cli.main(["config", "validate", "--config", "workflow.yml"]) == 0
    assert calls == [(Path("workflow.yml"), None)]

    assert (
        cli.main(
            ["config", "validate", "--config", "workflow.yml", "--workflow", "implement"]
        )
        == 0
    )
    assert calls[-1] == (Path("workflow.yml"), "implement")
