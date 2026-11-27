"""Operator §16 v1.2.1 mapping: thresholds, Handoff progress and exhaustion.

Covers the producer adoption of the patched shared mapping (issue #616):
the active Step Execution's selected Usable Threshold (pin-default,
hard-only, soft-plus-hard), the exact compared ``context_used`` with its
receiver availability and operation/finalisation omission, the Attempt-wide
peak including refused requests, ``main``/``subagent`` origins, pickup
counts, and the projected redacted native events for actual runtime
crossings, the AI-assisted Handoff result or refusal, Hard Handoff entry,
verified Round Handoff readiness and pickup exhaustion.

Live projections go through ``operator_listener.build_status_result`` (the
same builder the Attempt Archive read path derives from); archived
projections go through the idle ``build_status`` ``last_finished`` path, so
the durable exhaustion and human-reauthorisation diagnostic stays visible
during idle Intake, after restart and on an independent Worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.archive import ATTEMPT_EVENT_TYPES
from coding_agent.operator.live import LiveView
from coding_agent.operator.native_events import native_events_for_attempt
from coding_agent.runtime.ceilings import OriginCell, UsageTotals
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    render_diagnostic_section,
    render_handoff_block,
    render_handoff_section,
)
from coding_agent.runtime.step_finalisation import render_terminal_comment
from support.markers import terminal_marker_text
from coding_agent.runtime.transcript import work_conversation
from coding_agent.workflow.effective import make_test_record

ATTEMPT_ID = "implement#7/1"
PIN_THRESHOLD = 50_000
MODEL_WINDOW = 200_000
SAFETY = 1_000
OVERHEAD = 1_000
PIN_USABLE = PIN_THRESHOLD - SAFETY - OVERHEAD
HARD_TOKENS = 40_000
HARD_USABLE = HARD_TOKENS - SAFETY - OVERHEAD
SOFT_TOKENS = 30_000
SOFT_USABLE = SOFT_TOKENS - SAFETY - OVERHEAD
WORK = "a" * 40
BASE = "b" * 40
PICKUP_LABEL = "round-finished"
PICKUP_LIMIT = 2


def _live(phase: str = "process#1") -> LiveView:
    """A ready live view with a claim on the fixture Attempt."""
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    live.set_claim(ATTEMPT_ID, 7, "Fix the crash")
    live.set_phase(phase)
    return live


def _idle_live() -> LiveView:
    """A ready live view with no active Attempt (idle Intake)."""
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 9, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    return live


def _fresh_live() -> LiveView:
    """An independent Worker: a different process start, same ledger."""
    live = LiveView(
        process_started_at=datetime(2026, 11, 6, 10, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    return live


def _effective_config(
    *,
    steps: dict[str, Any] | None = None,
    round_handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A pinned Effective Configuration for the mapping tests."""
    return make_test_record(
        steps=steps,
        round_handoff=round_handoff,
        pin={
            "key": "anthropic:claude-test",
            "provider": "anthropic",
            "model": "claude-test",
            "context_threshold_tokens": PIN_THRESHOLD,
            "context_window": {
                "threshold_tokens": PIN_THRESHOLD,
                "safety_margin_tokens": SAFETY,
                "request_overhead_tokens": OVERHEAD,
            },
        },
    )


