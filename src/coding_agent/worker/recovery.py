"""Startup recovery: settle every unfinished Attempt before any selection.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup runs
recovery for every unfinished Attempt before any selection; intake stays
closed while one exists; a recovery write that fails is retried on the next
poll cycle, and three in a row halt the Worker), §5 (the Claim opens the
Attempt before the assignment, so recovery can always release it) and §17
(the recovery table; recovery never calls the model; pinned values only —
repository, label names, base branch, Agent Identity and rendered bodies
come from the ledger, never the current configuration; a missing or
unreadable Effective Configuration record refuses startup).

MVP row ids: REC-CLM-1, REC-CLM-2, REC-CLM-3, P-CLM-3, REC-CFG-1, REC-CFG-2,
W-PIN-3, REC-WRK-1.

This slice owns the startup entry point and the unconfirmed-Claim rows.
Later slices settle ``claim-confirmed`` Attempts with no recorded outcome
(the interrupted Attempt) and finish ``outcome-recorded`` Attempts (their
remaining terminal writes); until then this module reports those states as
pending and intake stays closed. Recovery takes no model and no provider
client: settling a Claim is ledger reads plus at most one assignment
removal, so no model call is structurally possible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from coding_agent.ledger.store import AttemptRecord, RunLedger
from coding_agent.tracker.port import TrackerPort


class RecoveryRefused(Exception):
    """An unfinished Attempt cannot be recovered from its ledger record.

    Contract §3 step 6 and §17: an unfinished Attempt whose Effective
    Configuration is missing or unreadable refuses startup with a non-zero
    exit and no GitHub write (REC-CFG-2). The Worker never guesses it from
    the current configuration.
    """


@dataclass(frozen=True)
class PinnedRecoveryConfig:
    """The pinned Attempt facts recovery runs under (contract §2.5, §17).

    Every field comes from the Attempt's own ``attempts`` row: the
    repository, the base branch, the Selection Label and the
    ``ready-for-human`` names, and the Agent Identity's numeric id. The
    current YAML and ``--workflow`` are never consulted, so a restart with
    a changed file, renamed labels or another Workflow still finishes the
    open Attempt with its pinned values (REC-CFG-1), and a label removal
    uses the pinned name, not the current one (W-PIN-3).
    """

    attempt_id: str
    repository: str
    base_branch: str
    selection_label: str
    ready_for_human: str
    agent_identity_id: int | None


def _pinned_string(record: Mapping[str, object], *path: str) -> str:
    """Read one required pinned string, or refuse the record (REC-CFG-2)."""
    current: object = record
    for key in path:
        if not isinstance(current, Mapping):
            raise RecoveryRefused(
                f"Effective Configuration record is not a mapping at {'.'.join(path)}"
            )
        current = current.get(key)
    if not isinstance(current, str) or not current:
        raise RecoveryRefused(
            f"Effective Configuration record is missing {'.'.join(path)}"
        )
    return current


def pinned_recovery_config(attempt: AttemptRecord) -> PinnedRecoveryConfig:
    """The pinned facts one unfinished Attempt recovers under (§17).

    Raises :class:`RecoveryRefused` where the record is missing or
    unreadable (REC-CFG-2): not a mapping, or without the repository, base
    branch, Selection Label or lifecycle-label names recovery needs. The
    caller validates every unfinished Attempt before settling any, so a
    refusal performs no GitHub write.
    """
    record = attempt.effective_configuration
    if not isinstance(record, Mapping) or not record:
        raise RecoveryRefused(
            f"{attempt.attempt_id}: Effective Configuration record is "
            "missing or unreadable"
        )
    target = record.get("target")
    workflow = record.get("workflow")
    if not isinstance(target, Mapping) or not isinstance(workflow, Mapping):
        raise RecoveryRefused(
            f"{attempt.attempt_id}: Effective Configuration record is "
            "missing its target or workflow section"
        )
    selector = workflow.get("selector")
    if not isinstance(selector, Mapping):
        raise RecoveryRefused(
            f"{attempt.attempt_id}: Effective Configuration record is "
            "missing its workflow selector"
        )
    return PinnedRecoveryConfig(
        attempt_id=attempt.attempt_id,
        repository=_pinned_string(record, "target", "repository"),
        base_branch=_pinned_string(record, "target", "base_branch"),
        selection_label=_pinned_string(record, "workflow", "selector", "label"),
        ready_for_human=_pinned_string(record, "target", "ready_for_human"),
        agent_identity_id=attempt.agent_identity_id,
    )


def validate_recovery_configs(ledger: RunLedger) -> tuple[AttemptRecord, ...]:
    """Refuse startup where a pinned record is missing or unreadable (§3).

    Returns every unfinished Attempt with its pinned record validated
    (REC-CFG-2). Raises :class:`RecoveryRefused` before any GitHub write —
    including before the tracker's first read — so the refusal exits
    non-zero with no Attempt opened and no write. A corrupted row the
    database cannot even decode refuses the same way.
    """
    try:
        unfinished = ledger.unfinished_attempts()
    except Exception as exc:
        raise RecoveryRefused(
            f"cannot read unfinished Attempts from the Run Ledger: {exc}"
        ) from exc
    for attempt in unfinished:
        try:
            pinned_recovery_config(attempt)
        except RecoveryRefused:
            raise
        except Exception as exc:
            raise RecoveryRefused(
                f"{attempt.attempt_id}: Effective Configuration record is "
                f"unreadable: {exc}"
            ) from exc
    return unfinished


@dataclass(frozen=True)
class RecoverySummary:
    """What one recovery pass settled and what it left pending (§17)."""

    settled: tuple[str, ...]
    """Attempt ids this pass brought to ``released`` (unconfirmed Claims)."""

    pending: tuple[str, ...]
    """Unfinished Attempt ids this slice leaves for later recovery slices."""


def _issue_number(attempt_id: str) -> int:
    """The issue half of ``#<issue>/<n>``; refuses a malformed id."""
    try:
        head, _slash, _tail = attempt_id.partition("/")
        return int(head.removeprefix("#"))
    except ValueError as exc:
        raise RecoveryRefused(
            f"{attempt_id}: malformed Attempt id in the Run Ledger"
        ) from exc


