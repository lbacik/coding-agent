"""The Run Ledger's ``attempts`` table across schema generations.

Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Run Ledger
decides whose assignment it is: ``assigned_by_us`` records whether the
Agent Identity's assignment is the Worker's own) and §17 (one SQLite file,
WAL with ``synchronous=FULL``, ``IF NOT EXISTS``, no schema version — a
ledger file opened by an earlier slice gains new columns on open).

MVP row ids: C-OUT-1.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from coding_agent.ledger.store import RunLedger


def test_open_attempt_round_trips_assigned_by_us(tmp_path: Path) -> None:
    """§5: the ledger records whose assignment the Attempt's is."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "#7/1",
            effective_configuration={"repo": "example/test", "issue": 7},
            assigned_by_us=True,
        )

        assert ledger.get_attempt("#7/1").assigned_by_us is True
    finally:
        ledger.close()


def test_older_ledger_file_without_the_column_still_opens(tmp_path: Path) -> None:
    """§17: a pre-M6 ledger gains ``assigned_by_us`` on open, reading as ours-never.

    The column is nullable, so rows written before M6 read back as "not
    recorded as ours" and the Worker's assignment is never removed for
    them (C-OUT-1) instead of failing the ``get_attempt`` read.
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
                json.dumps({"repo": "example/test", "issue": 7}, sort_keys=True),
                "digest",
                "2026-11-03T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    ledger = RunLedger(path)
    try:
        assert ledger.get_attempt("#7/1").assigned_by_us is None
    finally:
        ledger.close()
