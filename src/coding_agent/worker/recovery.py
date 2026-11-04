"""Startup recovery: settle every unfinished Attempt before any selection.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup runs
recovery for every unfinished Attempt before any selection; intake stays
closed while one exists; a recovery write that fails is retried on the next
poll cycle, and three in a row halt the Worker), §5 (the Claim opens the
Attempt before the assignment, so recovery can always release it), §12 (a
crash during the final checks leaves the pushed Delivery Snapshot kept, and
recovery opens no pull request, REC-INT-6), §13 (fast-forward only, never
force; the remote ref is ours when it equals an open push intent's sha,
REC-INT-4), §14 (the terminal writes, in order), §15 (the recovery comment
carries the last persisted Continuation Snapshot only if its commit is an
ancestor of the pushed head, REC-INT-5) and §17 (the recovery table;
recovery never calls the model; pinned values only — repository, label
names, base branch, Agent Identity and rendered bodies come from the ledger,
never the current configuration; a missing or unreadable Effective
Configuration record refuses startup).

MVP row ids: REC-CLM-1, REC-CLM-2, REC-CLM-3, P-CLM-3, REC-CFG-1, REC-CFG-2,
W-PIN-3, REC-WRK-1, REC-OUT-1, REC-OUT-2, REC-DEL-1, REC-DEL-2, REC-DEL-3,
REC-DEL-4, REC-DEL-5, REC-DEL-6, P-REC-2, L3-DEL-15, C-OUT-1, REC-INT-1,
REC-INT-2, REC-INT-3, REC-INT-4, REC-INT-5, REC-INT-6, P-REC-1, P-REC-3,
P-REC-4.

This slice owns the startup entry point, the unconfirmed-Claim rows, the
interrupted Attempt and the recorded-outcome rows: a ``claim-confirmed``
Attempt with no recorded outcome is finished through the shared
claim-confirmed finalization — the same routine the live SIGTERM path calls,
now from startup recovery — then its recorded terminal writes are performed;
an ``outcome-recorded`` Attempt is finished from its recorded Terminal
Outcome without re-deciding it (ADR 0017) — the pull request found by head
branch and Attempt Marker, the terminal comment found by the full paginated
author-filtered lookup, then the labels and assignment finished with no
second comment. Recovery takes no model and no provider client: settling a
Claim is ledger reads plus at most one assignment removal, settling an
interrupted Attempt is git plus the recorded tracker writes, and finishing
recorded writes is ledger reads plus tracker writes, so no model call is
structurally possible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from coding_agent.ledger.store import (
    AttemptRecord,
    OutcomeRecord,
    RunLedger,
)
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.interrupted import (
    finalize_interrupted_attempt,
    perform_recorded_terminal_writes,
    settle_open_push_intents,
)
from coding_agent.runtime.publication import (
    ADD_READY_FOR_HUMAN_WRITE_KIND,
    DELIVERY_INTENT_KIND,
    PUBLISH_WRITE_KIND,
    RELEASE_ASSIGNMENT_WRITE_KIND,
    REMOVE_SELECTION_LABEL_WRITE_KIND,
    TERMINAL_COMMENT_KIND,
    TERMINAL_COMMENT_WRITE_KIND,
    WITHDRAWAL_REASON,
    attempt_marker,
    render_terminal_comment,
    terminal_next_action,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.runtime.shutdown import ABANDONED_OUTCOME, FAILED_OUTCOME
from coding_agent.tracker.port import TrackerPort

DELIVERED_OUTCOME = "delivered"
"""The Terminal Outcome with a pull request (§14, P-DEL-1)."""

BRANCH_MOVED_REASON = "branch-moved"
"""The Terminal Outcome reason when the remote branch is not at the Delivery
Intent's sha: ``failed`` / ``branch-moved`` with ``ready-for-human`` (§13).

