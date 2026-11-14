"""Static validation of `version: 2` step-workflow configuration files.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7 (strict
parsing), §2.8 (trust and paths), §2.9 (``agent config validate`` is static
and offline; every refusal names the YAML path of every fault) and §3
(static validation).

Slice S1 of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: CFG-1 … CFG-39, CFG-43, CFG-44, CFG-45, CFG-46. The 35
prototype cases (``tests/fixtures/workflows/invalid/01-…-35-…``) plus the
S1 additions (``36-…``–``41-…``) are the table-driven acceptance; each file
carries its ``# expect:`` line. ``examples/workflows.yaml`` is the shipped
valid case (CFG-36).
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.workflow import V2Config, validate_v2_file

FIXTURES = Path(__file__).parent / "fixtures" / "workflows" / "invalid"
EXAMPLES = Path(__file__).parent.parent / "examples" / "workflows.yaml"


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _minimal_config(**overrides: str) -> str:
    """A minimal valid `version: 2` file; ``overrides`` replaces whole blocks."""
    blocks = {
        "version": "version: 2\n",
        "target": (
            "target:\n"
            "  kind: github\n"
            "  repository: OWNER/REPO\n"
            "  base-branch: main\n"
            "  token-env: GITHUB_TOKEN\n"
        ),
        "models": (
            "models:\n"
            "  sonnet:\n"
            "    provider: anthropic\n"
            "    model: claude-sonnet-5\n"
            "    endpoint: messages\n"
            "    effort: medium\n"
            "    api-key-env: ANTHROPIC_API_KEY\n"
            "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
            " cache-read: 0.20, cache-write: 2.50}\n"
        ),
        "workflows": (
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
            "          tools: [workspace-read]\n"
            "          results:\n"
            "            reviewed: {body: document}\n"
            "        routes: {reviewed: publish}\n"
            "      publish:\n"
            "        operation: publish-review\n"
            "        with: {body: review.reviewed.body}\n"
            "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
        ),
    }
    blocks.update(overrides)
    return "".join(blocks[key] for key in ("version", "target", "models", "workflows"))


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build validation reads no secrets: run with the credential names unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)


def _expect_of(fixture: Path) -> str:
    match = re.search(r"^# expect: (.+)$", fixture.read_text(encoding="utf-8"), re.M)
    assert match is not None, f"{fixture.name} carries no '# expect:' line"
    return match.group(1)


# --- CFG-1 … CFG-35, CFG-39, CFG-43, CFG-44: one fixture per refusal ---------


def _invalid_fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.yaml"))


@pytest.mark.parametrize("fixture", _invalid_fixtures(), ids=lambda p: p.name)
def test_invalid_fixture_is_refused_naming_its_expect(
    fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `tests/fixtures/workflows/invalid/` case is refused, and the
    refusal names the fixture's `# expect:` line."""
    _without_env(monkeypatch)
    expect = _expect_of(fixture)

    _, faults = validate_v2_file(fixture)

    assert faults, f"{fixture.name} validated but must be refused"
    assert any(expect in str(fault) for fault in faults), (
        f"{fixture.name}: expected {expect!r} in {[str(f) for f in faults]}"
    )


# --- CFG-36: the shipped example is valid --------------------------------------


def test_examples_workflows_yaml_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)

    cfg, faults = validate_v2_file(EXAMPLES)

    assert not faults
    assert cfg is not None
    assert set(cfg.workflows) == {"implement", "review"}


