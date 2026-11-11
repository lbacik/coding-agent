"""Step-workflow pinning: skill texts, model tables and startup checks.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10
(the Effective Configuration pins skill texts, the Skill Bundle identity
and the Pinned Model's context window and output cap under one digest;
recovery uses pinned values only), §4.1 (the Provider Capability Assertion
runs with the output cap) and §16 (a missing or unreadable pinned record
refuses startup).

Matrix rows: CFG-41, CFG-42, STEP-17 (all S2).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import (
    UNHONOURED_EFFORT,
    assert_provider_capability,
)
from coding_agent.provider.config import (
    DEFAULT_OUTPUT_CAPS,
    PINNED_MODELS,
    output_cap_for,
)
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.workflow import (
    WorkflowConfig,
    WorkflowConfigError,
    effective_configuration_record,
    load_workflow_config,
    resolve_effective_configuration,
)
from coding_agent.worker.recovery import (
    RecoveryRefused,
    pinned_recovery_config,
    validate_recovery_configs,
)


def _write_config(
    tmp_path: Path, text: str, prompt_files: dict[str, str] | None = None
) -> Path:
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


def _load(
    tmp_path: Path, text: str, prompts: dict[str, str] | None = None
) -> WorkflowConfig:
    return load_workflow_config(_write_config(tmp_path, text, prompts))


class _FakeModel:
    """A scripted model recording every call's kwargs (output-cap travel)."""

    def __init__(self, outcomes: list[BaseMessage | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.call_kwargs: list[dict[str, Any]] = []

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> _FakeModel:
        _ = (tools, tool_choice)
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        _ = input
        self.call_kwargs.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ai_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "capability_probe",
                "args": {"city": "paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
        usage_metadata={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
        response_metadata={"model_name": "fake-model"},
    )


def _effort_refused() -> ModelInvalidRequestError:
    return ModelInvalidRequestError(
        "Error code: 400 - output_config.effort: Input should be 'low', 'medium' or 'high'"
    )


PIN = PinnedModel(
    provider="anthropic", model="claude-sonnet-5", endpoint="messages", effort="medium"
)
PRICE_TABLE = {
    PIN.key: TokenPrices(input=1.0, output=1.0, cache_read=1.0, cache_write=1.0)
}


# --- CFG-41: pinning ----------------------------------------------------------


def test_effective_model_carries_code_owned_window_and_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.10: the Pinned Model's context window and output cap are pinned."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())

    effective = resolve_effective_configuration(cfg, "implement")

    assert effective.model.context_window is not None
    assert effective.model.context_window > 0
    assert effective.model.output_cap == output_cap_for(PIN)
    assert effective.model.output_cap == 32000


def test_resolve_pins_statically_named_skill_texts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    """§2.10: every statically named skill's SKILL.md bytes are pinned."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())

    effective = resolve_effective_configuration(
        cfg, "implement", skills_dir=skills_dir
    )

    assert [skill.name for skill in effective.skills] == ["implement"]
    assert (
        effective.skills[0].content
        == (skills_dir / "implement" / "SKILL.md").read_bytes()
    )
    assert effective.skill_bundle_source != ""
    assert effective.skill_bundle_commit != ""
    assert effective.skill_bundle_digest != ""


def test_digest_is_stable_across_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    """§2.10: nothing moves the digest on a re-resolve of the same tree."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config(), {"notes.md": "Notes.\n"})

    first = resolve_effective_configuration(cfg, "implement", skills_dir=skills_dir)
    second = resolve_effective_configuration(cfg, "implement", skills_dir=skills_dir)

    assert first.digest == second.digest


def test_edited_skill_text_moves_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    """§2.10: an edited skill moves the digest, so recovery can tell."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())
    before = resolve_effective_configuration(cfg, "implement", skills_dir=skills_dir)

    (skills_dir / "implement" / "SKILL.md").write_text(
        "# edited after pinning\n", encoding="utf-8"
    )
    after = resolve_effective_configuration(cfg, "implement", skills_dir=skills_dir)

    assert after.digest != before.digest
    assert after.skill_bundle_digest != before.skill_bundle_digest


def test_edited_prompt_file_moves_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    """§2.10: prompt bytes are pinned too — an edit moves the digest."""
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
            "      - id: notes\n"
            "        prompt_file: notes.md\n"
        )
    )
    config = _write_config(tmp_path, text, {"notes.md": "Notes v1.\n"})
    cfg = load_workflow_config(config)
    before = resolve_effective_configuration(cfg, "implement", skills_dir=skills_dir)

    (tmp_path / "notes.md").write_text("Notes v2.\n", encoding="utf-8")
    after = resolve_effective_configuration(cfg, "implement", skills_dir=skills_dir)

    assert after.digest != before.digest


def test_output_cap_table_moves_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.10: the model table is pinned — a cap change moves the digest."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())
    before = resolve_effective_configuration(cfg, "implement")

    monkeypatch.setitem(DEFAULT_OUTPUT_CAPS, PIN.key, 16000)
    after = resolve_effective_configuration(cfg, "implement")

    assert after.model.output_cap == 16000
    assert after.digest != before.digest


def test_restart_with_edited_file_leaves_the_stored_row_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    """CFG-41: the Attempt's pinned row survives a restart with edits."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())
    effective = resolve_effective_configuration(
        cfg, "implement", skills_dir=skills_dir
    )
    record = effective_configuration_record(effective)

    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration=record)

        (skills_dir / "implement" / "SKILL.md").write_text(
            "# edited while the Attempt is open\n", encoding="utf-8"
        )
        fresh = resolve_effective_configuration(
            cfg, "implement", skills_dir=skills_dir
        )
        assert fresh.digest != effective.digest

        stored = ledger.get_attempt("#7/1")
        assert stored.effective_configuration_digest == effective.digest
        assert stored.effective_configuration == record

        pinned = pinned_recovery_config(stored)
        assert pinned.repository == "octocat/sandbox"
        assert pinned.selection_label == "ready-for-agent"
    finally:
        ledger.close()


# --- CFG-42: missing or unreadable pinned records ----------------------------


def test_missing_pinned_record_refuses_startup(tmp_path: Path) -> None:
    """§16: an unfinished Attempt with no pinned record refuses startup."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration={})

        with pytest.raises(RecoveryRefused):
            validate_recovery_configs(ledger)
    finally:
        ledger.close()


def test_unreadable_pinned_record_refuses_startup(tmp_path: Path) -> None:
    """§16: an unfinished Attempt with a broken record refuses startup."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "#7/1", effective_configuration={"target": "not-a-mapping"}
        )

        with pytest.raises(RecoveryRefused):
            validate_recovery_configs(ledger)
    finally:
        ledger.close()


def test_empty_digest_refuses_startup(tmp_path: Path) -> None:
    """§16: an unfinished Attempt with no digest refuses startup (CFG-42)."""
    path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(path)
    try:
        ledger.open_attempt(
            "#7/1",
            effective_configuration={
                "target": {"repository": "o/r", "ready_for_human": "ready"},
                "workflow": {"selector": {"label": "x"}},
            },
        )
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE attempts SET effective_configuration_digest = '' "
            "WHERE attempt_id = '#7/1'"
        )
        connection.commit()
    finally:
        connection.close()

    ledger = RunLedger(path)
    try:
        with pytest.raises(RecoveryRefused, match="digest"):
            validate_recovery_configs(ledger)
    finally:
        ledger.close()