No pull request opens; recovery records the superseding outcome before
finishing its terminal writes (REC-DEL-4).
"""


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
    a changed file, renamed labels or another Workflow still settles the
    open Attempt from its pinned values (REC-CFG-1). The names are resolved
    and validated here so the recorded-outcome slice reuses them when it
    removes a label by the pinned name, not the current one (W-PIN-3); the
    open-Claim path itself writes no label.
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
    """Attempt ids this pass settled: ``released`` (unconfirmed Claims) and
    ``finished`` (interrupted Attempts recovered to their Terminal Outcome)."""

    pending: tuple[str, ...]
    """Unfinished Attempt ids this slice leaves for later recovery slices
    (``outcome-recorded`` Attempts, whose remaining writes belong to the
    slice that follows)."""


@dataclass(frozen=True)
class InterruptedRecovery:
    """Where startup recovery settles an interrupted Attempt (§17).

    One Worker serves one Target Repository, so the workspace and the
    remote are fixed for the run: ``workspace_path`` is the state volume's
    checkout holding the Attempt's local commits (never rebuilt before the
    push — cleanup runs only when no unfinished Attempt exists), and
    ``remote_url`` is the canonical credential-free remote (a file path in
    scripted tests). ``evidence_root`` is the per-repository evidence
    directory, so the Attempt's own directory is
    ``evidence_root / <issue> / <n>``; its ``tool-results`` subtree holds
    the persisted Continuation Snapshots the recovery comment reads back
    (REC-INT-5). ``redact`` and ``helper`` authenticate git the same way
    the live Attempt does (§18); recovery performs no model call, so no
    provider client appears here.
    """

    workspace_path: Path
    """The state volume checkout carrying the Attempt's local commits."""

    remote_url: str | None
    """The canonical remote, or ``None`` where there is nothing to push to."""

    evidence_root: Path | None = None
    """The repository's evidence directory, or ``None`` for no snapshots."""

    redact: Redactable = None
    helper: GitCredentialHelper | None = None


def _issue_number(attempt_id: str) -> int:
    """The issue half of ``#<issue>/<n>``; refuses a malformed id."""
    try:
        head, _slash, _tail = attempt_id.partition("/")
        return int(head.removeprefix("#"))
    except ValueError as exc:
        raise RecoveryRefused(
            f"{attempt_id}: malformed Attempt id in the Run Ledger"
        ) from exc


def _attempt_number(attempt_id: str) -> int:
    """The Attempt half of ``#<issue>/<n>``; refuses a malformed id."""
    try:
        _head, _slash, tail = attempt_id.partition("/")
        return int(tail)
    except ValueError as exc:
        raise RecoveryRefused(
            f"{attempt_id}: malformed Attempt id in the Run Ledger"
        ) from exc