def recover_open_attempt(
    tracker: TrackerPort,
    ledger: RunLedger,
    attempt: AttemptRecord,
    *,
    agent_login: str,
) -> bool:
    """Settle one ``open`` Attempt: an unconfirmed Claim (§17, §5).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (after the
    transaction, before the read-back: ``released``; remove our assignment
    if present; no comment and no label change).

    MVP row ids: REC-CLM-1, REC-CLM-2, P-CLM-3.

    - Where the ledger records the assignment as ours (``assigned_by_us``),
      the issue is read back and our assignment removed where present
      (REC-CLM-2: a crash after ``assign``, before the read-back). No
      comment is written and no label changes.
    - Where it does not — the crash came before ``assign`` (REC-CLM-1) —
      there is no GitHub write at all: not even a read, since nothing the
      Worker wrote could be waiting. The Attempt is marked ``released``
      and the issue is claimable on the next sweep as ``#N/n+1``.
    - An Agent Identity assignment with no open Attempt is a human's
      (REC-CLM-3, P-SEL-2): this function only ever runs for an open
      Attempt of the ledger, and it removes an assignment only where the
      ledger records it as the Worker's own — never another assignee.

    Returns ``True`` where the Attempt was settled to ``released``.
    Tracker errors propagate: the poll loop retries the write on the next
    poll cycle and halts after three consecutive failures (REC-WRK-1).
    """
    if attempt.state != "open":
        return False
    if attempt.assigned_by_us is True:
        fresh = tracker.fetch_candidate(_issue_number(attempt.attempt_id))
        if agent_login in fresh.assignees:
            tracker.release_assignment(fresh.number)
    ledger.set_attempt_state(attempt.attempt_id, "released")
    return True


def recover_unfinished_attempts(
    tracker: TrackerPort,
    ledger: RunLedger,
    *,
    agent_login: str,
) -> RecoverySummary:
    """Settle every unfinished Attempt the ledger holds, before selection.

    Validates every pinned record first (REC-CFG-2), so a refusal performs
    no write; then settles each ``open`` Attempt (REC-CLM-1, REC-CLM-2).
    ``claim-confirmed`` and ``outcome-recorded`` Attempts are reported as
    pending: their recovery (the interrupted Attempt, the recorded
    outcome's remaining writes) belongs to the slices that follow, and
    until then intake stays closed. Takes no model: settling a Claim is a
    ledger read plus at most one assignment removal.
    """
    unfinished = validate_recovery_configs(ledger)
    settled: list[str] = []
    pending: list[str] = []
    for attempt in unfinished:
        if recover_open_attempt(
            tracker, ledger, attempt, agent_login=agent_login
        ):
            settled.append(attempt.attempt_id)
        else:
            pending.append(attempt.attempt_id)
    return RecoverySummary(settled=tuple(settled), pending=tuple(pending))