def _agent_steps(
    step_id: str = "process",
    *,
    thresholds: dict[str, Any] | None = None,
    routes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One agent step entry in Effective Configuration shape."""
    entry: dict[str, Any] = {
        "agent": {"tools": ["workspace-read"]},
        "routes": routes or {"done": {"end": "completed"}},
    }
    if thresholds is not None:
        entry["context_thresholds"] = thresholds
    return {step_id: entry}


def _open(
    ledger: RunLedger,
    config: dict[str, Any],
    attempt_id: str = ATTEMPT_ID,
) -> None:
    ledger.open_attempt(attempt_id, effective_configuration=config)


def _flush(
    ledger: RunLedger,
    context: dict[str, Any] | None,
    *,
    by_origin: Any = "zeros",
    attempt_id: str = ATTEMPT_ID,
) -> None:
    """Record one Attempt usage flush with the given main context."""
    from coding_agent.runtime.ceilings import ORIGINS

    cells: dict[str, OriginCell] | None
    if isinstance(by_origin, str) and by_origin == "zeros":
        cells = {origin: OriginCell() for origin in ORIGINS}
    elif by_origin is None or isinstance(by_origin, dict):
        cells = by_origin
    else:
        raise TypeError(f"unexpected by_origin {by_origin!r}")
    ledger.record_usage(
        attempt_id,
        BudgetSnapshot(
            elapsed_seconds=90.0,
            usage=UsageTotals(
                tokens=1200,
                effective_tokens=1000,
                cost_usd=0.02,
                tool_calls=7,
                input_tokens=800,
                output_tokens=200,
                cache_read_tokens=200,
            ),
            context=context,
            by_origin=cells,
        ),
    )


def _status(ledger: RunLedger, live: LiveView) -> dict[str, Any]:
    return operator_listener.build_status_result(ledger.path, live)


def _figures(ledger: RunLedger, live: LiveView) -> dict[str, Any]:
    current = _status(ledger, live)["current_attempt"]
    assert isinstance(current, dict)
    figures = current["figures"]
    assert isinstance(figures, dict)
    return figures


def _offer(**overrides: Any) -> RoundHandoffOffer:
    fields: dict[str, Any] = {
        "workflow_id": "implement",
        "issue": 7,
        "attempt": ATTEMPT_ID,
        "work_commit": WORK,
        "ref": "agent/implement-7-1-work",
        "base_revision": BASE,
        "note_kind": "ai-assisted",
        "note": "did the thing",
        "pickup_label": PICKUP_LABEL,
        "used": 0,
        "limit": PICKUP_LIMIT,
    }
    fields.update(overrides)
    return RoundHandoffOffer(**fields)


def _terminal_body(
    *,
    outcome: str = "unfinished",
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    handoff_section: str = "",
) -> str:
    return render_terminal_comment(
        attempt_id=ATTEMPT_ID,
        outcome=outcome,
        marker_text=terminal_marker_text(ATTEMPT_ID),
        ending_step=ending_step,
        step_result=step_result,
        workflow_end=workflow_end,
        handoff_section=handoff_section,
    )


def _record_outcome(ledger: RunLedger, body: str) -> None:
    ledger.record_terminal_outcome(
        ATTEMPT_ID,
        outcome="unfinished",
        reason="ended",
        terminal_comment_body=body,
    )


def _record_receipt(ledger: RunLedger, key: str, ordinal: int) -> None:
    ledger.record_step_effect_intent(
        ATTEMPT_ID,
        "",
        0,
        ordinal,
        rendered_payload=f"payload-{key}",
        expected_target={"terminal-write": key},
    )
    ledger.record_step_effect_receipt(ATTEMPT_ID, "", 0, ordinal, remote_id="r1")


def _record_all_receipts(ledger: RunLedger) -> None:
    _record_receipt(ledger, "terminal", 1)
    _record_receipt(ledger, "remove-selection-label", 2)
    _record_receipt(ledger, "release-assignment", 3)


def _native(
    ledger: RunLedger, attempt_id: str = ATTEMPT_ID
) -> list[dict[str, Any]]:
    with RunLedgerReader(ledger.path) as reader:
        return native_events_for_attempt(
            reader,
            attempt_id,
            process_started_at="2026-11-06T08:00:00Z",
            observed_at="2026-11-10T08:00:00Z",
        )


def _threshold_payload(
    value: int, basis: str, usable: int, step: str | None = None, seq: int | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversation_context": {"value": value, "basis": basis},
        "usable_threshold": usable,
    }
    if step is not None:
        payload["step_id"] = step
    if seq is not None:
        payload["seq"] = seq
    return payload


def _snapshot() -> BudgetSnapshot:
    return BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())


# --- Selected Usable Thresholds ---------------------------------------------


def test_pin_default_threshold_without_policy(tmp_path: Path) -> None:
    """A step without its own policy reports the pin-default Usable Threshold."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 10000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
        )
        figures = _figures(ledger, _live("process#1"))
        assert figures["context_threshold"] == {
            "value": PIN_USABLE,
            "unit": "tokens",
            "basis": "configured",
            "reason": (
                "the Usable Threshold: the Context Threshold less the safety "
                "margin and the request overhead"
            ),
        }
        assert "native.context_hard_usable_threshold_tokens" not in figures
    finally:
        ledger.close()


