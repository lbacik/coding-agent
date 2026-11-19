"""Pinned fault lists for deliberately broken Workflow Configuration fixtures.

Slice 1 of #466 (field descriptors): this test pins today's full fault list
— YAML path, text and order — for one broken fixture per section in all three
slices' scope (``worker``, ``target``, prices, ``skills``, ``models``,
``input``, ``defaults``, steps, step limits, the Attempt ceiling). It passed
before the descriptor refactor and must stay unchanged (and green) through
all three slices: every fault message keeps its identical YAML path, text
and order in ``Problems``.

The fixtures build on one minimal valid ``version: 3`` file so each case
exercises exactly one section; knock-on faults inherent to today's
short-circuit behaviour (an unusable model is an unknown model; a step that
returns before its routes are recorded leaves later steps unreachable) are
part of the pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support.workflow_config import load_config


def _base(**overrides: str) -> str:
    """A minimal valid ``version: 3`` file; ``overrides`` replaces blocks."""
    blocks = {
        "version": "version: 3\n",
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
        "skills": "",
        "worker": "",
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
    return "".join(
        blocks[key] for key in ("version", "target", "models", "skills", "worker", "workflows")
    )


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation reads no secrets: run with the credential names unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _faults(tmp_path: Path, text: str) -> list[tuple[str, str]]:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    cfg, faults = load_config(config)
    assert cfg is None
    return [(fault.path, fault.detail) for fault in faults]


def test_pinned_worker_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown ``worker`` key plus two bad leaves: the first bad leaf
    short-circuits, so the second is never checked."""
    _without_env(monkeypatch)
    text = _base(
        worker="worker:\n"
        "  bogus: 1\n"
        "  poll-interval-seconds: -5\n"
        "  shutdown-grace-seconds: 0\n"
    )

    assert _faults(tmp_path, text) == [
        ("worker", "unsupported field 'bogus'"),
        (
            "worker.poll-interval-seconds",
            "'poll-interval-seconds' must be a positive number, got -5",
        ),
    ]


def test_pinned_target_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``target`` leaf is collected: the missing key faults twice
    (required, then non-empty), then kind membership and the token-env name."""
    _without_env(monkeypatch)
    text = _base(
        target="target:\n"
        "  kind: gitlab\n"
        "  repository: ''\n"
        "  token-env: ghp_secret-value\n"
    )

    assert _faults(tmp_path, text) == [
        ("target.base-branch", "missing required field 'base-branch'"),
        ("target.repository", "'target.repository' must be a non-empty string"),
        ("target.base-branch", "'target.base-branch' must be a non-empty string"),
        ("target.kind", "unsupported target kind 'gitlab': must be 'github'"),
        (
            "target.token-env",
            "looks like a secret value, not an environment-variable name",
        ),
    ]


def test_pinned_prices_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first bad price short-circuits the rest; the unusable model then
    faults at the Workflow that names it."""
    _without_env(monkeypatch)
    text = _base(
        models="models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: -2.00, output: -10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
    )

    assert _faults(tmp_path, text) == [
        (
            "models.sonnet.prices-usd-per-mtok.input",
            "'input' must be a positive number, got -2.0",
        ),
        ("workflows.review.model", "unknown model 'sonnet': available: "),
    ]


def test_pinned_skills_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad ``skills`` entry faults every leaf after the unknown field."""
    _without_env(monkeypatch)
    text = _base(
        skills="skills:\n"
        "  - source: ''\n"
        "    branch: main\n"
        "    include: implement\n"
        "    ref: 123\n"
    )

    assert _faults(tmp_path, text) == [
        ("skills[0]", "unsupported field 'branch'"),
        ("skills[0].source", "'source' must be a non-empty string"),
        ("skills[0].include", "'include' must be a non-empty list of skill names"),
        ("skills[0].ref", "'ref' must be a non-empty string"),
    ]


def test_pinned_model_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad effort and a secret-shaped key fault in leaf order; the unusable
    model then faults at the Workflow that names it."""
    _without_env(monkeypatch)
    text = _base(
        models="models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: ultra\n"
        "    api-key-env: sk-ant-secret-value\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
    )

    assert _faults(tmp_path, text) == [
        (
            "models.sonnet.effort",
            "unknown effort 'ultra': must be 'low', 'medium' or 'high'",
        ),
        (
            "models.sonnet.api-key-env",
            "looks like a secret value, not an environment-variable name",
        ),
        ("workflows.review.model", "unknown model 'sonnet': available: "),
    ]


def test_pinned_input_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _base(
        workflows="workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: email, label: ''}\n"
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

    assert _faults(tmp_path, text) == [
        (
            "workflows.review.input.type",
            "must be `issue` or `pull-request` (exactly one input type per Workflow)",
        ),
        (
            "workflows.review.input.label",
            "'input.label' must be a non-empty string",
        ),
    ]


def test_pinned_defaults_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``defaults.limits`` block is refused twice (unknown field, then the
    targeted refusal) before the bad ceiling."""
    _without_env(monkeypatch)
    text = _base(
        workflows="workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: review\n"
        "    defaults:\n"
        "      limits: {cost-usd: 1}\n"
        "      max-executions: 0\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: failed}}\n"
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

    assert _faults(tmp_path, text) == [
        ("workflows.review.defaults", "unsupported field 'limits'"),
        ("workflows.review.defaults", "there is no `defaults.limits`"),
        (
            "workflows.review.defaults.max-executions",
            "'max-executions' must be a positive integer, got 0",
        ),
    ]


def test_pinned_step_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown step key, two executors, and the knock-on: the step
    returns before its routes are recorded, so the next step is
    unreachable."""
    _without_env(monkeypatch)
    text = _base(
        workflows="workflows:\n"
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
        "        foo: 1\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            reviewed: {body: document}\n"
        "        operation: check-readiness\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )

    assert _faults(tmp_path, text) == [
        ("workflows.review.steps.review", "unsupported field 'foo'"),
        (
            "workflows.review.steps.review",
            "a step has exactly one executor: `agent` or `operation`",
        ),
        ("workflows.review.steps.publish", "unreachable from `start`"),
    ]


def test_pinned_step_limits_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Reserved Step with one meter faults the whole allowance; its
    routes are never recorded, so the next step is unreachable."""
    _without_env(monkeypatch)
    text = _base(
        workflows="workflows:\n"
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
        "        limits: {cost-usd: 1}\n"
        "        reserved: true\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )

    assert _faults(tmp_path, text) == [
        (
            "workflows.review.steps.review.limits",
            "a Reserved Step declares all four meters "
            "(missing: tokens, tool-calls, wall-clock-minutes)",
        ),
        ("workflows.review.steps.publish", "unreachable from `start`"),
    ]


def test_pinned_attempt_ceiling_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing meter faults as required, then the first bad meter
    short-circuits the rest."""
    _without_env(monkeypatch)
    text = _base(
        workflows="workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        "    limits: {cost-usd: -6, tool-calls: 300, wall-clock-minutes: 30}\n"
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

    assert _faults(tmp_path, text) == [
        ("workflows.review.limits.tokens", "missing required field 'tokens'"),
        (
            "workflows.review.limits.cost-usd",
            "'cost-usd' must be a positive number, got -6",
        ),
    ]