def recover_interrupted_attempt(
    *,
    tracker: TrackerPort,
    ledger: RunLedger,
    attempt: AttemptRecord,
    recovery: InterruptedRecovery,
) -> bool:
    """Settle one ``claim-confirmed`` Attempt with no recorded outcome (§17).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (the recovery
    table row "Claim confirmed, no outcome recorded": preserve — push local
    commits that descend from the remote head, fast-forward only, never
    commit the dirty tree; decide — re-read the issue once, closed or
    Selection Label gone is ``abandoned``, otherwise ``failed`` /
    ``interrupted``; record the outcome, then perform its terminal writes).

    MVP row ids: REC-INT-1, REC-INT-2, REC-INT-3, REC-INT-4, REC-INT-5,
    REC-INT-6, P-REC-1, P-REC-3, P-REC-4.

    Runs the shared claim-confirmed finalization — the same routine the
    live SIGTERM path calls, now at startup — after receipting any open
    push intent the remote already holds (REC-INT-4), then performs the
    recorded terminal writes (the comment, the Selection Label removal,
    ``ready-for-human``, the Worker's own assignment removal — exactly one
    comment, and no pull request, P-REC-3) and marks the Attempt
    ``finished``. The Delivery Snapshot a crashed final check left pushed
    is kept (REC-INT-6); a foreign remote head is never overwritten
    (P-REC-4). Returns ``True`` where the Attempt was settled to
    ``finished``; a ``claim-confirmed`` Attempt that already recorded its
    outcome belongs to the outcome-recorded slice and returns ``False``.
    Tracker and git errors propagate: the poll loop retries the recovery on
    the next poll cycle and halts after three consecutive failures.
    """
    if attempt.state != "claim-confirmed":
        return False
    if ledger.get_outcome(attempt.attempt_id) is not None:
        return False
    pinned = pinned_recovery_config(attempt)
    issue_number = _issue_number(attempt.attempt_id)
    attempt_number = _attempt_number(attempt.attempt_id)
    issue_title = tracker.fetch_candidate(issue_number).title
    settle_open_push_intents(
        ledger=ledger,
        attempt_id=attempt.attempt_id,
        remote_url=recovery.remote_url,
        redact=recovery.redact,
        helper=recovery.helper,
    )
    evidence_dir: Path | None = None
    if recovery.evidence_root is not None:
        evidence_dir = (
            recovery.evidence_root / str(issue_number) / str(attempt_number)
        )
    finalize_interrupted_attempt(
        ledger=ledger,
        tracker=tracker,
        workspace_path=recovery.workspace_path,
        attempt_id=attempt.attempt_id,
        issue_number=issue_number,
        attempt_number=attempt_number,
        issue_title=issue_title,
        remote_url=recovery.remote_url,
        base_revision=attempt.base_revision or "",
        workflow_id=_pinned_workflow_id(attempt),
        configuration_digest=attempt.effective_configuration_digest,
        selection_label=pinned.selection_label,
        redact=recovery.redact,
        helper=recovery.helper,
        evidence_dir=evidence_dir,
        use_persisted_snapshot=True,
    )
    perform_recorded_terminal_writes(
        ledger=ledger,
        tracker=tracker,
        attempt_id=attempt.attempt_id,
        issue_number=issue_number,
    )
    ledger.set_attempt_state(attempt.attempt_id, "finished")
    return True


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
      (REC-CLM-2: a crash after ``assign``, before the read-back), matched
      by the startup login; the pinned numeric identity id is kept for the
      later slices' author-filtered lookups. No comment is written and no
      label changes.
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


def _pinned_workflow_id(attempt: AttemptRecord) -> str:
    """The Workflow id the Attempt was pinned to (contract §2.5, §15).

    The terminal comment discloses it; recovery reads it from the Attempt's
    own Effective Configuration record, never the current YAML (REC-CFG-1).
    """
    record = attempt.effective_configuration
    workflow = record.get("workflow")
    if isinstance(workflow, Mapping):
        workflow_id = workflow.get("id")
        if isinstance(workflow_id, str) and workflow_id:
            return workflow_id
    raise RecoveryRefused(
        f"{attempt.attempt_id}: Effective Configuration record is "
        "missing its workflow id"
    )


def _delivery_intent_sha(
    ledger: RunLedger, attempt_id: str
) -> tuple[str, str] | None:
    """The ``(branch, head_sha)`` the Delivery Intent recorded (§12, §13).

    The ``delivery_intent`` event carries the Attempt id, branch, head sha,
    verdict and writes; the ``open-pull-request`` intent effect carries the
    same branch and sha. Either decides, the event first. ``None`` where
    neither was recorded: the crash came before the Intent, which cannot
    happen for a recorded ``delivered`` outcome — the caller treats it as a
    refusal rather than guessing.
    """
    for record in ledger.records(attempt_id):
        if record.kind != DELIVERY_INTENT_KIND:
            continue
        payload = record.payload
        branch = payload.get("branch")
        head_sha = payload.get("head_sha")
        if isinstance(branch, str) and isinstance(head_sha, str):
            return (branch, head_sha)
    effect = ledger.get_effect(attempt_id, PUBLISH_WRITE_KIND)
    if effect is not None and isinstance(effect.payload, dict):
        branch = effect.payload.get("branch")
        head_sha = effect.payload.get("head_sha")
        if isinstance(branch, str) and isinstance(head_sha, str):
            return (branch, head_sha)
    return None