def test_hard_only_step_reports_hard_usable(tmp_path: Path) -> None:
    """A hard-only step reports its Hard Usable Threshold with no hard twin."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps("plan", thresholds={"hard_tokens": HARD_TOKENS})
            ),
        )
        _flush(
            ledger,
            {
                "estimate": 10000,
                "estimate_basis": "anchored",
                "conversation": work_conversation("plan", 2),
            },
        )
        figures = _figures(ledger, _live("plan#2"))
        assert figures["context_threshold"]["value"] == HARD_USABLE
        assert figures["context_threshold"]["basis"] == "configured"
        assert "native.context_hard_usable_threshold_tokens" not in figures
    finally:
        ledger.close()


def test_soft_plus_hard_reports_both_thresholds(tmp_path: Path) -> None:
    """A soft-plus-hard step selects soft and exposes the hard twin figure."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "plan",
                    thresholds={
                        "hard_tokens": HARD_TOKENS,
                        "soft_tokens": SOFT_TOKENS,
                    },
                )
            ),
        )
        _flush(
            ledger,
            {
                "estimate": 5000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("plan", 1),
            },
        )
        figures = _figures(ledger, _live("plan#1"))
        assert figures["context_threshold"]["value"] == SOFT_USABLE
        assert figures["native.context_hard_usable_threshold_tokens"] == {
            "value": HARD_USABLE,
            "unit": "tokens",
            "basis": "configured",
            "reason": (
                "the Hard Usable Threshold: the step's Hard Context "
                "Threshold less the safety margin and the request overhead"
            ),
        }
    finally:
        ledger.close()


# --- Receiver availability ----------------------------------------------------


def test_inherited_opening_stays_unavailable(tmp_path: Path) -> None:
    """A receiver that has not evaluated its own request reads unavailable."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 12000,
                "estimate_basis": "anchored",
                "conversation": "step/prev/3",
            },
        )
        figures = _figures(ledger, _live("next#1"))
        assert figures["context_used"] == {
            "value": None,
            "unit": "tokens",
            "basis": "unavailable",
            "reason": "the receiver has not evaluated its own request yet",
        }
    finally:
        ledger.close()


def test_matching_receiver_reports_compared_value(tmp_path: Path) -> None:
    """The receiver's own evaluated request reports the compared value."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 12000,
                "estimate_basis": "anchored",
                "conversation": work_conversation("next", 1),
            },
        )
        figures = _figures(ledger, _live("next#1"))
        assert figures["context_used"] == {
            "value": 12000,
            "unit": "tokens",
            "basis": "estimated",
            "reason": "anchored on provider-reported input",
        }
    finally:
        ledger.close()


def test_inherited_legacy_opening_stays_unavailable(tmp_path: Path) -> None:
    """A legacy sequence-less flush from another step reads unavailable."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 12000,
                "estimate_basis": "anchored",
                "conversation": "step/prev",
            },
        )
        figures = _figures(ledger, _live("next#1"))
        assert figures["context_used"] == {
            "value": None,
            "unit": "tokens",
            "basis": "unavailable",
            "reason": "the receiver has not evaluated its own request yet",
        }
    finally:
        ledger.close()


def test_matching_legacy_opening_reports_compared_value(tmp_path: Path) -> None:
    """A legacy sequence-less flush of the receiver's own step still reports."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 12000,
                "estimate_basis": "anchored",
                "conversation": "step/next",
            },
        )
        figures = _figures(ledger, _live("next#1"))
        assert figures["context_used"] == {
            "value": 12000,
            "unit": "tokens",
            "basis": "estimated",
            "reason": "anchored on provider-reported input",
        }
    finally:
        ledger.close()


