"""The Provider Capability Report: persisted at startup, served by status (#474).

Contract: step-workflow contract §4.1 step 7 and §9.1. Each run of the
Provider Capability Assertion produces one report — the pin key, when the
assertion ran, and one entry per check in the order they ran. The Worker
persists the latest report in the operator store on success and on refusal;
``agentctl status`` returns it as ``status.provider_capability`` (``None``
where no assertion has run yet) from both the live and the offline source.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator import offline as offline_reader
from coding_agent.operator.live import LiveView
from coding_agent.operator.snapshots import fresh_offline_status
from coding_agent.operator.store import (
    OperatorStore,
    open_read_connection,
    read_provider_capability_report,
)
from coding_agent.provider.capability import (
    CHECK_EFFORT,
    CHECK_IDENTITY,
    CHECK_PRICE,
    CHECK_TOOL_CALL_AUTO,
    CHECK_TOOL_CALL_FORCED,
    CHECK_USAGE,
    OUTCOME_NOT_RUN,
    OUTCOME_PASSED,
    OUTCOME_REFUSED,
    OUTCOME_TEXT_ANSWER,
    VIA_INVALID_EFFORT_REFUSED,
    VIA_REPORTED,
    CapabilityAssertionResult,
    CapabilityCheck,
    ProviderCapabilityRefused,
    ProviderCapabilityReport,
    assert_provider_capability,
)
from coding_agent.provider.config import PINNED_MODELS
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import PriceTable
from coding_agent.runtime.redaction import SecretRegistry
from support.scripted_model import ScriptedModel

PIN_KEY = "anthropic:claude-sonnet-5@messages"


def _sample_report(pin_key: str = PIN_KEY) -> ProviderCapabilityReport:
    """A passed assertion's report: every check recorded in run order."""
    return ProviderCapabilityReport(
        pin_key=pin_key,
        ran_at="2026-11-18T12:00:00Z",
        checks=(
            CapabilityCheck(
                check=CHECK_PRICE,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence=f"price table entry for {pin_key!r}",
            ),
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence="called 'capability_probe' under auto",
            ),
            CapabilityCheck(
                check=CHECK_TOOL_CALL_FORCED,
                gating=False,
                outcome=OUTCOME_REFUSED,
                evidence="only `auto` is supported for `tool_choice`",
            ),
            CapabilityCheck(
                check=CHECK_USAGE,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence="usage input=8 output=4 total=12",
            ),
            CapabilityCheck(
                check=CHECK_IDENTITY,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence="response model 'claude-sonnet-5' equals the pinned model",
            ),
            CapabilityCheck(
                check=CHECK_EFFORT,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence="the probe response ran at the pinned effort "
                "'medium' and reported it",
                via=VIA_REPORTED,
                reported="medium",
                pinned="medium",
            ),
        ),
    )


def _store(tmp_path: Path) -> OperatorStore:
    return OperatorStore(tmp_path / "run-ledger.sqlite3")


# --- store round-trip --------------------------------------------------------


def test_no_report_before_any_assertion_has_run(tmp_path: Path) -> None:
    """The store, the read side and fresh offline status all read absent."""
    store = _store(tmp_path)
    try:
        assert store.get_provider_capability_report() is None
        assert read_provider_capability_report(store._connection) is None
    finally:
        store.close()
    assert fresh_offline_status("2026-11-18T12:00:00Z")["provider_capability"] is None


def test_record_and_read_round_trip(tmp_path: Path) -> None:
    """The recorded report reads back exactly as ``status`` carries it."""
    store = _store(tmp_path)
    try:
        stored = store.record_provider_capability_report(
            _sample_report().to_dict()
        )
        assert stored is not None
        assert store.get_provider_capability_report() == _sample_report().to_dict()
    finally:
        store.close()