def _finish_terminal_writes(
    *,
    tracker: TrackerPort,
    ledger: RunLedger,
    attempt: AttemptRecord,
    stored: OutcomeRecord,
    pinned: PinnedRecoveryConfig,
    agent_login: str,
) -> None:
    """Perform the recorded outcome's remaining terminal writes (§13, §14).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (each write is
    looked up before it is written: a present comment is the receipt and is
    never written twice — never a second comment; labels and assignment are
    read back, so a finished write is a no-op) and §14 (the writes run in
    C-OUT-1 order from the recorded outcome, which recovery never
    re-decides, ADR 0017).

    MVP row ids: REC-OUT-1, REC-OUT-2, REC-DEL-2, C-OUT-1.
    """
    attempt_id = attempt.attempt_id
    issue_number = _issue_number(attempt_id)
    terminal_marker = attempt_marker(attempt_id, kind=TERMINAL_COMMENT_KIND)
    for write_kind in stored.writes:
        if write_kind == PUBLISH_WRITE_KIND:
            continue
        if write_kind == TERMINAL_COMMENT_WRITE_KIND:
            found = tracker.find_terminal_comment(issue_number, terminal_marker)
            if found is not None:
                continue
            tracker.post_terminal_comment(
                issue_number, stored.terminal_comment_body
            )
        elif write_kind == REMOVE_SELECTION_LABEL_WRITE_KIND:
            state = tracker.read_issue_state(issue_number)
            if pinned.selection_label in state.labels:
                tracker.remove_selection_label(issue_number)
        elif write_kind == ADD_READY_FOR_HUMAN_WRITE_KIND:
            state = tracker.read_issue_state(issue_number)
            if pinned.ready_for_human not in state.labels:
                tracker.add_ready_for_human(issue_number)
        elif write_kind == RELEASE_ASSIGNMENT_WRITE_KIND:
            if attempt.assigned_by_us is True:
                fresh = tracker.fetch_candidate(issue_number)
                if agent_login in fresh.assignees:
                    tracker.release_assignment(fresh.number)
        else:
            raise AssertionError(f"unknown terminal write kind: {write_kind!r}")


def _record_branch_moved(
    *,
    ledger: RunLedger,
    attempt: AttemptRecord,
    stored: OutcomeRecord,
    branch: str,
    intent_sha: str,
    remote_sha: str | None,
    workflow_id: str,
) -> OutcomeRecord:
    """Supersede a Delivery Intent whose branch moved (contract §13).

    The remote branch is not at the Intent's sha: there is no pull request
    and the outcome is ``failed`` / ``branch-moved`` with
    ``ready-for-human`` (REC-DEL-4). The superseding outcome is recorded
    before its terminal writes are performed — recovery still finishes a
    recorded outcome and never re-decides one (ADR 0017).
    """
    detail = (
        f"the remote branch {branch} is not at the Delivery Intent's sha "
        f"(expected {intent_sha}, found {remote_sha}); no pull request was opened."
    )
    comment_body = render_terminal_comment(
        attempt_id=attempt.attempt_id,
        outcome=FAILED_OUTCOME,
        reason=BRANCH_MOVED_REASON,
        pull_request_url=None,
        branch_name=branch,
        workflow_id=workflow_id,
        configuration_digest=attempt.effective_configuration_digest,
        unmet_conditions=(),
        next_action=terminal_next_action(BRANCH_MOVED_REASON),
        detail=detail,
    )
    return ledger.update_outcome_for_publish(
        attempt.attempt_id,
        outcome=FAILED_OUTCOME,
        reason=BRANCH_MOVED_REASON,
        pull_request_body=stored.pull_request_body,
        terminal_comment_body=comment_body,
        writes=list(terminal_writes_for_outcome(outcome=FAILED_OUTCOME)),
    )


