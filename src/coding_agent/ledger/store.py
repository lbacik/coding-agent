"""The Run Ledger's SQLite store: events, Attempts, effects and Terminal Outcomes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (the review
snapshot commits the current tree and pushes the branch as X), §13 (the write sequence: pass the in-tool Gate, record
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
REC-CLM-1, REC-CLM-2, REC-CFG-2, REC-WRK-1, C-LIM-1, P-LIM-1, L3-IMP-7,
L3-ERR-11.

Contract §18 (the ledger is an output surface: every detail, reason,
body and payload passes through the redaction registry before it is
stored, so a stored row carries only ``«redacted:<name>»`` markers).
Every ``record_*`` method funnels through the one registry held here;
callers never redact on their own.

Contract §11 (the M7 hard-ceiling finalization): usage is counted after
every model response and flushed to the Run Ledger at that moment as a
readable ``usage`` record carrying cumulative totals plus per-bucket tokens
and dollars; the wall clock is measured from the Attempt's opening in the
Run Ledger, never from process start, so review time counts too.
``USAGE_KIND`` is that record's event kind.

M1 tables are ``run_ledger_events`` (progress, budget and snapshot
references, carried over from ``runtime.progress`` unchanged), ``attempts``
(one row per ``#<issue>/<n>`` with its lifecycle state and its Effective
Configuration) and ``outcomes`` (one row per Attempt with the Terminal
Outcome, its reason, the rendered comment and pull-request bodies and the
writes it calls for; a Delivery Intent is an outcome row carrying the
pull-request fields). The Effective
Configuration column holds the normalised Effective Configuration record
``agent run`` pins on the Attempt
(``workflow.effective.effective_configuration_record``) and its SHA-256
digest (contract §2.5, W-PIN-2); scripted tests pass their own settings.
``effects`` arrives in M3, keyed by
``(attempt_id, write_kind)`` with a status of ``intent`` or ``receipt``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from coding_agent.runtime.ceilings import LoopCeilings, UsageTotals
from coding_agent.runtime.redaction import Redactable, registry_from

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

USAGE_KIND = "usage"
"""Run Ledger event kind for a flushed usage record (contract §11, C-LIM-1).

