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


def test_open_attempt_round_trips_claim_pinning(tmp_path: Path) -> None:
    """§5: the Claim records the Fingerprint, Base Revision and Agent Identity."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "#7/1",
            effective_configuration={"repo": "example/test", "issue": 7},
            fingerprint="fp",
            base_revision="b" * 40,
            agent_identity_id=4242,
        )

        record = ledger.get_attempt("#7/1")

        assert record.fingerprint == "fp"
        assert record.base_revision == "b" * 40
        assert record.agent_identity_id == 4242
    finally:
        ledger.close()


def test_next_attempt_number_starts_at_one_and_never_reuses(
    tmp_path: Path,
) -> None:
    """§5: ``n`` is one more than the highest held for the issue, released included."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        assert ledger.next_attempt_number(7) == 1

        ledger.open_attempt(
            "#7/1", effective_configuration={"repo": "example/test", "issue": 7}
        )
        assert ledger.next_attempt_number(7) == 2
        assert ledger.next_attempt_number(8) == 1

        ledger.set_attempt_state("#7/1", "released")
        assert ledger.next_attempt_number(7) == 2
    finally:
        ledger.close()


def test_set_assigned_by_us_records_the_claim_assignment(
    tmp_path: Path,
) -> None:
    """§5: the assignment is recorded as ours only after the Worker assigns."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "#7/1", effective_configuration={"repo": "example/test", "issue": 7}
        )
        assert ledger.get_attempt("#7/1").assigned_by_us is None

        ledger.set_assigned_by_us("#7/1", True)

        assert ledger.get_attempt("#7/1").assigned_by_us is True
    finally:
        ledger.close()


def test_has_unfinished_attempt_gates_intake(tmp_path: Path) -> None:
    """§3: intake waits while an Attempt is neither ``finished`` nor ``released``."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        assert ledger.has_unfinished_attempt() is False

        ledger.open_attempt(
            "#7/1", effective_configuration={"repo": "example/test", "issue": 7}
        )
        assert ledger.has_unfinished_attempt() is True

        ledger.set_attempt_state("#7/1", "claim-confirmed")
        assert ledger.has_unfinished_attempt() is True

        ledger.set_attempt_state("#7/1", "released")
        assert ledger.has_unfinished_attempt() is False

        ledger.open_attempt(
            "#7/2", effective_configuration={"repo": "example/test", "issue": 7}
        )
        ledger.set_attempt_state("#7/2", "finished")
        assert ledger.has_unfinished_attempt() is False
    finally:
        ledger.close()