def test_missing_historical_evidence_reads_unavailable(tmp_path: Path) -> None:
    """A flush from before the context travelled reads unavailable, never zero."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(ledger, None, by_origin=None)
        figures = _figures(ledger, _live("process#1"))
        used = figures["context_used"]
        assert used["value"] is None
        assert used["basis"] == "unavailable"
        peak = figures["context_peak"]
        assert peak["value"] is None
        assert peak["basis"] == "unavailable"
        assert peak["reason"] == "covers only requests recorded since upgrade"
    finally:
        ledger.close()


# --- Operation and finalisation omission --------------------------------------


def test_operation_step_omits_current_pair(tmp_path: Path) -> None:
    """An operation step omits the current pair but keeps the peak."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps={
                    "push": {
                        "operation": "commit-and-push",
                        "routes": {"pushed": {"end": "unfinished"}},
                    }
                }
            ),
        )
        _flush(
            ledger,
            {
                "estimate": 9000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("push", 1),
            },
        )
        figures = _figures(ledger, _live("push#1"))
        assert "context_used" not in figures
        assert "context_threshold" not in figures
        assert "native.context_hard_usable_threshold_tokens" not in figures
        assert figures["context_peak"]["value"] == 9000
    finally:
        ledger.close()


def test_finalisation_omits_pair_and_keeps_peak_and_pickups(
    tmp_path: Path,
) -> None:
    """After the Terminal Outcome the pair omits; peak and pickups stay."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps("publish"),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        _flush(
            ledger,
            {
                "estimate": 11000,
                "estimate_basis": "anchored",
                "conversation": work_conversation("publish", 1),
            },
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_section(_offer()),
        )
        _record_outcome(ledger, body)
        figures = _figures(ledger, _live("publish#1"))
        assert "context_used" not in figures
        assert "context_threshold" not in figures
        assert figures["context_peak"]["value"] == 11000
        assert figures["native.pickups_used"]["value"] == 0
        assert figures["native.pickup_limit"]["value"] == PICKUP_LIMIT
    finally:
        ledger.close()


# --- Peak, origins, pickups ----------------------------------------------------


def test_overflow_refusal_survives_in_peak(tmp_path: Path) -> None:
    """A refused Context Overflow still counts toward the Attempt-wide peak."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 8000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
        )
        ledger.record(
            ATTEMPT_ID,
            "context_overflow",
            _snapshot(),
            payload=_threshold_payload(45000, "anchored", PIN_USABLE),
        )
        figures = _figures(ledger, _live("process#1"))
        assert figures["context_peak"]["value"] == 45000
    finally:
        ledger.close()


def test_origins_are_main_and_subagent_only(tmp_path: Path) -> None:
    """Spend projects under ``main`` once with Sub-agent spend separate."""
    from coding_agent.runtime.ceilings import ORIGINS

    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        cells = {origin: OriginCell() for origin in ORIGINS}
        cells["main"] = OriginCell(
            input_tokens=100,
            output_tokens=50,
            effective_tokens=150,
            tool_calls=3,
            responses=2,
        )
        cells["subagent"] = OriginCell(
            input_tokens=40,
            output_tokens=10,
            effective_tokens=50,
            tool_calls=1,
            responses=1,
        )
        _flush(
            ledger,
            {
                "estimate": 7000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
            by_origin=cells,
        )
        result = _status(ledger, _live("process#1"))
        by_origin = result["current_attempt"]["budget"]["by_origin"]
        assert set(by_origin) == {"main", "subagent"}
        assert "handoff" not in by_origin
        main = by_origin["main"]
        assert main["input_tokens"]["value"] == 100
        assert main["effective_tokens"]["value"] == 150
        assert main["cost_usd"]["basis"] in ("estimated", "unavailable")
        subagent = by_origin["subagent"]
        assert subagent["input_tokens"]["value"] == 40
        figures = result["current_attempt"]["figures"]
        assert "continuations" not in figures
        assert "native.handoff_threshold_tokens" not in figures
    finally:
        ledger.close()


def test_concurrent_subagents_count_running_and_ended(tmp_path: Path) -> None:
    """Two started members with one ending read running one, ended one."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 6000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
        )
        ledger.record(ATTEMPT_ID, "subagent_started", _snapshot(), payload={})
        ledger.record(ATTEMPT_ID, "subagent_started", _snapshot(), payload={})
        ledger.record(
            ATTEMPT_ID,
            "subagent_ended",
            _snapshot(),
            payload={"usage": {"input_tokens": 10}},
        )
        figures = _figures(ledger, _live("process#1"))
        assert figures["subagents_running"]["value"] == 1
        assert figures["subagents_ended"]["value"] == 1
        assert figures["subagents_ended"]["basis"] == "measured"
    finally:
        ledger.close()


def test_pickup_limit_configured_only_when_opted_in(tmp_path: Path) -> None:
    """The pickup limit is configured where Round Handoff is enabled."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                }
            ),
        )
        _flush(
            ledger,
            {
                "estimate": 6000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
        )
        figures = _figures(ledger, _live("process#1"))
        assert figures["native.pickup_limit"] == {
            "value": PICKUP_LIMIT,
            "unit": "count",
            "basis": "configured",
            "reason": "the human-authorised pickup limit for the series",
        }
        assert "native.pickups_used" not in figures
    finally:
        ledger.close()


def test_pickup_limit_absent_without_enablement(tmp_path: Path) -> None:
    """Without Round Handoff enablement no pickup limit figure reports."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        _flush(
            ledger,
            {
                "estimate": 6000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
        )
        figures = _figures(ledger, _live("process#1"))
        assert "native.pickup_limit" not in figures
        assert "native.pickups_used" not in figures
    finally:
        ledger.close()


def test_pickups_used_measured_from_offer(tmp_path: Path) -> None:
    """A recorded offer's consumed count reports measured pickups used."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                }
            ),
        )
        _flush(
            ledger,
            {
                "estimate": 6000,
                "estimate_basis": "estimated",
                "conversation": work_conversation("process", 1),
            },
        )
        body = _terminal_body(
            handoff_section=render_handoff_section(_offer(used=1))
        )
        _record_outcome(ledger, body)
        with RunLedgerReader(ledger.path) as reader:
            from coding_agent.operator.snapshots import _pickup_figures

            record = reader.get_attempt(ATTEMPT_ID)
            assert record is not None
            detail = {"effective_configuration": record.effective_configuration}
            pickup = _pickup_figures(reader, ATTEMPT_ID, detail)
        assert pickup["native.pickups_used"] == {
            "value": 1,
            "unit": "count",
            "basis": "measured",
            "reason": "pickups the series consumed before this round",
        }
    finally:
        ledger.close()


