"""The step subgraph's progress, soft-stall and verification-reserve policy.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Progress Gates
and the verification reserve": the Run Ledger records a ``progress_event``
only when a model response resolves an Acceptance Criterion or Seam Set
uncertainty, changes the candidate diff, or produces new Criterion Evidence
after such a change; five consecutive responses without one record a
``soft_stall``; at 70% of any Attempt-wide meter the ledger records one
durable ``Gate`` with a concrete next action and a remaining-budget
snapshot; at 80% it records ``reserve_entry``; broad exploration ends and
the phase only moves forward; usage is counted after every model response
and flushed to the Run Ledger at that moment as a readable ``usage`` record;
a crossed ceiling stops the loop inside the node, ending ``failed-limit``
naming the limit) and §9 (Implementation Outcomes come from
the step subgraph alone; the category follows the moment of acceptance,
not the moment of measurement).

MVP row ids: L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-30, C-LIM-1, P-LIM-1,
L3-IMP-7.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.runtime.ceilings import LoopCeilings

__all__ = [
    "SOFT_STALL",
    "GATE",
    "RESERVE",
    "MAX_NON_PROGRESS_RESPONSES",
    "StepProgress",
    "ProgressEvent",
    "workspace_fingerprint",
    "NARROW_NEXT_ACTION",
]

SOFT_STALL = "soft_stall"
GATE = "gate"
RESERVE = "reserve_entry"

# Give an implementer enough room to map an unfamiliar codebase before
# requiring a concrete edit, test, or snapshot.
MAX_NON_PROGRESS_RESPONSES = 5

NARROW_NEXT_ACTION = (
    "narrow implementation, snapshot preparation or verification"
)
"""The single narrow next action offered after a soft stall or Gate (§11).

