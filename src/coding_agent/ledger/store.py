"""The Run Ledger's SQLite store: events, Attempts, effects and Terminal Outcomes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (the review
snapshot commits the current tree and pushes the branch before either
reviewer starts), §13 (the write sequence: pass the in-tool Gate, record
the intent keyed by ``(attempt_id, write_kind)``, look the remote ref up,
push only when it is absent or behind, record the receipt; a kind that can
repeat, the review-snapshot push, carries its ordinal), §17 ("Storage": one
SQLite file on the state volume, opened in WAL mode with
``synchronous=FULL``; tables created with ``CREATE TABLE IF NOT EXISTS`` and
no schema version), §14 ("Recording before publishing": the Terminal
Outcome row exists before ``terminal_writes`` performs any tracker call),
§18 (one branch per Attempt, ``agent/<issue>/<n>-<slug>``; author and
committer are the id-based noreply address; every commit carries the
``Attempt: #<issue>/<n>`` trailer) and §5 (the Claim records the Fingerprint
beside the Effective Configuration; L1-6 reads both back).

MVP row ids: REC-OUT-1, REC-OUT-2, C-GRF-1, L1-6, L3-DEL-19, L3-DEL-20,
L3-ERR-9, L3-ERR-10.

M1 tables are ``run_ledger_events`` (progress, budget and snapshot
references, carried over from ``runtime.progress`` unchanged), ``attempts``
(one row per ``#<issue>/<n>`` with its lifecycle state and its Effective
Configuration) and ``outcomes`` (one row per Attempt with the Terminal
Outcome, its reason and the writes it calls for; a Delivery Intent is an
outcome row carrying the pull-request fields, filled in by M4). Until M5 the
Effective Configuration column holds the CLI-derived settings the Attempt
was opened with and their SHA-256 digest; M5 replaces them with the
normalised Workflow Configuration. ``effects`` arrives in M3, keyed by
``(attempt_id, write_kind)`` with a status of ``intent`` or ``receipt``.
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


EFFECT_STATUS_INTENT = "intent"
EFFECT_STATUS_RECEIPT = "receipt"

EffectStatus = Literal["intent", "receipt"]


@dataclass(frozen=True)
class EffectRecord:
    """One ``effects`` row: the intent or receipt of one external write.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (every external
    write follows the write sequence; the key is ``(attempt_id, write_kind)``,
    unique within an Attempt; a kind that can repeat, the review-snapshot
    push, carries its ordinal; the payload's digest is kept for audit only).

    MVP row ids: L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10.
    """

    attempt_id: str
    write_kind: str
    status: str
    remote_id: str | None
    payload: dict[str, object]
    payload_digest: str


def digest_effect_payload(payload: Mapping[str, object]) -> str:
    """Digest an effect payload's canonical JSON bytes for audit (§13)."""
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS effects (
                attempt_id TEXT NOT NULL,
                write_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                remote_id TEXT,
                payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (attempt_id, write_kind)
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

    def record_effect_intent(
        self,
        attempt_id: str,
        write_kind: str,
        *,
        remote_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> EffectRecord:
        """Record the intent of one external write (§13, step 2).

        Keyed by ``(attempt_id, write_kind)``; a repeat with the same key
        returns the existing row, so re-entering an intent pushes nothing
        new. A row already at ``receipt`` stays a receipt: an intent never
        overwrites a receipt.
        """
        existing = self.get_effect(attempt_id, write_kind)
        if existing is not None:
            return existing
        body = dict(payload or {})
        digest = digest_effect_payload(body)
        self._connection.execute(
            """
            INSERT INTO effects
                (attempt_id, write_kind, status, remote_id, payload,
                 payload_digest, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                write_kind,
                EFFECT_STATUS_INTENT,
                remote_id,
                json.dumps(body, sort_keys=True),
                digest,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._connection.commit()
        record = self.get_effect(attempt_id, write_kind)
        assert record is not None
        return record

    def record_effect_receipt(
        self,
        attempt_id: str,
        write_kind: str,
        *,
        remote_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> EffectRecord:
        """Record the receipt of one external write (§13, step 5).

        Where an intent row exists it transitions to ``receipt`` carrying
        the remote sha; where a receipt already exists it is returned
        unchanged, so re-entering a receipt pushes nothing. Where no row
        exists a receipt row is inserted, so a remote that already holds
        our sha is recognised as ours.
        """
        existing = self.get_effect(attempt_id, write_kind)
        if existing is not None and existing.status == EFFECT_STATUS_RECEIPT:
            return existing
        body = dict(payload or {}) if payload is not None else dict(existing.payload) if existing is not None else {}
        digest = digest_effect_payload(body)
        kept_remote = remote_id if remote_id is not None else (existing.remote_id if existing is not None else None)
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO effects
                    (attempt_id, write_kind, status, remote_id, payload,
                     payload_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    write_kind,
                    EFFECT_STATUS_RECEIPT,
                    kept_remote,
                    json.dumps(body, sort_keys=True),
                    digest,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        else:
            self._connection.execute(
                """
                UPDATE effects
                SET status = ?, remote_id = ?, payload = ?, payload_digest = ?
                WHERE attempt_id = ? AND write_kind = ?
                """,
                (
                    EFFECT_STATUS_RECEIPT,
                    kept_remote,
                    json.dumps(body, sort_keys=True),
                    digest,
                    attempt_id,
                    write_kind,
                ),
            )
        self._connection.commit()
        record = self.get_effect(attempt_id, write_kind)
        assert record is not None
        return record

    def get_effect(self, attempt_id: str, write_kind: str) -> EffectRecord | None:
        """Return one ``effects`` row by its ``(attempt_id, write_kind)`` key."""
        row = self._connection.execute(
            """
            SELECT attempt_id, write_kind, status, remote_id, payload, payload_digest
            FROM effects
            WHERE attempt_id = ? AND write_kind = ?
            """,
            (attempt_id, write_kind),
        ).fetchone()
        if row is None:
            return None
        return EffectRecord(
            attempt_id=str(row[0]),
            write_kind=str(row[1]),
            status=str(row[2]),
            remote_id=str(row[3]) if row[3] is not None else None,
            payload=json.loads(str(row[4])),
            payload_digest=str(row[5]),
        )

    def list_effects(self, attempt_id: str) -> tuple[EffectRecord, ...]:
        """Return every ``effects`` row for one Attempt, ordered by kind."""
        rows = self._connection.execute(
            """
            SELECT attempt_id, write_kind, status, remote_id, payload, payload_digest
            FROM effects
            WHERE attempt_id = ?
            ORDER BY write_kind
            """,
            (attempt_id,),
        ).fetchall()
        return tuple(
            EffectRecord(
                attempt_id=str(row[0]),
                write_kind=str(row[1]),
                status=str(row[2]),
                remote_id=str(row[3]) if row[3] is not None else None,
                payload=json.loads(str(row[4])),
                payload_digest=str(row[5]),
            )
            for row in rows
        )
