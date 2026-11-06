"""Contract Events: the single producer for operator status events (issue #250).

Contract: adoption step 4 of the operator contract (#245). One producer for
every Contract Event, with redaction through the existing registry and text
capped at 300 characters. The newest 50 events of the current process are
shown as ``status.recent_events``. This slice emits ``process_started``,
``startup_check``, ``ready``, ``halted``, ``command_accepted`` and
``command_resolved``. Stdout prose stays unchanged and outside the contract.

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
from datetime import datetime, timezone
from typing import Any

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

ENSURE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS operator_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_started_at TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_operator_events_process
ON operator_events (process_started_at, id)
"""


def ensure_operator_events_table(connection: sqlite3.Connection) -> None:
    """Create the events table and its index where absent (fresh volumes)."""
    connection.execute(ENSURE_EVENTS_SQL)
    try:
        connection.execute(CREATE_INDEX_SQL)
    except sqlite3.Error:
        pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cap_text(text: str, limit: int = MAX_EVENT_TEXT_LEN) -> str:
    """Cap event text to the contract limit (no marker, never silently over)."""
    if len(text) <= limit:
        return text
    return text[:limit]


def emit_contract_event(
    connection: sqlite3.Connection,
    *,
    process_started_at: str,
    event_type: str,
    text: str,
    registry: Redactable = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Record one Contract Event in the caller's transaction (no commit).

    Single producer: every Contract Event in this process funnels through
    here. ``text`` is redacted through the existing registry and capped at
    300 characters before it is stored. The caller holds the write lock and
    commits: the listener's mutation path commits the event with its command,
    startup halts commit the halt row with the ``halted`` event together.
    Pruning to the newest 50 per process runs in the same call.
    """
    redacted = registry_from(registry).redact(text)
    capped = cap_text(redacted)
    moment = created_at or _utc_now_iso()
    ensure_operator_events_table(connection)
    connection.execute(
        "INSERT INTO operator_events"
        " (process_started_at, type, text, created_at)"
        " VALUES (?, ?, ?, ?)",
        (process_started_at, event_type, capped, moment),
    )
    connection.execute(
        "DELETE FROM operator_events WHERE process_started_at = ? AND id NOT IN"
        " (SELECT id FROM operator_events WHERE process_started_at = ?"
        "  ORDER BY id DESC LIMIT ?)",
        (process_started_at, process_started_at, MAX_EVENTS_PER_PROCESS),
    )
    return {
        "type": event_type,
        "text": capped,
        "at": moment,
        "process_started_at": process_started_at,
    }


def list_recent_contract_events(
    connection: sqlite3.Connection,
    *,
    process_started_at: str,
    limit: int = MAX_EVENTS_PER_PROCESS,
) -> list[dict[str, Any]]:
    """Newest ``limit`` events of this process, oldest first (contract order).

    At most 50, every one already redacted and capped at write time. Oldest
    first matches ``recent_commands`` (newest last); "newest per contract
    order" is the tail of this list.
    """
    try:
        ensure_operator_events_table(connection)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT type, text, created_at FROM operator_events"
            " WHERE process_started_at = ? ORDER BY id DESC LIMIT ?",
            (process_started_at, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    # DESC for the newest slice, then reverse to contract (chronological) order.
    ordered = list(reversed(rows))
    return [
        {"type": str(row[0]), "text": str(row[1]), "at": str(row[2])}
        for row in ordered
    ]