def test_only_the_latest_report_is_kept(tmp_path: Path) -> None:
    """A second assertion replaces the first: one row, never a history."""
    store = _store(tmp_path)
    try:
        store.record_provider_capability_report(_sample_report().to_dict())
        newer = ProviderCapabilityReport(
            pin_key=PIN_KEY,
            ran_at="2026-11-18T13:00:00Z",
            checks=(
                CapabilityCheck(
                    check=CHECK_PRICE,
                    gating=True,
                    outcome=OUTCOME_REFUSED,
                    evidence="no Price Table entry",
                ),
                CapabilityCheck(
                    check=CHECK_TOOL_CALL_AUTO,
                    gating=True,
                    outcome=OUTCOME_NOT_RUN,
                    evidence="",
                ),
                CapabilityCheck(
                    check=CHECK_TOOL_CALL_FORCED,
                    gating=False,
                    outcome=OUTCOME_NOT_RUN,
                    evidence="",
                ),
                CapabilityCheck(
                    check=CHECK_USAGE,
                    gating=True,
                    outcome=OUTCOME_NOT_RUN,
                    evidence="",
                ),
                CapabilityCheck(
                    check=CHECK_IDENTITY,
                    gating=True,
                    outcome=OUTCOME_NOT_RUN,
                    evidence="",
                ),
                CapabilityCheck(
                    check=CHECK_EFFORT,
                    gating=True,
                    outcome=OUTCOME_NOT_RUN,
                    evidence="",
                ),
            ),
        )
        store.record_provider_capability_report(newer.to_dict())
        assert store.get_provider_capability_report() == newer.to_dict()
    finally:
        store.close()


def test_report_survives_reopening_the_store(tmp_path: Path) -> None:
    """A restart reads the last recorded report: the refused startup stays visible."""
    path = tmp_path / "run-ledger.sqlite3"
    first = _store(tmp_path)
    try:
        first.record_provider_capability_report(_sample_report().to_dict())
    finally:
        first.close()
    second = OperatorStore(path)
    try:
        assert second.get_provider_capability_report() == _sample_report().to_dict()
    finally:
        second.close()


def test_report_is_redacted_and_capped_at_write(tmp_path: Path) -> None:
    """A secret in a provider error never reaches ``agentctl`` output."""
    secret = "sk-ant-secret-value"
    report = ProviderCapabilityReport(
        pin_key=PIN_KEY,
        ran_at="2026-11-18T12:00:00Z",
        checks=(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_FORCED,
                gating=False,
                outcome=OUTCOME_REFUSED,
                evidence=f"401: invalid x-api-key {secret} " + "x" * 500,
            ),
        ),
    )
    store = _store(tmp_path)
    try:
        stored = store.record_provider_capability_report(
            report.to_dict(), registry=SecretRegistry({"ANTHROPIC_API_KEY": secret})
        )
        assert stored is not None
        read_back = store.get_provider_capability_report()
        assert read_back is not None
        evidence = str(read_back["checks"][0]["evidence"])
        assert secret not in evidence
        assert secret not in json.dumps(read_back)
        assert secret not in json.dumps(stored)
        assert len(evidence) <= 300
    finally:
        store.close()


def test_missing_table_reads_as_absent(tmp_path: Path) -> None:
    """A volume predating the report table reads as no report, never an error."""
    path = tmp_path / "bare.sqlite3"
    connection = sqlite3.connect(path)
    try:
        assert read_provider_capability_report(connection) is None
    finally:
        connection.close()


def test_report_without_pin_key_or_timestamp_stores_nothing(
    tmp_path: Path,
) -> None:
    """A report missing its identity refuses nothing and stores nothing."""
    store = _store(tmp_path)
    try:
        assert store.record_provider_capability_report({}) is None
        assert store.record_provider_capability_report(
            {"pin_key": "", "ran_at": "", "checks": []}
        ) is None
        assert store.get_provider_capability_report() is None
    finally:
        store.close()


# --- report shape ------------------------------------------------------------


def test_report_dict_round_trip_ignores_unknown_fields() -> None:
    """``from_dict`` rebuilds what ``to_dict`` carries, ignoring the rest."""
    record = _sample_report().to_dict()
    record["future_field"] = "ignored"
    assert isinstance(record["checks"], list)
    first = record["checks"][0]
    assert isinstance(first, dict)
    first["future_field"] = "ignored"
    rebuilt = ProviderCapabilityReport.from_dict(record)
    assert rebuilt.to_dict() == _sample_report().to_dict()
    assert CapabilityCheck.from_dict({}) == CapabilityCheck(
        check="", gating=True, outcome=OUTCOME_NOT_RUN, evidence=""
    )


