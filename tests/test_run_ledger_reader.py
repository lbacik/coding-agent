"""Behaviour tests for the read-only Run Ledger reader (issue #435).

The reader opens its own connection in SQLite ``mode=ro``: it can never
write (a write attempt raises ``sqlite3.OperationalError``) and merely
opening it leaves the ledger file's schema and data unchanged. A ledger
written by an older version — one that lacks the ``step_executions``,
``step_effects`` and ``step_transitions`` tables — reads back with empty
step results through :func:`open_offline_ledger`, never with an error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coding_agent.ledger.offline import open_offline_ledger
from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.ledger.store import RunLedger

STEP_TABLES = ("step_executions", "step_effects", "step_transitions")


def _snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    """The schema plus row counts: the reader must leave both unchanged."""
    schema = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    counts: dict[str, int] = {}
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]
    for table in tables:
        counts[str(table)] = int(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        )
    return {"schema": [tuple(row) for row in schema], "counts": counts}


def _build_small_ledger(ledger_path: Path) -> None:
    """One open Attempt with a step execution, then close the writer."""
    ledger = RunLedger(ledger_path)
    try:
        ledger.open_attempt(
            "implement#1/1", effective_configuration={"workflow": {}}
        )
        ledger.open_step_execution(
            "implement#1/1", "plan", 1, entry_from_step=None, entry_result=None
        )
    finally:
        ledger.close()


def test_reader_cannot_write_and_leaves_file_unchanged(tmp_path: Path) -> None:
    """A write through the reader raises; schema and data are unchanged."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    _build_small_ledger(ledger_path)
    probe = sqlite3.connect(ledger_path)
    try:
        before = _snapshot(probe)
    finally:
        probe.close()

    with RunLedgerReader(ledger_path) as reader:
        assert reader.get_attempt("implement#1/1") is not None
        assert len(reader.list_step_executions("implement#1/1")) == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.connection.execute(
                "UPDATE attempts SET state = 'finished'"
                " WHERE attempt_id = 'implement#1/1'"
            )

    probe = sqlite3.connect(ledger_path)
    try:
        after = _snapshot(probe)
    finally:
        probe.close()
    assert after == before


def _drop_step_tables(ledger_path: Path) -> None:
    """Simulate a volume written by an older version (no step tables)."""
    connection = sqlite3.connect(ledger_path)
    try:
        for table in STEP_TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
    finally:
        connection.close()


def _assert_steps_empty(reader: RunLedgerReader, attempt_id: str) -> None:
    """The Attempt reads back with empty step results, not an error."""
    assert reader.get_attempt(attempt_id) is not None
    assert reader.list_step_executions(attempt_id) == ()
    assert reader.list_step_effects(attempt_id) == ()
    assert reader.list_step_transitions(attempt_id) == ()


def test_reader_on_legacy_ledger_reads_empty_steps(tmp_path: Path) -> None:
    """A ledger without step tables reads back empty, not an error."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    _build_small_ledger(ledger_path)
    _drop_step_tables(ledger_path)

    with RunLedgerReader(ledger_path) as reader:
        _assert_steps_empty(reader, "implement#1/1")


def test_offline_ledger_on_legacy_volume_reads_empty_steps(
    tmp_path: Path,
) -> None:
    """Offline Read completes the schema: empty newer tables, no error."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    _build_small_ledger(ledger_path)
    _drop_step_tables(ledger_path)

    with open_offline_ledger(ledger_path) as reader:
        _assert_steps_empty(reader, "implement#1/1")
