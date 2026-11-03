"""The Worker chooses its own work and claims it (contract §4, §5).

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (candidates are the
open issues carrying the Workflow's Selection Label; the fixed guards and
the Workflow's ``require_all``, ``require_any`` and ``exclude`` filters
narrow the set; the order is ``(created_at, number)`` ascending; selection
writes nothing) and §5 (the Claim is an assignment of the Agent Identity
confirmed by reading the issue back: one ledger transaction opens Attempt
``#<issue>/<n>`` in state ``open`` with the Fingerprint, the Base Revision,
the Effective Configuration and its digest and the Agent Identity's id;
then the Worker assigns and records ``assigned_by_us``; then it
re-evaluates every guard and filter — another assignee or any failure
removes only its own assignment, marks the Attempt ``released`` and moves
to the next candidate, otherwise it records ``claim-confirmed``).

MVP row ids: P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1, P-CLM-1,
P-CLM-2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.fingerprint import compute_fingerprint
from coding_agent.tracker.port import CandidateIssue, TrackerPort
from coding_agent.tracker.selection import exclusion_reason, sort_candidates


@dataclass(frozen=True)
class ClaimConfig:
    """What one Selection-and-Claim sweep runs under (contract §2.5, §4, §5).

    ``selection_label`` is the Workflow's Selection Label; ``require_all``,
    ``require_any`` and ``exclude`` are its filters, re-evaluated at the
    read-back and never at a later Gate; ``ready_for_human`` is the
    lifecycle label's configured name; ``base_branch`` is the configured
    base branch the Base Revision is resolved from; ``agent_login`` and
    ``agent_identity_id`` are the Agent Identity; ``effective_configuration``
    is the record the Claim stores in ``attempts`` (contract §2.5, W-PIN-2).
    """

    selection_label: str
    ready_for_human: str = "ready-for-human"
    base_branch: str = "main"
    require_all: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    agent_login: str = ""
    agent_identity_id: int | None = None
    effective_configuration: Mapping[str, object] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class ClaimedAttempt:
    """The Attempt one sweep claimed and confirmed (contract §5)."""

    attempt_id: str
    issue_number: int
    attempt_number: int


def _guard_reason(
    tracker: TrackerPort, config: ClaimConfig, candidate: CandidateIssue
) -> str | None:
    """The reason ``candidate`` is excluded, or ``None`` where selectable (§4).

    Every guard input is read fresh — the native blocker and the open
    marked pull request as well as the candidate facts — so the read-back
    re-evaluates the same predicate the intake did. Any assignee excludes:
    at intake no Attempt of ours is open (the intake gate holds), so the
    Agent Identity's own assignment is a human's and keeps the issue
    ineligible (§5).
    """
    blocker_open = tracker.has_open_blocker(candidate.number)
    attempt_pr_open = tracker.has_open_attempt_pull_request(candidate.number)
    return exclusion_reason(
        candidate,
        ready_for_human=config.ready_for_human,
        blocker_open=blocker_open,
        attempt_pr_open=attempt_pr_open,
        require_all=config.require_all,
        require_any=config.require_any,
        exclude=config.exclude,
    )


def _attempt_id(issue_number: int, attempt_number: int) -> str:
    """The Attempt id: ``#<issue>/<n>`` (§5, plan "Run Ledger tables by slice")."""
    return f"#{issue_number}/{attempt_number}"


def _claim_candidate(
    tracker: TrackerPort,
    ledger: RunLedger,
    config: ClaimConfig,
    candidate: CandidateIssue,
) -> ClaimedAttempt | None:
    """Claim one candidate: open, assign, read back, confirm or release (§5).

    The Attempt row exists before the assignment is made, so recovery can
    always release or finish it (§17). A failed read-back removes only the
    Worker's own assignment and marks the Attempt ``released`` — no comment
    and no label change — and the sweep moves to the next candidate.
    """
    attempt_number = ledger.next_attempt_number(candidate.number)
    attempt_id = _attempt_id(candidate.number, attempt_number)
    ledger.open_attempt(
        attempt_id,
        effective_configuration=config.effective_configuration,
        fingerprint=compute_fingerprint(candidate.title, candidate.body),
        base_revision=tracker.fetch_base_revision(config.base_branch),
        agent_identity_id=config.agent_identity_id,
    )
    tracker.assign_issue(candidate.number)
    ledger.set_assigned_by_us(attempt_id, True)

    fresh = tracker.fetch_candidate(candidate.number)
    others = tuple(
        login for login in fresh.assignees if login != config.agent_login
    )
    if others or config.agent_login not in fresh.assignees:
        tracker.release_assignment(candidate.number)
        ledger.set_attempt_state(attempt_id, "released")
        return None
    reason = exclusion_reason(
        replace(fresh, assignees=others),
        ready_for_human=config.ready_for_human,
        blocker_open=tracker.has_open_blocker(candidate.number),
        attempt_pr_open=tracker.has_open_attempt_pull_request(candidate.number),
        require_all=config.require_all,
        require_any=config.require_any,
        exclude=config.exclude,
    )
    if reason is not None:
        tracker.release_assignment(candidate.number)
        ledger.set_attempt_state(attempt_id, "released")
        return None
    ledger.set_attempt_state(attempt_id, "claim-confirmed")
    return ClaimedAttempt(
        attempt_id=attempt_id,
        issue_number=candidate.number,
        attempt_number=attempt_number,
    )


def select_and_claim(
    *,
    tracker: TrackerPort,
    ledger: RunLedger,
    config: ClaimConfig,
) -> ClaimedAttempt | None:
    """Run one sweep: the first selectable candidate claimed wins (§4, §5).

    While the ledger holds an Attempt that is not ``finished`` or
    ``released`` the Worker selects no new work (§3): returns ``None``
    having written nothing. Otherwise candidates are tried in
    ``(created_at, number)`` order; returns the confirmed Attempt, or
    ``None`` where no candidate survived the read-back.
    """
    if ledger.has_unfinished_attempt():
        return None
    for candidate in sort_candidates(tracker.list_candidates(config.selection_label)):
        if _guard_reason(tracker, config, candidate) is not None:
            continue
        claimed = _claim_candidate(tracker, ledger, config, candidate)
        if claimed is not None:
            return claimed
    return None
