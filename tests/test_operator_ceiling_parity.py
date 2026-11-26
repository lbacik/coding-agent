"""The status snapshot and the step projection read one Attempt Ceiling.

Contract: ADR 0017 — recovery and reporting accept records written by
older releases. ``ceiling_from_effective_configuration`` is the single
reader of the pinned Attempt Ceiling; the status snapshot builds its
configured-limit figures from it, so both surfaces agree on current and
legacy Effective Configuration shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.operator.step_projection import (
    ceiling_from_effective_configuration,
)
from coding_agent.runtime.ceilings import UsageTotals


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _live() -> LiveView:
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _claim(live: LiveView, attempt_id: str = "#40/1") -> LiveView:
    live.set_claim(attempt_id, 40, "Add a greeting")
    live.set_phase("run_step")
    return live


def _flush_usage(ledger: RunLedger, attempt_id: str) -> None:
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
                cache_write_tokens=0,
                cost_input_usd=0.01,
                cost_output_usd=0.005,
                cost_cache_read_usd=0.001,
                cost_cache_write_usd=0.0,
            ),
        ),
    )


def _snapshot_limits(
    tmp_path: Path, effective_configuration: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """The snapshot ``budget.limits`` for one pinned configuration."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration=effective_configuration
        )
        _flush_usage(ledger, "#40/1")
        result = operator_listener.build_status_result(
            ledger.path, _claim(_live())
        )
        current = result["current_attempt"]
        assert current is not None
        entries = current["budget"]["limits"]
        assert isinstance(entries, list)
        return {entry["name"]: entry for entry in entries}
    finally:
        ledger.close()


def test_legacy_limits_shape_matches_shared_reader(tmp_path: Path) -> None:
    """A pre-v4 record (``limits`` + ``tokens``) agrees on all four ceilings."""
    effective: dict[str, Any] = {
        "workflow": {
            "limits": {
                "tokens": 500000,
                "cost_usd": 2.0,
                "wall_clock_minutes": 30,
                "tool_calls": 200,
            }
        }
    }
    expected = ceiling_from_effective_configuration(effective)
    assert expected["effective_tokens"] == 500000

    limits = _snapshot_limits(tmp_path, effective)

    assert set(limits) == {
        "effective_tokens",
        "cost_usd",
        "wall_clock_seconds",
        "tool_calls",
    }
    assert limits["effective_tokens"]["limit"] == {
        "value": 500000,
        "unit": "tokens",
        "basis": "configured",
    }
    assert limits["effective_tokens"]["limit"]["value"] == (
        expected["effective_tokens"]
    )
    assert limits["cost_usd"]["limit"]["value"] == expected["cost_usd"]
    assert limits["cost_usd"]["limit"]["basis"] == "configured"
    assert limits["wall_clock_seconds"]["limit"]["value"] == (
        expected["wall_clock_seconds"]
    )
    assert limits["wall_clock_seconds"]["limit"]["basis"] == "configured"
    assert limits["tool_calls"]["limit"]["value"] == expected["tool_calls"]
    assert limits["tool_calls"]["limit"]["basis"] == "configured"


def test_non_numeric_and_bool_meters_agree_on_unavailable(
    tmp_path: Path,
) -> None:
    """Junk meters read as ``None`` in the shared reader, unavailable here."""
    effective: dict[str, Any] = {
        "workflow": {
            "ceiling": {
                "effective_tokens": "lots",
                "cost_usd": True,
                "wall_clock_minutes": "soon",
                "tool_calls": False,
            }
        }
    }
    expected = ceiling_from_effective_configuration(effective)
    assert expected == {
        "cost_usd": None,
        "effective_tokens": None,
        "tool_calls": None,
        "wall_clock_seconds": None,
    }

    limits = _snapshot_limits(tmp_path, effective)

    for name in (
        "effective_tokens",
        "cost_usd",
        "wall_clock_seconds",
        "tool_calls",
    ):
        assert limits[name]["limit"]["basis"] == "unavailable"
        assert limits[name]["limit"]["value"] is None
