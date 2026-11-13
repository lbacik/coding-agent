"""Contract Events: the single producer for operator status events (issue #250).

Contract: adoption step 4 of the operator contract (#245). One producer for
every Contract Event, with redaction through the existing registry and text
capped at 300 characters. The newest 50 events of the current process are
shown as ``status.recent_events``. This slice emits ``process_started``,
``startup_check``, ``ready``, ``halted``, ``command_accepted`` and
``command_resolved``. Every entry carries the contract's ``at``/``level``/
``event``/``text`` base plus the extra fields its event requires
(spec section 10, ``v1.0.4`` ``contract-event`` schema): ``agent_version``
for ``process_started``, ``check``/``result`` for ``startup_check``,
``reason`` for ``halted``, ``request_id``/``kind``/``acknowledgement`` for
command events, and ``attempt_id`` (plus ``issue``/``phase``/``outcome``)
for Attempt-scoped events.

Storage is the Run Ledger's SQLite file (separate ``operator_events`` table,
``CREATE TABLE IF NOT EXISTS``, fresh volumes only, no migration), so the
listener thread and the Worker thread share the same ring without sharing a
connection: every insert carries the current process's ``process_started_at``
and the read filters on it. Pruning keeps at most 50 rows per process, which
is the ring buffer. All writes run in the caller's transaction (the caller
commits); reads open their own short connection.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from coding_agent.operator.contract import to_z, utc_now_z
from coding_agent.runtime.redaction import Redactable, registry_from

MAX_EVENT_TEXT_LEN = 300
"""Contract cap: every event's text is at most 300 characters."""

MAX_EVENTS_PER_PROCESS = 50
"""Ring size: ``status.recent_events`` holds at most the newest 50."""

EVENT_PROCESS_STARTED = "process_started"
EVENT_STARTUP_CHECK = "startup_check"
EVENT_READY = "ready"
EVENT_HALTED = "halted"
EVENT_COMMAND_ACCEPTED = "command_accepted"
EVENT_COMMAND_RESOLVED = "command_resolved"

LEVEL_DEBUG = "debug"
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

EVENT_LEVELS: tuple[str, ...] = (LEVEL_DEBUG, LEVEL_INFO, LEVEL_WARNING, LEVEL_ERROR)
"""Every Contract Event ``level`` (spec section 10)."""

STARTUP_CHECK_RESULTS: tuple[str, ...] = ("started", "ok", "skipped", "failed")
"""Every ``startup_check`` event ``result`` (spec section 10)."""

ENSURE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS operator_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_started_at TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    attempt_id TEXT,
    phase TEXT,
    issue INTEGER,
    request_id TEXT,
    kind TEXT,
    acknowledgement TEXT,
    outcome TEXT,
    reason TEXT,
    check_name TEXT,
    result TEXT,
    agent_version TEXT
)
"""

_EVENT_COLUMNS: tuple[str, ...] = (
    "level",
    "attempt_id",
    "phase",
    "issue",
    "request_id",
    "kind",
    "acknowledgement",
    "outcome",
    "reason",
    "check_name",
    "result",
    "agent_version",
)

_EVENT_COLUMN_TYPES: dict[str, str] = {
    "level": "TEXT",
    "attempt_id": "TEXT",
    "phase": "TEXT",
    "issue": "INTEGER",
    "request_id": "TEXT",
    "kind": "TEXT",
    "acknowledgement": "TEXT",
    "outcome": "TEXT",
    "reason": "TEXT",
    "check_name": "TEXT",
    "result": "TEXT",
    "agent_version": "TEXT",
}

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_operator_events_process
ON operator_events (process_started_at, id)
"""


def _utc_now_iso() -> str:
    return utc_now_z()


def ensure_operator_events_table(connection: sqlite3.Connection) -> None:
    """Create the events table and its index where absent (fresh volumes)."""
    connection.execute(ENSURE_EVENTS_SQL)
    try:
        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(operator_events)").fetchall()
        }
    except sqlite3.Error:
        existing = set()
    for column in _EVENT_COLUMNS:
        if column not in existing:
            try:
                connection.execute(
                    f"ALTER TABLE operator_events ADD COLUMN {column}"
                    f" {_EVENT_COLUMN_TYPES[column]}"
                )
            except sqlite3.Error:
                pass
    try:
        connection.execute(CREATE_INDEX_SQL)
    except sqlite3.Error:
        pass


def cap_text(text: str, limit: int = MAX_EVENT_TEXT_LEN) -> str:
    """Cap event text to the contract limit (no marker, never silently over)."""
    if len(text) > limit:
        return text[:limit]
    return text


def _default_level(event_type: str) -> str:
    """The level for an event whose emitter passed none."""
    if event_type == EVENT_HALTED:
        return LEVEL_ERROR
    return LEVEL_INFO


