"""The operator store: instance metadata and Intake in the ledger file.

Contract: adoption step 4 of the operator contract (#245, #248). The store
lives in its own module, in the Run Ledger's SQLite file, with separate
tables and its own connection. It opens right after configuration
validation and the volume lock. At every start it writes the instance
metadata: Target Repository, backend and agent version.

Like :class:`RunLedger` the tables are created with
``CREATE TABLE IF NOT EXISTS`` and there is no schema version. The Intake
table exists so ``status`` reports the durable Intake state; this slice
always writes ``open`` (stop/resume arrive later) and a fresh volume starts
open. Rows are never deleted.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

OPERATOR_BACKEND = "coding-agent"
"""The backend name ``identity`` reports (contract major 1)."""


def agent_version() -> str:
    """The agent version ``identity`` reports: the package version."""
    try:
        return metadata.version("coding-agent")
    except metadata.PackageNotFoundError:
        return "0.1.0"


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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_intake (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL DEFAULT 'open',
                updated_at TEXT NOT NULL
            )
            """
        )
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

    def table_names(self) -> tuple[str, ...]:
        """Table names in this file, for diagnostics (not a contract surface)."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)