# --- Native events --------------------------------------------------------------


def test_crossing_event_reports_compared_value(tmp_path: Path) -> None:
    """An actual runtime crossing projects with its value, basis and threshold."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps("plan", thresholds={"hard_tokens": HARD_TOKENS})
            ),
        )
        ledger.record(
            ATTEMPT_ID,
            "context_threshold",
            _snapshot(),
            payload=_threshold_payload(49000, "anchored", PIN_USABLE, "plan", 1),
        )
        events = _native(ledger)
        crossed = [
            e for e in events if e["event"] == "native.context_threshold_crossed"
        ]
        assert len(crossed) == 1
        assert "49000" in crossed[0]["text"]
        assert "anchored on provider-reported input" in crossed[0]["text"]
        assert str(PIN_USABLE) in crossed[0]["text"]
        assert crossed[0]["attempt_id"] == ATTEMPT_ID
    finally:
        ledger.close()


def test_soft_and_hard_crossings_report_each_threshold(tmp_path: Path) -> None:
    """Soft and hard crossings each project with their own threshold."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(ledger, _effective_config())
        ledger.record(
            ATTEMPT_ID,
            "soft_context_threshold",
            _snapshot(),
            payload=_threshold_payload(29000, "estimated", SOFT_USABLE, "plan", 1),
        )
        ledger.record(
            ATTEMPT_ID,
            "hard_context_threshold",
            _snapshot(),
            payload=_threshold_payload(39000, "anchored", HARD_USABLE, "plan", 1),
        )
        events = _native(ledger)
        crossed = [
            e for e in events if e["event"] == "native.context_threshold_crossed"
        ]
        assert len(crossed) == 2
        assert "soft context threshold" in crossed[0]["text"]
        assert "hard context threshold" in crossed[1]["text"]
    finally:
        ledger.close()