def test_report_dict_omits_absent_effort_fields() -> None:
    """Only the effort entry carries ``via``/``reported``/``pinned``."""
    record = _sample_report().to_dict()
    assert isinstance(record["checks"], list)
    by_check = {entry["check"]: entry for entry in record["checks"]}
    assert by_check[CHECK_EFFORT]["via"] == VIA_REPORTED
    assert by_check[CHECK_EFFORT]["reported"] == "medium"
    assert by_check[CHECK_EFFORT]["pinned"] == "medium"
    assert "via" not in by_check[CHECK_TOOL_CALL_AUTO]
    assert "reported" not in by_check[CHECK_TOOL_CALL_AUTO]
    assert "pinned" not in by_check[CHECK_TOOL_CALL_AUTO]


def test_forced_probe_is_the_only_non_gating_check() -> None:
    """Every check gates startup except the diagnostic forced probe."""
    by_check = {entry.check: entry for entry in _sample_report().checks}
    assert by_check[CHECK_TOOL_CALL_FORCED].gating is False
    assert by_check[CHECK_TOOL_CALL_FORCED].outcome == OUTCOME_REFUSED
    for name in (
        CHECK_PRICE,
        CHECK_TOOL_CALL_AUTO,
        CHECK_USAGE,
        CHECK_IDENTITY,
        CHECK_EFFORT,
    ):
        assert by_check[name].gating is True
    assert by_check[CHECK_TOOL_CALL_FORCED].outcome != OUTCOME_TEXT_ANSWER
    text_answer = CapabilityCheck(
        check=CHECK_TOOL_CALL_FORCED,
        gating=False,
        outcome=OUTCOME_TEXT_ANSWER,
        evidence="the forced choice was silently downgraded to auto",
    )
    assert ProviderCapabilityReport(
        pin_key=PIN_KEY, ran_at="2026-11-18T12:00:00Z", checks=(text_answer,)
    ).to_dict()["checks"][0]["outcome"] == OUTCOME_TEXT_ANSWER


# --- status sources ----------------------------------------------------------


def _live_ready() -> LiveView:
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    return live


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "run-ledger.sqlite3")


def test_live_status_carries_null_before_any_assertion(
    tmp_path: Path,
) -> None:
    """``status.provider_capability`` is ``None`` before the assertion runs."""
    ledger = _ledger(tmp_path)
    try:
        result = operator_listener.build_status_result(ledger.path, _live_ready())
        assert result["provider_capability"] is None
    finally:
        ledger.close()


def test_live_status_carries_the_report_after_a_record(
    tmp_path: Path,
) -> None:
    """The live source serves the recorded report through the shared builder."""
    ledger = _ledger(tmp_path)
    store = OperatorStore(ledger.path)
    try:
        store.record_provider_capability_report(_sample_report().to_dict())
        result = operator_listener.build_status_result(ledger.path, _live_ready())
        assert result["provider_capability"] == _sample_report().to_dict()
    finally:
        store.close()
        ledger.close()


def test_offline_status_carries_the_report_after_a_record(
    tmp_path: Path,
) -> None:
    """The offline source serves the same report: the refused check stays visible."""
    ledger = _ledger(tmp_path)
    store = OperatorStore(ledger.path)
    try:
        store.record_provider_capability_report(_sample_report().to_dict())
    finally:
        store.close()
        ledger.close()
    result = offline_reader.load_offline_status(
        tmp_path / "run-ledger.sqlite3", tmp_path
    )
    assert result["instance"]["state"] == "stopped"
    assert result["provider_capability"] == _sample_report().to_dict()


