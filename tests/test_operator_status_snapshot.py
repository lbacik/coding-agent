"""Operator ``status`` Attempt snapshot: current Attempt and last finished (#253).

Contract: adoption step 4 of the operator contract (#245). While an Attempt
runs, ``status`` shows the native Attempt id, the issue, the current graph
node and a fresh activity time; budget figures match the ledger's usage
record with ``enforced_tokens: "effective"`` and an ``estimated`` cost
basis; ``limits`` lists exactly the four pinned ceilings; unknown figures
are ``unavailable``, never zero; ``last_finished`` shows the outcome and
the delivered pull request with the ``attention_required`` rules; and the
snapshot never exceeds 256 KiB, trimming in contract order.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.runtime.ceilings import UsageTotals

SNAPSHOT_LIMIT_BYTES = 256 * 1024


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _live() -> LiveView:
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "limits": {
                "wall_clock_minutes": 60,
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
                "max_parallel_subagents": 4,
            }
        }
    }


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "run-ledger.sqlite3")


def _open(ledger: RunLedger, attempt_id: str) -> None:
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())


def _flush_usage(
    ledger: RunLedger,
    attempt_id: str,
    *,
    elapsed_seconds: float = 90.0,
    usage: UsageTotals | None = None,
) -> None:
    ledger.record_usage(
        attempt_id,
        BudgetSnapshot(
            elapsed_seconds=elapsed_seconds,
            usage=usage
            if usage is not None
            else UsageTotals(
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


def _claim(live: LiveView, attempt_id: str = "#40/1") -> LiveView:
    live.set_claim(attempt_id, 40, "Add a greeting")
    live.set_phase("run_step")
    return live


def _status(ledger_path: Path, live: LiveView) -> dict[str, Any]:
    return operator_listener.build_status_result(ledger_path, live)


def test_current_attempt_shows_native_id_phase_and_fresh_activity(
    tmp_path: Path,
) -> None:
    """The running Attempt: native id, issue, graph node, fresh activity."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush_usage(ledger, "#40/1")
        live = _claim(_live())
        before = datetime.now(timezone.utc)
        result = _status(ledger.path, live)
        after = datetime.now(timezone.utc)
        current = result["current_attempt"]
        assert current is not None
        assert current["attempt_id"] == "#40/1"
        assert current["issue"] == {"number": 40, "title": "Add a greeting"}
        assert current["native_phase"] == "run_step"
        from coding_agent.operator.contract import to_z

        assert current["started_at"] == to_z(
            ledger.attempt_opened_at("#40/1")
        )
        assert str(current["started_at"]).endswith("Z")
        activity = datetime.fromisoformat(
            str(current["last_activity_at"]).replace("Z", "+00:00")
        )
        assert before - activity < timedelta(seconds=120)
        assert activity <= after
        assert str(current["observed_at"]).endswith("Z")
    finally:
        ledger.close()


def test_budget_matches_ledger_usage_record(tmp_path: Path) -> None:
    """Budget figures match the usage flush; cost basis is estimated."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush_usage(ledger, "#40/1")
        result = _status(ledger.path, _claim(_live()))
        current = result["current_attempt"]
        assert current is not None
        assert current["budget"]["raw_tokens"] == {
            "value": 1200,
            "unit": "tokens",
            "basis": "measured",
        }
        assert current["budget"]["effective_tokens"] == {
            "value": 1000,
            "unit": "tokens",
            "basis": "measured",
        }
        assert current["budget"]["enforced_tokens"] == "effective"
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
        assert limits["cost_usd"]["used"]["value"] == pytest.approx(0.02)
        figures = current["figures"]
        assert figures["tool_calls"]["value"] == 7
        assert figures["elapsed_seconds"]["value"] == 90.0
        assert figures["cache_hit_rate"]["value"] == pytest.approx(200 / 800)
        assert figures["continuations"]["value"] == 0
        assert figures["review_seats_running"]["value"] == 0
        assert figures["turns"]["basis"] == "not_applicable"
        assert figures["context_used"]["basis"] == "not_applicable"
    finally:
        ledger.close()


def test_limits_lists_exactly_the_four_pinned_ceilings(tmp_path: Path) -> None:
    """``limits`` carries exactly the pinned ceilings with used/configured."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush_usage(ledger, "#40/1")
        result = _status(ledger.path, _claim(_live()))
        current = result["current_attempt"]
        assert current is not None
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        assert set(limits) == {
            "effective_tokens",
            "cost_usd",
            "wall_clock_seconds",
            "tool_calls",
        }
        assert limits["effective_tokens"]["used"]["value"] == 1000
        assert limits["effective_tokens"]["used"]["basis"] == "measured"
        assert limits["effective_tokens"]["limit"] == {
            "value": 100_000,
            "unit": "tokens",
            "basis": "configured",
        }
        assert limits["cost_usd"]["used"]["value"] == pytest.approx(0.02)
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
        assert limits["cost_usd"]["limit"]["value"] == 5.0
        assert limits["wall_clock_seconds"]["used"]["value"] == 90.0
        assert limits["wall_clock_seconds"]["limit"]["value"] == 3600.0
        assert limits["tool_calls"]["used"]["value"] == 7
        assert limits["tool_calls"]["limit"]["value"] == 200
    finally:
        ledger.close()