def test_hard_entry_projects_before_terminal(tmp_path: Path) -> None:
    """A hard crossing while the Attempt runs projects Hard Handoff entry."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps("plan", thresholds={"hard_tokens": HARD_TOKENS})
            ),
        )
        ledger.record(
            ATTEMPT_ID,
            "hard_context_threshold",
            _snapshot(),
            payload=_threshold_payload(39000, "anchored", HARD_USABLE, "plan", 1),
        )
        events = _native(ledger)
        kinds = [e["event"] for e in events]
        assert "native.hard_handoff_entered" in kinds
    finally:
        ledger.close()


def test_ai_handoff_result_projects_offer(tmp_path: Path) -> None:
    """An AI-assisted Handoff round with an offer projects its result."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_section(_offer(note_kind="ai-assisted")),
        )
        _record_outcome(ledger, body)
        events = _native(ledger)
        kinds = [e["event"] for e in events]
        assert "native.ai_handoff_result" in kinds
        assert "native.ai_handoff_refused" not in kinds
        result = next(e for e in events if e["event"] == "native.ai_handoff_result")
        assert PICKUP_LABEL in result["text"]
    finally:
        ledger.close()


def test_failed_push_projects_refusal(tmp_path: Path) -> None:
    """Note success followed by publication failure projects the refusal."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        diagnostic = HandoffDiagnostic(
            reason="failed-push", detail="the push never landed"
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_diagnostic_section(diagnostic),
        )
        _record_outcome(ledger, body)
        events = _native(ledger)
        refused = [e for e in events if e["event"] == "native.ai_handoff_refused"]
        assert len(refused) == 1
        assert refused[0]["reason"] == "failed-push"
        assert "the push never landed" in refused[0]["text"]
        assert "native.ai_handoff_result" not in [e["event"] for e in events]
    finally:
        ledger.close()


def test_ordinary_result_with_reserved_spelling_emits_no_crossing(
    tmp_path: Path,
) -> None:
    """An ordinary business result using a reserved spelling emits no crossing."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "soft-context-threshold": {"end": "completed"},
                    },
                ),
            ),
        )
        body = _terminal_body(
            outcome="completed",
            ending_step="publish",
            step_result="soft-context-threshold",
            workflow_end="completed",
        )
        _record_outcome(ledger, body)
        events = _native(ledger)
        assert "native.context_threshold_crossed" not in [
            e["event"] for e in events
        ]
    finally:
        ledger.close()


def test_readiness_only_after_three_receipts(tmp_path: Path) -> None:
    """Readiness reports only with comment, label and release all confirmed."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_section(_offer()),
        )
        _record_outcome(ledger, body)
        assert "native.round_handoff_ready" not in [e["event"] for e in _native(ledger)]
        _record_receipt(ledger, "terminal", 1)
        assert "native.round_handoff_ready" not in [e["event"] for e in _native(ledger)]
        _record_receipt(ledger, "remove-selection-label", 2)
        assert "native.round_handoff_ready" not in [e["event"] for e in _native(ledger)]
        _record_receipt(ledger, "release-assignment", 3)
        ready = [e for e in _native(ledger) if e["event"] == "native.round_handoff_ready"]
        assert len(ready) == 1
        assert PICKUP_LABEL in ready[0]["text"]
    finally:
        ledger.close()


def test_readiness_survives_outcome_replay(tmp_path: Path) -> None:
    """A re-recorded Terminal Outcome (replay) keeps verified readiness."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_section(_offer()),
        )
        _record_outcome(ledger, body)
        _record_all_receipts(ledger)
        _record_outcome(ledger, body + "tampered-replay")
        ready = [e for e in _native(ledger) if e["event"] == "native.round_handoff_ready"]
        assert len(ready) == 1
    finally:
        ledger.close()


def test_exhausted_offer_projects_exhaustion(tmp_path: Path) -> None:
    """An exhausted series projects exhaustion with the reauthorisation note."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_block(
                _offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached")
            ),
        )
        _record_outcome(ledger, body)
        events = _native(ledger)
        exhausted = [e for e in events if e["event"] == "native.pickup_exhausted"]
        assert len(exhausted) == 1
        assert "a human must reauthorise a new series" in exhausted[0]["text"]
        assert "native.ai_handoff_result" not in [e["event"] for e in events]
        assert "native.round_handoff_ready" not in [e["event"] for e in events]
    finally:
        ledger.close()


def test_exhausted_hard_offer_enters_hard_handoff(tmp_path: Path) -> None:
    """An exhausted hard round still records Hard Handoff entry."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "hard"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_block(
                _offer(
                    note_kind="hard",
                    used=2,
                    exhausted=True,
                    exhaustion_reason="pickup-limit-reached",
                )
            ),
        )
        _record_outcome(ledger, body)
        kinds = [e["event"] for e in _native(ledger)]
        assert "native.hard_handoff_entered" in kinds
        assert "native.pickup_exhausted" in kinds
    finally:
        ledger.close()


