"""Limits ``max_parallel_subagents``: parsing, pinning and recovery (C-CFG-4).

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table:
``workflows.<id>.limits.max_parallel_subagents``, default 2), §2.2 (strict
parsing: an integer >= 1), §2.5 (the Effective Configuration and its digest)
and §17 (recovery uses the ledger's pinned value, never the current file).

MVP row id: C-CFG-4 (acceptance scenario 12 of #208).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.ledger.store import RunLedger
from coding_agent.workflow import (
    WorkflowConfigError,
    load_workflow_config,
    resolve_effective_configuration,
)
from coding_agent.worker.recovery import RecoveryRefused, pinned_recovery_config


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflow.yml"
    config.write_text(text, encoding="utf-8")
    return config


def _minimal_config(limits_block: str = "") -> str:
    limits = f"    limits:\n{limits_block}" if limits_block else ""
    # NOTE: keep the blank line out when there are no limits so the YAML stays valid.
    limits_section = f"{limits}\n" if limits else ""
    return (
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
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    selector:\n"
        "      label: ready-for-agent\n"
        f"{limits_section}"
        "    steps:\n"
        "      - id: implement\n"
        "        skill: implement\n"
    )


def _env() -> dict[str, str]:
    return {"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}


def _pinned_record(max_parallel: int) -> dict[str, object]:
    return {
        "target": {
            "kind": "github",
            "repository": "example-owner/example-repo",
            "base_branch": "main",
            "token_env": "GITHUB_TOKEN",
            "ready_for_human": "ready-for-human",
        },
        "workflow": {
            "id": "implement",
            "selector": {"label": "ready-for-agent"},
            "limits": {"max_parallel_subagents": max_parallel},
        },
    }


# --- Default and normalisation -------------------------------------------------


def test_omitted_max_parallel_subagents_records_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = load_workflow_config(_write_config(tmp_path, _minimal_config()))

    effective = resolve_effective_configuration(cfg, "implement", env=_env())

    assert effective.workflow.limits.max_parallel_subagents == 2


def test_declared_value_survives_normalisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = load_workflow_config(
        _write_config(tmp_path, _minimal_config("      max_parallel_subagents: 4\n"))
    )

    effective = resolve_effective_configuration(cfg, "implement", env=_env())

    assert effective.workflow.limits.max_parallel_subagents == 4


def test_changing_the_value_changes_the_digest(tmp_path: Path) -> None:
    first = resolve_effective_configuration(
        load_workflow_config(
            _write_config(tmp_path, _minimal_config("      max_parallel_subagents: 2\n"))
        ),
        "implement",
        env=_env(),
    )
    second = resolve_effective_configuration(
        load_workflow_config(
            _write_config(tmp_path, _minimal_config("      max_parallel_subagents: 3\n"))
        ),
        "implement",
        env=_env(),
    )

    assert first.digest != second.digest


# --- Strict parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    "limits_block",
    [
        "      max_parallel_subagents: 0\n",
        "      max_parallel_subagents: -1\n",
        "      max_parallel_subagents: 1.5\n",
        '      max_parallel_subagents: "2"\n',
        "      max_parallel_subagents: true\n",
    ],
    ids=["zero", "negative", "float", "string", "boolean"],
)
def test_bad_max_parallel_subagents_is_refused(
    tmp_path: Path, limits_block: str
) -> None:
    config = _write_config(tmp_path, _minimal_config(limits_block))

    with pytest.raises(WorkflowConfigError) as excinfo:
        load_workflow_config(config)

    assert excinfo.value.path == "workflows.implement.limits.max_parallel_subagents"


# --- Recovery reads the ledger -------------------------------------------------


def test_recovery_reads_the_pinned_value_not_the_current_file(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration=_pinned_record(5))

        pinned = pinned_recovery_config(ledger.get_attempt("#7/1"))
    finally:
        ledger.close()

    assert pinned.max_parallel_subagents == 5


def test_recovery_defaults_to_two_for_records_without_limits(tmp_path: Path) -> None:
    record: dict[str, object] = {
        "target": {
            "kind": "github",
            "repository": "example-owner/example-repo",
            "base_branch": "main",
            "token_env": "GITHUB_TOKEN",
            "ready_for_human": "ready-for-human",
        },
        "workflow": {
            "id": "implement",
            "selector": {"label": "ready-for-agent"},
        },
    }
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration=record)

        pinned = pinned_recovery_config(ledger.get_attempt("#7/1"))
    finally:
        ledger.close()

    assert pinned.max_parallel_subagents == 2


@pytest.mark.parametrize("bad_value", [0, -3, 1.5, "2", True])
def test_recovery_refuses_an_invalid_pinned_value(
    tmp_path: Path, bad_value: object
) -> None:
    record: dict[str, object] = {
        "target": {
            "kind": "github",
            "repository": "example-owner/example-repo",
            "base_branch": "main",
            "token_env": "GITHUB_TOKEN",
            "ready_for_human": "ready-for-human",
        },
        "workflow": {
            "id": "implement",
            "selector": {"label": "ready-for-agent"},
            "limits": {"max_parallel_subagents": bad_value},
        },
    }
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration=record)

        with pytest.raises(RecoveryRefused):
            pinned_recovery_config(ledger.get_attempt("#7/1"))
    finally:
        ledger.close()