def test_offline_source_serves_the_report_without_a_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentctl --offline status`` carries the report when no Worker runs."""
    from coding_agent.operator.paths import STATE_DIR_ENV_VAR

    ledger = _ledger(tmp_path)
    store = OperatorStore(ledger.path)
    try:
        store.record_provider_capability_report(_sample_report().to_dict())
    finally:
        store.close()
        ledger.close()
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    code = agentctl.main(["--contract", "1", "--offline", "status"])
    assert code == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is True
    assert envelope["result"]["provider_capability"] == _sample_report().to_dict()


# --- startup persistence -----------------------------------------------------


class _CapableModel(ScriptedModel):
    """A ScriptedModel that tolerates the assertion's call-time kwargs.

    The real assertion sends the Pinned Model's output cap as a call-time
    ``max_tokens`` kwarg (startup always pins one); the base fake's
    ``invoke`` takes the input only, so the override drops the kwargs and
    pops the next queued response as usual.
    """

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        _ = kwargs
        return super().invoke(input, **kwargs)


def _real_assertion(
    pin: PinnedModel,
    model: InvokableToolModel,
    price_table: PriceTable,
    *,
    max_tokens: int | None,
) -> CapabilityAssertionResult:
    """The production assertion, for the end-to-end persistence tests."""
    return assert_provider_capability(pin, model, price_table, max_tokens=max_tokens)


def _run_worker_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_error: Exception | None,
    model: ScriptedModel | None = None,
    real_assertion: bool = False,
) -> tuple[int, Path]:
    """Run the Worker offline with fake ports; return exit code and state dir.

    ``real_assertion`` drives the production assertion against ``model``
    instead of the fake port's canned report, so the persisted report is
    the one the assertion actually built.
    """
    from test_worker_startup_progress import _fakes, _run
    from support.fake_step_tracker import FakeStepTracker
    from coding_agent.identity.startup import Identity

    tracker = FakeStepTracker(
        agent_login=Identity(account_id=999, login="coding-agent").login,
        agent_id=999,
    )
    fakes = _fakes(
        monkeypatch,
        tracker,
        model if model is not None else ScriptedModel([]),
        provider_error=provider_error,
    )
    if real_assertion:
        monkeypatch.setattr(
            fakes.model, "assert_provider_capability", _real_assertion
        )
    exit_code, _ = _run(tmp_path, monkeypatch, tracker, max_sweeps=1, fakes=fakes)
    return exit_code, tmp_path / "state"


def test_successful_startup_persists_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passed assertion is persisted: live ``status`` shows the report.

    Drives the production assertion end to end: the gating probe answers
    with a schema-valid ``capability_probe`` call, reports usage and the
    pinned effort, and the forced probe answers with text, so the stored
    report is the one the assertion actually built.
    """
    pin = PINNED_MODELS["anthropic"]
    auto = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "capability_probe",
                "args": {"city": "paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 8,
            "output_tokens": 4,
            "total_tokens": 12,
        },
    )
    forced = AIMessage(content="OK")
    model = _CapableModel(
        [auto, forced],
        response_models=[pin.model, pin.model],
        reported_efforts=[pin.effort, None],
    )
    exit_code, state_dir = _run_worker_with(
        tmp_path, monkeypatch, provider_error=None, model=model, real_assertion=True
    )
    assert exit_code == 0
    assert model.tool_choices == [None, "capability_probe"]
    connection = open_read_connection(state_dir / "run-ledger.sqlite3")
    try:
        stored = read_provider_capability_report(connection)
    finally:
        connection.close()
    assert stored is not None
    assert stored["pin_key"] == PINNED_MODELS["anthropic"].key
    assert [entry["check"] for entry in stored["checks"]] == [
        CHECK_PRICE,
        CHECK_TOOL_CALL_AUTO,
        CHECK_TOOL_CALL_FORCED,
        CHECK_USAGE,
        CHECK_IDENTITY,
        CHECK_EFFORT,
    ]
    assert all(entry["gating"] for entry in stored["checks"]
               if entry["check"] != CHECK_TOOL_CALL_FORCED)