def test_unknown_figures_are_unavailable_never_zero(tmp_path: Path) -> None:
    """Before the first usage flush, unknown figures are ``unavailable``."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        result = _status(ledger.path, _claim(_live()))
        current = result["current_attempt"]
        assert current is not None
        assert current["budget"]["raw_tokens"]["basis"] == "unavailable"
        assert current["budget"]["raw_tokens"]["value"] is None
        assert current["budget"]["effective_tokens"]["basis"] == "unavailable"
        assert current["budget"]["effective_tokens"]["value"] is None
        assert current["budget"]["enforced_tokens"] == "effective"
        figures = current["figures"]
        assert figures["tool_calls"]["basis"] == "unavailable"
        assert figures["tool_calls"]["value"] is None
        assert figures["elapsed_seconds"]["basis"] == "unavailable"
        assert figures["elapsed_seconds"]["value"] is None
        assert figures["cache_hit_rate"]["basis"] == "unavailable"
        assert figures["cache_hit_rate"]["value"] is None
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        for name in (
            "effective_tokens",
            "cost_usd",
            "wall_clock_seconds",
            "tool_calls",
        ):
            assert limits[name]["used"]["basis"] == "unavailable"
            assert limits[name]["used"]["value"] is None
        assert current["budget"]["raw_tokens"]["value"] is None
    finally:
        ledger.close()


def test_no_current_attempt_without_claim(tmp_path: Path) -> None:
    """With no Claim held, ``current_attempt`` is ``None``."""
    ledger = _ledger(tmp_path)
    try:
        result = _status(ledger.path, _live())
        assert result["current_attempt"] is None
        assert result["last_finished"] is None
        assert result["truncated"] is False
    finally:
        ledger.close()


def _is_step_attempt(attempt_id: str) -> bool:
    """Whether ``attempt_id`` carries a Workflow (step) or is a Legacy id."""
    from coding_agent.attempt_id import AttemptId

    attempt = AttemptId.try_parse(attempt_id)
    return attempt is not None and attempt.workflow is not None


def _finish(
    ledger: RunLedger,
    attempt_id: str,
    *,
    outcome: str | None,
    reason: str = "",
    state: str = "finished",
    pull_request: dict[str, Any] | None = None,
    pending_effect: bool = False,
) -> None:
    _open(ledger, attempt_id)
    if _is_step_attempt(attempt_id):
        # Step Attempts wire delivery through the step projection: an
        # ``open-pr`` execution whose ``opened`` result pairs with the
        # ``open-pr`` receipt (number from the remote id, URL from the
        # execution facts). Pending work is an unsettled step intent.
        if pull_request is not None:
            number = pull_request["pull_request_number"]
            url = pull_request["pull_request_url"]
            ledger.open_step_execution(
                attempt_id,
                step_id="open-pr",
                seq=1,
                entry_from_step="work",
                entry_result="done",
            )
            ledger.record_step_effect_intent(
                attempt_id,
                "open-pr",
                1,
                0,
                rendered_payload="open pull request",
                expected_target={},
            )
            ledger.end_step_execution(
                attempt_id,
                step_id="open-pr",
                seq=1,
                result="opened",
                facts={"pull_url": url, "pull_number": number},
            )
            ledger.record_step_effect_receipt(
                attempt_id, "open-pr", 1, 0, remote_id=str(number)
            )
            ledger.record_workflow_end(
                attempt_id, from_step="open-pr", result="opened", end="completed"
            )
        if pending_effect:
            ledger.record_step_effect_intent(
                attempt_id,
                "work",
                1,
                0,
                rendered_payload="push",
                expected_target={},
            )
        if outcome is not None:
            ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason=reason)
    else:
        # Legacy Attempts record only outcome and lifecycle state. The
        # runtime no longer reads or writes the legacy ``effects`` table
        # (Decision A), so ``pull_request`` and ``pending_effect`` are
        # ignored here; history rows are simulated by hand with
        # :func:`_insert_legacy_effect_row`.
        if outcome is not None:
            ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason=reason)
    if state in ("finished", "released"):
        ledger.set_attempt_state(
            attempt_id, state  # type: ignore[arg-type]
        )


def _insert_legacy_effect_row(
    ledger: RunLedger,
    attempt_id: str,
    write_kind: str,
    status: str,
    *,
    remote_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Simulate MVP-history rows in the retired ``effects`` table.

    The runtime no longer creates or touches ``effects``; tests recreate
    the table by hand and insert rows to prove the snapshot ignores them.
    """
    connection = sqlite3.connect(str(ledger.path))
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS effects (
                attempt_id TEXT NOT NULL,
                write_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                remote_id TEXT,
                payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (attempt_id, write_kind)
            )
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO effects
                (attempt_id, write_kind, status, remote_id, payload,
                 payload_digest, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                write_kind,
                status,
                remote_id,
                json.dumps(payload or {}, sort_keys=True),
                "test-digest",
                "2026-11-06T08:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_last_finished_delivered_with_pull_request(tmp_path: Path) -> None:
    """A completed step Attempt shows its outcome and pull request, no attention."""
    ledger = _ledger(tmp_path)
    try:
        _finish(
            ledger,
            "implement#40/1",
            outcome="completed",
            reason="verified-completion",
            pull_request={
                "pull_request_number": 12,
                "pull_request_url": "https://example.invalid/pr/12",
            },
        )
        result = _status(ledger.path, _live())
        assert result["current_attempt"] is None
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "implement#40/1"
        assert last["issue"] == {"number": 40, "title": None}
        assert last["lifecycle_state"] == "finished"
        assert last["outcome"] == {
            "native": "completed",
            "reason": "verified-completion",
            "label": "completed (verified-completion)",
        }
        assert last["delivery"] == {
            "pull_request": {
                "number": 12,
                "url": "https://example.invalid/pr/12",
            }
        }
        assert last["attention_required"] is False
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("outcome", "reason", "state", "pending_effect", "expected"),
    [
        ("failed", "red-baseline", "finished", False, True),
        ("completed", "verified-completion", "finished", False, False),
        ("completed", "verified-completion", "finished", True, True),
        (None, "", "released", False, False),
        ("completed", "verified-completion", "released", True, False),
        ("failed", "interrupted", "outcome-recorded", True, True),
        ("completed", "verified-completion", "outcome-recorded", True, True),
        ("completed", "verified-completion", "outcome-recorded", False, False),
    ],
)
def test_attention_required_rules(
    tmp_path: Path,
    outcome: str | None,
    reason: str,
    state: str,
    pending_effect: bool,
    expected: bool,
) -> None:
    """``attention_required`` follows the step outcome and pending-effect rules."""
    ledger = _ledger(tmp_path)
    try:
        _finish(
            ledger,
            "implement#40/2",
            outcome=outcome,
            reason=reason,
            state=state,
            pending_effect=pending_effect,
        )
        result = _status(ledger.path, _live())
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "implement#40/2"
        assert last["lifecycle_state"] == state
        assert last["attention_required"] is expected
    finally:
        ledger.close()


