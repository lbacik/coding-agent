"""The stall, Gate and verification-reserve policy plus the outcome chooser.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (progress Gates and
the verification reserve) and §14 (the outcome chooser feeds M2's
Implementation Outcomes).

MVP row ids: L3-IMP-20; M1 keeps this module's rows green unchanged. The
durable ledger half (``RunLedger``, ``BudgetSnapshot``, ``LedgerRecord``)
moved to ``coding_agent.ledger`` in M1 (§17).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from coding_agent.ledger.store import (
    PROGRESS_ACCEPTANCE,
    PROGRESS_DIFF,
    PROGRESS_READINESS,
    PROGRESS_SEAM,
    PROGRESS_TARGETED_RESULT,
    PROGRESS_VALIDATION_RESULT,
    BudgetSnapshot,
    LedgerRecord,
    RunLedger,
)
from coding_agent.runtime.ceilings import LoopCeilings

__all__ = [
    "PROGRESS_ACCEPTANCE",
    "PROGRESS_SEAM",
    "PROGRESS_READINESS",
    "PROGRESS_DIFF",
    "PROGRESS_TARGETED_RESULT",
    "PROGRESS_VALIDATION_RESULT",
    "BudgetSnapshot",
    "LedgerRecord",
    "RunLedger",
]

SOFT_STALL = "soft_stall"
GATE = "gate"
RESERVE = "reserve_entry"

# Give an implementer enough room to map an unfamiliar codebase before
# requiring a concrete edit, test, or snapshot.
MAX_NON_PROGRESS_RESPONSES = 5

VERIFIED_COMPLETION = "verified completion"
IMPLEMENTED_BUT_UNVERIFIED = "implemented but unverified"
SAVED_PARTIAL_WORK = "saved partial work"
NO_CHANGE_PRODUCED_OUTCOME = "no change produced"
DELIVERY_BRANCH_COLLISION_OUTCOME = "delivery branch collision"
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
    "no change produced",
    "delivery branch collision",
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
        if self.non_progress_responses < MAX_NON_PROGRESS_RESPONSES or self.soft_stalled:
            return ()
        self.soft_stalled = True
        return self._transition(
            SOFT_STALL,
            snapshot,
            detail=f"{MAX_NON_PROGRESS_RESPONSES} model responses without progress",
        )

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
    *,
    delivery_pushed: bool,
    validation_clean: bool,
    stopped_by: str | None,
    no_change_produced: bool = False,
    delivery_branch_collision: bool = False,
) -> TerminalOutcome:
    """Choose exactly one human-facing category; an interrupted run is never verified."""
    if stopped_by in {"handoff-failure", "stage-failure"}:
        return cast(TerminalOutcome, FAILED)
    if no_change_produced:
        return cast(TerminalOutcome, NO_CHANGE_PRODUCED_OUTCOME)
    if delivery_branch_collision:
        return cast(TerminalOutcome, DELIVERY_BRANCH_COLLISION_OUTCOME)
    if delivery_pushed and validation_clean:
        return cast(TerminalOutcome, VERIFIED_COMPLETION)
    if delivery_pushed and stopped_by is None:
        return cast(TerminalOutcome, IMPLEMENTED_BUT_UNVERIFIED)
    return cast(TerminalOutcome, SAVED_PARTIAL_WORK)
