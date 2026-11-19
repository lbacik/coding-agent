"""Pinned field faults for the Workflow Configuration descriptor slices.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.9 (every
fault names its YAML path) and ADR 0027 (strict parsing is unchanged).

These tests pin today's full fault list — YAML path, text **and order** —
for one deliberately broken fixture per section in the scope of the field
descriptor slices (#507 worker/target/prices/skills, #508 models/gateway,
#509 input/defaults/steps/step-limits/Attempt-ceiling). They were written
before any descriptor refactor and must stay unchanged (and green) through
all three slices: any difference in path, text, order or count fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support.workflow_config import load_config


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _minimal_config(extra: str = "") -> str:
    """A minimal valid `version: 3` file; ``extra`` is spliced after models."""
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
        f"{extra}"
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
    )


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation reads no secrets: run with the credential names unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)


def _faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> list[tuple[str, str]]:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, text)
    _, faults = load_config(config)
    assert faults, "broken fixture validated but must be refused"
    return [(fault.path, fault.detail) for fault in faults]


def test_worker_faults_stop_at_first_bad_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both worker leaves are broken; only the first fault is reported."""
    text = _minimal_config(
        "worker:\n  poll-interval-seconds: 0\n  shutdown-grace-seconds: soon\n"
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "worker.poll-interval-seconds",
            "'poll-interval-seconds' must be a positive number, got 0",
        ),
    ]


def test_target_faults_collect_kind_then_token_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _minimal_config().replace(
        "  kind: github\n", "  kind: bogus\n"
    ).replace("  token-env: GITHUB_TOKEN\n", "  token-env: 'sekret!!'\n")

    assert _faults(tmp_path, monkeypatch, text) == [
        ("target.kind", "unsupported target kind 'bogus': must be 'github'"),
        (
            "target.token-env",
            "looks like a secret value, not an environment-variable name",
        ),
    ]


def test_prices_faults_stop_at_first_bad_meter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bad `input` price is reported; `output` is never checked. The
    unusable model then cascades into the workflow's `model` fault."""
    text = _minimal_config().replace("{input: 2.00, output: 10.00,", "{input: 0, output: -1,")

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "models.sonnet.prices-usd-per-mtok.input",
            "'input' must be a positive number, got 0",
        ),
        ("workflows.review.model", "unknown model 'sonnet': available: "),
    ]


def test_skills_entry_faults_in_source_include_ref_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _minimal_config(
        "skills:\n"
        "  - source: 'ftp://example.com/team-skills.git'\n"
        "    include: ['']\n"
        "    ref: ''\n"
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "skills[0].source",
            "skill source must use 'https://' or 'git+https://' (HTTPS Git only)",
        ),
        ("skills[0].include[0]", "skill name must be a non-empty string"),
        ("skills[0].ref", "'ref' must be a non-empty string"),
    ]


def test_models_direct_pin_faults_in_check_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prices are checked before the effort/api-key-env extras; the unusable
    model then cascades into the workflow's `model` fault."""
    text = (
        _minimal_config()
        .replace("    effort: medium\n", "    effort: absurd\n")
        .replace("    api-key-env: ANTHROPIC_API_KEY\n", "    api-key-env: 'sekret!!'\n")
        .replace("{input: 2.00, output: 10.00,", "{input: 0, output: 10.00,")
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "models.sonnet.prices-usd-per-mtok.input",
            "'input' must be a positive number, got 0",
        ),
        (
            "models.sonnet.effort",
            "unknown effort 'absurd': must be 'low', 'medium' or 'high'",
        ),
        (
            "models.sonnet.api-key-env",
            "looks like a secret value, not an environment-variable name",
        ),
        ("workflows.review.model", "unknown model 'sonnet': available: "),
    ]