def test_finished_legacy_attempt_reports_null_delivery_and_no_attention(
    tmp_path: Path,
) -> None:
    """Decision A: legacy ``effects`` rows no longer shape the snapshot.

    A finished Legacy Attempt keeps its outcome and lifecycle state, but
    reports no pull request and no effect-based attention even where the
    retired ``effects`` table still holds a receipt and a pending intent
    for it. The sealed archives still hold the evidence.
    """
    ledger = _ledger(tmp_path)
    try:
        _finish(
            ledger,
            "#40/1",
            outcome="delivered",
            reason="verified-completion",
            state="finished",
        )
        _insert_legacy_effect_row(
            ledger,
            "#40/1",
            "open-pull-request",
            "receipt",
            remote_id="12",
            payload={
                "pull_request_number": 12,
                "pull_request_url": "https://example.invalid/pr/12",
            },
        )
        _insert_legacy_effect_row(ledger, "#40/1", "terminal-comment", "intent")
        result = _status(ledger.path, _live())
        assert result["current_attempt"] is None
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "#40/1"
        assert last["lifecycle_state"] == "finished"
        assert last["outcome"] == {
            "native": "delivered",
            "reason": "verified-completion",
            "label": "delivered (verified-completion)",
        }
        assert last["delivery"] == {"pull_request": None}
        assert last["attention_required"] is False
    finally:
        ledger.close()