def _record_withdrawal(
    *,
    ledger: RunLedger,
    attempt: AttemptRecord,
    stored: OutcomeRecord,
    branch: str,
    pull_number: int,
    pull_url: str,
    workflow_id: str,
) -> OutcomeRecord:
    """Supersede a Delivery Intent a human withdrew (contract §13).

    A pull request from this branch that a human closed is a withdrawal,
    not an absence: the Attempt ends ``abandoned`` with one comment naming
    it, and the pull request is never reopened (L3-DEL-14, REC-DEL-3). The
    superseding outcome is recorded before its terminal writes — recovery
    still finishes a recorded outcome (ADR 0017).
    """
    detail = (
        f"the pull request #{pull_number} from this branch was closed "
        "by a human before its receipt (withdrawal); it will not be "
        "reopened."
    )
    comment_body = render_terminal_comment(
        attempt_id=attempt.attempt_id,
        outcome=ABANDONED_OUTCOME,
        reason=WITHDRAWAL_REASON,
        pull_request_url=pull_url or None,
        branch_name=branch,
        workflow_id=workflow_id,
        configuration_digest=attempt.effective_configuration_digest,
        unmet_conditions=(),
        next_action=terminal_next_action(WITHDRAWAL_REASON),
        detail=detail,
    )
    return ledger.update_outcome_for_publish(
        attempt.attempt_id,
        outcome=ABANDONED_OUTCOME,
        reason=WITHDRAWAL_REASON,
        pull_request_body=stored.pull_request_body,
        terminal_comment_body=comment_body,
        writes=[
            PUBLISH_WRITE_KIND,
            *terminal_writes_for_outcome(outcome=ABANDONED_OUTCOME),
        ],
    )


def _record_delivered_receipt(
    *,
    ledger: RunLedger,
    stored: OutcomeRecord,
    attempt_id: str,
    branch: str,
    head_sha: str,
    workflow_id: str,
    digest: str,
    pull_number: int,
    pull_url: str,
) -> OutcomeRecord:
    """Record the pull-request receipt and link it in the comment (§13, §15).

    The same deterministic bytes the live ``publish`` node records: the
    receipt carries the pull-request number and URL, and the terminal
    comment links the opened pull request. Recorded before the terminal
    writes perform anything, so a retried recovery still sends the same
    bytes. A comment already linking the URL is left alone.
    """
    ledger.record_effect_receipt(
        attempt_id,
        PUBLISH_WRITE_KIND,
        remote_id=str(pull_number),
        payload={
            "attempt_id": attempt_id,
            "branch": branch,
            "head_sha": head_sha,
            "issue_number": _issue_number(attempt_id),
            "verdict": "verified-completion",
            "pull_request_body": stored.pull_request_body,
            "pull_request_number": pull_number,
            "pull_request_url": pull_url,
        },
    )
    if pull_url and pull_url not in stored.terminal_comment_body:
        comment_body = render_terminal_comment(
            attempt_id=attempt_id,
            outcome=stored.outcome,
            reason=stored.reason,
            pull_request_url=pull_url,
            branch_name=branch,
            workflow_id=workflow_id,
            configuration_digest=digest,
        )
        return ledger.update_outcome_for_publish(
            attempt_id,
            outcome=stored.outcome,
            reason=stored.reason,
            pull_request_body=stored.pull_request_body,
            terminal_comment_body=comment_body,
            writes=list(stored.writes),
        )
    return stored