def emit_contract_event(
    connection: sqlite3.Connection,
    *,
    process_started_at: str,
    event_type: str,
    text: str,
    registry: Redactable = None,
    created_at: str | None = None,
    level: str | None = None,
    attempt_id: str | None = None,
    phase: str | None = None,
    issue: int | None = None,
    request_id: str | None = None,
    kind: str | None = None,
    acknowledgement: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
    check: str | None = None,
    result: str | None = None,
    agent_version: str | None = None,
) -> dict[str, Any]:
    """Record one Contract Event in the caller's transaction (no commit).

    Single producer: every Contract Event in this process funnels through
    here. ``text`` is redacted through the existing registry and capped at
    300 characters before it is stored. The caller holds the write lock and
    commits: the listener's mutation path commits the event with its command,
    startup halts commit the halt row with the ``halted`` event together.
    Pruning to the newest 50 per process runs in the same call.

    The extra keyword arguments are the contract's per-event fields (spec
    section 10): ``agent_version`` for ``process_started``, ``check`` and
    ``result`` for ``startup_check``, ``reason`` for ``halted``,
    ``request_id``/``kind``/``acknowledgement`` for command events, and
    ``attempt_id`` (plus ``issue``/``phase``/``outcome``) for Attempt-scoped
    events. ``level`` defaults to ``error`` for ``halted`` and ``info``
    otherwise.
    """
    redacted = registry_from(registry).redact(text)
    capped = cap_text(redacted)
    moment = to_z(created_at) if created_at is not None else utc_now_z()
    if moment is None:
        moment = utc_now_z()
    normalized_process = to_z(process_started_at)
    if normalized_process is None:
        normalized_process = process_started_at
    resolved_level = level if level in EVENT_LEVELS else _default_level(event_type)
    ensure_operator_events_table(connection)
    connection.execute(
        "INSERT INTO operator_events"
        " (process_started_at, type, text, created_at, level,"
        "  attempt_id, phase, issue, request_id, kind, acknowledgement,"
        "  outcome, reason, check_name, result, agent_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            normalized_process,
            event_type,
            capped,
            moment,
            resolved_level,
            attempt_id,
            phase,
            issue,
            request_id,
            kind,
            acknowledgement,
            outcome,
            reason,
            check,
            result,
            agent_version,
        ),
    )
    connection.execute(
        "DELETE FROM operator_events WHERE process_started_at = ? AND id NOT IN"
        " (SELECT id FROM operator_events WHERE process_started_at = ?"
        "  ORDER BY id DESC LIMIT ?)",
        (normalized_process, normalized_process, MAX_EVENTS_PER_PROCESS),
    )
    record: dict[str, Any] = {
        "at": moment,
        "level": resolved_level,
        "event": event_type,
        "text": capped,
        "process_started_at": normalized_process,
    }
    if attempt_id is not None:
        record["attempt_id"] = attempt_id
    if phase is not None:
        record["phase"] = phase
    if issue is not None:
        record["issue"] = issue
    if request_id is not None:
        record["request_id"] = request_id
    if kind is not None:
        record["kind"] = kind
    if acknowledgement is not None:
        record["acknowledgement"] = acknowledgement
    if outcome is not None:
        record["outcome"] = outcome
    if reason is not None:
        record["reason"] = reason
    if check is not None:
        record["check"] = check
    if result is not None:
        record["result"] = result
    if agent_version is not None:
        record["agent_version"] = agent_version
    return record


def _event_from_row(row: Any) -> dict[str, Any]:
    """One contract event document from an ``operator_events`` row tuple.

    Column order is the ``list_recent_contract_events`` select: ``type``,
    ``text``, ``created_at``, ``level``, then the optional extras
    (``attempt_id`` through ``agent_version``, with ``check_name`` reading
    back as ``check``).
    """
    record: dict[str, Any] = {
        "at": to_z(str(row[2])),
        "level": str(row[3]) if len(row) > 3 and row[3] else LEVEL_INFO,
        "event": str(row[0]),
        "text": str(row[1]),
    }
    names = (
        "attempt_id",
        "phase",
        "issue",
        "request_id",
        "kind",
        "acknowledgement",
        "outcome",
        "reason",
        "check",
        "result",
        "agent_version",
    )
    for index, name in enumerate(names, start=4):
        if index >= len(row):
            break
        raw = row[index]
        if raw is None:
            continue
        if name == "issue":
            try:
                record[name] = int(raw) if not isinstance(raw, bool) else raw
            except (TypeError, ValueError):
                continue
        else:
            record[name] = str(raw)
    return record


def list_recent_contract_events(
    connection: sqlite3.Connection,
    *,
    process_started_at: str,
    limit: int = MAX_EVENTS_PER_PROCESS,
) -> list[dict[str, Any]]:
    """Newest ``limit`` events of this process, oldest first (contract order).

    At most 50, every one already redacted and capped at write time. Oldest
    first matches ``recent_commands`` (newest last); "newest per contract
    order" is the tail of this list. Every entry carries the contract's
    ``at``/``level``/``event``/``text`` base plus the extra fields its event
    requires (spec section 10).
    """
    try:
        ensure_operator_events_table(connection)
    except sqlite3.Error:
        return []
    normalized = to_z(process_started_at)
    candidates = (
        (process_started_at, normalized)
        if normalized is not None and normalized != process_started_at
        else (process_started_at, process_started_at)
    )
    try:
        rows = connection.execute(
            "SELECT type, text, created_at, level, attempt_id, phase, issue,"
            " request_id, kind, acknowledgement, outcome, reason, check_name,"
            " result, agent_version FROM operator_events"
            " WHERE process_started_at IN (?, ?) ORDER BY id DESC LIMIT ?",
            (candidates[0], candidates[1], limit),
        ).fetchall()
    except sqlite3.Error:
        try:
            rows = connection.execute(
                "SELECT type, text, created_at FROM operator_events"
                " WHERE process_started_at IN (?, ?) ORDER BY id DESC LIMIT ?",
                (candidates[0], candidates[1], limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        rows = [(*row, *[None] * 12) for row in rows]
    # DESC for the newest slice, then reverse to contract (chronological) order.
    ordered = list(reversed(rows))
    return [_event_from_row(row) for row in ordered]