def test_step_attempt_without_projection_delivery_reports_null(
    tmp_path: Path,
) -> None:
    """A step Attempt without an ``open-pr`` receipt reports null delivery.

    Even where the retired ``effects`` table still holds a receipt row for
    the Attempt id, delivery comes only from the step projection.
    """
    ledger = _ledger(tmp_path)
    try:
        _finish(
            ledger,
            "implement#40/3",
            outcome="completed",
            reason="verified-completion",
            state="finished",
        )
        _insert_legacy_effect_row(
            ledger,
            "implement#40/3",
            "open-pull-request",
            "receipt",
            remote_id="12",
            payload={
                "pull_request_number": 12,
                "pull_request_url": "https://example.invalid/pr/12",
            },
        )
        result = _status(ledger.path, _live())
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "implement#40/3"
        assert last["outcome"] == {
            "native": "completed",
            "reason": "verified-completion",
            "label": "completed (verified-completion)",
        }
        assert last["delivery"] == {"pull_request": None}
        assert last["attention_required"] is False
    finally:
        ledger.close()


def test_fresh_ledger_has_no_effects_table(tmp_path: Path) -> None:
    """New ledgers stop creating the retired ``effects`` table."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        connection = sqlite3.connect(str(ledger.path))
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()
        assert "effects" not in tables
        assert "step_effects" in tables
    finally:
        ledger.close()


def test_ledger_with_legacy_effects_table_opens_untouched(tmp_path: Path) -> None:
    """A ledger file that already has ``effects`` opens fine and keeps it."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
    finally:
        ledger.close()
    seed = sqlite3.connect(str(tmp_path / "run-ledger.sqlite3"))
    try:
        seed.execute(
            """
            CREATE TABLE effects (
                attempt_id TEXT NOT NULL,
                write_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                remote_id TEXT,
                payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (attempt_id, write_kind)
            )
            """
        )
        seed.execute(
            """
            INSERT INTO effects
                (attempt_id, write_kind, status, remote_id, payload,
                 payload_digest, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "#40/1",
                "open-pull-request",
                "receipt",
                "12",
                "{}",
                "test-digest",
                "2026-11-06T08:00:00+00:00",
            ),
        )
        seed.commit()
    finally:
        seed.close()
    reopened = _ledger(tmp_path)
    try:
        _open(reopened, "#40/2")
        check = sqlite3.connect(str(tmp_path / "run-ledger.sqlite3"))
        try:
            rows = check.execute(
                "SELECT attempt_id, write_kind, status FROM effects"
            ).fetchall()
        finally:
            check.close()
        assert rows == [("#40/1", "open-pull-request", "receipt")]
    finally:
        reopened.close()


def test_last_finished_prefers_latest_attempt(tmp_path: Path) -> None:
    """The projection follows the latest finished Attempt."""
    ledger = _ledger(tmp_path)
    try:
        _finish(ledger, "#40/1", outcome="delivered", reason="verified-completion")
        _finish(ledger, "#40/2", outcome="failed", reason="red-baseline")
        result = _status(ledger.path, _live())
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "#40/2"
        assert last["attention_required"] is True
    finally:
        ledger.close()


def test_snapshot_trims_over_limit_in_contract_order() -> None:
    """An oversized snapshot trims events before commands and marks truncated."""
    live = _live()
    big = "x" * 10_000
    result: dict[str, Any] = {
        "generated_at": "2026-11-06T08:00:00+00:00",
        "process_started_at": live.process_started_at_iso(),
        "startup_check": None,
        "instance": {"state": "idle", "observed_at": "t", "last_halt": None},
        "intake": {"state": "open", "pending_stop": None, "observed_at": "t"},
        "current_attempt": None,
        "last_finished": None,
        "recent_commands": [{"request_id": f"r-{i}", "detail": big} for i in range(5)],
        "recent_events": [{"text": big, "index": i} for i in range(30)],
    }
    assert len(json.dumps(result).encode("utf-8")) > SNAPSHOT_LIMIT_BYTES
    trimmed = operator_listener._trim_status_snapshot(result)
    assert len(json.dumps(trimmed).encode("utf-8")) <= SNAPSHOT_LIMIT_BYTES
    assert trimmed["truncated"] is True
    # Contract order: events trim before commands are touched.
    assert trimmed["recent_commands"] == result["recent_commands"]
    assert len(trimmed["recent_commands"]) == 5
    assert len(trimmed["recent_events"]) < 30


def test_small_snapshot_is_not_truncated(tmp_path: Path) -> None:
    """An ordinary snapshot fits and reports ``truncated: false``."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush_usage(ledger, "#40/1")
        result = _status(ledger.path, _claim(_live()))
        assert len(json.dumps(result).encode("utf-8")) <= SNAPSHOT_LIMIT_BYTES
        assert result["truncated"] is False
    finally:
        ledger.close()


def test_live_counters_reach_current_attempt(tmp_path: Path) -> None:
    """Handoffs and review seats flow from the live view into the snapshot."""
    ledger = _ledger(tmp_path)
    try:
        _open(ledger, "#40/1")
        _flush_usage(ledger, "#40/1")
        live = _claim(_live())
        live.note_continuation()
        live.set_review_seats_running(2)
        result = _status(ledger.path, live)
        current = result["current_attempt"]
        assert current is not None
        assert current["figures"]["continuations"] == {
            "value": 1,
            "unit": "count",
            "basis": "measured",
        }
        assert current["figures"]["review_seats_running"] == {
            "value": 2,
            "unit": "count",
            "basis": "measured",
        }
    finally:
        ledger.close()


@pytest.fixture
def _blocked_remote(tmp_path: Path) -> Path:
    """A plain bare remote the step Workflow run mirrors offline."""
    import subprocess

    from conftest import commit_validation_profile, init_origin_repo

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    commit_validation_profile(src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def test_status_shows_current_attempt_while_model_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    _blocked_remote: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """While the step Workflow blocks, ``status`` names it fast (S10, OPC-4/5)."""
    from test_operator_listener import (
        _call_agentctl,
        _run_in_background,
        _tracker_with_issue,
        _wait_for_socket,
    )
    from coding_agent.ledger.store import RunLedger as _RunLedger
    from coding_agent.runtime.graph.workflow_graph import WorkflowResult as _WorkflowResult
    from support.scripted_model import ScriptedModel as _ScriptedModel

    _ = skills_dir
    entered = threading.Event()
    release = threading.Event()

    def _blocking_workflow(**kwargs: Any) -> _WorkflowResult:
        attempt_id = str(kwargs["attempt_id"])
        ledger: _RunLedger = kwargs["ledger"]
        entered.set()
        assert release.wait(timeout=30.0), "workflow wait timed out"
        ledger.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        return _WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
        )

    tracker = _tracker_with_issue()
    thread, _ = _run_in_background(
        tmp_path,
        monkeypatch,
        tracker,
        _ScriptedModel([]),
        remote=_blocked_remote,
        max_sweeps=None,
        max_attempts=1,
        workflow_runner=_blocking_workflow,
    )
    try:
        _wait_for_socket()
        assert entered.wait(timeout=30.0), "Attempt never reached the workflow"
        started = time.monotonic()
        code, envelope = _call_agentctl(["--contract", "1", "status"], capsys)
        elapsed = time.monotonic() - started
        assert code == 0
        assert envelope["ok"] is True
        result = envelope["result"]
        assert result["instance"]["state"] == "active"
        assert elapsed < 3.0
        current = result["current_attempt"]
        assert current is not None
        assert current["attempt_id"] == "implement#40/1"
        assert current["issue"] == {"number": 40, "title": "Add a greeting"}
        # The live ``claim`` phase is not native and no Step Execution has
        # opened yet: the generic v1 shape echoes the live phase.
        assert current["native_phase"] == "claim"
        activity = datetime.fromisoformat(
            str(current["last_activity_at"]).replace("Z", "+00:00")
        )
        assert datetime.now(timezone.utc) - activity < timedelta(seconds=120)
        assert current["budget"]["enforced_tokens"] == "effective"
        assert {entry["name"] for entry in current["budget"]["limits"]} == {
            "effective_tokens",
            "cost_usd",
            "wall_clock_seconds",
            "tool_calls",
        }
    finally:
        release.set()
        thread.join(timeout=60.0)