def test_refused_startup_persists_the_report_before_the_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused pin still records its report, so offline ``status`` names it."""
    refused = ProviderCapabilityRefused(
        "pinned model refused the capability probe",
        report=_sample_report(),
    )
    exit_code, state_dir = _run_worker_with(
        tmp_path, monkeypatch, provider_error=refused
    )
    assert exit_code == 1
    result = offline_reader.load_offline_status(
        state_dir / "run-ledger.sqlite3", state_dir
    )
    assert result["provider_capability"] == _sample_report().to_dict()
    assert result["instance"]["last_halt"] is not None
    assert result["instance"]["last_halt"]["reason"] == "startup-check-refused"


# --- provider-check ----------------------------------------------------------


def _write_provider_check_config(tmp_path: Path) -> Path:
    """A v4 Workflow Configuration holding the direct `sonnet` pin."""
    path = tmp_path / "workflow.yml"
    path.write_text(
        "version: 4\n"
        "target:\n"
        "  kind: github\n"
        "  repository: example-owner/example-repo\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok:"
        " {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "    context-window-tokens: 1000000\n"
        "    context-threshold-tokens: 80000\n"
        "    output-cap-tokens: 32000\n"
        "worker:\n"
        "  poll-interval-seconds: 5\n"
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        "    limits: {cost-usd: 6, effective-tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: work\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      work:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: {end: completed}}\n",
        encoding="utf-8",
    )
    return path


def test_provider_check_prints_the_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``provider-check`` prints the report instead of its ad-hoc lines."""
    from coding_agent import cli

    pin = PINNED_MODELS["anthropic"]
    monkeypatch.setattr(cli, "build_chat_model", lambda _pin, **_kwargs: object())
    monkeypatch.setattr(
        cli,
        "assert_provider_capability",
        lambda _pin, _model, _table, **_kwargs: CapabilityAssertionResult(
            pin=pin,
            response=AIMessage(content=""),
            report=_sample_report(pin_key=pin.key),
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert cli.run_provider_check_command(_write_provider_check_config(tmp_path), "sonnet") == 0
    out = capsys.readouterr().out
    assert f"pinned model: {pin.key}" in out
    assert "check tool-call-auto: gating passed" in out
    assert "check tool-call-forced: diagnostic refused" in out
    assert "check effort: gating passed via reported" in out
    assert "no Attempt opened" in out


def test_provider_check_prints_the_report_on_refusal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A refused ``provider-check`` still prints the report built up to it."""
    from coding_agent import cli

    pin = PINNED_MODELS["anthropic"]
    monkeypatch.setattr(cli, "build_chat_model", lambda _pin, **_kwargs: object())

    def _refuse(_pin: Any, _model: Any, _table: Any, **_kwargs: Any) -> Any:
        raise ProviderCapabilityRefused(
            "pinned model refused the capability probe",
            report=_sample_report(pin_key=pin.key),
        )

    monkeypatch.setattr(cli, "assert_provider_capability", _refuse)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert cli.run_provider_check_command(_write_provider_check_config(tmp_path), "sonnet") == 1
    captured = capsys.readouterr()
    assert "check tool-call-auto: gating passed" in captured.out
    assert "FAILED" in captured.err


def test_provider_check_redacts_secrets_in_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A secret in a provider error never reaches ``provider-check`` output."""
    from coding_agent import cli

    secret = "sk-ant-secret-value"
    pin = PINNED_MODELS["anthropic"]
    report = ProviderCapabilityReport(
        pin_key=pin.key,
        ran_at="2026-11-18T12:00:00Z",
        checks=(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_FORCED,
                gating=False,
                outcome=OUTCOME_REFUSED,
                evidence=f"401: bad key {secret}",
            ),
        ),
    )
    monkeypatch.setattr(cli, "build_chat_model", lambda _pin: object())
    monkeypatch.setattr(
        cli,
        "assert_provider_capability",
        lambda _pin, _model, _table: CapabilityAssertionResult(
            pin=pin, response=AIMessage(content=""), report=report
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    assert cli.run_provider_check_command("anthropic") == 0
    out = capsys.readouterr().out
    assert secret not in out
    assert "«redacted:ANTHROPIC_API_KEY»" in out


def test_invalid_effort_refusal_evidence_names_the_effort() -> None:
    """The fallback leg's entry tells the two kinds of evidence apart."""
    entry = CapabilityCheck(
        check=CHECK_EFFORT,
        gating=True,
        outcome=OUTCOME_PASSED,
        evidence="refused naming the effort",
        via=VIA_INVALID_EFFORT_REFUSED,
        pinned="medium",
    )
    record = entry.to_dict()
    assert record["via"] == VIA_INVALID_EFFORT_REFUSED
    assert record["pinned"] == "medium"
    assert "reported" not in record
