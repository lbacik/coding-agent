"""The operator store: instance metadata and Intake in the ledger file.

Contract: adoption step 4 of the operator contract (#245, #248, #249, #250).
The store lives in its own module, in the Run Ledger's SQLite file, with
separate tables and its own connection. It opens right after
configuration validation and the volume lock. At every start it writes
the instance metadata: Target Repository, backend and agent version.

Like :class:`RunLedger` the tables are created with
``CREATE TABLE IF NOT EXISTS`` and there is no schema version. The Intake
table holds the durable Intake state (``open``/``closed`` plus the
pending ``stop``; absent means open) and the Operator Commands table
holds every committed command; rows are never deleted. A fresh volume
starts with Intake open; a restart keeps the recorded state.

Issue #250 adds the durable Halt (``operator_halt``, one row, replaced on
every halt: reason, detail, time) and ensures the Contract Event table
(``operator_events``) exists beside it. The instance row is replaced every
start but the Halt survives: the next start reports it as
``instance.last_halt``.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from coding_agent.operator.commands import ensure_operator_tables
from coding_agent.runtime.redaction import Redactable, registry_from

OPERATOR_BACKEND = "coding-agent"
"""The backend name ``identity`` reports (contract major 1)."""


def agent_version() -> str:
    """The agent version ``identity`` reports: the package version."""
    try:
        return metadata.version("coding-agent")
    except metadata.PackageNotFoundError:
        return "0.1.0"


ENSURE_HALT_SQL = """
CREATE TABLE IF NOT EXISTS operator_halt (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    occurred_at TEXT NOT NULL
)
"""


class OperatorStore:
    """Instance metadata and Intake rows in the ledger SQLite file.

    Owns a private connection: the Worker thread writes through this
    handle while the control listener reads through its own per-request
    connections, so a slow Worker-side call can never block observation.
    """

    def __init__(self, ledger_path: Path) -> None:
        """Open the store on the ledger file, creating its tables."""
        self.path = ledger_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_instance (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                target_repository TEXT NOT NULL,
                backend TEXT NOT NULL,
                agent_version TEXT NOT NULL,
                process_started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_operator_tables(self._connection)
        self._connection.execute(ENSURE_HALT_SQL)
        try:
            from coding_agent.operator.events import ensure_operator_events_table

            ensure_operator_events_table(self._connection)
        except Exception:
            pass
        self._connection.commit()

    def close(self) -> None:
        """Close the store's private connection."""
        with self._lock:
            self._connection.close()

    def write_instance_metadata(
        self,
        *,
        target_repository: str,
        backend: str = OPERATOR_BACKEND,
        version: str | None = None,
        process_started_at: datetime | None = None,
    ) -> None:
        """Record this start's instance metadata (one row, replaced each start)."""
        resolved_version = version if version is not None else agent_version()
        started = process_started_at or datetime.now(timezone.utc)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO operator_instance
                    (id, target_repository, backend, agent_version,
                     process_started_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    target_repository = excluded.target_repository,
                    backend = excluded.backend,
                    agent_version = excluded.agent_version,
                    process_started_at = excluded.process_started_at,
                    updated_at = excluded.updated_at
                """,
                (
                    target_repository,
                    backend,
                    resolved_version,
                    started.isoformat(),
                    now,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO operator_intake (id, state, updated_at)
                VALUES (1, 'open', ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now,),
            )
            self._connection.commit()

    def record_halt(
        self,
        *,
        reason: str,
        detail: str,
        occurred_at: datetime | None = None,
        registry: Redactable = None,
    ) -> dict[str, Any]:
        """Record the durable Halt for this process (one row, replaced).

        Every non-zero exit the Worker decides on after taking the volume
        lock records here before exiting: the reason, the redacted detail
        (capped at 300 characters like every Contract Event text) and the
        time. A restart keeps the row, reporting it as
        ``instance.last_halt``; only the next halt replaces it.
        """
        from coding_agent.operator.events import MAX_EVENT_TEXT_LEN

        moment = occurred_at or datetime.now(timezone.utc)
        redacted = registry_from(registry).redact(detail)
        if len(redacted) > MAX_EVENT_TEXT_LEN:
            redacted = redacted[:MAX_EVENT_TEXT_LEN]
        with self._lock:
            self._connection.execute(ENSURE_HALT_SQL)
            self._connection.execute(
                """
                INSERT INTO operator_halt (id, reason, detail, occurred_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    reason = excluded.reason,
                    detail = excluded.detail,
                    occurred_at = excluded.occurred_at
                """,
                (reason, redacted, moment.isoformat()),
            )
            try:
                from coding_agent.operator.events import (
                    EVENT_HALTED,
                    emit_contract_event,
                    ensure_operator_events_table,
                )

                ensure_operator_events_table(self._connection)
                process_started = moment.isoformat()
                try:
                    inst = self._connection.execute(
                        "SELECT process_started_at FROM operator_instance"
                        " WHERE id = 1"
                    ).fetchone()
                    if inst is not None and inst[0]:
                        process_started = str(inst[0])
                except Exception:
                    pass
                emit_contract_event(
                    self._connection,
                    process_started_at=process_started,
                    event_type=EVENT_HALTED,
                    text=f"halted {reason}: {redacted}",
                    registry=registry,
                )
            except Exception:
                pass
            self._connection.commit()
        return {
            "reason": reason,
            "detail": redacted,
            "occurred_at": moment.isoformat(),
            "time": moment.isoformat(),
        }

    def get_last_halt(self) -> dict[str, Any] | None:
        """The last recorded Halt, if any (survives restarts)."""
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT reason, detail, occurred_at FROM operator_halt"
                    " WHERE id = 1"
                ).fetchone()
            except sqlite3.Error:
                return None
        if row is None:
            return None
        occurred = str(row[2])
        return {
            "reason": str(row[0]),
            "detail": str(row[1]),
            "occurred_at": occurred,
            "time": occurred,
        }

    def table_names(self) -> tuple[str, ...]:
        """Table names in this file, for diagnostics (not a contract surface)."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)
