"""Durable Intake and Operator Commands (adoption step 4, #249, #252).

Contract: operator contract major 1 at ``06ee46c`` §6.2, §7.5, §8
(parent #245). Intake and Operator Commands are durable in the Run
Ledger's SQLite file, in separate tables owned by this module, and they
serialise with the Claim boundary and with finalization through
SQLite's write lock. They gate only the decision to open a Claim and
never write lifecycle state: no command writes an Attempt row, an
outcome or an effect.

Tables (``CREATE TABLE IF NOT EXISTS``, fresh volumes only, no
migration):

- ``operator_intake``: one row (``id = 1``) with ``state``
  (``open``/``closed``), the pending ``stop`` (request ID and sequence)
  and ``updated_at``. Absent means open.
- ``operator_commands``: one row per committed command, keyed by
  ``request_id`` (unique) with an autoincrement ``sequence`` that
  increases strictly in commit order, ``kind``, canonical
  ``arguments`` JSON, ``payload_digest``, ``repository``, the
  ``acknowledgement``, a human ``detail`` and timestamps. Rows are
  never deleted.

Issue #252 (``next issue N``): the operator's priority for the next
Claim. A ``next`` command carries ``arguments`` ``{"issue": N}`` and is
``accepted`` while pending. The commit-time eligibility read happens
outside the write transaction (network reads never hold the write
lock); the write transaction only re-checks the idempotency, supersedes
older accepted priorities and inserts the new row. At the next Claim
boundary the sweep tries issue N first: ``completed`` where the Claim
opens and confirms, ``not_fulfilled`` where N is absent, guarded or
released by its read-back. A newer priority supersedes the older one.
A priority never reopens Intake.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.operator.contract import to_z, utc_now_z
from coding_agent.runtime.redaction import Redactable

INTAKE_OPEN = "open"
INTAKE_CLOSED = "closed"

ACK_ACCEPTED = "accepted"
ACK_COMPLETED = "completed"
ACK_SUPERSEDED = "superseded"
ACK_NOT_FULFILLED = "not_fulfilled"

KIND_STOP = "stop"
KIND_STOP_AFTER = "stop_after"
KIND_RESUME = "resume"
KIND_NEXT_ISSUE = "next_issue"
KIND_NEXT_LEGACY = "next"
"""The pre-``v1.0.4`` ``next`` kind, read back but never written."""

KIND_NEXT = KIND_NEXT_ISSUE
"""Back-compat alias: the canonical ``next`` kind is ``next_issue``."""

STOP_ACCEPTED_DETAIL = "finish the active attempt, then close intake"
STOP_COMPLETED_DETAIL = "intake closed"
RESUME_REOPENED_DETAIL = "intake reopened"
RESUME_ALREADY_OPEN_DETAIL = "intake already open"

NEXT_ACCEPTED_DETAIL = "priority recorded, tried first at the next claim boundary"
NEXT_COMPLETED_DETAIL = "priority claimed first"

STOP_AFTER_MIN = 1
STOP_AFTER_MAX = 999


def stop_after_accepted_detail(remaining: int) -> str:
    """Human detail for a committed ``stop --after N`` plan."""
    return f"finish {remaining} more attempts, then close intake"


def stop_after_completed_detail(counted: int) -> str:
    """Human detail for a ``stop --after`` plan that reached zero."""
    return f"finished {counted} attempts, intake closed"


ENSURE_INTAKE_SQL = """
CREATE TABLE IF NOT EXISTS operator_intake (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'open',
    pending_stop_request_id TEXT,
    pending_stop_sequence INTEGER,
    stop_plan_request_id TEXT,
    stop_plan_sequence INTEGER,
    stop_plan_remaining INTEGER,
    stop_plan_counted INTEGER,
    updated_at TEXT NOT NULL
)
"""

ENSURE_COMMANDS_SQL = """
CREATE TABLE IF NOT EXISTS operator_commands (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    arguments TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    repository TEXT NOT NULL,
    acknowledgement TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _utc_now_iso() -> str:
    return utc_now_z()


def _process_started_for_events(connection: sqlite3.Connection) -> str:
    """The current process start tagging Contract Events (same transaction)."""
    try:
        row = connection.execute(
            "SELECT process_started_at FROM operator_instance WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row[0]:
        return str(row[0])
    return _utc_now_iso()


def _emit_command_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    request_id: str,
    kind: str,
    acknowledgement: str = "",
    registry: Redactable = None,
) -> None:
    """Emit one command Contract Event in the caller's transaction.

    The text passes through the worker's redaction registry like every
    other Contract Event (#250); ``None`` redacts nothing (unit tests
    without a worker).
    """
    from coding_agent.operator import events as contract_events

    process_started = _process_started_for_events(connection)
    if acknowledgement:
        text = f"{kind} {request_id} {acknowledgement}"
    else:
        text = f"{kind} {request_id}"
    contract_events.emit_contract_event(
        connection,
        process_started_at=process_started,
        event_type=event_type,
        text=text,
        registry=registry,
        level=contract_events.LEVEL_INFO,
        request_id=request_id,
        kind=kind,
        acknowledgement=acknowledgement or None,
    )


def canonical_arguments(arguments: dict[str, Any]) -> str:
    """Canonical JSON bytes of a command's arguments (sorted keys)."""
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))


