"""Single-query, single-decoder reads for the Run Ledger lifecycle tables.

Contract: the operator refactor owns the table layout here, not in the
listener, attempts or offline modules. Each table has exactly one row
decoder in this module. :class:`RunLedger` calls these functions on its
writer connection; :class:`RunLedgerReader` calls them on its read-only
connection.

Tables owned here: ``attempts``, ``outcomes``,
``step_executions``, ``step_effects``, ``step_transitions`` and
``run_ledger_events``. ``operator_instance`` and ``operator_halt`` belong
to :mod:`coding_agent.operator.store`; Intake and Operator Command SQL
belongs to :mod:`coding_agent.operator.commands` (ADR 0026).

The legacy ``effects`` table is retired: nothing reads it. Existing
ledger files keep the table and its rows untouched; new ledgers do not
create it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from coding_agent.ledger.store import (
    AdmissionHoldRecord,
    AttemptRecord,
    OutcomeRecord,
    StepEffectRecord,
    StepExecutionRecord,
    StepTransitionRecord,
)


def attempt_from_row(row: object) -> AttemptRecord:
    """Rebuild an :class:`AttemptRecord` from an ``attempts`` row tuple."""
    assert isinstance(row, tuple)

    def _content_refs() -> dict[str, object]:
        if len(row) <= 8 or row[8] is None:
            return {}
        try:
            parsed: object = json.loads(str(row[8]))
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    return AttemptRecord(
        attempt_id=str(row[0]),
        state=str(row[1]),
        effective_configuration=json.loads(str(row[2])),
        effective_configuration_digest=str(row[3]),
        fingerprint=str(row[4]) if row[4] is not None else None,
        base_revision=str(row[5]) if row[5] is not None else None,
        agent_identity_id=int(row[6]) if row[6] is not None else None,
        assigned_by_us=bool(row[7]) if row[7] is not None else None,
        content_refs=_content_refs(),
    )


def outcome_from_row(row: object) -> OutcomeRecord:
    """Rebuild an :class:`OutcomeRecord` from an ``outcomes`` row tuple."""
    assert isinstance(row, tuple)
    raw_writes = str(row[5]) if len(row) > 5 and row[5] is not None else "[]"
    try:
        parsed = json.loads(raw_writes)
    except json.JSONDecodeError:
        parsed = []
    writes = tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()
    return OutcomeRecord(
        attempt_id=str(row[0]),
        outcome=str(row[1]),
        reason=str(row[2]),
        pull_request_body=str(row[3]) if len(row) > 3 and row[3] is not None else "",
        terminal_comment_body=str(row[4]) if len(row) > 4 and row[4] is not None else "",
        writes=writes,
    )


def step_execution_from_row(row: object) -> StepExecutionRecord:
    """Rebuild a :class:`StepExecutionRecord` from a row tuple."""
    assert isinstance(row, tuple)

    def _maybe_json(index: int) -> dict[str, object] | None:
        if len(row) <= index or row[index] is None:
            return None
        parsed: object = json.loads(str(row[index]))
        return parsed if isinstance(parsed, dict) else None

    return StepExecutionRecord(
        attempt_id=str(row[0]),
        step_id=str(row[1]),
        seq=int(row[2]),
        entry_from_step=str(row[3]) if row[3] is not None else None,
        entry_result=str(row[4]) if row[4] is not None else None,
        state=str(row[5]),
        result=str(row[6]) if row[6] is not None else None,
        payload=_maybe_json(7),
        facts=_maybe_json(8),
        usage=_maybe_json(9),
        limit_fact=_maybe_json(10),
    )


def step_effect_from_row(row: object) -> StepEffectRecord:
    """Rebuild a :class:`StepEffectRecord` from a ``step_effects`` row tuple."""
    assert isinstance(row, tuple)
    raw_target = str(row[6]) if row[6] is not None else "{}"
    try:
        parsed: object = json.loads(raw_target)
    except json.JSONDecodeError:
        parsed = {}
    expected = parsed if isinstance(parsed, dict) else {}
    return StepEffectRecord(
        attempt_id=str(row[0]),
        step_id=str(row[1]),
        execution_number=int(row[2]),
        ordinal=int(row[3]),
        status=str(row[4]),
        rendered_payload=str(row[5]),
        expected_target={str(key): value for key, value in expected.items()},
        remote_id=str(row[7]) if row[7] is not None else None,
        payload_digest=str(row[8]),
    )


def step_transition_from_row(row: object) -> StepTransitionRecord:
    """Rebuild a :class:`StepTransitionRecord` from a row tuple."""
    assert isinstance(row, tuple)
    return StepTransitionRecord(
        id=int(row[0]),
        attempt_id=str(row[1]),
        kind=str(row[2]),
        from_step=str(row[3]) if row[3] is not None else None,
        result=str(row[4]) if row[4] is not None else None,
        to_step=str(row[5]) if row[5] is not None else None,
        to_end=str(row[6]) if row[6] is not None else None,
        reason=str(row[7]) if row[7] is not None else None,
    )


_ATTEMPT_COLUMNS = (
    "attempt_id, state, effective_configuration,"
    " effective_configuration_digest, fingerprint, base_revision,"
    " agent_identity_id, assigned_by_us, content_refs"
)
_ATTEMPT_COLUMNS_LEGACY = (
    "attempt_id, state, effective_configuration,"
    " effective_configuration_digest, fingerprint, base_revision,"
    " agent_identity_id, assigned_by_us"
)


def get_attempt(
    connection: sqlite3.Connection, attempt_id: str
) -> AttemptRecord | None:
    """One ``attempts`` row by id, or ``None`` where missing."""
    try:
        try:
            row = connection.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = connection.execute(
                f"SELECT {_ATTEMPT_COLUMNS_LEGACY} FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return attempt_from_row(row)
    except (ValueError, KeyError, TypeError):
        return None


def get_attempt_created_at(
    connection: sqlite3.Connection, attempt_id: str
) -> str | None:
    """The ``created_at`` timestamp for one Attempt, if recorded."""
    try:
        row = connection.execute(
            "SELECT created_at FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def has_unfinished_attempt(connection: sqlite3.Connection) -> bool:
    """Whether the ledger holds an Attempt neither finished nor released."""
    try:
        row = connection.execute(
            "SELECT 1 FROM attempts"
            " WHERE state NOT IN ('finished', 'released') LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def get_outcome(
    connection: sqlite3.Connection, attempt_id: str
) -> OutcomeRecord | None:
    """One ``outcomes`` row by Attempt id, or ``None`` where missing."""
    try:
        row = connection.execute(
            "SELECT attempt_id, outcome, reason, pull_request_body,"
            " terminal_comment_body, writes FROM outcomes WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return outcome_from_row(row)


def list_step_executions(
    connection: sqlite3.Connection, attempt_id: str
) -> tuple[StepExecutionRecord, ...]:
    """Every ``step_executions`` row for one Attempt, in open order."""
    try:
        rows = connection.execute(
            "SELECT attempt_id, step_id, seq, entry_from_step, entry_result,"
            " state, result, payload, facts, usage, limit_fact"
            " FROM step_executions WHERE attempt_id = ? ORDER BY rowid",
            (attempt_id,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(step_execution_from_row(row) for row in rows)


def list_step_effects(
    connection: sqlite3.Connection, attempt_id: str
) -> tuple[StepEffectRecord, ...]:
    """Every ``step_effects`` row for one Attempt, in key order."""
    try:
        rows = connection.execute(
            "SELECT attempt_id, step_id, execution_number, ordinal, status,"
            " rendered_payload, expected_target, remote_id, payload_digest"
            " FROM step_effects WHERE attempt_id = ?"
            " ORDER BY step_id, execution_number, ordinal",
            (attempt_id,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(step_effect_from_row(row) for row in rows)


def list_step_transitions(
    connection: sqlite3.Connection, attempt_id: str
) -> tuple[StepTransitionRecord, ...]:
    """Every ``step_transitions`` row for one Attempt, in order."""
    try:
        rows = connection.execute(
            "SELECT id, attempt_id, kind, from_step, result, to_step, to_end, reason"
            " FROM step_transitions WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(step_transition_from_row(row) for row in rows)


def get_responses_without_usage(
    connection: sqlite3.Connection, attempt_id: str
) -> int:
    """The durable worst-case counter for one Attempt, else zero."""
    try:
        row = connection.execute(
            "SELECT responses_without_usage FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or row[0] is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def get_responses_with_unreported_cache_write(
    connection: sqlite3.Connection, attempt_id: str
) -> int:
    """The durable unreported-cache-write counter for one Attempt, else zero."""
    try:
        row = connection.execute(
            "SELECT responses_with_unreported_cache_write FROM attempts"
            " WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or row[0] is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def get_latest_usage(
    connection: sqlite3.Connection, attempt_id: str
) -> dict[str, Any] | None:
    """The newest ``usage`` record payload for one Attempt, if any."""
    try:
        row = connection.execute(
            "SELECT payload FROM run_ledger_events"
            " WHERE attempt_id = ? AND kind = 'usage'"
            " ORDER BY id DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def last_finished_attempt(
    connection: sqlite3.Connection,
) -> tuple[str, str] | None:
    """The latest finished, released or outcome-recorded-with-outcome Attempt."""
    try:
        rows = connection.execute(
            "SELECT attempt_id, state FROM attempts"
            " WHERE state IN ('finished', 'released', 'outcome-recorded')"
            " ORDER BY rowid DESC"
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        attempt_id = str(row[0])
        state = str(row[1])
        if state in ("finished", "released"):
            return (attempt_id, state)
        try:
            outcome_row = connection.execute(
                "SELECT 1 FROM outcomes WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if outcome_row is not None:
            return (attempt_id, state)
    return None


def list_attempts_page_rows(
    connection: sqlite3.Connection, *, limit: int, bound: int | None
) -> list[tuple[Any, ...]]:
    """Raw ``attempts`` page rows, newest first (``limit + 1`` for the cursor)."""
    try:
        if bound is None:
            rows = connection.execute(
                "SELECT rowid, attempt_id, state, created_at, fingerprint"
                " FROM attempts ORDER BY rowid DESC LIMIT ?",
                (limit + 1,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT rowid, attempt_id, state, created_at, fingerprint"
                " FROM attempts WHERE rowid < ? ORDER BY rowid DESC LIMIT ?",
                (bound, limit + 1),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [tuple(row) for row in rows]


def get_attempt_page_row(
    connection: sqlite3.Connection, attempt_id: str
) -> tuple[Any, ...] | None:
    """The ``attempts`` page row for one Attempt id, if recorded."""
    try:
        row = connection.execute(
            "SELECT rowid, attempt_id, state, created_at, fingerprint"
            " FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return tuple(row)


def list_all_outcomes(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, str]]:
    """Every outcome keyed by Attempt id (for the attempts-page join)."""
    try:
        rows = connection.execute(
            "SELECT attempt_id, outcome, reason FROM outcomes"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        str(row[0]): {"outcome": str(row[1]), "reason": str(row[2])} for row in rows
    }


def admission_hold_from_row(row: object) -> AdmissionHoldRecord:
    """Rebuild an :class:`AdmissionHoldRecord` from a row tuple."""
    assert isinstance(row, tuple)
    return AdmissionHoldRecord(
        id=int(row[0]),
        attempt_id=str(row[1]),
        step_id=str(row[2]),
        seq=int(row[3]),
        hold_id=str(row[4]),
        bound_tokens=int(row[5]),
        bound_cost_usd=float(row[6]),
        status=str(row[7]),
    )


def count_events_by_kind(
    connection: sqlite3.Connection, attempt_id: str, kind: str
) -> int:
    """How many ``run_ledger_events`` rows of ``kind`` one Attempt recorded."""
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM run_ledger_events"
            " WHERE attempt_id = ? AND kind = ?",
            (attempt_id, kind),
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def list_event_payloads_by_kinds(
    connection: sqlite3.Connection,
    attempt_id: str,
    kinds: tuple[str, ...],
) -> list[tuple[str, dict[str, Any] | None]]:
    """Every ``(kind, payload)`` pair for one Attempt's events of ``kinds``.

    In ledger id order. A row whose payload does not decode to a dict
    reads as ``None`` — recorded but without readable fields — so callers
    can tell an event that lacks a record from no event at all.
    """
    if not kinds:
        return []
    placeholders = ", ".join("?" for _ in kinds)
    try:
        rows = connection.execute(
            "SELECT kind, payload FROM run_ledger_events"
            f" WHERE attempt_id = ? AND kind IN ({placeholders})"
            " ORDER BY id",
            (attempt_id, *kinds),
        ).fetchall()
    except sqlite3.Error:
        return []
    pairs: list[tuple[str, dict[str, Any] | None]] = []
    for row in rows:
        kind = str(row[0])
        try:
            payload = json.loads(str(row[1]))
        except (ValueError, TypeError):
            payload = None
        pairs.append((kind, payload if isinstance(payload, dict) else None))
    return pairs