def recover_recorded_outcome(
    tracker: TrackerPort,
    ledger: RunLedger,
    attempt: AttemptRecord,
    *,
    agent_login: str,
    remote_branch_sha: Callable[[str], str | None] | None = None,
) -> bool:
    """Finish an ``outcome-recorded`` Attempt from its recorded outcome (§17).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (the recovery
    table row "A Delivery Intent recorded, writes partly done": check the
    branch, find the pull request by head branch and marker, then the
    comment, labels and assignment, each looked up before it is written;
    the row "An outcome without a pull request recorded, writes partly
    done": finish the remaining writes), §14 ("Recording before
    publishing": the outcome is recorded before its first write, so
    recovery finishes it and never re-decides it) and §17 (recovery never
    calls the model; pinned values only; rendered bodies come from the
    ledger).

    MVP row ids: REC-OUT-1, REC-OUT-2, REC-DEL-1, REC-DEL-2, REC-DEL-3,
    REC-DEL-4, REC-DEL-5, REC-DEL-6, P-REC-2, C-OUT-1.

    - Where the recorded outcome calls for a pull request (``delivered``):
      the remote branch must be at the Intent's sha, otherwise the outcome
      is superseded to ``failed`` / ``branch-moved`` with
      ``ready-for-human`` and no pull request opens (REC-DEL-4). The pull
      request is found by head branch and Attempt Marker: an open one is
      the receipt and none is created (REC-DEL-1, P-REC-2); a human-closed
      one is a withdrawal — the outcome is superseded to ``abandoned``
      with one comment and never reopened (REC-DEL-3); an absent one is
      created from the recorded body. Then the comment, labels and
      assignment follow (REC-DEL-2).
    - Where it does not: the labels and assignment are finished with no
      second comment, and the recorded outcome (for example
      ``failed-limit``) stands (REC-OUT-1). A readiness refusal recorded
      before the comment ends with exactly one comment carrying the
      recorded reason (REC-OUT-2).

    The terminal comment is found by the full paginated lookup filtered to
    the Agent Identity's numeric id (REC-DEL-5); a comment by another
    account carrying our marker is never our receipt (REC-DEL-6). Takes no
    model: every byte sent was rendered before the crash. Returns ``True``
    where the Attempt was finished. Tracker errors propagate: the poll
    loop retries the write on the next poll cycle and halts after three
    consecutive failures (REC-WRK-1).
    """
    if attempt.state != "outcome-recorded":
        return False
    stored = ledger.get_outcome(attempt.attempt_id)
    if stored is None:
        raise RecoveryRefused(
            f"{attempt.attempt_id}: outcome-recorded with no outcomes row"
        )
    pinned = pinned_recovery_config(attempt)
    workflow_id = _pinned_workflow_id(attempt)
    if stored.outcome == DELIVERED_OUTCOME:
        intent = _delivery_intent_sha(ledger, attempt.attempt_id)
        if intent is None:
            raise RecoveryRefused(
                f"{attempt.attempt_id}: delivered with no Delivery Intent record"
            )
        branch, head_sha = intent
        if remote_branch_sha is None:
            raise GitFailure(
                f"cannot check remote branch {branch}: no remote accessor"
            )
        remote_sha = remote_branch_sha(branch)
        if remote_sha != head_sha:
            stored = _record_branch_moved(
                ledger=ledger,
                attempt=attempt,
                stored=stored,
                branch=branch,
                intent_sha=head_sha,
                remote_sha=remote_sha,
                workflow_id=workflow_id,
            )
            _finish_terminal_writes(
                tracker=tracker,
                ledger=ledger,
                attempt=attempt,
                stored=stored,
                pinned=pinned,
                agent_login=agent_login,
            )
            ledger.set_attempt_state(attempt.attempt_id, "finished")
            return True
        delivery_marker = attempt_marker(
            attempt.attempt_id, kind="delivery"
        )
        found = tracker.find_pull_request(branch, delivery_marker)
        if found is not None and found.state == "open":
            stored = _record_delivered_receipt(
                ledger=ledger,
                stored=stored,
                attempt_id=attempt.attempt_id,
                branch=branch,
                head_sha=head_sha,
                workflow_id=workflow_id,
                digest=attempt.effective_configuration_digest,
                pull_number=found.number,
                pull_url=found.url,
            )
        elif found is not None:
            stored = _record_withdrawal(
                ledger=ledger,
                attempt=attempt,
                stored=stored,
                branch=branch,
                pull_number=found.number,
                pull_url=found.url,
                workflow_id=workflow_id,
            )
            ledger.record_effect_receipt(
                attempt.attempt_id,
                PUBLISH_WRITE_KIND,
                remote_id=str(found.number),
                payload={
                    "attempt_id": attempt.attempt_id,
                    "branch": branch,
                    "head_sha": head_sha,
                    "pull_request_number": found.number,
                    "pull_request_url": found.url,
                    "withdrawal": True,
                },
            )
        else:
            title = tracker.fetch_issue(
                _issue_number(attempt.attempt_id)
            ).title
            created = tracker.open_pull_request(
                title=title,
                head_branch=branch,
                base_branch=pinned.base_branch,
                body=stored.pull_request_body,
            )
            stored = _record_delivered_receipt(
                ledger=ledger,
                stored=stored,
                attempt_id=attempt.attempt_id,
                branch=branch,
                head_sha=head_sha,
                workflow_id=workflow_id,
                digest=attempt.effective_configuration_digest,
                pull_number=created.number,
                pull_url=created.url,
            )
    _finish_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt=attempt,
        stored=stored,
        pinned=pinned,
        agent_login=agent_login,
    )
    ledger.set_attempt_state(attempt.attempt_id, "finished")
    return True


