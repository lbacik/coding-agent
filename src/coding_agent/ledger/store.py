"""The Run Ledger's SQLite store: events, Attempts and Terminal Outcomes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §17 ("Storage": one
SQLite file on the state volume, opened in WAL mode with
``synchronous=FULL``; tables created with ``CREATE TABLE IF NOT EXISTS`` and
no schema version), §14 ("Recording before publishing": the Terminal
Outcome row exists before ``terminal_writes`` performs any tracker call)
and §5 (the Claim records the Fingerprint beside the Effective
Configuration; L1-6 reads both back).

MVP row ids: REC-OUT-1, REC-OUT-2, C-GRF-1, L1-6.

M1 tables are ``run_ledger_events`` (progress, budget and snapshot
references, carried over from ``runtime.progress`` unchanged), ``attempts``
(one row per ``#<issue>/<n>`` with its lifecycle state and its Effective
Configuration) and ``outcomes`` (one row per Attempt with the Terminal
Outcome, its reason and the writes it calls for; a Delivery Intent is an
outcome row carrying the pull-request fields, filled in by M4). Until M5 the
Effective Configuration column holds the CLI-derived settings the Attempt
was opened with and their SHA-256 digest; M5 replaces them with the
normalised Workflow Configuration. ``effects`` arrives in M3.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from coding_agent.runtime.ceilings import LoopCeilings, UsageTotals

OPEN = "open"
CLAIM_CONFIRMED = "claim-confirmed"
OUTCOME_RECORDED = "outcome-recorded"
FINISHED = "finished"
RELEASED = "released"

AttemptState = Literal["open", "claim-confirmed", "outcome-recorded", "finished", "released"]

PROGRESS_ACCEPTANCE = "acceptance_criterion"
PROGRESS_SEAM = "seam_set"
PROGRESS_READINESS = "readiness"
PROGRESS_DIFF = "diff"
PROGRESS_TARGETED_RESULT = "targeted_result"
PROGRESS_VALIDATION_RESULT = "validation_result"


@dataclass(frozen=True)
class BudgetSnapshot:
    """The Attempt-wide meter values at one observable ledger boundary."""

    elapsed_seconds: float
    usage: UsageTotals

    def remaining(self, ceilings: LoopCeilings) -> dict[str, float | int | None]:
        """Return remaining capacity without inventing a limit for an unset meter."""
        return {
            "wall_clock_seconds": (
                max(0.0, ceilings.max_wall_clock_seconds - self.elapsed_seconds)
                if ceilings.max_wall_clock_seconds is not None
                else None
            ),
            "cost_usd": (
                max(0.0, ceilings.max_cost_usd - self.usage.cost_usd)
                if ceilings.max_cost_usd is not None
                else None
            ),
            "effective_tokens": (
                max(0, ceilings.max_effective_tokens - self.usage.effective_tokens)
                if ceilings.max_effective_tokens is not None
                else None
            ),
            "tool_calls": (
                max(0, ceilings.max_tool_calls - self.usage.tool_calls)
                if ceilings.max_tool_calls is not None
                else None
            ),
        }


def digest_effective_configuration(settings: Mapping[str, object]) -> str:
    """Digest the Effective Configuration bytes the ledger pins per Attempt."""
    canonical = json.dumps(dict(settings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerRecord:
    """One durable Run Ledger event, including the budget at its boundary."""

    id: int
    attempt_id: str
    kind: str
    detail: str | None
    artifact_ref: str | None
    diff_ref: str | None
    remaining_budget: dict[str, float | int | None]
    payload: dict[str, object]


@dataclass(frozen=True)
class AttemptRecord:
    """One ``attempts`` row: the Attempt's lifecycle state and pinning.

    Contract §5: the Fingerprint recorded at Claim time travels with the
    Effective Configuration (L1-6 reads both back).
    """

    attempt_id: str
    state: str
    effective_configuration: dict[str, object]
    effective_configuration_digest: str
    fingerprint: str | None = None


@dataclass(frozen=True)
class OutcomeRecord:
    """One ``outcomes`` row: the recorded Terminal Outcome and its reason."""

    attempt_id: str
    outcome: str
    reason: str


class RunLedger:
    """One SQLite file holding the Worker's events, Attempts and outcomes.

    Opened in WAL mode with ``synchronous=FULL`` (§17). Every table is
    created with ``CREATE TABLE IF NOT EXISTS`` and there is no schema
    version: re-opening an existing file creates nothing new.
    """

    def __init__(self, path: Path, *, ceilings: LoopCeilings | None = None) -> None:
        self.path = path
        self.ceilings = ceilings or LoopCeilings()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_ledger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT,
                artifact_ref TEXT,
                diff_ref TEXT,
                remaining_budget TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                effective_configuration TEXT NOT NULL,
                effective_configuration_digest TEXT NOT NULL,
                fingerprint TEXT,
                base_revision TEXT,
                agent_identity_id INTEGER,
                assigned_by_us INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                attempt_id TEXT PRIMARY KEY,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def journal_mode(self) -> str:
        row = self._connection.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        return str(row[0]).lower()

    def synchronous(self) -> str:
        row = self._connection.execute("PRAGMA synchronous").fetchone()
        assert row is not None
        return str(row[0])

    def table_names(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def open_attempt(
        self,
        attempt_id: str,
        *,
        effective_configuration: Mapping[str, object],
        fingerprint: str | None = None,
        base_revision: str | None = None,
        agent_identity_id: int | None = None,
        assigned_by_us: bool | None = None,
    ) -> AttemptRecord:
        """Open ``#<issue>/<n>`` in state ``open`` with its pinned configuration.

        Until M5 the configuration is the CLI-derived settings the Attempt
        was opened with; the digest is over their canonical JSON bytes.
        """
        settings = dict(effective_configuration)
        digest = digest_effective_configuration(settings)
        self._connection.execute(
            """
            INSERT INTO attempts
                (attempt_id, state, effective_configuration,
                 effective_configuration_digest, fingerprint, base_revision,
                 agent_identity_id, assigned_by_us, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                OPEN,
                json.dumps(settings, sort_keys=True),
                digest,
                fingerprint,
                base_revision,
                agent_identity_id,
                None if assigned_by_us is None else int(assigned_by_us),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._connection.commit()
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        row = self._connection.execute(
            """
            SELECT attempt_id, state, effective_configuration,
                   effective_configuration_digest, fingerprint
            FROM attempts WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        assert row is not None, f"no attempts row for {attempt_id}"
        return AttemptRecord(
            attempt_id=str(row[0]),
            state=str(row[1]),
            effective_configuration=json.loads(str(row[2])),
            effective_configuration_digest=str(row[3]),
            fingerprint=str(row[4]) if row[4] is not None else None,
        )

    def set_attempt_state(self, attempt_id: str, state: AttemptState) -> None:
        self._connection.execute(
            "UPDATE attempts SET state = ? WHERE attempt_id = ?",
            (state, attempt_id),
        )
        self._connection.commit()

    def record_terminal_outcome(
        self, attempt_id: str, *, outcome: str, reason: str
    ) -> OutcomeRecord:
        """Record the Terminal Outcome **before** any tracker write (§14).

        Moves the Attempt to ``outcome-recorded`` and inserts its ``outcomes``
        row in the same transaction discipline as every later slice: recovery
        finishes a recorded outcome and never re-decides it. At most one
        outcome row exists per Attempt; a second call leaves the first in
        place and returns it.
        """
        existing = self._connection.execute(
            "SELECT attempt_id, outcome, reason FROM outcomes WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if existing is not None:
            return OutcomeRecord(
                attempt_id=str(existing[0]),
                outcome=str(existing[1]),
                reason=str(existing[2]),
            )
        self._connection.execute(
            """
            INSERT INTO outcomes (attempt_id, outcome, reason, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                attempt_id,
                outcome,
                reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._connection.execute(
            "UPDATE attempts SET state = ? WHERE attempt_id = ?",
            (OUTCOME_RECORDED, attempt_id),
        )
        self._connection.commit()
        return OutcomeRecord(attempt_id=attempt_id, outcome=outcome, reason=reason)

    def get_outcome(self, attempt_id: str) -> OutcomeRecord | None:
        row = self._connection.execute(
            "SELECT attempt_id, outcome, reason FROM outcomes WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return OutcomeRecord(
            attempt_id=str(row[0]), outcome=str(row[1]), reason=str(row[2])
        )

    def record(
        self,
        attempt_id: str,
        kind: str,
        snapshot: BudgetSnapshot,
        *,
        detail: str | None = None,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> LedgerRecord:
        remaining = snapshot.remaining(self.ceilings)
        event_payload = dict(payload or {})
        self._connection.execute(
            """
            INSERT INTO run_ledger_events
                (attempt_id, kind, detail, artifact_ref, diff_ref,
                 remaining_budget, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                kind,
                detail,
                artifact_ref,
                diff_ref,
                json.dumps(remaining, sort_keys=True),
                json.dumps(event_payload, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._connection.commit()
        row = self._connection.execute("SELECT last_insert_rowid()").fetchone()
        assert row is not None
        return LedgerRecord(
            id=int(row[0]),
            attempt_id=attempt_id,
            kind=kind,
            detail=detail,
            artifact_ref=artifact_ref,
            diff_ref=diff_ref,
            remaining_budget=remaining,
            payload=event_payload,
        )

    def record_progress(
        self,
        attempt_id: str,
        progress_kind: str,
        snapshot: BudgetSnapshot,
        *,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
    ) -> LedgerRecord:
        return self.record(
            attempt_id,
            "progress_event",
            snapshot,
            artifact_ref=artifact_ref,
            diff_ref=diff_ref,
            payload={"event": "progress_event", "progress_kind": progress_kind},
        )

    def record_outcome(
        self,
        attempt_id: str,
        outcome: str,
        snapshot: BudgetSnapshot,
        *,
        detail: str,
        stop_reason: str | None = None,
        next_action: str | None = None,
        finalization_result: str | None = None,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
    ) -> LedgerRecord:
        return self.record(
            attempt_id,
            "terminal_outcome",
            snapshot,
            detail=detail,
            artifact_ref=artifact_ref,
            diff_ref=diff_ref,
            payload={
                "outcome": outcome,
                "stop_reason": stop_reason,
                "next_action": next_action,
                "finalization_result": finalization_result,
            },
        )

    def records(self, attempt_id: str) -> tuple[LedgerRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT id, attempt_id, kind, detail, artifact_ref, diff_ref,
                   remaining_budget, payload
            FROM run_ledger_events
            WHERE attempt_id = ?
            ORDER BY id
            """,
            (attempt_id,),
        ).fetchall()
        return tuple(
            LedgerRecord(
                id=int(row[0]),
                attempt_id=str(row[1]),
                kind=str(row[2]),
                detail=row[3],
                artifact_ref=row[4],
                diff_ref=row[5],
                remaining_budget=json.loads(row[6]),
                payload=json.loads(row[7]),
            )
            for row in rows
        )
