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
Outcome row exists before ``terminal_writes`` performs any tracker call; the
row carries the outcome, its reason, the rendered comment and pull-request
bodies and the list of writes it calls for, so a retried or recovered write
sends the same bytes), §15 (the pull-request and terminal comment contents)
and §18 (one branch per Attempt, ``agent/<issue>/<n>-<slug>``; author and
committer are the id-based noreply address; every commit carries the
``Attempt: #<issue>/<n>`` trailer) and §5 (the Claim records the Fingerprint
beside the Effective Configuration; L1-6 reads both back).

MVP row ids: REC-OUT-1, REC-OUT-2, C-GRF-1, L1-6, L3-DEL-19, L3-DEL-20,
L3-ERR-9, L3-ERR-10, P-DEL-1, P-DEL-10, L3-DEL-5, L3-DEL-14, L3-DEL-21,
L3-REV-10, P-DEL-5, L2-17, L2-18, C-OUT-1, W-PIN-2, P-CLM-1, P-CLM-2,
REC-CLM-1.

M1 tables are ``run_ledger_events`` (progress, budget and snapshot
references, carried over from ``runtime.progress`` unchanged), ``attempts``
(one row per ``#<issue>/<n>`` with its lifecycle state and its Effective
Configuration) and ``outcomes`` (one row per Attempt with the Terminal
Outcome, its reason, the rendered comment and pull-request bodies and the
writes it calls for; a Delivery Intent is an outcome row carrying the
pull-request fields). The Effective
Configuration column holds the CLI-derived settings the Attempt was opened
with and their SHA-256 digest, or — on the ``implement --config --workflow``
path — the normalised Effective Configuration record
(``workflow.effective.effective_configuration_record``) and its digest
(contract §2.5, W-PIN-2). ``effects`` arrives in M3, keyed by
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
    Effective Configuration (L1-6 reads both back), and ``assigned_by_us``
    records whether the Agent Identity's assignment is the Worker's own —
    only then does ``terminal_writes`` remove it (C-OUT-1).
    """

    attempt_id: str
    state: str
    effective_configuration: dict[str, object]
    effective_configuration_digest: str
    fingerprint: str | None = None
    base_revision: str | None = None
    agent_identity_id: int | None = None
    assigned_by_us: bool | None = None


@dataclass(frozen=True)
class OutcomeRecord:
    """One ``outcomes`` row: the recorded Terminal Outcome and its reason.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §14 ("Recording
    before publishing": the row carries the outcome, its reason, the rendered
    comment and pull-request bodies and the list of writes it calls for).
    ``pull_request_body`` is the rendered §15 body for a ready pull request
    and an empty string where the Attempt opens none; ``writes`` lists the
    §14 terminal writes in the order they are performed. A Delivery Intent is
    an ``outcomes`` row with outcome ``delivered`` carrying the pull-request
    fields.

    MVP row ids: REC-OUT-1, REC-OUT-2, P-DEL-1, C-OUT-1.
    """

    attempt_id: str
    outcome: str
    reason: str
    pull_request_body: str = ""
    terminal_comment_body: str = ""
    writes: tuple[str, ...] = ()


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
                pull_request_body TEXT NOT NULL DEFAULT '',
                terminal_comment_body TEXT NOT NULL DEFAULT '',
                writes TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """
        )
        self._ensure_outcomes_columns()
        self._ensure_attempts_columns()
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

    def _ensure_outcomes_columns(self) -> None:
        """Add the M4 body columns to ``outcomes`` rows written before M4.

        Tables are created with ``CREATE TABLE IF NOT EXISTS`` and there is no
        schema version (§17): a ledger file opened by an earlier slice holds an
        ``outcomes`` table with only ``(attempt_id, outcome, reason,
        created_at)``. The new columns carry defaults, so old rows read back
        as an empty pull-request body and no writes.
        """
        existing = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(outcomes)").fetchall()
        }
        for column, default in (
            ("pull_request_body", "''"),
            ("terminal_comment_body", "''"),
            ("writes", "'[]'"),
        ):
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE outcomes ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}"
                )
        self._connection.commit()

    def _ensure_attempts_columns(self) -> None:
        """Add the M6 ``assigned_by_us`` column to ``attempts`` rows written before M6.

        Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Run
        Ledger decides whose assignment it is) and §17 (no schema version: a
        ledger file opened by an earlier slice holds an ``attempts`` table
        without this column). The column is nullable, so old rows read back
        as "not recorded as ours" and the Worker's assignment is never
        removed for them (C-OUT-1).
        """
        existing = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(attempts)").fetchall()
        }
        if "assigned_by_us" not in existing:
            self._connection.execute("ALTER TABLE attempts ADD COLUMN assigned_by_us INTEGER")
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

        The configuration is what the Attempt was opened with: the
        CLI-derived settings on the ``implement`` path without ``--config``,
        or the Effective Configuration record
        (``workflow.effective.effective_configuration_record``) on the
        ``implement --config --workflow`` path (contract §2.5, W-PIN-2). The
        digest is over the stored bytes' canonical JSON form, so the stored
        digest equals the Effective Configuration's own digest.
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
                   effective_configuration_digest, fingerprint, base_revision,
                   agent_identity_id, assigned_by_us
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
            base_revision=str(row[5]) if row[5] is not None else None,
            agent_identity_id=int(row[6]) if row[6] is not None else None,
            assigned_by_us=bool(row[7]) if row[7] is not None else None,
        )

    def set_attempt_state(self, attempt_id: str, state: AttemptState) -> None:
        self._connection.execute(
            "UPDATE attempts SET state = ? WHERE attempt_id = ?",
            (state, attempt_id),
        )
        self._connection.commit()

    def set_assigned_by_us(self, attempt_id: str, assigned: bool) -> None:
        """Record whether the Agent Identity's assignment is ours (§5).

        Written only after the Worker performs the assignment, so the
        Attempt row exists before the assignment is made and recovery can
        still release an unconfirmed Claim (REC-CLM-1).
        """
        self._connection.execute(
            "UPDATE attempts SET assigned_by_us = ? WHERE attempt_id = ?",
            (int(assigned), attempt_id),
        )
        self._connection.commit()

    def next_attempt_number(self, issue_number: int) -> int:
        """One more than the highest Attempt number held for the issue (§5).

        Released Attempts count: numbers are never reused, not even after
        a ``released`` Claim.
        """
        prefix = f"#{issue_number}/"
        rows = self._connection.execute(
            "SELECT attempt_id FROM attempts WHERE attempt_id LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        highest = 0
        for row in rows:
            suffix = str(row[0])[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return highest + 1

    def has_unfinished_attempt(self) -> bool:
        """Whether intake must wait: an Attempt neither finished nor released (§3)."""
        row = self._connection.execute(
            "SELECT 1 FROM attempts WHERE state NOT IN ('finished', 'released') LIMIT 1",
        ).fetchone()
        return row is not None

    def record_terminal_outcome(
        self,
        attempt_id: str,
        *,
        outcome: str,
        reason: str,
        pull_request_body: str = "",
        terminal_comment_body: str = "",
        writes: tuple[str, ...] | list[str] = (),
    ) -> OutcomeRecord:
        """Record the Terminal Outcome **before** any tracker write (§14).

        Moves the Attempt to ``outcome-recorded`` and inserts its ``outcomes``
        row in the same transaction discipline as every later slice: recovery
        finishes a recorded outcome and never re-decides it. The row carries
        the rendered comment and pull-request bodies and the list of writes it
        calls for, so a retried or recovered write sends the same bytes. At
        most one outcome row exists per Attempt; a second call leaves the
        first in place and returns it, which is what keeps ``delivery_intent``
        (the first recorder for ``delivered``) and ``terminal_writes`` (its
        second, idempotent recorder) on the same bytes.
        """
        existing = self._connection.execute(
            "SELECT attempt_id, outcome, reason, pull_request_body,"
            " terminal_comment_body, writes FROM outcomes WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if existing is not None:
            return self._outcome_from_row(existing)
        ordered_writes = list(writes)
        self._connection.execute(
            """
            INSERT INTO outcomes
                (attempt_id, outcome, reason, pull_request_body,
                 terminal_comment_body, writes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                outcome,
                reason,
                pull_request_body,
                terminal_comment_body,
                json.dumps(ordered_writes, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._connection.execute(
            "UPDATE attempts SET state = ? WHERE attempt_id = ?",
            (OUTCOME_RECORDED, attempt_id),
        )
        self._connection.commit()
        return OutcomeRecord(
            attempt_id=attempt_id,
            outcome=outcome,
            reason=reason,
            pull_request_body=pull_request_body,
            terminal_comment_body=terminal_comment_body,
            writes=tuple(ordered_writes),
        )

    @staticmethod
    def _outcome_from_row(row: object) -> OutcomeRecord:
        """Rebuild an :class:`OutcomeRecord` from an ``outcomes`` row tuple."""
        assert isinstance(row, tuple)
        raw_writes = str(row[5]) if len(row) > 5 and row[5] is not None else "[]"
        try:
            parsed = json.loads(raw_writes)
        except json.JSONDecodeError:
            parsed = []
        writes = tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()
        return OutcomeRecord(
            attempt_id=str(row[0]),
            outcome=str(row[1]),
            reason=str(row[2]),
            pull_request_body=str(row[3]) if len(row) > 3 and row[3] is not None else "",
            terminal_comment_body=str(row[4]) if len(row) > 4 and row[4] is not None else "",
            writes=writes,
        )

    def get_outcome(self, attempt_id: str) -> OutcomeRecord | None:
        row = self._connection.execute(
            "SELECT attempt_id, outcome, reason, pull_request_body,"
            " terminal_comment_body, writes FROM outcomes WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return self._outcome_from_row(row)

    def update_outcome_for_publish(
        self,
        attempt_id: str,
        *,
        outcome: str,
        reason: str,
        pull_request_body: str,
        terminal_comment_body: str,
        writes: tuple[str, ...] | list[str],
    ) -> OutcomeRecord:
        """Supersede one recorded outcome before its writes are performed (§13, §14).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (the pull
        request's receipt is known only after the remote write: the terminal
        comment linking it, and a withdrawal ending ``abandoned``, are
        recorded before the terminal writes perform anything) and §14
        ("Recording before publishing": the final bytes still predate the
        first tracker call that sends them).

        MVP row ids: P-DEL-1, L3-DEL-14.

        The Delivery Intent is the first recorder for ``delivered``; this is
        its single update path: filling the terminal comment with the opened
        pull request's link, or replacing the whole row when a human-closed
        pull request turns the Attempt into a withdrawal. Recovery replays
        the same deterministic bytes from the receipt, so a retried write
        still sends the same bytes.
        """
        ordered_writes = list(writes)
        self._connection.execute(
            """
            UPDATE outcomes
            SET outcome = ?, reason = ?, pull_request_body = ?,
                terminal_comment_body = ?, writes = ?
            WHERE attempt_id = ?
            """,
            (
                outcome,
                reason,
                pull_request_body,
                terminal_comment_body,
                json.dumps(ordered_writes, sort_keys=True),
                attempt_id,
            ),
        )
        self._connection.commit()
        record = self.get_outcome(attempt_id)
        assert record is not None, f"no outcomes row for {attempt_id}"
        return record

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