def recover_unfinished_attempts(
    tracker: TrackerPort,
    ledger: RunLedger,
    *,
    agent_login: str,
    remote_branch_sha: Callable[[str], str | None] | None = None,
    interrupted: InterruptedRecovery | None = None,
) -> RecoverySummary:
    """Settle every unfinished Attempt the ledger holds, before selection.

    Validates every pinned record first (REC-CFG-2), so a refusal performs
    no write; then settles each ``open`` Attempt (REC-CLM-1, REC-CLM-2),
    finishes each ``outcome-recorded`` Attempt from its recorded outcome
    (REC-OUT-1, REC-OUT-2, REC-DEL-1 … REC-DEL-4, P-REC-2), and, where
    ``interrupted`` is provided, each ``claim-confirmed`` Attempt with no
    recorded outcome (REC-INT-1 … REC-INT-6). Without it a
    ``claim-confirmed`` Attempt without a recorded outcome reports as
    pending and intake stays closed. Takes no model: settling a Claim is a
    ledger read plus at most one assignment removal, settling an
    interrupted Attempt is git plus the recorded tracker writes, and
    finishing a recorded outcome replays ledger bytes plus tracker writes.
    """
    unfinished = validate_recovery_configs(ledger)
    settled: list[str] = []
    pending: list[str] = []
    for attempt in unfinished:
        if recover_open_attempt(
            tracker, ledger, attempt, agent_login=agent_login
        ):
            settled.append(attempt.attempt_id)
        elif (
            interrupted is not None
            and attempt.state == "claim-confirmed"
            and recover_interrupted_attempt(
                tracker=tracker,
                ledger=ledger,
                attempt=attempt,
                recovery=interrupted,
            )
        ):
            settled.append(attempt.attempt_id)
            continue
        if recover_recorded_outcome(
            tracker,
            ledger,
            attempt,
            agent_login=agent_login,
            remote_branch_sha=remote_branch_sha,
        ):
            settled.append(attempt.attempt_id)
        else:
            pending.append(attempt.attempt_id)
    return RecoverySummary(settled=tuple(settled), pending=tuple(pending))