def payload_digest(kind: str, arguments: dict[str, Any]) -> str:
    """Digest of the canonical payload: ``kind`` plus ``arguments``.

    ``--expect-repo`` is not part of the payload (§8.1): the same ID
    with the same kind/arguments replays even when the casing of the
    repo differs, while a different kind or arguments mismatches.
    """
    canonical = json.dumps(
        {"kind": kind, "arguments": dict(arguments)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CommandRecord:
    """One durable Operator Command row."""

    request_id: str
    sequence: int
    kind: str
    arguments: dict[str, Any]
    repository: str
    acknowledgement: str
    detail: str
    created_at: str
    updated_at: str
    payload_digest: str

    def to_dict(self) -> dict[str, Any]:
        """The contract ``CommandRecord`` shape (§7.5, ``v1.0.4``)."""
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "arguments": dict(self.arguments),
            "repository": self.repository,
            "acknowledgement": self.acknowledgement,
            "detail": self.detail,
            "created_at": to_z(self.created_at),
            "updated_at": to_z(self.updated_at),
        }


def _record_from_row(row: Any) -> CommandRecord:
    """Rebuild a record from an ``operator_commands`` row tuple."""
    assert isinstance(row, tuple)
    try:
        arguments = json.loads(str(row[3]))
    except (json.JSONDecodeError, TypeError, ValueError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return CommandRecord(
        request_id=str(row[1]),
        sequence=int(row[0]),
        kind=str(row[2]),
        arguments=dict(arguments),
        repository=str(row[5]),
        acknowledgement=str(row[6]),
        detail=str(row[7]),
        created_at=str(row[8]),
        updated_at=str(row[9]),
        payload_digest=str(row[4]),
    )


def ensure_operator_tables(connection: sqlite3.Connection) -> None:
    """Create the Intake and Commands tables where absent (fresh volumes)."""
    connection.execute(ENSURE_INTAKE_SQL)
    connection.execute(ENSURE_COMMANDS_SQL)
    # Tolerate volumes created by the previous slice (#248), whose intake
    # table lacks the pending-stop columns: fresh volumes only means no
    # migration is required, but adding the columns keeps a restart on an
    # older file working instead of failing the Worker.
    try:
        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(operator_intake)").fetchall()
        }
    except sqlite3.Error:
        existing = set()
    if "pending_stop_request_id" not in existing:
        try:
            connection.execute(
                "ALTER TABLE operator_intake"
                " ADD COLUMN pending_stop_request_id TEXT"
            )
        except sqlite3.Error:
            pass
    if "pending_stop_sequence" not in existing:
        try:
            connection.execute(
                "ALTER TABLE operator_intake ADD COLUMN pending_stop_sequence INTEGER"
            )
        except sqlite3.Error:
            pass
    for column, ddl in (
        ("stop_plan_request_id", "TEXT"),
        ("stop_plan_sequence", "INTEGER"),
        ("stop_plan_remaining", "INTEGER"),
        ("stop_plan_counted", "INTEGER"),
    ):
        if column not in existing:
            try:
                connection.execute(
                    f"ALTER TABLE operator_intake ADD COLUMN {column} {ddl}"
                )
            except sqlite3.Error:
                pass
    connection.commit()


def read_intake(connection: sqlite3.Connection) -> dict[str, Any]:
    """The Intake row, or open-with-no-pending where absent."""
    try:
        row = connection.execute(
            "SELECT state, pending_stop_request_id, pending_stop_sequence,"
            " stop_plan_request_id, stop_plan_sequence,"
            " stop_plan_remaining, stop_plan_counted"
            " FROM operator_intake WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return {"state": INTAKE_OPEN, "pending_stop": None, "stop_plan": None}
    if row is None:
        return {"state": INTAKE_OPEN, "pending_stop": None, "stop_plan": None}
    state = str(row[0]) if row[0] else INTAKE_OPEN
    if state not in (INTAKE_OPEN, INTAKE_CLOSED):
        state = INTAKE_OPEN
    pending = None
    if row[1] is not None:
        try:
            pending_sequence = int(row[2]) if row[2] is not None else 0
        except (TypeError, ValueError):
            pending_sequence = 0
        pending = {"request_id": str(row[1]), "sequence": pending_sequence}
    plan: dict[str, Any] | None = None
    if row[3] is not None:
        try:
            plan_sequence = int(row[4]) if row[4] is not None else 0
        except (TypeError, ValueError):
            plan_sequence = 0
        try:
            plan_remaining = int(row[5]) if row[5] is not None else 0
        except (TypeError, ValueError):
            plan_remaining = 0
        try:
            plan_counted = int(row[6]) if row[6] is not None else 0
        except (TypeError, ValueError):
            plan_counted = 0
        plan = {
            "request_id": str(row[3]),
            "sequence": plan_sequence,
            "remaining": plan_remaining,
            "counted": plan_counted,
        }
    return {"state": state, "pending_stop": pending, "stop_plan": plan}


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


def get_command(
    connection: sqlite3.Connection, request_id: str
) -> CommandRecord | None:
    """The stored command for ``request_id``, or ``None`` where unknown."""
    try:
        row = connection.execute(
            "SELECT sequence, request_id, kind, arguments, payload_digest,"
            " repository, acknowledgement, detail, created_at, updated_at"
            " FROM operator_commands WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _record_from_row(row)


def list_recent_commands(
    connection: sqlite3.Connection, limit: int = 20
) -> list[CommandRecord]:
    """At most ``limit`` commands, newest last (§7.2 ``recent_commands``)."""
    try:
        rows = connection.execute(
            "SELECT sequence, request_id, kind, arguments, payload_digest,"
            " repository, acknowledgement, detail, created_at, updated_at"
            " FROM operator_commands ORDER BY sequence DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [ _record_from_row(row) for row in reversed(rows) ]


def _insert_command(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    kind: str,
    arguments: dict[str, Any],
    repository: str,
    acknowledgement: str,
    detail: str,
    now: str,
) -> CommandRecord:
    """Insert one command row; the caller holds the write lock."""
    digest = payload_digest(kind, arguments)
    cursor = connection.execute(
        "INSERT INTO operator_commands"
        " (request_id, kind, arguments, payload_digest, repository,"
        "  acknowledgement, detail, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id,
            kind,
            canonical_arguments(arguments),
            digest,
            repository,
            acknowledgement,
            detail,
            now,
            now,
        ),
    )
    sequence = int(cursor.lastrowid or 0)
    row = connection.execute(
        "SELECT sequence, request_id, kind, arguments, payload_digest,"
        " repository, acknowledgement, detail, created_at, updated_at"
        " FROM operator_commands WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    assert row is not None
    record = _record_from_row(row)
    assert record.sequence == sequence or sequence == 0
    return record


def _set_intake(
    connection: sqlite3.Connection,
    *,
    state: str,
    pending_request_id: str | None,
    pending_sequence: int | None,
    now: str,
    stop_plan_request_id: str | None = None,
    stop_plan_sequence: int | None = None,
    stop_plan_remaining: int | None = None,
    stop_plan_counted: int | None = None,
) -> None:
    """Write the single Intake row (the caller holds the write lock)."""
    connection.execute(
        "INSERT INTO operator_intake"
        " (id, state, pending_stop_request_id, pending_stop_sequence,"
        "  stop_plan_request_id, stop_plan_sequence,"
        "  stop_plan_remaining, stop_plan_counted, updated_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " state = excluded.state,"
        " pending_stop_request_id = excluded.pending_stop_request_id,"
        " pending_stop_sequence = excluded.pending_stop_sequence,"
        " stop_plan_request_id = excluded.stop_plan_request_id,"
        " stop_plan_sequence = excluded.stop_plan_sequence,"
        " stop_plan_remaining = excluded.stop_plan_remaining,"
        " stop_plan_counted = excluded.stop_plan_counted,"
        " updated_at = excluded.updated_at",
        (
            state,
            pending_request_id,
            pending_sequence,
            stop_plan_request_id,
            stop_plan_sequence,
            stop_plan_remaining,
            stop_plan_counted,
            now,
        ),
    )


def _supersede_accepted(
    connection: sqlite3.Connection, request_id: str, *, now: str
) -> None:
    """Mark one ``accepted`` command superseded (idempotent)."""
    connection.execute(
        "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
        " WHERE request_id = ? AND acknowledgement = ?",
        (ACK_SUPERSEDED, now, request_id, ACK_ACCEPTED),
    )


def _command_kind(
    connection: sqlite3.Connection, request_id: str, default: str
) -> str:
    """The stored kind for ``request_id``, or ``default`` where unknown."""
    try:
        record = get_command(connection, request_id)
    except sqlite3.Error:
        return default
    if record is None:
        return default
    return record.kind


def _supersede_intake_commands(
    connection: sqlite3.Connection, intake: dict[str, Any], *, now: str
) -> list[tuple[str, str]]:
    """Supersede the pending stop and any earlier stop plan.

    Returns ``(request_id, kind)`` pairs so the caller can emit one
    ``command_resolved`` Contract Event per superseded command in the
    same transaction, with the superseded command's own kind.
    """
    superseded: list[tuple[str, str]] = []
    pending = intake.get("pending_stop")
    if isinstance(pending, dict) and pending.get("request_id"):
        old_id = str(pending["request_id"])
        _supersede_accepted(connection, old_id, now=now)
        superseded.append((old_id, _command_kind(connection, old_id, KIND_STOP)))
    plan = intake.get("stop_plan")
    if isinstance(plan, dict) and plan.get("request_id"):
        plan_id = str(plan["request_id"])
        _supersede_accepted(connection, plan_id, now=now)
        superseded.append(
            (plan_id, _command_kind(connection, plan_id, KIND_STOP_AFTER))
        )
    return superseded


def validate_stop_after(value: Any) -> int | None:
    """The validated ``--after`` count, or ``None`` where invalid."""
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value)) if not isinstance(value, int) else value
    except (TypeError, ValueError):
        return None
    if number < STOP_AFTER_MIN or number > STOP_AFTER_MAX:
        return None
    return number


def commit_stop(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    expect_repo: str,
    target_repository: str,
    after: Any = None,
    registry: Redactable = None,
) -> tuple[CommandRecord | None, bool, dict[str, Any] | None]:
    """Commit a ``stop`` in this connection's write transaction.

    Returns ``(record, replayed, error)``: on success ``record`` is the
    stored command and ``error`` is ``None``; on a guard failure
    ``record`` is ``None`` and ``error`` carries ``code`` plus the
    fields the listener needs (``repository`` for a mismatch). The
    caller opens ``BEGIN IMMEDIATE`` and commits on success. Nothing
    is stored for a repository mismatch, a payload mismatch or an
    invalid ``after``.

    With ``after`` (``stop --after N``, 1 ≤ N ≤ 999) the command commits
    a stop plan with ``remaining = N`` instead of closing Intake: the
    Worker takes N more Attempts with a recorded Terminal Outcome and
    then drains. The plan supersedes any earlier plan or pending stop.
    """
    if expect_repo.lower() != target_repository.lower():
        return (
            None,
            False,
            {"code": "repository_mismatch", "repository": target_repository},
        )
    now = _utc_now_iso()
    if after is None:
        arguments: dict[str, Any] = {}
        kind = KIND_STOP
    else:
        validated = validate_stop_after(after)
        if validated is None:
            return (None, False, {"code": "invalid_argument"})
        arguments = {"after": validated}
        kind = KIND_STOP_AFTER
    digest = payload_digest(kind, arguments)
    existing = get_command(connection, request_id)
    if existing is not None:
        if existing.payload_digest != digest:
            return (None, False, {"code": "payload_mismatch"})
        return (existing, True, None)
    intake = read_intake(connection)
    if arguments.get("after") is not None:
        remaining = int(arguments["after"])
        superseded_plan_ids = _supersede_intake_commands(
            connection, intake, now=now
        )
        record = _insert_command(
            connection,
            request_id=request_id,
            kind=kind,
            arguments=arguments,
            repository=target_repository,
            acknowledgement=ACK_ACCEPTED,
            detail=stop_after_accepted_detail(remaining),
            now=now,
        )
        from coding_agent.operator import events as _events_after

        _emit_command_event(
            connection,
            event_type=_events_after.EVENT_COMMAND_ACCEPTED,
            request_id=request_id,
            kind=kind,
            acknowledgement=ACK_ACCEPTED,
            registry=registry,
        )
        for superseded_plan_id, superseded_kind in superseded_plan_ids:
            _emit_command_event(
                connection,
                event_type=_events_after.EVENT_COMMAND_RESOLVED,
                request_id=superseded_plan_id,
                kind=superseded_kind,
                acknowledgement=ACK_SUPERSEDED,
                registry=registry,
            )
        _set_intake(
            connection,
            state=INTAKE_OPEN,
            pending_request_id=None,
            pending_sequence=None,
            stop_plan_request_id=request_id,
            stop_plan_sequence=record.sequence,
            stop_plan_remaining=remaining,
            stop_plan_counted=0,
            now=now,
        )
        return (record, False, None)
    unfinished = has_unfinished_attempt(connection)
    if not unfinished:
        superseded_immediate_ids = _supersede_intake_commands(
            connection, intake, now=now
        )
        record = _insert_command(
            connection,
            request_id=request_id,
            kind=KIND_STOP,
            arguments=arguments,
            repository=target_repository,
            acknowledgement=ACK_COMPLETED,
            detail=STOP_COMPLETED_DETAIL,
            now=now,
        )
        from coding_agent.operator import events as _events_mod

        _emit_command_event(
            connection,
            event_type=_events_mod.EVENT_COMMAND_ACCEPTED,
            request_id=request_id,
            kind=KIND_STOP,
            acknowledgement=ACK_ACCEPTED,
            registry=registry,
        )
        _emit_command_event(
            connection,
            event_type=_events_mod.EVENT_COMMAND_RESOLVED,
            request_id=request_id,
            kind=KIND_STOP,
            acknowledgement=ACK_COMPLETED,
            registry=registry,
        )
        for superseded_immediate_id, superseded_kind in superseded_immediate_ids:
            _emit_command_event(
                connection,
                event_type=_events_mod.EVENT_COMMAND_RESOLVED,
                request_id=superseded_immediate_id,
                kind=superseded_kind,
                acknowledgement=ACK_SUPERSEDED,
                registry=registry,
            )
        _set_intake(
            connection,
            state=INTAKE_CLOSED,
            pending_request_id=None,
            pending_sequence=None,
            now=now,
        )
        return (record, False, None)
    # Active Attempt: drain. A pending stop or plan is superseded by the newer one.
    superseded_drain_ids = _supersede_intake_commands(
        connection, intake, now=now
    )
    record = _insert_command(
        connection,
        request_id=request_id,
        kind=KIND_STOP,
        arguments=arguments,
        repository=target_repository,
        acknowledgement=ACK_ACCEPTED,
        detail=STOP_ACCEPTED_DETAIL,
        now=now,
    )
    from coding_agent.operator import events as _events_mod2

    _emit_command_event(
        connection,
        event_type=_events_mod2.EVENT_COMMAND_ACCEPTED,
        request_id=request_id,
        kind=KIND_STOP,
        acknowledgement=ACK_ACCEPTED,
        registry=registry,
    )
    for superseded_drain_id, superseded_kind in superseded_drain_ids:
        _emit_command_event(
            connection,
            event_type=_events_mod2.EVENT_COMMAND_RESOLVED,
            request_id=superseded_drain_id,
            kind=superseded_kind,
            acknowledgement=ACK_SUPERSEDED,
            registry=registry,
        )
    _set_intake(
        connection,
        state=INTAKE_CLOSED,
        pending_request_id=request_id,
        pending_sequence=record.sequence,
        now=now,
    )
    return (record, False, None)


def commit_resume(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    expect_repo: str,
    target_repository: str,
    registry: Redactable = None,
) -> tuple[CommandRecord | None, bool, dict[str, Any] | None]:
    """Commit a ``resume`` in this connection's write transaction.

    Reopens Intake and supersedes a pending ``stop``. With Intake
    already open and nothing pending it completes with a detail saying
    so. Never starts a process or resumes interrupted work: only the
    Intake row and the command rows change.
    """
    if expect_repo.lower() != target_repository.lower():
        return (
            None,
            False,
            {"code": "repository_mismatch", "repository": target_repository},
        )
    now = _utc_now_iso()
    arguments: dict[str, Any] = {}
    digest = payload_digest(KIND_RESUME, arguments)
    existing = get_command(connection, request_id)
    if existing is not None:
        if existing.payload_digest != digest:
            return (None, False, {"code": "payload_mismatch"})
        return (existing, True, None)
    intake = read_intake(connection)
    pending = intake.get("pending_stop")
    plan = intake.get("stop_plan")
    if intake.get("state") == INTAKE_OPEN and pending is None and plan is None:
        record = _insert_command(
            connection,
            request_id=request_id,
            kind=KIND_RESUME,
            arguments=arguments,
            repository=target_repository,
            acknowledgement=ACK_COMPLETED,
            detail=RESUME_ALREADY_OPEN_DETAIL,
            now=now,
        )
        from coding_agent.operator import events as _events_resume

        _emit_command_event(
            connection,
            event_type=_events_resume.EVENT_COMMAND_ACCEPTED,
            request_id=request_id,
            kind=KIND_RESUME,
            acknowledgement=ACK_ACCEPTED,
            registry=registry,
        )
        _emit_command_event(
            connection,
            event_type=_events_resume.EVENT_COMMAND_RESOLVED,
            request_id=request_id,
            kind=KIND_RESUME,
            acknowledgement=ACK_COMPLETED,
            registry=registry,
        )
        return (record, False, None)
    superseded_resume_ids = _supersede_intake_commands(
        connection, intake, now=now
    )
    record = _insert_command(
        connection,
        request_id=request_id,
        kind=KIND_RESUME,
        arguments=arguments,
        repository=target_repository,
        acknowledgement=ACK_COMPLETED,
        detail=RESUME_REOPENED_DETAIL,
        now=now,
    )
    from coding_agent.operator import events as _events_resume2

    _emit_command_event(
        connection,
        event_type=_events_resume2.EVENT_COMMAND_ACCEPTED,
        request_id=request_id,
        kind=KIND_RESUME,
        acknowledgement=ACK_ACCEPTED,
        registry=registry,
    )
    _emit_command_event(
        connection,
        event_type=_events_resume2.EVENT_COMMAND_RESOLVED,
        request_id=request_id,
        kind=KIND_RESUME,
        acknowledgement=ACK_COMPLETED,
        registry=registry,
    )
    for superseded_resume_id, superseded_kind in superseded_resume_ids:
        _emit_command_event(
            connection,
            event_type=_events_resume2.EVENT_COMMAND_RESOLVED,
            request_id=superseded_resume_id,
            kind=superseded_kind,
            acknowledgement=ACK_SUPERSEDED,
            registry=registry,
        )
    _set_intake(
        connection,
        state=INTAKE_OPEN,
        pending_request_id=None,
        pending_sequence=None,
        now=now,
    )
    return (record, False, None)


def get_pending_next_command(
    connection: sqlite3.Connection,
) -> CommandRecord | None:
    """The newest ``accepted`` ``next_issue`` command, or ``None`` where none pends.

    A newer priority supersedes the older one at commit, so at most one
    row is ``accepted``; the newest-by-sequence wins regardless. ``None``
    means no priority waits for the next Claim boundary. Reads both the
    ``v1.0.4`` ``next_issue`` kind and the pre-``v1.0.4`` ``next`` kind.
    """
    try:
        row = connection.execute(
            "SELECT sequence, request_id, kind, arguments, payload_digest,"
            " repository, acknowledgement, detail, created_at, updated_at"
            " FROM operator_commands"
            " WHERE kind IN (?, ?) AND acknowledgement = ?"
            " ORDER BY sequence DESC LIMIT 1",
            (KIND_NEXT_ISSUE, KIND_NEXT_LEGACY, ACK_ACCEPTED),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _record_from_row(row)


def pending_next_priority(
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    """The accepted ``next-issue`` priority as ``{request_id, issue}``, if any.

    Used by the ``status`` intake projection: ``{request_id, issue}`` while
    an accepted ``next-issue`` Operator Command has not been claimed yet,
    ``None`` otherwise. Malformed rows (missing or non-positive issue)
    read as no priority.
    """
    pending = get_pending_next_command(connection)
    if pending is None:
        return None
    raw_issue = pending.arguments.get("issue")
    try:
        issue = int(str(raw_issue)) if not isinstance(raw_issue, bool) else 0
    except (TypeError, ValueError):
        return None
    if issue < 1:
        return None
    return {"request_id": pending.request_id, "issue": issue}


def commit_next(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    expect_repo: str,
    target_repository: str,
    issue_number: int,
    registry: Redactable = None,
) -> tuple[CommandRecord | None, bool, dict[str, Any] | None]:
    """Commit a ``next issue N`` priority in this connection's transaction.

    The caller runs the bounded commit-time eligibility read first, with
    its own tracker instance and no write lock held: this transaction
    only re-checks the idempotency, supersedes every older accepted
    priority and inserts the new ``accepted`` row. A definite
    ineligibility never reaches here — the listener answers ``rejected``
    without storing anything — so every call commits (or replays).

    Returns ``(record, replayed, error)`` like :func:`commit_stop`.
    Nothing is stored for a repository or payload mismatch.
    """
    if expect_repo.lower() != target_repository.lower():
        return (
            None,
            False,
            {"code": "repository_mismatch", "repository": target_repository},
        )
    now = _utc_now_iso()
    arguments: dict[str, Any] = {"issue": issue_number}
    digest = payload_digest(KIND_NEXT_ISSUE, arguments)
    existing = get_command(connection, request_id)
    if existing is not None:
        if existing.payload_digest != digest:
            return (None, False, {"code": "payload_mismatch"})
        return (existing, True, None)
    try:
        older = connection.execute(
            "SELECT request_id, kind FROM operator_commands"
            " WHERE kind IN (?, ?) AND acknowledgement = ?",
            (KIND_NEXT_ISSUE, KIND_NEXT_LEGACY, ACK_ACCEPTED),
        ).fetchall()
    except sqlite3.Error:
        older = []
    superseded: list[tuple[str, str]] = [
        (str(row[0]), str(row[1]) if len(row) > 1 else KIND_NEXT_ISSUE)
        for row in older
    ]
    for old_id, _old_kind in superseded:
        connection.execute(
            "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
            " WHERE request_id = ? AND acknowledgement = ?",
            (ACK_SUPERSEDED, now, old_id, ACK_ACCEPTED),
        )
    record = _insert_command(
        connection,
        request_id=request_id,
        kind=KIND_NEXT_ISSUE,
        arguments=arguments,
        repository=target_repository,
        acknowledgement=ACK_ACCEPTED,
        detail=NEXT_ACCEPTED_DETAIL,
        now=now,
    )
    from coding_agent.operator import events as _events_next

    _emit_command_event(
        connection,
        event_type=_events_next.EVENT_COMMAND_ACCEPTED,
        request_id=request_id,
        kind=KIND_NEXT_ISSUE,
        acknowledgement=ACK_ACCEPTED,
        registry=registry,
    )
    for old_id, old_kind in superseded:
        _emit_command_event(
            connection,
            event_type=_events_next.EVENT_COMMAND_RESOLVED,
            request_id=old_id,
            kind=old_kind,
            acknowledgement=ACK_SUPERSEDED,
            registry=registry,
        )
    return (record, False, None)


def resolve_next_command(
    connection: sqlite3.Connection,
    request_id: str,
    acknowledgement: str,
    *,
    detail: str,
    registry: Redactable = None,
) -> bool:
    """Resolve one accepted priority to ``completed``/``not_fulfilled``.

    Called in the Claim boundary's own write transaction (or the
    finalization transaction that settles a failed read-back), emitting
    ``command_resolved`` beside the Attempt write. Returns ``True``
    where an ``accepted`` row was resolved, ``False`` where nothing
    pended (already superseded or resolved).
    """
    stored_kind = _command_kind(connection, request_id, KIND_NEXT_ISSUE)
    now = _utc_now_iso()
    cursor = connection.execute(
        "UPDATE operator_commands SET acknowledgement = ?, detail = ?,"
        " updated_at = ? WHERE request_id = ? AND acknowledgement = ?",
        (acknowledgement, detail, now, request_id, ACK_ACCEPTED),
    )
    if cursor.rowcount == 0:
        return False
    try:
        from coding_agent.operator import events as _events_resolve

        _emit_command_event(
            connection,
            event_type=_events_resolve.EVENT_COMMAND_RESOLVED,
            request_id=request_id,
            kind=stored_kind,
            acknowledgement=acknowledgement,
            registry=registry,
        )
    except Exception:
        pass
    return True


def _attempt_issue_number(attempt_id: str) -> int | None:
    """The issue half of an Attempt id, or ``None`` where malformed.

    Accepts the step-workflow ``<workflow>#<issue>/<n>`` (for example
    ``implement#44/1``) and the legacy ``#<issue>/<n>``.
    """
    head, slash, _tail = attempt_id.partition("/")
    if not slash or "#" not in head:
        return None
    try:
        number = int(head.rsplit("#", 1)[1])
    except ValueError:
        return None
    return number if number > 0 else None


def _has_attempt_pin(connection: sqlite3.Connection, attempt_id: str) -> bool:
    """Whether ``attempt_id`` recorded its Claim pins (the confirm path).

    Pins are recorded only after a successful Claim read-back, just before
    the Claim confirms — so a pin marks an Attempt whose Claim confirmed
    (or died after its read-back held). A failed read-back never pins.
    """
    try:
        row = connection.execute(
            "SELECT 1 FROM attempt_pins WHERE attempt_id = ? LIMIT 1",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _parse_ledger_timestamp(value: str) -> datetime | None:
    """Parse a stored ledger timestamp to an aware datetime, if possible.

    Stored timestamps are RFC 3339 ``Z`` or ISO-8601 with an offset;
    naive values read as UTC. ``None`` where unparseable.
    """
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _pending_newer_than_attempt(
    connection: sqlite3.Connection, pending: CommandRecord, attempt_id: str
) -> bool:
    """Whether the pending priority was committed after the Attempt opened.

    Pins plus a newer pending priority mean the Claim confirmed (resolving
    its own priority at the Claim boundary) and a later ``next`` committed
    after: the next Claim boundary tries it, so finalization leaves it
    alone. Pins plus an older-or-equal pending priority are the crash
    window — the pins landed but the confirm write never did — so the
    Attempt finalizes without a confirmed Claim and the priority resolves
    below. Missing or unparseable timestamps fail open to ``False``
    (resolve).
    """
    try:
        row = connection.execute(
            "SELECT created_at FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row[0] is None:
        return False
    attempt_at = _parse_ledger_timestamp(str(row[0]))
    pending_at = _parse_ledger_timestamp(pending.created_at)
    if attempt_at is None or pending_at is None:
        return False
    return pending_at > attempt_at


def resolve_matching_next_on_finalize(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    registry: Redactable = None,
) -> None:
    """Resolve a pending priority whose issue just finalized (#252).

    A priority resolved by a failed Claim read-back is resolved here, in
    the same write transaction that marks the Attempt ``finished`` or
    ``released``: the read-back opened the Attempt before the tracker
    read failed, so no earlier transaction could resolve it. Nothing
    pending, or a pending priority for another issue, is left alone. An
    Attempt that recorded its Claim pins confirmed its Claim (or died
    after its read-back held), so its own priority already resolved at
    the Claim boundary — but only where the pending priority is newer
    than the Attempt (committed after it opened). Pins with an
    older-or-equal pending priority are the crash window: the pins
    landed but the confirm write never did, so the priority resolves
    below like any Attempt that finalized without a confirmed Claim.
    """
    pending = get_pending_next_command(connection)
    if pending is None:
        return
    raw_issue = pending.arguments.get("issue")
    try:
        priority_issue = int(str(raw_issue)) if not isinstance(raw_issue, bool) else 0
    except (TypeError, ValueError):
        return
    if priority_issue < 1:
        return
    if _attempt_issue_number(attempt_id) != priority_issue:
        return
    if _has_attempt_pin(connection, attempt_id) and _pending_newer_than_attempt(
        connection, pending, attempt_id
    ):
        # The Claim confirmed (resolving its own priority) and a newer
        # priority committed after it: the next Claim boundary tries the
        # newer one, so finalization must not resolve it.
        return
    resolve_next_command(
        connection,
        pending.request_id,
        ACK_NOT_FULFILLED,
        detail=(
            f"priority for #{priority_issue} not fulfilled:"
            f" attempt {attempt_id} finalized without a confirmed claim"
        ),
        registry=registry,
    )


def complete_pending_stop(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    registry: Redactable = None,
) -> None:
    """Complete the pending ``stop`` where one is recorded.

    Called in the same write transaction that moves an Attempt to
    ``finished`` or ``released``. Where no pending stop is recorded,
    or it is already resolved, this is a no-op. Intake stays closed:
    completing the drain does not reopen it. Completing emits
    ``command_resolved`` in the same transaction. A stop plan is
    preserved: a ``released`` Claim never counts toward it.
    """
    intake = read_intake(connection)
    pending = intake.get("pending_stop")
    if not isinstance(pending, dict) or not pending.get("request_id"):
        return
    pending_id = str(pending["request_id"])
    moment = now if now is not None else _utc_now_iso()
    connection.execute(
        "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
        " WHERE request_id = ? AND acknowledgement = ?",
        (ACK_COMPLETED, moment, pending_id, ACK_ACCEPTED),
    )
    try:
        from coding_agent.operator import events as _events_complete

        _emit_command_event(
            connection,
            event_type=_events_complete.EVENT_COMMAND_RESOLVED,
            request_id=pending_id,
            kind=KIND_STOP,
            acknowledgement=ACK_COMPLETED,
            registry=registry,
        )
    except Exception:
        pass
    plan = intake.get("stop_plan")
    if isinstance(plan, dict) and plan.get("request_id"):
        _set_intake(
            connection,
            state=INTAKE_CLOSED,
            pending_request_id=None,
            pending_sequence=None,
            stop_plan_request_id=str(plan["request_id"]),
            stop_plan_sequence=int(plan.get("sequence") or 0),
            stop_plan_remaining=int(plan.get("remaining") or 0),
            stop_plan_counted=int(plan.get("counted") or 0),
            now=moment,
        )
    else:
        _set_intake(
            connection,
            state=INTAKE_CLOSED,
            pending_request_id=None,
            pending_sequence=None,
            now=moment,
        )


def _has_recorded_outcome(connection: sqlite3.Connection, attempt_id: str) -> bool:
    """Whether ``attempt_id`` has a recorded Terminal Outcome row."""
    try:
        row = connection.execute(
            "SELECT 1 FROM outcomes WHERE attempt_id = ? LIMIT 1",
            (attempt_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def count_stop_plan(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    now: str | None = None,
    registry: Redactable = None,
) -> None:
    """Count one finished Attempt with an outcome toward the stop plan.

    Called in the same write transaction that moves an Attempt to
    ``finished``. A ``released`` Claim never reaches here, and a
    ``finished`` without a recorded Terminal Outcome does not count.
    At zero the plan completes: Intake closes and the command becomes
    ``completed``, emitting ``command_resolved`` in the same
    transaction. Otherwise Intake stays open with the decremented
    ``remaining`` and incremented ``counted``.
    """
    intake = read_intake(connection)
    plan = intake.get("stop_plan")
    if not isinstance(plan, dict) or not plan.get("request_id"):
        return
    if not _has_recorded_outcome(connection, attempt_id):
        return
    try:
        remaining = int(plan.get("remaining") or 0)
    except (TypeError, ValueError):
        return
    try:
        counted = int(plan.get("counted") or 0)
    except (TypeError, ValueError):
        counted = 0
    if remaining <= 0:
        return
    moment = now if now is not None else _utc_now_iso()
    plan_id = str(plan["request_id"])
    new_remaining = remaining - 1
    new_counted = counted + 1
    if new_remaining <= 0:
        connection.execute(
            "UPDATE operator_commands SET acknowledgement = ?, detail = ?,"
            " updated_at = ? WHERE request_id = ? AND acknowledgement = ?",
            (
                ACK_COMPLETED,
                stop_after_completed_detail(new_counted),
                moment,
                plan_id,
                ACK_ACCEPTED,
            ),
        )
        try:
            from coding_agent.operator import events as _events_count

            _emit_command_event(
                connection,
                event_type=_events_count.EVENT_COMMAND_RESOLVED,
                request_id=plan_id,
                kind=KIND_STOP_AFTER,
                acknowledgement=ACK_COMPLETED,
                registry=registry,
            )
        except Exception:
            pass
        _set_intake(
            connection,
            state=INTAKE_CLOSED,
            pending_request_id=None,
            pending_sequence=None,
            now=moment,
        )
    else:
        _set_intake(
            connection,
            state=INTAKE_OPEN,
            pending_request_id=None,
            pending_sequence=None,
            stop_plan_request_id=plan_id,
            stop_plan_sequence=int(plan.get("sequence") or 0),
            stop_plan_remaining=new_remaining,
            stop_plan_counted=new_counted,
            now=moment,
        )


def reconcile_accepted(
    connection: sqlite3.Connection, *, registry: Redactable = None
) -> None:
    """Resolve ``accepted`` commands left by an earlier process.

    Every ``accepted`` command whose Attempt is already final becomes
    ``completed`` before the first Claim boundary, so nothing stays
    ``accepted`` forever across a restart. A pending stop whose
    Attempt is still unfinished stays pending: the drain still holds.
    A pending ``next`` priority stays ``accepted`` until a Claim
    boundary tries it: future sweeps still have to run, so
    reconciliation never completes it. A stop plan stays ``accepted``
    until its countdown reaches zero: future Attempts still have to
    run, so reconciliation never completes it. The caller holds the
    write lock (a single write transaction).
    """
    intake = read_intake(connection)
    plan = intake.get("stop_plan")
    plan_id: str | None = None
    if isinstance(plan, dict) and plan.get("request_id"):
        plan_id = str(plan["request_id"])
    if has_unfinished_attempt(connection):
        return
    now = _utc_now_iso()
    try:
        rows = connection.execute(
            "SELECT request_id, kind FROM operator_commands"
            " WHERE acknowledgement = ?",
            (ACK_ACCEPTED,),
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        candidate_id = str(row[0])
        kind_guess = str(row[1]) if len(row) > 1 else KIND_STOP
        if kind_guess in (KIND_NEXT_ISSUE, KIND_NEXT_LEGACY):
            continue
        if plan_id is not None and candidate_id == plan_id:
            continue
        connection.execute(
            "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
            " WHERE request_id = ? AND acknowledgement = ?",
            (ACK_COMPLETED, now, candidate_id, ACK_ACCEPTED),
        )
        try:
            from coding_agent.operator import events as _events_reconcile

            _emit_command_event(
                connection,
                event_type=_events_reconcile.EVENT_COMMAND_RESOLVED,
                request_id=candidate_id,
                kind=kind_guess,
                acknowledgement=ACK_COMPLETED,
                registry=registry,
            )
        except Exception:
            pass
    intake = read_intake(connection)
    pending = intake.get("pending_stop")
    plan = intake.get("stop_plan")
    if isinstance(pending, dict) and pending.get("request_id"):
        if isinstance(plan, dict) and plan.get("request_id"):
            _set_intake(
                connection,
                state=INTAKE_CLOSED,
                pending_request_id=None,
                pending_sequence=None,
                stop_plan_request_id=str(plan["request_id"]),
                stop_plan_sequence=int(plan.get("sequence") or 0),
                stop_plan_remaining=int(plan.get("remaining") or 0),
                stop_plan_counted=int(plan.get("counted") or 0),
                now=now,
            )
        else:
            _set_intake(
                connection,
                state=INTAKE_CLOSED,
                pending_request_id=None,
                pending_sequence=None,
                now=now,
            )


def open_ledger_connection(ledger_path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
    """A private connection to the ledger file for operator work."""
    connection = sqlite3.connect(str(ledger_path), timeout=timeout_seconds)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    try:
        connection.execute("PRAGMA synchronous=FULL")
    except sqlite3.Error:
        pass
    try:
        connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
    except sqlite3.Error:
        pass
    ensure_operator_tables(connection)
    return connection
