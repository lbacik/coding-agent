"""Durable Intake and Operator Commands (adoption step 4, #249).

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
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INTAKE_OPEN = "open"
INTAKE_CLOSED = "closed"

ACK_ACCEPTED = "accepted"
ACK_COMPLETED = "completed"
ACK_SUPERSEDED = "superseded"

KIND_STOP = "stop"
KIND_RESUME = "resume"

STOP_ACCEPTED_DETAIL = "finish the active attempt, then close intake"
STOP_COMPLETED_DETAIL = "intake closed"
RESUME_REOPENED_DETAIL = "intake reopened"
RESUME_ALREADY_OPEN_DETAIL = "intake already open"

ENSURE_INTAKE_SQL = """
CREATE TABLE IF NOT EXISTS operator_intake (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'open',
    pending_stop_request_id TEXT,
    pending_stop_sequence INTEGER,
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
    return datetime.now(timezone.utc).isoformat()


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
) -> None:
    """Emit one command Contract Event in the caller's transaction."""
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
        """The contract ``CommandRecord`` shape (§7.5)."""
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "arguments": dict(self.arguments),
            "repository": self.repository,
            "acknowledgement": self.acknowledgement,
            "detail": self.detail,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
    connection.commit()


def read_intake(connection: sqlite3.Connection) -> dict[str, Any]:
    """The Intake row, or open-with-no-pending where absent."""
    try:
        row = connection.execute(
            "SELECT state, pending_stop_request_id, pending_stop_sequence"
            " FROM operator_intake WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return {"state": INTAKE_OPEN, "pending_stop": None}
    if row is None:
        return {"state": INTAKE_OPEN, "pending_stop": None}
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
    return {"state": state, "pending_stop": pending}


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
) -> None:
    """Write the single Intake row (the caller holds the write lock)."""
    connection.execute(
        "INSERT INTO operator_intake"
        " (id, state, pending_stop_request_id, pending_stop_sequence, updated_at)"
        " VALUES (1, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " state = excluded.state,"
        " pending_stop_request_id = excluded.pending_stop_request_id,"
        " pending_stop_sequence = excluded.pending_stop_sequence,"
        " updated_at = excluded.updated_at",
        (state, pending_request_id, pending_sequence, now),
    )


def commit_stop(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    expect_repo: str,
    target_repository: str,
) -> tuple[CommandRecord | None, bool, dict[str, Any] | None]:
    """Commit a ``stop`` in this connection's write transaction.

    Returns ``(record, replayed, error)``: on success ``record`` is the
    stored command and ``error`` is ``None``; on a guard failure
    ``record`` is ``None`` and ``error`` carries ``code`` plus the
    fields the listener needs (``repository`` for a mismatch). The
    caller opens ``BEGIN IMMEDIATE`` and commits on success. Nothing
    is stored for a repository mismatch.
    """
    if expect_repo.lower() != target_repository.lower():
        return (
            None,
            False,
            {"code": "repository_mismatch", "repository": target_repository},
        )
    now = _utc_now_iso()
    arguments: dict[str, Any] = {}
    digest = payload_digest(KIND_STOP, arguments)
    existing = get_command(connection, request_id)
    if existing is not None:
        if existing.payload_digest != digest:
            return (None, False, {"code": "payload_mismatch"})
        return (existing, True, None)
    intake = read_intake(connection)
    unfinished = has_unfinished_attempt(connection)
    if not unfinished:
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
        )
        _emit_command_event(
            connection,
            event_type=_events_mod.EVENT_COMMAND_RESOLVED,
            request_id=request_id,
            kind=KIND_STOP,
            acknowledgement=ACK_COMPLETED,
        )
        _set_intake(
            connection,
            state=INTAKE_CLOSED,
            pending_request_id=None,
            pending_sequence=None,
            now=now,
        )
        return (record, False, None)
    # Active Attempt: drain. A pending stop is superseded by the newer one.
    pending = intake.get("pending_stop")
    superseded_id: str | None = None
    superseded_resume_id: str | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        old_id = str(pending["request_id"])
        connection.execute(
            "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
            " WHERE request_id = ? AND acknowledgement = ?",
            (ACK_SUPERSEDED, now, old_id, ACK_ACCEPTED),
        )
        superseded_resume_id = old_id
        superseded_id = old_id
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
    )
    if superseded_id is not None:
        _emit_command_event(
            connection,
            event_type=_events_mod2.EVENT_COMMAND_RESOLVED,
            request_id=superseded_id,
            kind=KIND_STOP,
            acknowledgement=ACK_SUPERSEDED,
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
    if intake.get("state") == INTAKE_OPEN and pending is None:
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
        )
        _emit_command_event(
            connection,
            event_type=_events_resume.EVENT_COMMAND_RESOLVED,
            request_id=request_id,
            kind=KIND_RESUME,
            acknowledgement=ACK_COMPLETED,
        )
        return (record, False, None)
    superseded_resume_id: str | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        old_id = str(pending["request_id"])
        connection.execute(
            "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
            " WHERE request_id = ? AND acknowledgement = ?",
            (ACK_SUPERSEDED, now, old_id, ACK_ACCEPTED),
        )
        superseded_resume_id = old_id
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
    )
    _emit_command_event(
        connection,
        event_type=_events_resume2.EVENT_COMMAND_RESOLVED,
        request_id=request_id,
        kind=KIND_RESUME,
        acknowledgement=ACK_COMPLETED,
    )
    if superseded_resume_id is not None:
        _emit_command_event(
            connection,
            event_type=_events_resume2.EVENT_COMMAND_RESOLVED,
            request_id=superseded_resume_id,
            kind=KIND_STOP,
            acknowledgement=ACK_SUPERSEDED,
        )
    _set_intake(
        connection,
        state=INTAKE_OPEN,
        pending_request_id=None,
        pending_sequence=None,
        now=now,
    )
    return (record, False, None)


def complete_pending_stop(connection: sqlite3.Connection, *, now: str | None = None) -> None:
    """Complete the pending ``stop`` where one is recorded.

    Called in the same write transaction that moves an Attempt to
    ``finished`` or ``released``. Where no pending stop is recorded,
    or it is already resolved, this is a no-op. Intake stays closed:
    completing the drain does not reopen it. Completing emits
    ``command_resolved`` in the same transaction.
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


def reconcile_accepted(connection: sqlite3.Connection) -> None:
    """Resolve ``accepted`` commands left by an earlier process.

    Every ``accepted`` command whose Attempt is already final becomes
    ``completed`` before the first Claim boundary, so nothing stays
    ``accepted`` forever across a restart. A pending stop whose
    Attempt is still unfinished stays pending: the drain still holds.
    The caller holds the write lock (a single write transaction).
    """
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
        pending_id = str(row[0])
        connection.execute(
            "UPDATE operator_commands SET acknowledgement = ?, updated_at = ?"
            " WHERE request_id = ? AND acknowledgement = ?",
            (ACK_COMPLETED, now, pending_id, ACK_ACCEPTED),
        )
        try:
            from coding_agent.operator import events as _events_reconcile

            kind_guess = str(row[1]) if len(row) > 1 else KIND_STOP
            _emit_command_event(
                connection,
                event_type=_events_reconcile.EVENT_COMMAND_RESOLVED,
                request_id=pending_id,
                kind=kind_guess,
                acknowledgement=ACK_COMPLETED,
            )
        except Exception:
            pass
    intake = read_intake(connection)
    pending = intake.get("pending_stop")
    if isinstance(pending, dict) and pending.get("request_id"):
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
