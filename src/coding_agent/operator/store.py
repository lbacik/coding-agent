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
from collections.abc import Mapping
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

ENSURE_CAPABILITY_REPORT_SQL = """
CREATE TABLE IF NOT EXISTS provider_capability_report (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pin_key TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    report_json TEXT NOT NULL
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
        self._connection.execute(ENSURE_CAPABILITY_REPORT_SQL)
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
        from coding_agent.operator.contract import to_z, utc_now_z

        resolved_version = version if version is not None else agent_version()
        started = process_started_at or datetime.now(timezone.utc)
        started_z = to_z(started)
        assert started_z is not None
        now = utc_now_z()
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
                    started_z,
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

    def emit_startup_event(
        self,
        *,
        event_type: str,
        text: str,
        registry: Redactable = None,
        check: str | None = None,
        result: str | None = None,
        agent_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Emit one startup Contract Event in the store's own transaction.

        Contract ``v1.0.4`` extras: ``process_started`` carries
        ``agent_version``, ``startup_check`` carries ``check`` and
        ``result``. Reads this start's ``process_started_at`` from the
        instance row and commits the event with it in one transaction
        under the store's own lock. With no ``process_started_at``
        recorded nothing is emitted and nothing is raised; other
        failures propagate to the caller's best-effort wrapper.
        """
        from coding_agent.operator.events import emit_contract_event

        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT process_started_at FROM operator_instance WHERE id = 1"
                ).fetchone()
                process_started = str(row[0]) if row is not None and row[0] else ""
            except sqlite3.Error:
                return None
            if not process_started:
                return None
            record = emit_contract_event(
                self._connection,
                process_started_at=process_started,
                event_type=event_type,
                text=text,
                registry=registry,
                check=check,
                result=result,
                agent_version=agent_version,
            )
            self._connection.commit()
            try:
                from coding_agent.operator.runlog import print_contract_event

                print_contract_event(record)
            except Exception:
                pass
        return record

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
        from coding_agent.operator.contract import to_z, utc_now_z
        from coding_agent.operator.events import MAX_EVENT_TEXT_LEN

        moment = occurred_at or datetime.now(timezone.utc)
        moment_z = to_z(moment)
        assert moment_z is not None
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
                (reason, redacted, moment_z),
            )
            halt_record: dict[str, Any] | None = None
            try:
                from coding_agent.operator.events import (
                    EVENT_HALTED,
                    LEVEL_ERROR,
                    emit_contract_event,
                    ensure_operator_events_table,
                )

                ensure_operator_events_table(self._connection)
                process_started = moment_z
                try:
                    inst = self._connection.execute(
                        "SELECT process_started_at FROM operator_instance"
                        " WHERE id = 1"
                    ).fetchone()
                    if inst is not None and inst[0]:
                        process_started = str(inst[0])
                except Exception:
                    pass
                halt_record = dict(
                    emit_contract_event(
                        self._connection,
                        process_started_at=process_started,
                        event_type=EVENT_HALTED,
                        text=f"halted {reason}: {redacted}",
                        registry=registry,
                        level=LEVEL_ERROR,
                        reason=reason,
                    )
                )
            except Exception:
                halt_record = None
            try:
                self._connection.commit()
            except Exception:
                halt_record = None
            if halt_record is not None:
                try:
                    from coding_agent.operator.runlog import print_contract_event

                    print_contract_event(halt_record)
                except Exception:
                    pass
        return {
            "reason": reason,
            "detail": redacted,
            "at": moment_z,
        }

    def get_last_halt(self) -> dict[str, Any] | None:
        """The last recorded Halt, if any (survives restarts).

        Contract shape (``v1.0.4``): ``{reason, detail, at}`` with ``at``
        in RFC 3339 UTC; stored rows from older volumes (``+00:00``)
        convert on read so replies never carry them.
        """
        with self._lock:
            return read_last_halt(self._connection)

    def record_provider_capability_report(
        self,
        report: Mapping[str, Any],
        *,
        registry: Redactable = None,
    ) -> dict[str, Any] | None:
        """Persist the latest Provider Capability Report (one row, replaced).

        Each run of the Provider Capability Assertion produces one report;
        only the latest is kept. Redacted through the secret registry and
        capped at write time (like every Contract Event text), so a secret
        in a provider error never reaches ``agentctl`` output. A missing
        pin key or timestamp refuses nothing and stores nothing (``None``).
        """
        import json

        from coding_agent.provider.capability import redact_report

        redacted = redact_report(report, registry)
        pin_key = redacted.get("pin_key")
        ran_at = redacted.get("ran_at")
        if not isinstance(pin_key, str) or not pin_key:
            return None
        if not isinstance(ran_at, str) or not ran_at:
            return None
        stored = json.dumps(redacted, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._connection.execute(ENSURE_CAPABILITY_REPORT_SQL)
            self._connection.execute(
                """
                INSERT INTO provider_capability_report
                    (id, pin_key, ran_at, report_json)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pin_key = excluded.pin_key,
                    ran_at = excluded.ran_at,
                    report_json = excluded.report_json
                """,
                (pin_key, ran_at, stored),
            )
            self._connection.commit()
        return redacted

    def get_provider_capability_report(self) -> dict[str, Any] | None:
        """The latest recorded Provider Capability Report, if any.

        ``None`` where no assertion has run yet. The only reader besides
        the store's writer is :func:`read_provider_capability_report`,
        which the listener and Offline Read call on their read-only
        connection instead of raw SQL.
        """
        with self._lock:
            return read_provider_capability_report(self._connection)

    def table_names(self) -> tuple[str, ...]:
        """Table names in this file, for diagnostics (not a contract surface)."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)


def read_instance_row(
    connection: sqlite3.Connection,
) -> dict[str, str] | None:
    """This start's instance metadata through ``connection``, if recorded.

    The only reader of ``operator_instance`` besides
    :meth:`OperatorStore.write_instance_metadata`: the listener and Offline
    Read call this on their read-only connection instead of raw SQL.
    ``None`` where the file or row does not exist yet.
    """
    try:
        row = connection.execute(
            "SELECT target_repository, backend, agent_version,"
            " process_started_at FROM operator_instance WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return {
            "target_repository": str(row[0]),
            "backend": str(row[1]),
            "agent_version": str(row[2]),
            "process_started_at": str(row[3]),
        }
    except (IndexError, TypeError, ValueError):
        return None


def read_instance_repository(
    connection: sqlite3.Connection,
) -> str | None:
    """This start's Target Repository, or ``None`` where not recorded."""
    try:
        row = connection.execute(
            "SELECT target_repository FROM operator_instance WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def read_process_started_at(
    connection: sqlite3.Connection,
) -> str | None:
    """This start's ``process_started_at``, or ``None`` where not recorded."""
    try:
        row = connection.execute(
            "SELECT process_started_at FROM operator_instance WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def read_last_halt(
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    """The durable Halt, if any (survives restarts as ``instance.last_halt``).

    The only reader of ``operator_halt`` besides the store's writers:
    the listener and Offline Read call this on their read-only connection
    instead of raw SQL.
    """
    from coding_agent.operator.contract import to_z

    try:
        row = connection.execute(
            "SELECT reason, detail, occurred_at FROM operator_halt WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    occurred = to_z(str(row[2]))
    if occurred is None:
        return None
    return {
        "reason": str(row[0]),
        "detail": str(row[1]),
        "at": occurred,
    }


def read_provider_capability_report(
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    """The latest Provider Capability Report, if any (survives restarts).

    The only reader of ``provider_capability_report`` besides the store's
    writer: the listener and Offline Read call this on their read-only
    connection instead of raw SQL. ``None`` where no assertion has run yet.
    Stored rows hold the report exactly as ``status.provider_capability``
    carries it (redacted and capped at write time); a row that does not
    decode to an object reads as absent rather than raising.
    """
    import json

    try:
        row = connection.execute(
            "SELECT report_json FROM provider_capability_report WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    try:
        record = json.loads(str(row[0]))
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    return {str(key): value for key, value in record.items()}


def open_read_connection(
    ledger_path: Path, *, timeout_seconds: float = 2.0
) -> sqlite3.Connection:
    """A fresh read-only connection on ``ledger_path`` (``mode=ro``).

    For operator-table reads that need no full :class:`OperatorStore`:
    the caller passes the connection to :func:`read_instance_row` or
    :func:`read_last_halt` and closes it. Never writes.
    """
    return sqlite3.connect(
        f"file:{ledger_path}?mode=ro",
        uri=True,
        timeout=timeout_seconds,
        check_same_thread=False,
    )