# --- STEP-17: the capability assertion carries the output cap ----------------


def test_capability_probes_carry_the_output_cap() -> None:
    """§2.10: both probe requests carry the Pinned Model's output cap."""
    model = _FakeModel([_ai_message(), _effort_refused()])

    assert_provider_capability(PIN, model, PRICE_TABLE, max_tokens=32000)

    assert model.call_kwargs == [
        {"max_tokens": 32000},
        {"reasoning_effort": UNHONOURED_EFFORT, "max_tokens": 32000},
    ]


def test_capability_without_cap_keeps_the_mvp_shape() -> None:
    """§2.10: ``None`` sends the probes exactly as the MVP did."""
    model = _FakeModel([_ai_message(), _effort_refused()])

    assert_provider_capability(PIN, model, PRICE_TABLE, max_tokens=None)

    assert model.call_kwargs == [{}, {"reasoning_effort": UNHONOURED_EFFORT}]


def test_output_cap_table_answers_every_pinned_model() -> None:
    """§2.1: every code-owned pin has an output cap; strangers have none."""
    for pin in PINNED_MODELS.values():
        assert output_cap_for(pin) == 32000

    stranger = PinnedModel(
        provider="openai", model="gpt-9", endpoint="responses", effort="medium"
    )
    assert output_cap_for(stranger) is None


def test_unknown_model_pair_refuses_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.1: a (provider, model) pair outside the code-owned table refuses."""
    _with_env(monkeypatch)
    text = _minimal_config(
        models=(
            "models:\n"
            "  stranger:\n"
            "    provider: openai\n"
            "    model: gpt-9\n"
            "    endpoint: responses\n"
            "    effort: medium\n"
            "    api_key_env: ANTHROPIC_API_KEY\n"
            "    prices_usd_per_mtok: {input: 1.00, output: 1.00,"
            " cache_read: 0.01, cache_write: 0.01}\n"
        ),
        workflows=(
            "workflows:\n"
            "  implement:\n"
            "    model: stranger\n"
            "    selector:\n"
            "      label: ready-for-agent\n"
            "    steps:\n"
            "      - id: implement\n"
            "        skill: implement\n"
        ),
    )
    cfg = _load(tmp_path, text)

    with pytest.raises(WorkflowConfigError, match="outside the code-owned"):
        resolve_effective_configuration(cfg, "implement")


def test_pin_missing_from_output_cap_table_refuses_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STEP-17: a pin outside the output-cap table refuses startup."""
    _with_env(monkeypatch)
    cfg = _load(tmp_path, _minimal_config())
    monkeypatch.delitem(DEFAULT_OUTPUT_CAPS, PIN.key)

    with pytest.raises(WorkflowConfigError, match="output-cap"):
        resolve_effective_configuration(cfg, "implement")