def test_models_gateway_pin_faults_in_check_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _minimal_config().replace(
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n",
        "  sonnet:\n"
        "    gateway: {kind: bogus, url-env: 'bad!!'}\n"
        "    model: anything\n"
        "    endpoint: responses\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "    output-cap-tokens: 32000\n"
        "    context-threshold-tokens: 100000\n",
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "models.sonnet.gateway.kind",
            "unknown gateway kind 'bogus': must be one of litellm",
        ),
        (
            "models.sonnet.gateway.url-env",
            "looks like a secret value, not an environment-variable name",
        ),
        ("workflows.review.model", "unknown model 'sonnet': available: "),
    ]


def test_input_faults_missing_label_and_bad_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The missing required `label` is reported before any value check, and
    the absent label is reported again by the label check itself."""
    text = _minimal_config().replace(
        "    input: {type: pull-request, label: agent:review}\n",
        "    input: {type: bogus}\n",
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        ("workflows.review.input.label", "missing required field 'label'"),
        (
            "workflows.review.input.type",
            "must be `issue` or `pull-request` (exactly one input type per Workflow)",
        ),
        (
            "workflows.review.input.label",
            "'input.label' must be a non-empty string",
        ),
    ]


def test_defaults_faults_unknown_field_then_bad_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _minimal_config().replace(
        "      max-executions: 1\n", "      max-executions: 0\n      bogus: 1\n"
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        ("workflows.review.defaults", "unsupported field 'bogus'"),
        (
            "workflows.review.defaults.max-executions",
            "'max-executions' must be a positive integer, got 0",
        ),
    ]


def test_step_faults_in_file_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `reserved` type fault on `review` precedes the `limits` fault on
    `publish`: steps are checked in file order."""
    text = _minimal_config().replace(
        "            reviewed: {body: document}\n        routes: {reviewed: publish}\n",
        "            reviewed: {body: document}\n"
        "        reserved: 'yes'\n"
        "        routes: {reviewed: publish}\n",
    ).replace(
        "        operation: publish-review\n",
        "        operation: publish-review\n        limits: {cost-usd: 1}\n",
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        ("workflows.review.steps.review.reserved", "'reserved' must be a boolean"),
        (
            "workflows.review.steps.publish.limits",
            "`limits` apply only to agent steps; operations are bounded "
            "by code-owned timeouts",
        ),
    ]


def test_step_max_executions_stops_step_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad `max-executions` on the leaf step reports only that fault: the
    step's routes are never checked."""
    text = _minimal_config().replace(
        "        operation: publish-review\n",
        "        operation: publish-review\n        max-executions: 0\n",
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "workflows.review.steps.publish.max-executions",
            "'max-executions' must be a positive integer, got 0",
        ),
    ]


def test_reserved_step_missing_meters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Reserved Step without all four meters reports the missing meters;
    its routes are never recorded, so the downstream step is unreachable."""
    text = _minimal_config().replace(
        "            reviewed: {body: document}\n        routes: {reviewed: publish}\n",
        "            reviewed: {body: document}\n"
        "        reserved: true\n"
        "        limits: {cost-usd: 1}\n"
        "        routes: {reviewed: publish}\n",
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        (
            "workflows.review.steps.review.limits",
            "a Reserved Step declares all four meters "
            "(missing: tokens, tool-calls, wall-clock-minutes)",
        ),
        ("workflows.review.steps.publish", "unreachable from `start`"),
    ]


def test_ceiling_faults_missing_then_bad_meter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ceiling meters are reported before values are checked, and the
    first missing meter stops the value pass."""
    text = _minimal_config().replace(
        "    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300,"
        " wall-clock-minutes: 30}\n",
        "    limits: {cost-usd: 6, tokens: 1500000}\n",
    )

    assert _faults(tmp_path, monkeypatch, text) == [
        ("workflows.review.limits.tool-calls", "missing required field 'tool-calls'"),
        (
            "workflows.review.limits.wall-clock-minutes",
            "missing required field 'wall-clock-minutes'",
        ),
        (
            "workflows.review.limits.tool-calls",
            "'tool-calls' must be a positive number, got None",
        ),
    ]
