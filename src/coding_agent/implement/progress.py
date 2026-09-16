from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from coding_agent.implement.ceilings import LoopCeilings, UsageTotals


PROGRESS_ACCEPTANCE = "acceptance_criterion"
PROGRESS_SEAM = "seam_set"
PROGRESS_READINESS = "readiness"
PROGRESS_DIFF = "diff"
PROGRESS_TARGETED_RESULT = "targeted_result"
PROGRESS_VALIDATION_RESULT = "validation_result"

SOFT_STALL = "soft_stall"
GATE = "gate"
RESERVE = "reserve_entry"

VERIFIED_COMPLETION = "verified completion"
IMPLEMENTED_BUT_UNVERIFIED = "implemented but unverified"
SAVED_PARTIAL_WORK = "saved partial work"
FAILED = "failed"

ProgressKind = Literal[
    "acceptance_criterion",
    "seam_set",
    "readiness",
    "diff",
    "targeted_result",
    "validation_result",
]
TerminalOutcome = Literal[
    "verified completion",
    "implemented but unverified",
    "saved partial work",
    "failed",
]
PolicyPhase = Literal["normal", "gate", "verification_reserve"]

_PROGRESS_MARKER = re.compile(
    r"\bprogress\s*:\s*(acceptance_criterion|seam_set|readiness)\b", re.IGNORECASE
)


def detect_progress_markers(content: object) -> tuple[ProgressKind, ...]:
    """Recognise explicit model facts without treating prose or tool activity as progress."""
    matches = _PROGRESS_MARKER.findall(str(content))
    return tuple(cast(ProgressKind, match.lower()) for match in dict.fromkeys(matches))


@dataclass(frozen=True)
class BudgetSnapshot:
    """The Attempt-wide meter values at one observable policy transition."""

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


class RunLedger:
    """A small SQLite-backed durable record for one Worker's Attempts.

    The ledger stores policy events rather than conversation text. Returned
    tool artifacts remain in their artifact store; this record points at them
    and keeps enough budget state to explain why a transition happened.
    """

    def __init__(self, path: Path, *, ceilings: LoopCeilings | None = None) -> None:
        self.path = path
        self.ceilings = ceilings or LoopCeilings()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
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
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

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


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    snapshot: BudgetSnapshot
    artifact_ref: str | None = None
    diff_ref: str | None = None


class AttemptPolicy:
    """The deterministic stall, Gate and verification-reserve policy."""

    def __init__(
        self,
        attempt_id: str,
        ceilings: LoopCeilings,
        *,
        ledger: RunLedger | None = None,
    ) -> None:
        self.attempt_id = attempt_id
        self.ceilings = ceilings
        self.ledger = ledger
        self.phase: PolicyPhase = "normal"
        self.soft_stalled = False
        self.non_progress_responses = 0
        self.progress_events: list[ProgressEvent] = []
        self.policy_events: list[str] = []

    def record_progress(
        self,
        kind: str,
        snapshot: BudgetSnapshot,
        *,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
    ) -> ProgressEvent:
        event = ProgressEvent(kind, snapshot, artifact_ref, diff_ref)
        self.progress_events.append(event)
        self.non_progress_responses = 0
        self.soft_stalled = False
        if self.ledger is not None:
            self.ledger.record_progress(
                self.attempt_id,
                kind,
                snapshot,
                artifact_ref=artifact_ref,
                diff_ref=diff_ref,
            )
        return event

    def record_usage(self, snapshot: BudgetSnapshot, *, source: str) -> None:
        """Persist a per-response or per-tool usage snapshot for recovery and audit."""
        if self.ledger is not None:
            self.ledger.record(
                self.attempt_id,
                "usage",
                snapshot,
                payload={
                    "source": source,
                    "tokens": snapshot.usage.tokens,
                    "effective_tokens": snapshot.usage.effective_tokens,
                    "cost_usd": snapshot.usage.cost_usd,
                    "tool_calls": snapshot.usage.tool_calls,
                },
            )

    def observe_response(self, snapshot: BudgetSnapshot, *, progress: bool = False) -> tuple[str, ...]:
        """Count a model response only after its qualifying work is known."""
        if progress:
            self.non_progress_responses = 0
            self.soft_stalled = False
            return ()
        self.non_progress_responses += 1
        if self.non_progress_responses < 2 or self.soft_stalled:
            return ()
        self.soft_stalled = True
        return self._transition(SOFT_STALL, snapshot, detail="two model responses without progress")

    def observe_diagnostic(self, snapshot: BudgetSnapshot) -> tuple[str, ...]:
        """Treat the targeted-test suppression boundary as a soft stall."""
        if self.soft_stalled:
            return ()
        self.soft_stalled = True
        return self._transition(
            SOFT_STALL, snapshot, detail="targeted diagnostic made no further progress"
        )

    def observe_budget(self, snapshot: BudgetSnapshot) -> tuple[str, ...]:
        ratios = self._ratios(snapshot)
        transitions: list[str] = []
        if self.phase == "normal" and ratios and max(ratios.values()) >= 0.70:
            transitions.extend(self._transition(GATE, snapshot, detail=self._next_action()))
        if self.phase != "verification_reserve" and ratios and max(ratios.values()) >= 0.80:
            transitions.extend(self._transition(RESERVE, snapshot, detail="reserve capacity for snapshot and validation"))
        return tuple(transitions)

    def allows(self, action: str) -> bool:
        """Check whether a model/tool action is legal in the current phase."""
        if self.phase == "verification_reserve":
            return action in {"snapshot", "verification", "remediation"}
        if self.phase == "gate" or self.soft_stalled:
            return action in {"implementation", "verification", "snapshot", "remediation"}
        return True

    def _next_action(self) -> str:
        if self.progress_events:
            return "make the smallest implementation change or run its targeted test"
        return "resolve one acceptance, readiness, or Seam Set fact"

    def _ratios(self, snapshot: BudgetSnapshot) -> dict[str, float]:
        values: dict[str, float] = {}
        if self.ceilings.max_wall_clock_seconds:
            values["wall_clock"] = snapshot.elapsed_seconds / self.ceilings.max_wall_clock_seconds
        if self.ceilings.max_cost_usd:
            values["cost"] = snapshot.usage.cost_usd / self.ceilings.max_cost_usd
        if self.ceilings.max_effective_tokens:
            values["tokens"] = snapshot.usage.effective_tokens / self.ceilings.max_effective_tokens
        if self.ceilings.max_tool_calls:
            values["tool_calls"] = snapshot.usage.tool_calls / self.ceilings.max_tool_calls
        return values

    def _transition(self, kind: str, snapshot: BudgetSnapshot, *, detail: str) -> tuple[str, ...]:
        if kind == GATE:
            self.phase = "gate"
        elif kind == RESERVE:
            self.phase = "verification_reserve"
        self.policy_events.append(kind)
        if self.ledger is not None:
            self.ledger.record(self.attempt_id, kind, snapshot, detail=detail)
        return (kind,)


def terminal_outcome(
    *, delivery_pushed: bool, validation_clean: bool, stopped_by: str | None
) -> TerminalOutcome:
    """Choose exactly one human-facing category; an interrupted run is never verified."""
    if stopped_by == "handoff-failure":
        return cast(TerminalOutcome, FAILED)
    if delivery_pushed and validation_clean:
        return cast(TerminalOutcome, VERIFIED_COMPLETION)
    if delivery_pushed and stopped_by is None:
        return cast(TerminalOutcome, IMPLEMENTED_BUT_UNVERIFIED)
    return cast(TerminalOutcome, SAVED_PARTIAL_WORK)