One row per model response and per tool result while the Attempt runs, plus
one final row before the hard-ceiling finalization. The payload carries the
cumulative totals plus per-bucket tokens and dollars, so the terminal comment
and an operator report the spend directly instead of computing
limit-minus-remaining.
"""


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

    def __init__(
        self,
        path: Path,
        *,
        ceilings: LoopCeilings | None = None,
        registry: Redactable = None,
    ) -> None:
        """Open the ledger, holding the redaction registry (§18, L3-ERR-11).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (the ledger
        is an output surface: every detail, reason, body and payload passes
        through the redaction registry before it is stored). ``registry``
        accepts any :data:`Redactable` form; callers never redact on their
        own. The default holds no secrets, so scripted tests without one
        stay green.

        MVP row ids: L3-ERR-11.

        Concurrency (contract §8, ADR 0020): concurrent Sub-agents share this
        handle across threads. The connection opens with
        ``check_same_thread=False`` and every method touching it holds the
        one re-entrant writer lock, so ledger writes from concurrent
        Sub-agents serialise instead of interleaving.
        """
        from coding_agent.runtime.redaction import SecretRegistry

        self.path = path
        self.ceilings = ceilings or LoopCeilings()
        self._registry = (
            registry if isinstance(registry, SecretRegistry) else registry_from(registry)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
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
        from coding_agent.operator.commands import ensure_operator_tables

        ensure_operator_tables(self._connection)
        try:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_halt (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
        except Exception:
            pass
        try:
            from coding_agent.operator.events import ensure_operator_events_table

            ensure_operator_events_table(self._connection)
        except Exception:
            pass
        try:
            self._connection.commit()
        except Exception:
            pass

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

    @property
    def registry(self) -> object:
        """The redaction registry every stored row passes through (§18)."""
        return self._registry

    def _redacted_payload(
        self, payload: Mapping[str, object] | None
    ) -> dict[str, object]:
        """The payload with every registered secret replaced (§18, L3-ERR-11)."""
        return dict(self._registry.redact_json(dict(payload or {})))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def checkpoint(self) -> None:
        """Checkpoint the WAL with ``TRUNCATE`` after the last write (§17).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (one SQLite
        file on the state volume, in WAL mode). Every orderly Worker exit
        calls this after its last write, so a restarted or offline reader
        finds the committed state in the database file itself: the normal
        exit, the SIGTERM shutdown, and later the Halt paths. The call
        replays committed WAL frames into the database and truncates the
        ``-wal`` file to zero bytes. Settings (WAL, ``synchronous=FULL``)
        are unchanged.

        Raises :class:`sqlite3.OperationalError` where the checkpoint did
        not finish (``busy`` set or frames left uncheckpointed), so a
        best-effort caller swallows consciously instead of assuming
        success. Uncontended — the Worker exiting with its own connection
        — the checkpoint completes.
        """
        with self._lock:
            row = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert row is not None
        busy, log_frames, checkpointed = int(row[0]), int(row[1]), int(row[2])
        if busy != 0 or checkpointed != log_frames:
            raise sqlite3.OperationalError(
                f"wal_checkpoint(TRUNCATE) incomplete: busy={busy} "
                f"checkpointed={checkpointed}/{log_frames}"
            )

    def journal_mode(self) -> str:
        with self._lock:
            row = self._connection.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        return str(row[0]).lower()

    def synchronous(self) -> str:
        with self._lock:
            row = self._connection.execute("PRAGMA synchronous").fetchone()
        assert row is not None
        return str(row[0])

    def table_names(self) -> tuple[str, ...]:
        with self._lock:
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

        The configuration is what the Attempt was opened with: under
        ``agent run`` the Effective Configuration record
        (``workflow.effective.effective_configuration_record``) the Worker's
        Claim pins (contract §2.5, §5, W-PIN-2). The
        digest is over the stored bytes' canonical JSON form, so the stored
        digest equals the Effective Configuration's own digest.
        """
        settings = dict(effective_configuration)
        digest = digest_effective_configuration(settings)
        with self._lock:
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

    def has_attempt(self, attempt_id: str) -> bool:
        """Whether ``attempt_id`` already has its ``attempts`` row (§5).

        The Worker's Claim opens the row before the graph runs; the graph's
        ``claim`` node then runs that Attempt instead of opening another.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return row is not None

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        with self._lock:
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
        return self._attempt_from_row(row)

    @staticmethod
    def _attempt_from_row(row: object) -> AttemptRecord:
        """Rebuild an :class:`AttemptRecord` from an ``attempts`` row tuple."""
        assert isinstance(row, tuple)
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

    def unfinished_attempts(self) -> tuple[AttemptRecord, ...]:
        """Every Attempt startup recovery must settle, in ledger order (§17).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (recovery
        runs at startup, before any selection, for every unfinished Attempt)
        and §3 (intake: while one exists the Worker selects no new work).

        MVP row ids: REC-CLM-1, REC-CLM-2, REC-WRK-1.

        An Attempt is unfinished while its state is neither ``finished`` nor
        ``released``. The ``effective_configuration`` JSON of a row the
        database corrupted raises here; the caller turns that into a startup
        refusal (REC-CFG-2) instead of guessing the configuration.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT attempt_id, state, effective_configuration,
                       effective_configuration_digest, fingerprint, base_revision,
                       agent_identity_id, assigned_by_us
                FROM attempts
                WHERE state NOT IN ('finished', 'released')
                ORDER BY rowid
                """
            ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def attempt_opened_at(self, attempt_id: str) -> datetime:
        """When the Attempt opened in the Run Ledger (§11, L3-IMP-7).

        The wall clock is measured from this timestamp, never from process
        start, so review time counts too. A missing or unparseable timestamp
        falls back to now, yielding a zero elapsed rather than a failure.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT created_at FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return datetime.now(timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def attempt_elapsed_seconds(
        self, attempt_id: str, *, now: datetime | None = None
    ) -> float:
        """Seconds from the Attempt's ledger opening to ``now`` (§11)."""
        opened = self.attempt_opened_at(attempt_id)
        current = now if now is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (current - opened).total_seconds())

    def record_usage(
        self, attempt_id: str, snapshot: BudgetSnapshot, *, detail: str | None = None
    ) -> LedgerRecord:
        """Flush cumulative usage as a readable ``usage`` record (§11, C-LIM-1).

        The payload carries the cumulative totals plus per-bucket tokens and
        dollars (``UsageTotals.as_readable``), so the terminal comment and an
        operator report the spend directly. Called after every model response
        and every tool result, and once more before the hard-ceiling
        finalization.
        """
        payload: dict[str, object] = {
            "usage": snapshot.usage.as_readable(),
            "elapsed_seconds": snapshot.elapsed_seconds,
        }
        return self.record(
            attempt_id,
            USAGE_KIND,
            snapshot,
            detail=detail or "attempt usage flush",
            payload=payload,
        )

    def set_attempt_state(self, attempt_id: str, state: AttemptState) -> None:
        """Mark an Attempt's lifecycle state, completing a pending stop.

        The transition to ``finished`` or ``released`` completes a pending
        ``stop`` in the same write transaction (#249): the drain holds
        across the normal loop, the infrastructure-failure path, the
        Claim read-back release and every recovery settling path. Intake
        stays closed; completing never reopens it.
        """
        from coding_agent.operator.commands import complete_pending_stop

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE attempts SET state = ? WHERE attempt_id = ?",
                    (state, attempt_id),
                )
                if state in ("finished", "released"):
                    complete_pending_stop(
                        self._connection, registry=self._registry
                    )
                self._connection.commit()
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                raise

    def open_attempt_if_intake_open(
        self,
        attempt_id: str,
        *,
        effective_configuration: Mapping[str, object],
        fingerprint: str | None = None,
        base_revision: str | None = None,
        agent_identity_id: int | None = None,
        assigned_by_us: bool | None = None,
    ) -> AttemptRecord | None:
        """Open ``#<issue>/<n>`` only where durable Intake is open (#249).

        The Intake check and the commit of the new Attempt row happen in
        one immediate write transaction (the Claim boundary): a ``stop``
        committed before the boundary prevents the Claim from opening,
        while one committed after stays ``accepted`` until this Attempt
        finalizes. Returns ``None`` where Intake is closed, having
        written nothing. The GitHub assignment and its read-back stay in
        ``worker.claim``.
        """
        from coding_agent.operator.commands import read_intake

        settings = dict(effective_configuration)
        digest = digest_effective_configuration(settings)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                intake = read_intake(self._connection)
                if intake.get("state") != "open":
                    self._connection.rollback()
                    return None
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
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                raise
        return self.get_attempt(attempt_id)

    def intake_is_open(self) -> bool:
        """Whether durable Intake is open (absent means open, #249)."""
        from coding_agent.operator.commands import read_intake

        with self._lock:
            return read_intake(self._connection).get("state") == "open"

    def reconcile_accepted_commands(self) -> None:
        """Resolve ``accepted`` commands left by an earlier process (#249).

        Runs before the first Claim boundary: every ``accepted`` command
        whose Attempt is already final becomes ``completed``. A pending
        stop with an unfinished Attempt stays pending so the drain still
        holds after a restart.
        """
        from coding_agent.operator.commands import reconcile_accepted

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                reconcile_accepted(self._connection, registry=self._registry)
                self._connection.commit()
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                raise

    def set_assigned_by_us(self, attempt_id: str, assigned: bool) -> None:
        """Record whether the Agent Identity's assignment is ours (§5).

        Written only after the Worker performs the assignment, so the
        Attempt row exists before the assignment is made and recovery can
        still release an unconfirmed Claim (REC-CLM-1).
        """
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            existing = self._connection.execute(
                "SELECT attempt_id, outcome, reason, pull_request_body,"
                " terminal_comment_body, writes FROM outcomes WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                return self._outcome_from_row(existing)
            ordered_writes = list(writes)
            redacted_reason = self._registry.redact(reason)
            redacted_pr_body = self._registry.redact(pull_request_body)
            redacted_comment = self._registry.redact(terminal_comment_body)
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
                    redacted_reason,
                    redacted_pr_body,
                    redacted_comment,
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
            reason=redacted_reason,
            pull_request_body=redacted_pr_body,
            terminal_comment_body=redacted_comment,
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

    def record_halt(
        self, *, reason: str, detail: str, occurred_at: str | None = None
    ) -> dict[str, object]:
        """Record the durable Halt and its ``halted`` event before exiting (#250).

        Every non-zero exit the Worker decides on after taking the volume lock
        records here: the reason, the redacted detail (capped at 300
        characters) and the time, plus a ``halted`` Contract Event in the
        same file. A restart keeps the row, reporting it as
        ``instance.last_halt``. Uses this ledger's redaction registry.
        """
        from datetime import datetime, timezone

        from coding_agent.operator.events import (
            EVENT_HALTED,
            MAX_EVENT_TEXT_LEN,
            emit_contract_event,
        )

        moment = occurred_at or datetime.now(timezone.utc).isoformat()
        redacted = self._registry.redact(detail)
        if len(redacted) > MAX_EVENT_TEXT_LEN:
            redacted = redacted[:MAX_EVENT_TEXT_LEN]
        process_started = moment
        try:
            row = self._connection.execute(
                "SELECT process_started_at FROM operator_instance WHERE id = 1"
            ).fetchone()
            if row is not None and row[0]:
                process_started = str(row[0])
        except Exception:
            pass
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_halt (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                INSERT INTO operator_halt (id, reason, detail, occurred_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    reason = excluded.reason,
                    detail = excluded.detail,
                    occurred_at = excluded.occurred_at
                """,
                (reason, redacted, moment),
            )
            try:
                emit_contract_event(
                    self._connection,
                    process_started_at=process_started,
                    event_type=EVENT_HALTED,
                    text=f"halted {reason}: {redacted}",
                    registry=self._registry,
                )
            except Exception:
                pass
            self._connection.commit()
        occurred_s = str(moment)
        return {
            "reason": reason,
            "detail": redacted,
            "occurred_at": occurred_s,
            "time": occurred_s,
        }

    def emit_contract_event(
        self,
        *,
        process_started_at: str,
        event_type: str,
        text: str,
    ) -> dict[str, object]:
        """Record one Contract Event through this ledger's registry (#250)."""
        from coding_agent.operator.events import emit_contract_event as _emit

        with self._lock:
            record = _emit(
                self._connection,
                process_started_at=process_started_at,
                event_type=event_type,
                text=text,
                registry=self._registry,
            )
            self._connection.commit()
        return dict(record)

    def get_outcome(self, attempt_id: str) -> OutcomeRecord | None:
        with self._lock:
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
        with self._lock:
            self._connection.execute(
                """
                UPDATE outcomes
                SET outcome = ?, reason = ?, pull_request_body = ?,
                    terminal_comment_body = ?, writes = ?
                WHERE attempt_id = ?
                """,
                (
                    outcome,
                    self._registry.redact(reason),
                    self._registry.redact(pull_request_body),
                    self._registry.redact(terminal_comment_body),
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
        event_payload = self._redacted_payload(payload)
        redacted_detail = self._registry.redact(detail) if detail is not None else None
        with self._lock:
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
                    redacted_detail,
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
            detail=redacted_detail,
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
        with self._lock:
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
        body = self._redacted_payload(payload)
        digest = digest_effect_payload(body)
        with self._lock:
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
        if payload is not None:
            body = self._redacted_payload(payload)
        elif existing is not None:
            body = self._redacted_payload(existing.payload)
        else:
            body = {}
        digest = digest_effect_payload(body)
        kept_remote = remote_id if remote_id is not None else (existing.remote_id if existing is not None else None)
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