# --- Live and archived status ---------------------------------------------------


def test_live_status_carries_derived_native_events(tmp_path: Path) -> None:
    """The live snapshot appends the active Attempt's derived native events."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        ledger.record(
            ATTEMPT_ID,
            "context_threshold",
            _snapshot(),
            payload=_threshold_payload(49000, "anchored", PIN_USABLE),
        )
        result = _status(ledger, _live("publish#1"))
        kinds = [e["event"] for e in result["recent_events"]]
        assert "native.context_threshold_crossed" in kinds
        assert len(result["recent_events"]) <= 50
    finally:
        ledger.close()


def test_idle_status_shows_exhaustion_from_last_finished(
    tmp_path: Path,
) -> None:
    """Idle Intake keeps the durable exhaustion diagnostic from last finished."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_block(
                _offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached")
            ),
        )
        _record_outcome(ledger, body)
        ledger.set_attempt_state(ATTEMPT_ID, "finished")
        result = _status(ledger, _idle_live())
        assert result["current_attempt"] is None
        assert result["instance"]["state"] == "idle"
        kinds = [e["event"] for e in result["recent_events"]]
        assert "native.pickup_exhausted" in kinds
        exhausted = next(
            e for e in result["recent_events"] if e["event"] == "native.pickup_exhausted"
        )
        assert "a human must reauthorise a new series" in exhausted["text"]
    finally:
        ledger.close()


def test_exhaustion_visible_after_restart_and_independent_worker(
    tmp_path: Path,
) -> None:
    """The diagnostic holds on a fresh process reading the same ledger."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_handoff_block(
                _offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached")
            ),
        )
        _record_outcome(ledger, body)
        ledger.set_attempt_state(ATTEMPT_ID, "finished")
    finally:
        ledger.close()
    live = _fresh_live()
    with RunLedgerReader(tmp_path / "run-ledger.sqlite3") as reader:
        events = native_events_for_attempt(
            reader,
            ATTEMPT_ID,
            process_started_at=live.process_started_at_iso(),
            observed_at="2026-11-10T08:00:00Z",
        )
    assert "native.pickup_exhausted" in [e["event"] for e in events]
    ledger2 = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        result = _status(ledger2, _idle_live())
        assert "native.pickup_exhausted" in [
            e["event"] for e in result["recent_events"]
        ]
    finally:
        ledger2.close()


def test_archive_accepts_v121_native_event_types() -> None:
    """The Attempt Archive producer allows every v1.2.1 native event type."""
    for name in (
        "native.context_threshold_crossed",
        "native.ai_handoff_result",
        "native.ai_handoff_refused",
        "native.hard_handoff_entered",
        "native.round_handoff_ready",
        "native.pickup_exhausted",
    ):
        assert name in ATTEMPT_EVENT_TYPES


def test_native_event_texts_stay_capped(tmp_path: Path) -> None:
    """A long diagnostic detail still projects a 300-char capped event."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _open(
            ledger,
            _effective_config(
                steps=_agent_steps(
                    "publish",
                    routes={
                        "pushed": {
                            "end": "unfinished",
                            "round_handoff": {"kind": "ai-assisted"},
                        }
                    },
                ),
                round_handoff={
                    "pickup_label": PICKUP_LABEL,
                    "max_automatic_pickups": PICKUP_LIMIT,
                },
            ),
        )
        diagnostic = HandoffDiagnostic(reason="failed-push", detail="x" * 900)
        body = _terminal_body(
            ending_step="publish",
            step_result="pushed",
            workflow_end="unfinished",
            handoff_section=render_diagnostic_section(diagnostic),
        )
        _record_outcome(ledger, body)
        refused = [e for e in _native(ledger) if e["event"] == "native.ai_handoff_refused"]
        assert len(refused) == 1
        assert len(refused[0]["text"]) <= 300
    finally:
        ledger.close()
