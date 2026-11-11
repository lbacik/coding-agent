"""Step-workflow ledger primitives: schema version and step recording.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §13.1 (the
ledger carries an explicit schema version; evolution is additive), §13.2
(Step Execution open/end, transitions, admission holds) and §14.1 (Effect
Intent keyed ``(Attempt id, step, Step Execution number, write ordinal)``).

Matrix rows: REC-3 (S2), REC-1 (S2, with S3), REC-4 (S2, with S7b).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from coding_agent.ledger.store import (
    ADMISSION_HOLD_CHARGED,
    ADMISSION_HOLD_HELD,
    ADMISSION_HOLD_SETTLED,
    LEDGER_SCHEMA_VERSION,
    STEP_EFFECT_STATUS_INTENT,
    STEP_EFFECT_STATUS_NOT_PERFORMED,
    STEP_EFFECT_STATUS_RECEIPT,
    STEP_EXECUTION_ENDED,
    STEP_EXECUTION_INTERRUPTED,
    STEP_EXECUTION_OPEN,
    STEP_TRANSITION_END,
    STEP_TRANSITION_REFUSED,
    STEP_TRANSITION_ROUTE,
    RunLedger,
    step_effect_key,
    step_effect_marker,
)


def _open(path: Path) -> RunLedger:
    return RunLedger(path / "run-ledger.sqlite3")


# --- REC-3: explicit schema version, additive evolution -----------------------


def test_fresh_ledger_carries_schema_version_one(tmp_path: Path) -> None:
    """§13.1: a fresh ledger stamps its explicit schema version on open."""
    ledger = _open(tmp_path)
    try:
        assert ledger.schema_version() == LEDGER_SCHEMA_VERSION
        assert LEDGER_SCHEMA_VERSION == 1
    finally:
        ledger.close()


def test_mvp_legacy_ledger_opens_with_schema_stamped_and_data_kept(
    tmp_path: Path,
) -> None:
    """§13.1: a ledger file written by the MVP runtime keeps working.

    The legacy file has the MVP tables in their oldest shape and none of
    the step-workflow tables. Opening stamps the schema version, keeps the
    old Attempt row readable, and makes the new tables usable — the old
    runtime keeps working until S10.
    """
    path = tmp_path / "run-ledger.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                effective_configuration TEXT NOT NULL,
                effective_configuration_digest TEXT NOT NULL,
                fingerprint TEXT,
                base_revision TEXT,
                agent_identity_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO attempts
                (attempt_id, state, effective_configuration,
                 effective_configuration_digest, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "#7/1",
                "open",
                json.dumps({"repo": "example/test"}, sort_keys=True),
                "digest",
                "2026-11-03T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    ledger = RunLedger(path)
    try:
        assert ledger.schema_version() == LEDGER_SCHEMA_VERSION
        assert ledger.get_attempt("#7/1").state == "open"
        opened = ledger.open_step_execution("#7/1", "process", 1)
        assert opened.state == STEP_EXECUTION_OPEN
    finally:
        ledger.close()


# --- REC-1: Step Execution open/end ------------------------------------------


def test_step_execution_opening_is_written_before_the_step_starts(
    tmp_path: Path,
) -> None:
    """§13.2: the opening (step, sequence, entry Route) commits first."""
    ledger = _open(tmp_path)
    try:
        opened = ledger.open_step_execution(
            "#7/1", "process", 1, entry_from_step="plan", entry_result="ok"
        )

        assert opened.attempt_id == "#7/1"
        assert opened.step_id == "process"
        assert opened.seq == 1
        assert opened.state == STEP_EXECUTION_OPEN
        assert opened.entry_from_step == "plan"
        assert opened.entry_result == "ok"
        assert opened.result is None

        stored = ledger.get_step_execution("#7/1", "process", 1)
        assert stored == opened
    finally:
        ledger.close()


def test_step_execution_reopen_returns_the_stored_row(tmp_path: Path) -> None:
    """§13.2: re-entering an open writes nothing new (a retried open)."""
    ledger = _open(tmp_path)
    try:
        first = ledger.open_step_execution("#7/1", "process", 1)
        second = ledger.open_step_execution(
            "#7/1", "process", 1, entry_from_step="other", entry_result="other"
        )

        assert second == first
    finally:
        ledger.close()


def test_step_execution_end_commits_before_the_router_runs(tmp_path: Path) -> None:
    """§13.2: the end (result, payload, facts, usage, limit fact) commits."""
    ledger = _open(tmp_path)
    try:
        ledger.open_step_execution("#7/1", "process", 1)
        ended = ledger.end_step_execution(
            "#7/1",
            "process",
            1,
            result="done",
            payload={"summary": "shipped"},
            facts={"files": ["a.py"]},
            usage={"total_tokens": 9},
            limit_fact={"limit": "tokens"},
        )

        assert ended.state == STEP_EXECUTION_ENDED
        assert ended.result == "done"
        assert ended.payload == {"summary": "shipped"}
        assert ended.facts == {"files": ["a.py"]}
        assert ended.usage == {"total_tokens": 9}
        assert ended.limit_fact == {"limit": "tokens"}

        executions = ledger.list_step_executions("#7/1")
        assert [record.seq for record in executions] == [1]
    finally:
        ledger.close()


def test_step_executions_list_in_open_order(tmp_path: Path) -> None:
    """§13.2: every Step Execution of one Attempt lists in open order."""
    ledger = _open(tmp_path)
    try:
        ledger.open_step_execution("#7/1", "process", 1)
        ledger.open_step_execution("#7/1", "process", 2)
        ledger.open_step_execution("#7/1", "review", 1)

        executions = ledger.list_step_executions("#7/1")

        assert [(record.step_id, record.seq) for record in executions] == [
            ("process", 1),
            ("process", 2),
            ("review", 1),
        ]
    finally:
        ledger.close()


def test_process_loss_closes_open_executions_as_interrupted(
    tmp_path: Path,
) -> None:
    """§13.2, §16.1: recovery closes open rows as interrupted, never routed."""
    ledger = _open(tmp_path)
    try:
        ledger.open_step_execution("#7/1", "process", 1)
        ledger.open_step_execution("#7/1", "process", 2)
        ledger.end_step_execution("#7/1", "process", 1, result="done")

        assert ledger.close_interrupted_step_executions("#7/1") == 1

        states = {
            (record.step_id, record.seq): record.state
            for record in ledger.list_step_executions("#7/1")
        }
        assert states == {
            ("process", 1): STEP_EXECUTION_ENDED,
            ("process", 2): STEP_EXECUTION_INTERRUPTED,
        }
        assert ledger.close_interrupted_step_executions("#7/1") == 0
    finally:
        ledger.close()


# --- REC-1/REC-2: transitions -------------------------------------------------


def test_step_transitions_record_route_refused_and_end(tmp_path: Path) -> None:
    """§13.2: the Route taken, a refused entry and the Workflow End record."""
    ledger = _open(tmp_path)
    try:
        route = ledger.record_route_taken(
            "#7/1", from_step="process", result="done", to_step="review"
        )
        assert route.kind == STEP_TRANSITION_ROUTE
        assert route.to_step == "review"

        refused = ledger.record_refused_entry(
            "#7/1", step_id="process", reason="execution-ceiling"
        )
        assert refused.kind == STEP_TRANSITION_REFUSED
        assert refused.to_step == "process"
        assert refused.reason == "execution-ceiling"

        end = ledger.record_workflow_end(
            "#7/1", from_step="review", result="approved", end="completed"
        )
        assert end.kind == STEP_TRANSITION_END
        assert end.to_end == "completed"

        kinds = [record.kind for record in ledger.list_step_transitions("#7/1")]
        assert kinds == [
            STEP_TRANSITION_ROUTE,
            STEP_TRANSITION_REFUSED,
            STEP_TRANSITION_END,
        ]
    finally:
        ledger.close()


# --- §11.4/§13.2: admission holds --------------------------------------------


def test_admission_hold_records_and_settles(tmp_path: Path) -> None:
    """§11.4: the worst-case hold is in force, then settles with actuals."""
    ledger = _open(tmp_path)
    try:
        hold = ledger.record_admission_hold(
            "#7/1", "process", 1, "hold-1", bound_tokens=1000, bound_cost_usd=0.5
        )

        assert hold.status == ADMISSION_HOLD_HELD
        assert hold.bound_tokens == 1000

        again = ledger.record_admission_hold(
            "#7/1", "process", 1, "hold-1", bound_tokens=1000, bound_cost_usd=0.5
        )
        assert again.id == hold.id

        settled = ledger.settle_admission_hold("hold-1")
        assert settled is not None
        assert settled.status == ADMISSION_HOLD_SETTLED
    finally:
        ledger.close()


def test_crash_charges_open_holds_their_worst_case(tmp_path: Path) -> None:
    """§11.4, §13.2: holds a crash left open are charged, never dropped."""
    ledger = _open(tmp_path)
    try:
        ledger.record_admission_hold(
            "#7/1", "process", 1, "hold-1", bound_tokens=1000, bound_cost_usd=0.5
        )
        ledger.record_admission_hold(
            "#7/1", "process", 2, "hold-2", bound_tokens=2000, bound_cost_usd=1.0
        )
        ledger.settle_admission_hold("hold-1")

        assert ledger.charge_open_admission_holds("#7/1") == 1
        assert ledger.get_admission_hold("hold-2") is not None
        charged = ledger.get_admission_hold("hold-2")
        assert charged is not None
        assert charged.status == ADMISSION_HOLD_CHARGED
        assert ledger.list_open_admission_holds("#7/1") == ()
    finally:
        ledger.close()


# --- REC-4: Effect Intent key (Attempt, step, n, ordinal) ---------------------


def test_step_effect_key_names_attempt_step_number_and_ordinal() -> None:
    """§14.1: the key text carries all four intent coordinates."""
    key = step_effect_key("#1/2", "process", 3, 0)

    assert key == "#1/2/process/3/0"


def test_step_effect_intent_and_receipt_share_one_key(tmp_path: Path) -> None:
    """§14.1: the intent, then the receipt, on the same key."""
    ledger = _open(tmp_path)
    try:
        intent = ledger.record_step_effect_intent(
            "#7/1",
            "process",
            1,
            0,
            rendered_payload="push abc123",
            expected_target={"branch": "agent/implement/7-1", "head_sha": "abc"},
        )

        assert intent.status == STEP_EFFECT_STATUS_INTENT
        assert intent.rendered_payload == "push abc123"
        assert intent.expected_target == {
            "branch": "agent/implement/7-1",
            "head_sha": "abc",
        }

        again = ledger.record_step_effect_intent(
            "#7/1",
            "process",
            1,
            0,
            rendered_payload="push abc123",
            expected_target={"branch": "agent/implement/7-1", "head_sha": "abc"},
        )
        assert again.status == STEP_EFFECT_STATUS_INTENT

        receipt = ledger.record_step_effect_receipt(
            "#7/1", "process", 1, 0, remote_id="abc123"
        )
        assert receipt.status == STEP_EFFECT_STATUS_RECEIPT
        assert receipt.remote_id == "abc123"

        second = ledger.record_step_effect_intent(
            "#7/1",
            "process",
            1,
            1,
            rendered_payload="comment",
            expected_target={"issue": 7},
        )
        assert second.ordinal == 1
        assert len(ledger.list_step_effects("#7/1")) == 2
    finally:
        ledger.close()


def test_step_effect_receipt_without_intent_still_lands(tmp_path: Path) -> None:
    """§14.2: a remote already holding our key is recognised as ours."""
    ledger = _open(tmp_path)
    try:
        receipt = ledger.record_step_effect_receipt(
            "#7/1", "process", 1, 0, remote_id="pr-9"
        )

        assert receipt.status == STEP_EFFECT_STATUS_RECEIPT
        assert receipt.remote_id == "pr-9"
    finally:
        ledger.close()


def test_step_effect_not_performed_carries_its_reason(tmp_path: Path) -> None:
    """§14.2: a settled-without-writing intent records why."""
    ledger = _open(tmp_path)
    try:
        ledger.record_step_effect_intent(
            "#7/1",
            "process",
            1,
            0,
            rendered_payload="push abc123",
            expected_target={"branch": "agent/implement/7-1"},
        )
        settled = ledger.record_step_effect_not_performed(
            "#7/1", "process", 1, 0, reason="input withdrawn"
        )

        assert settled.status == STEP_EFFECT_STATUS_NOT_PERFORMED
        assert "withdrawn" in settled.rendered_payload
    finally:
        ledger.close()


def test_step_effect_marker_carries_the_intent_key(tmp_path: Path) -> None:
    """§14.1, §14.4: the Attempt Marker carries the key for remote lookup."""
    marker = step_effect_marker("#7/1", "process", 1, 0, kind="push")

    assert "#7/1" in marker
    assert "process" in marker
    assert "1" in marker
    assert "0" in marker
    assert "push" in marker