def test_examples_workflows_validates_through_the_cli(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)

    assert cli.main(["config", "validate", "--config", str(EXAMPLES)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


def test_examples_review_workflow_validates_through_the_cli(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)

    assert (
        cli.main(
            ["config", "validate", "--config", str(EXAMPLES), "--workflow", "review"]
        )
        == 0
    )
    assert "workflow 'review'" in capsys.readouterr().out


# --- CFG-37: the refusal edge proves availability -------------------------------


REFUSAL_EDGE_CONFIG = """\
version: 2
target:
  kind: github
  repository: OWNER/REPO
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
workflows:
  review:
    model: sonnet
    input: {type: pull-request, label: agent:review}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: process
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      process:
        agent:
          tools: [workspace-read]
          results:
            done: {summary: document}
        max-executions: 2
        routes: {done: {end: completed}, limit-exceeded: ai-handoff}
      ai-handoff:
        agent:
          tools: [workspace-read]
          inputs: [process.facts]
          results:
            handoff: {note: document}
        reserved: true
        limits: {cost-usd: 1, tokens: 150000, tool-calls: 30, wall-clock-minutes: 5}
        routes: {handoff: process}
"""


def test_reserved_step_reading_facts_through_limit_exceeded_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal edge implies the refused step has already run, so the
    must-analysis never treats its latest result as absent on that edge."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, REFUSAL_EDGE_CONFIG)

    cfg, faults = validate_v2_file(config)

    assert not faults
    assert cfg is not None


# --- CFG-38: `agent config validate` --------------------------------------------


def test_cli_refusal_names_the_yaml_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config(version="version: 4\n"))

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    assert "version" in capsys.readouterr().err


def test_cli_refusal_of_a_v2_fault_names_the_yaml_path(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)

    assert (
        cli.main(
            [
                "config",
                "validate",
                "--config",
                str(FIXTURES / "04-unknown-target.yaml"),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "unknown target" in err
    assert "workflows.review.steps.publish.routes" in err


def test_cli_unknown_workflow_lists_the_available_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)

    assert (
        cli.main(
            [
                "config",
                "validate",
                "--config",
                str(EXAMPLES),
                "--workflow",
                "does-not-exist",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "does-not-exist" in err
    assert "implement" in err
    assert "review" in err


def test_cli_validate_without_workflow_lists_the_available_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)

    assert cli.main(["config", "validate", "--config", str(EXAMPLES)]) == 0
    out = capsys.readouterr().out
    assert "implement" in out
    assert "review" in out


def test_cli_validate_passes_with_no_runtime_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build validation checks no secrets: a valid file passes with an
    empty environment."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "static only; no secrets checked" in out


def test_cli_validate_prints_names_never_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "probe-github-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "probe-anthropic-secret-value")

    assert cli.main(["config", "validate", "--config", str(EXAMPLES)]) == 0
    out = capsys.readouterr().out
    assert "probe-github-secret-value" not in out
    assert "probe-anthropic-secret-value" not in out


def test_validate_makes_no_network_call(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent config validate` on a `version: 2` file is static and offline:
    with every socket refused, the shipped example still validates."""
    _without_env(monkeypatch)

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert cli.main(["config", "validate", "--config", str(EXAMPLES)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


def test_validate_refusal_makes_no_network_call(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)

    assert (
        cli.main(
            ["config", "validate", "--config", str(FIXTURES / "04-unknown-target.yaml")]
        )
        == 1
    )
    assert "unknown target" in capsys.readouterr().err


def test_validate_compiles_every_workflow_graph_without_invoking_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inert graph compilation runs for every Workflow and invokes no
    operation, model, tool or credential."""
    _without_env(monkeypatch)
    calls: list[str] = []

    def _no_model(*args: object, **kwargs: object) -> object:
        calls.append("model")
        raise AssertionError("no model may be built during validation")

    def _no_operation(*args: object, **kwargs: object) -> object:
        calls.append("operation")
        raise AssertionError("no operation may run during validation")

    from coding_agent.runtime.graph import workflow_graph as _graph

    monkeypatch.setattr(
        _graph, "EXECUTOR_REGISTRY", {"agent": _no_model, "operation": _no_operation}
    )
    import coding_agent.provider.pinned_model as _pinned

    monkeypatch.setattr(_pinned, "build_chat_model", _no_model)

    cfg, faults = validate_v2_file(EXAMPLES)

    assert not faults
    assert cfg is not None
    assert set(cfg.workflows) == {"implement", "review"}
    assert calls == []


# --- CFG-39: trust and paths ------------------------------------------------------


def test_inline_secret_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(
        tmp_path,
        _minimal_config(
            target=(
                "target:\n"
                "  kind: github\n"
                "  repository: OWNER/REPO\n"
                "  base-branch: main\n"
                "  token-env: sk-ant-secret-value\n"
            )
        ),
    )

    _, faults = validate_v2_file(config)

    assert faults
    assert any("secret value" in str(fault) for fault in faults)


def test_prompt_path_escaping_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)

    _, faults = validate_v2_file(FIXTURES / "37-prompt-escapes-directory.yaml")

    assert faults
    assert any("escapes the configuration directory" in str(fault) for fault in faults)


def test_skill_outside_the_bundle_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)

    _, faults = validate_v2_file(FIXTURES / "38-skill-outside-bundle.yaml")

    assert faults
    assert any("not in the pinned Skill Bundle" in str(fault) for fault in faults)


# --- CFG-43, CFG-44: end labels and outcomes ----------------------------------------


def test_end_label_equal_to_own_selection_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_env(monkeypatch)

    _, faults = validate_v2_file(FIXTURES / "39-end-label-equals-own-selection.yaml")

    assert faults
    assert any("never applies a Selection Label" in str(fault) for fault in faults)


def test_end_label_equal_to_another_selection_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_env(monkeypatch)

    _, faults = validate_v2_file(FIXTURES / "40-end-label-equals-other-selection.yaml")

    assert faults
    assert any("never applies a Selection Label" in str(fault) for fault in faults)


def test_canceled_outcome_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)

    _, faults = validate_v2_file(FIXTURES / "41-canceled-outcome.yaml")

    assert faults
    assert any(
        "canceled" in str(fault) and "unknown Terminal Outcome" in str(fault)
        for fault in faults
    )


# --- CFG-45: the worker and fan-out defaults -----------------------------------------


def test_missing_worker_block_and_fan_out_take_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    cfg, faults = validate_v2_file(config)

    assert not faults
    assert cfg is not None
    assert cfg.worker.poll_interval_seconds == 60
    assert cfg.worker.shutdown_grace_seconds == 30
    assert cfg.workflows["review"].max_parallel_subagents == 2


def test_explicit_worker_and_fan_out_values_are_used_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config() + "worker:\n  poll-interval-seconds: 10\n  shutdown-grace-seconds: 5\n"
    text = text.replace("label: agent:review}", "label: agent:review}\n    max-parallel-subagents: 3")
    config = _write_config(tmp_path, text)

    cfg, faults = validate_v2_file(config)

    assert not faults
    assert cfg is not None
    assert cfg.worker.poll_interval_seconds == 10
    assert cfg.worker.shutdown_grace_seconds == 5
    assert cfg.workflows["review"].max_parallel_subagents == 3


# --- CFG-46: max-parallel-subagents ----------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "1.5", '"2"'])
def test_bad_max_parallel_subagents_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config().replace(
        "label: agent:review}",
        "label: agent:review}\n    max-parallel-subagents: " + value,
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v2_file(config)

    assert faults
    assert any("max-parallel-subagents" in str(fault) for fault in faults)


# --- The typed shape -------------------------------------------------------------------


def test_minimal_file_loads_a_version_2_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    cfg, faults = validate_v2_file(config)

    assert not faults
    assert isinstance(cfg, V2Config)
    assert cfg is not None
    assert cfg.version == 2
    assert cfg.target.kind == "github"
    assert set(cfg.models) == {"sonnet"}
    assert cfg.workflows["review"].start == "review"
    assert set(cfg.workflows["review"].steps) == {"review", "publish"}


# --- S1 is not reachable from `agent run` -------------------------------------------------


GATEWAY_MODELS = """\
models:
  muse:
    gateway: {kind: litellm, url: http://pi.lan:4000}
    model: muse-spark-1.3-contributor
    endpoint: responses
    effort: medium
    api-key-env: LITELLM_PROXY_API_KEY
    output-cap-tokens: 32000
    context-threshold-tokens: 100000
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}
"""

GATEWAY_WORKFLOWS = """\
workflows:
  review:
    model: muse
    input: {type: pull-request, label: agent:review}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: review
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      review:
        agent:
          tools: [workspace-read]
          results:
            reviewed: {body: document}
        routes: {reviewed: publish}
      publish:
        operation: publish-review
        with: {body: review.reviewed.body}
        routes: {published: {end: completed}, stale-head: {end: failed}}
"""


def _with_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_env(monkeypatch)


def _gateway_config(models_block: str = GATEWAY_MODELS) -> str:
    return _minimal_config(models=models_block, workflows=GATEWAY_WORKFLOWS)


def test_gateway_pin_from_the_ticket_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.1: the ticket's gateway pin validates offline."""
    _with_gateway_env(monkeypatch)
    config = _write_config(tmp_path, _gateway_config())

    cfg, faults = validate_v2_file(config)

    assert not faults
    assert cfg is not None
    decl = cfg.models["muse"]
    assert decl.provider is None
    assert decl.gateway_kind == "litellm"
    assert decl.gateway_url == "http://pi.lan:4000"
    assert decl.output_cap_tokens == 32000
    assert decl.context_threshold_tokens == 100000


def test_two_spellings_of_one_gateway_url_canonicalise_to_one_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.1: scheme/host case and a trailing slash do not change the record."""
    _with_gateway_env(monkeypatch)
    upper = _write_config(
        tmp_path,
        _gateway_config(
            GATEWAY_MODELS.replace("http://pi.lan:4000", "HTTP://PI.LAN:4000/")
        ),
    )

    cfg, faults = validate_v2_file(upper)

    assert not faults
    assert cfg is not None
    assert cfg.models["muse"].gateway_url == "http://pi.lan:4000"


@pytest.mark.parametrize(
    ("models_block", "expect"),
    [
        # Exactly one of provider or gateway.
        (
            GATEWAY_MODELS.replace(
                "    gateway: {kind: litellm, url: http://pi.lan:4000}\n",
                "    provider: anthropic\n    gateway: {kind: litellm, url: http://pi.lan:4000}\n",
            ),
            "models.muse",
        ),
        (
            GATEWAY_MODELS.replace(
                "    gateway: {kind: litellm, url: http://pi.lan:4000}\n", ""
            ),
            "models.muse",
        ),
        # Closed gateway-kind list.
        (
            GATEWAY_MODELS.replace("kind: litellm", "kind: openrouter"),
            "models.muse.gateway.kind",
        ),
        # Literal URL checks.
        (
            GATEWAY_MODELS.replace("http://pi.lan:4000", "ftp://pi.lan:4000"),
            "models.muse.gateway.url",
        ),
        (
            GATEWAY_MODELS.replace(
                "http://pi.lan:4000", "http://user:pass@pi.lan:4000"
            ),
            "models.muse.gateway.url",
        ),
        (
            GATEWAY_MODELS.replace(
                "http://pi.lan:4000", '"http://pi.lan:4000?k=v"'
            ),
            "models.muse.gateway.url",
        ),
        (
            GATEWAY_MODELS.replace(
                "http://pi.lan:4000", '"http://pi.lan:4000#frag"'
            ),
            "models.muse.gateway.url",
        ),
        # Endpoint admissibility per gateway kind.
        (
            GATEWAY_MODELS.replace("endpoint: responses", "endpoint: messages"),
            "models.muse.endpoint",
        ),
        # Free-form but non-empty model name.
        (
            GATEWAY_MODELS.replace(
                "model: muse-spark-1.3-contributor", "model: ''"
            ),
            "models.muse.model",
        ),
        # api-key-env is a required name, never a value.
        (
            GATEWAY_MODELS.replace(
                "api-key-env: LITELLM_PROXY_API_KEY", "api-key-env: sk-ant-secret-value"
            ),
            "models.muse.api-key-env",
        ),
        # Effort and prices validate as for a direct pin.
        (
            GATEWAY_MODELS.replace("effort: medium", "effort: turbo"),
            "models.muse.effort",
        ),
        (
            GATEWAY_MODELS.replace("input: 2.00", "input: 0"),
            "models.muse.prices-usd-per-mtok.input",
        ),
        # Token caps are present positive integers.
        (
            GATEWAY_MODELS.replace("    output-cap-tokens: 32000\n", ""),
            "models.muse.output-cap-tokens",
        ),
        (
            GATEWAY_MODELS.replace("    context-threshold-tokens: 100000\n", ""),
            "models.muse.context-threshold-tokens",
        ),
        (
            GATEWAY_MODELS.replace("output-cap-tokens: 32000", "output-cap-tokens: 0"),
            "models.muse.output-cap-tokens",
        ),
        # Threshold must exceed safety margin plus request overhead.
        (
            GATEWAY_MODELS.replace(
                "context-threshold-tokens: 100000", "context-threshold-tokens: 2000"
            ),
            "models.muse.context-threshold-tokens",
        ),
    ],
)
def test_gateway_pin_refusals_name_the_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models_block: str,
    expect: str,
) -> None:
    """§3: every gateway-pin check refuses offline, naming its field."""
    _with_gateway_env(monkeypatch)
    config = _write_config(tmp_path, _gateway_config(models_block))

    _, faults = validate_v2_file(config)

    assert faults
    assert any(expect in str(fault) for fault in faults), (
        f"expected {expect!r} in {[str(f) for f in faults]}"
    )


def test_direct_pin_naming_a_token_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3: token keys are gateway-pin fields; a direct pin must not name them."""
    _without_env(monkeypatch)
    text = _minimal_config().replace(
        "    api-key-env: ANTHROPIC_API_KEY\n",
        "    api-key-env: ANTHROPIC_API_KEY\n    output-cap-tokens: 32000\n",
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v2_file(config)

    assert faults
    assert any("models.sonnet.output-cap-tokens" in str(fault) for fault in faults)


def test_direct_pins_validate_and_record_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway support changes nothing about direct pins."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    cfg, faults = validate_v2_file(config)

    assert not faults
    assert cfg is not None
    decl = cfg.models["sonnet"]
    assert decl.provider == "anthropic"
    assert decl.gateway_kind is None
    assert decl.gateway_url is None
    assert decl.output_cap_tokens is None
    assert decl.context_threshold_tokens is None


def test_agent_run_passes_version_parsing_for_a_version_2_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the cutover slice the Worker runs the step runtime: a
    `version: 2` file passes version parsing (the refusal, if any, is a
    later startup step — here the missing toolchain matrix)."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    assert (
        cli.main(
            [
                "run",
                "--config",
                str(config),
                "--workflow",
                "review",
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "unsupported version" not in err
    assert "toolchain-matrix" in err