A soft-stalled or gated step may only take one narrow progress-producing
action, prepare its snapshot, make its claim, or run verification. Broad
exploration cannot resume.
"""

StepPhase = Literal["normal", "gate", "verification_reserve"]

_PROGRESS_MARKER = re.compile(
    r"\bprogress\s*:\s*(acceptance_criterion|seam_set|readiness)\b", re.IGNORECASE
)


def has_explicit_progress_marker(content: object) -> bool:
    """Whether model text explicitly resolves an AC/Seam/Readiness fact.

    Tool calls, reads, directory traversal, turn count and bare activity
    never count; only an explicit qualifying fact does. The marker is the
    narrow machine-readable form of "resolves an Acceptance Criterion or
    Seam Set uncertainty" (§11). Prose without it is telemetry.
    """
    return bool(_PROGRESS_MARKER.search(str(content)))


@dataclass(frozen=True)
class ProgressEvent:
    """One qualifying model response recorded to the Run Ledger."""

    kind: str
    snapshot: BudgetSnapshot
    artifact_ref: str | None = None
    diff_ref: str | None = None


def workspace_fingerprint(workspace: Path) -> str:
    """Content hash of the candidate diff's working tree (§11).

    A ``progress_event`` of kind ``diff`` is recorded exactly when this
    changes: any tracked or untracked file edit, creation or deletion under
    ``workspace`` (excluding ``.git``) is a candidate-diff change. Presence
    alone is not compared; content is, so a second edit while a diff is
    already present still counts as progress.
    """
    root = workspace.resolve()
    digests: list[str] = []
    try:
        candidates = sorted(root.rglob("*"))
    except OSError:
        return "unreadable"
    for path in candidates:
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            relative = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            continue
        digests.append(f"{relative}:{digest}")
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


class StepProgress:
    """The step subgraph's deterministic stall, Gate and reserve policy.

    One tracker per Attempt: ``run_step`` builds it once and every step and
    work node shares it, so the Gate and reserve phases, like the meters,
    run on unbroken across steps and handoffs (§11, W-STP-6). The tracker
    records ``progress_event``, ``soft_stall``, ``Gate`` and
    ``reserve_entry`` rows to the Run Ledger, and flushes cumulative usage
    as readable ``usage`` rows after every model response and tool result
    (contract §11, C-LIM-1, P-LIM-1): per-bucket tokens and dollars, so the
    terminal comment reports the spend directly.
    """

    def __init__(
        self,
        attempt_id: str,
        ceilings: LoopCeilings,
        *,
        ledger: RunLedger | None = None,
        live: object | None = None,
    ) -> None:
        self.attempt_id = attempt_id
        self.ceilings = ceilings
        self.ledger = ledger
        self.live = live
        self.phase: StepPhase = "normal"
        self.soft_stalled = False
        self.non_progress_responses = 0
        self.progress_events: list[ProgressEvent] = []
        self.policy_events: list[str] = []
        self.last_snapshot: BudgetSnapshot | None = None
        self.ceiling: str | None = None

    def record_progress(
        self,
        kind: str,
        snapshot: BudgetSnapshot,
        *,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
    ) -> ProgressEvent:
        """Record one qualifying model response (§11, L3-IMP-16)."""
        event = ProgressEvent(kind, snapshot, artifact_ref, diff_ref)
        self.progress_events.append(event)
        self.non_progress_responses = 0
        self.soft_stalled = False
        self.last_snapshot = snapshot
        if self.ledger is not None:
            self.ledger.record_progress(
                self.attempt_id,
                kind,
                snapshot,
                artifact_ref=artifact_ref,
                diff_ref=diff_ref,
            )
        return event

    def observe_response(
        self, snapshot: BudgetSnapshot, *, progress: bool = False
    ) -> tuple[str, ...]:
        """Count one model response only after its qualifying work is known.

        A response with qualifying work resets the stall counter; any other
        response increments it, and the fifth consecutive one without a
        ``progress_event`` records one durable ``soft_stall`` (L3-IMP-16).
        Silence is a non-progress response on the ordinary terms: the
        silence budget and the stall counter are two counters with two
        different jobs.
        """
        self.last_snapshot = snapshot
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
            detail=(
                f"{MAX_NON_PROGRESS_RESPONSES} model responses without progress; "
                f"next action: {NARROW_NEXT_ACTION}"
            ),
        )

    def observe_diagnostic(self, snapshot: BudgetSnapshot) -> tuple[str, ...]:
        """Enter the same soft-stall path for a suppressed diagnostic (L3-IMP-17).

        A suppressed, unchanged targeted diagnostic carries no new
        Criterion Evidence and changes no diff, so activity around it and
        the diagnostic itself never reset progress without a relevant
        change. The first such diagnostic records the one ``soft_stall``;
        repeats add nothing.
        """
        self.last_snapshot = snapshot
        if self.soft_stalled:
            return ()
        self.soft_stalled = True
        return self._transition(
            SOFT_STALL,
            snapshot,
            detail=(
                "targeted diagnostic made no further progress; "
                f"next action: {NARROW_NEXT_ACTION}"
            ),
        )

    def observe_budget(self, snapshot: BudgetSnapshot) -> tuple[str, ...]:
        """Record the durable Gate, then the monotonic reserve entry (L3-IMP-18).

        At 70% of any Attempt-wide meter the ledger records one durable
        ``Gate`` with a concrete next action and the remaining-budget
        snapshot it already carries; at 80% it records ``reserve_entry``.
        The phase never goes back, so crossing another meter cannot
        restore broad exploration.
        """
        self.last_snapshot = snapshot
        ratios = self._ratios(snapshot)
        transitions: list[str] = []
        if self.phase == "normal" and ratios and max(ratios.values()) >= 0.70:
            transitions.extend(
                self._transition(GATE, snapshot, detail=self._next_action())
            )
        if (
            self.phase != "verification_reserve"
            and ratios
            and max(ratios.values()) >= 0.80
        ):
            transitions.extend(
                self._transition(
                    RESERVE,
                    snapshot,
                    detail="reserve capacity for the claim, snapshot and validation",
                )
            )
        return tuple(transitions)

    def flush_usage(self, snapshot: BudgetSnapshot) -> None:
        """Flush cumulative usage as a readable ``usage`` record (§11, C-LIM-1).

        Called after every model response and every tool result, and once
        more before the hard-ceiling finalization, so a crossed ceiling
        stops the loop with the spend already durable. The payload carries
        cumulative totals plus per-bucket tokens and dollars. Each flush
        also touches the live view's activity time (#253).
        """
        self.last_snapshot = snapshot
        if self.ledger is not None:
            self.ledger.record_usage(self.attempt_id, snapshot)
        live = self.live
        touch = getattr(live, "touch", None)
        if callable(touch):
            try:
                touch()
            except Exception:
                pass

    def allows(self, action: str) -> bool:
        """Whether a model/tool action is legal in the current phase (§11).

        In the verification reserve only snapshot, verification and
        remediation may run; broad exploration cannot resume. After a Gate
        or soft stall only narrow implementation, verification, snapshot
        and remediation may run.
        """
        if self.phase == "verification_reserve":
            return action in {"snapshot", "verification", "remediation"}
        if self.phase == "gate" or self.soft_stalled:
            return action in {"implementation", "verification", "snapshot", "remediation"}
        return True

    def _next_action(self) -> str:
        if self.progress_events:
            return (
                "make the smallest implementation change or run its targeted test; "
                f"next action: {NARROW_NEXT_ACTION}"
            )
        return (
            "resolve one acceptance, readiness, or Seam Set fact; "
            f"next action: {NARROW_NEXT_ACTION}"
        )

    def _ratios(self, snapshot: BudgetSnapshot) -> dict[str, float]:
        values: dict[str, float] = {}
        if self.ceilings.max_wall_clock_seconds:
            values["wall_clock"] = (
                snapshot.elapsed_seconds / self.ceilings.max_wall_clock_seconds
            )
        if self.ceilings.max_cost_usd:
            values["cost"] = snapshot.usage.cost_usd / self.ceilings.max_cost_usd
        if self.ceilings.max_effective_tokens:
            values["tokens"] = (
                snapshot.usage.effective_tokens / self.ceilings.max_effective_tokens
            )
        if self.ceilings.max_tool_calls:
            values["tool_calls"] = snapshot.usage.tool_calls / self.ceilings.max_tool_calls
        return values

    def _transition(
        self, kind: str, snapshot: BudgetSnapshot, *, detail: str
    ) -> tuple[str, ...]:
        if kind == GATE:
            self.phase = "gate"
        elif kind == RESERVE:
            self.phase = "verification_reserve"
        self.policy_events.append(kind)
        if self.ledger is not None:
            self.ledger.record(self.attempt_id, kind, snapshot, detail=detail)
        return (kind,)
