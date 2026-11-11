"""Step-workflow restart settlement and recovery (§14.2, §16).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14.2
(settlement on restart: look the write up remotely by its Effect Intent key;
a landed write records the receipt and is never written twice; an absent
write re-checks the factual preconditions and is performed once where they
hold, otherwise recorded ``not-performed``; a human-closed own pull request
is never re-created), §14.3 (no point of no return; landed effects are never
reverted), §12 (a runtime-only ending discards an in-flight model request
and settles an in-flight write; the dirty tree is never committed or
pushed; no further Workflow Step runs), §15 (finalisation) and §16
(restart never enters the Workflow graph and never runs a Workflow Step;
an unconfirmed Claim is released; a Credential Failure ends the Attempt
``interrupted`` / ``credential-failure`` and halts the Worker; three
consecutive recovery write failures halt with ``recovery-write-failure``).

Row ids: REC-5 … REC-10, REC-12, REC-15 … REC-21, REC-24, REC-25.

Reuses :mod:`coding_agent.runtime.shutdown` for the SIGTERM flag,
:mod:`coding_agent.runtime.step_finalisation` for the write-once outcome and
the terminal writes, and ``inputs.claim.recover_unconfirmed_claim`` for the
unconfirmed-Claim path. Never calls the model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from coding_agent.github.errors import CredentialFailure
from coding_agent.inputs.claim import recover_unconfirmed_claim
from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import InputType
from coding_agent.ledger.store import (
    RunLedger,
    StepEffectRecord,
    step_effect_marker,
)
from coding_agent.runtime.step_finalisation import (
    FINALISATION_EXECUTION,
    FINALISATION_STEP,
    TERMINAL_COMMENT_KIND,
    filter_end_labels,
    perform_terminal_writes,
    record_terminal_outcome,
    render_terminal_comment,
    terminal_writes_for_outcome,
)

PROCESS_LOSS_REASON = "process-loss"
"""The ``interrupted`` reason for a process loss settled at restart (§16.1)."""

SHUTDOWN_REASON = "shutdown"
"""The ``interrupted`` reason for a SIGTERM-ended live Attempt (§12)."""

CREDENTIAL_FAILURE_REASON = "credential-failure"
"""The ``interrupted`` reason for a Credential Failure (§16.3)."""

RECOVERY_WRITE_HALT_REASON = "recovery-write-failure"
"""The Worker halt after three consecutive recovery write failures (§16.3)."""

MAX_CONSECUTIVE_RECOVERY_FAILURES = 3
"""The halt bound for consecutive recovery write failures (§16.3)."""


class SettlementTracker(Protocol):
    """The tracker surface restart settlement needs (§14.2)."""

    @property
    def agent_login(self) -> str: ...
    @property
    def agent_id(self) -> int | None: ...

    def list_comments(self, number: int, input_type: InputType) -> Sequence[Any]: ...
    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: str
    ) -> Any | None: ...
    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> Any: ...
    def find_review_by_marker(self, number: int, marker: str) -> Any | None: ...
    def publish_review(self, number: int, *, commit_id: str, body: str) -> Any: ...
    def find_pull_request(self, head_branch: str, marker: str) -> Any | None: ...
    def open_pull_request(
        self,
        *,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        draft: bool = False,
    ) -> Any: ...
    def fetch_input(self, number: int, input_type: InputType) -> Any: ...
    def remove_label(self, number: int, input_type: InputType, label: str) -> None: ...
    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None: ...
    def unassign_input(self, number: int, input_type: InputType) -> None: ...


GateCheck = Callable[[], GateResult]
"""Re-read the input and answer the Gate before a settled write (§7.3)."""


def _intent_body(intent: StepEffectRecord) -> str:
    """The write body carried in an intent's rendered payload.

    Publishing intents render ``"<summary line>\\n<body>"``; the terminal
    comment intent carries the body verbatim. The marker travels in the body
    itself, so the lookup key is the marker, never this text.
    """
    payload = intent.rendered_payload
    _, sep, rest = payload.partition("\n")
    return rest if sep else payload


def _marker_for(intent: StepEffectRecord, kind: str) -> str:
    return step_effect_marker(
        intent.attempt_id,
        intent.step_id,
        intent.execution_number,
        intent.ordinal,
        kind=kind,
    )


def _operation_of(intent: StepEffectRecord) -> str:
    target = intent.expected_target
    operation = target.get("operation")
    return str(operation) if isinstance(operation, str) else ""


def settle_step_effect(
    *,
    tracker: SettlementTracker,
    ledger: RunLedger,
    intent: StepEffectRecord,
    number: int,
    input_type: InputType,
    gate: GateCheck | None = None,
    claim_active: bool = True,
) -> str:
    """Settle one intent without a receipt by key lookup (REC-5 … REC-8) (§14.2).

    Returns the settlement: ``"receipt"`` where the write had landed (the
    receipt is recorded, nothing written twice), ``"performed"`` where the
    absent write was performed once after the preconditions held, or
    ``"not-performed"`` with its reason recorded. A human-closed own pull
    request is never re-created: it settles ``not-performed`` and is
    reported. :class:`CredentialFailure` propagates: the caller records
    ``interrupted`` / ``credential-failure`` and halts (REC-17).
    """
    if intent.status != "intent":
        return intent.status
    operation = _operation_of(intent)
    kind = operation if operation else TERMINAL_COMMENT_KIND
    marker = _marker_for(intent, kind)
    if operation in ("post-comment", TERMINAL_COMMENT_KIND, "terminal", ""):
        existing = tracker.find_comment_by_marker(number, input_type, marker)
        if existing is not None:
            remote = getattr(existing, "comment_id", None)
            ledger.record_step_effect_receipt(
                intent.attempt_id,
                intent.step_id,
                intent.execution_number,
                intent.ordinal,
                remote_id=str(remote) if isinstance(remote, int) else None,
            )
            return "receipt"
    elif operation == "publish-review":
        existing_review = tracker.find_review_by_marker(number, marker)
        if existing_review is not None:
            remote_review = getattr(existing_review, "review_id", None)
            ledger.record_step_effect_receipt(
                intent.attempt_id,
                intent.step_id,
                intent.execution_number,
                intent.ordinal,
                remote_id=str(remote_review)
                if isinstance(remote_review, int)
                else None,
            )
            return "receipt"
    elif operation == "open-pull-request":
        target = intent.expected_target
        head_branch = target.get("head_branch")
        branch = str(head_branch) if isinstance(head_branch, str) else ""
        existing_pr = tracker.find_pull_request(branch, marker)
        if existing_pr is not None:
            remote_pr = getattr(existing_pr, "number", None)
            ledger.record_step_effect_receipt(
                intent.attempt_id,
                intent.step_id,
                intent.execution_number,
                intent.ordinal,
                remote_id=str(remote_pr) if isinstance(remote_pr, int) else None,
            )
            return "receipt"
    else:
        # Assignment, label and push intents: the terminal path replays them
        # idempotently; unknown step kinds settle through the same lookup
        # only where a marker exists, otherwise they are left for the
        # terminal writes.
        return intent.status
    if not claim_active:
        ledger.record_step_effect_not_performed(
            intent.attempt_id,
            intent.step_id,
            intent.execution_number,
            intent.ordinal,
            reason="withdrawn: no active Claim for this Attempt",
        )
        return "not-performed"
    if gate is not None:
        verdict = gate()
        if verdict.status != "ok":
            ledger.record_step_effect_not_performed(
                intent.attempt_id,
                intent.step_id,
                intent.execution_number,
                intent.ordinal,
                reason=f"{verdict.status}: {verdict.reason or verdict.status}",
            )
            return "not-performed"
    body = _intent_body(intent)
    target_map = dict(intent.expected_target)
    if operation in ("post-comment", TERMINAL_COMMENT_KIND, "terminal", ""):
        posted = tracker.post_comment(number, input_type, body)
        posted_id = getattr(posted, "comment_id", None)
        ledger.record_step_effect_receipt(
            intent.attempt_id,
            intent.step_id,
            intent.execution_number,
            intent.ordinal,
            remote_id=str(posted_id) if isinstance(posted_id, int) else None,
        )
        return "performed"
    if operation == "publish-review":
        commit = target_map.get("commit_id")
        commit_id = str(commit) if isinstance(commit, str) and commit else ""
        if not commit_id:
            ledger.record_step_effect_not_performed(
                intent.attempt_id,
                intent.step_id,
                intent.execution_number,
                intent.ordinal,
                reason="error: no commit id in the recorded intent",
            )
            return "not-performed"
        published = tracker.publish_review(number, commit_id=commit_id, body=body)
        published_id = getattr(published, "review_id", None)
        ledger.record_step_effect_receipt(
            intent.attempt_id,
            intent.step_id,
            intent.execution_number,
            intent.ordinal,
            remote_id=str(published_id)
            if isinstance(published_id, int)
            else None,
        )
        return "performed"
    if operation == "open-pull-request":
        head = target_map.get("head_branch")
        base = target_map.get("base_branch")
        title = target_map.get("title")
        draft = target_map.get("draft")
        head_branch = str(head) if isinstance(head, str) else ""
        base_branch = str(base) if isinstance(base, str) else "main"
        title_text = str(title) if isinstance(title, str) else intent.attempt_id
        # A human-closed own pull request found at restart is never
        # re-created (REC-8): the lookup above returned nothing only where
        # no open or closed ours exists, so reaching here means absent.
        opened = tracker.open_pull_request(
            title=title_text,
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
            draft=bool(draft) if isinstance(draft, bool) else False,
        )
        opened_number = getattr(opened, "number", None)
        ledger.record_step_effect_receipt(
            intent.attempt_id,
            intent.step_id,
            intent.execution_number,
            intent.ordinal,
            remote_id=str(opened_number)
            if isinstance(opened_number, int)
            else None,
        )
        return "performed"
    return intent.status


def settle_open_step_effects(
    *,
    tracker: SettlementTracker,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    gate: GateCheck | None = None,
    claim_active: bool = True,
) -> dict[str, int]:
    """Settle every intent without a receipt (REC-5 … REC-8) (§14.2).

    Looks each write up remotely by its Effect Intent key before writing
    anything; landed writes record the receipt, absent writes are performed
    once where the preconditions hold and recorded ``not-performed``
    otherwise. Returns the settlement counts.
    """
    settled = {"receipt": 0, "performed": 0, "not-performed": 0}
    for intent in ledger.list_step_effects(attempt_id):
        if intent.status != "intent":
            continue
        # Terminal and Claim writes belong to the finalisation path below;
        # settling them here would double-post the terminal comment.
        if intent.step_id == FINALISATION_STEP and intent.execution_number == (
            FINALISATION_EXECUTION
        ):
            continue
        if intent.step_id == "" and intent.execution_number == 0 and intent.ordinal in (
            0,
            1,
        ):
            continue
        outcome = settle_step_effect(
            tracker=tracker,
            ledger=ledger,
            intent=intent,
            number=number,
            input_type=input_type,
            gate=gate,
            claim_active=claim_active,
        )
        if outcome in settled:
            settled[outcome] += 1
    return settled


@dataclass(frozen=True)
class RestartDecision:
    """What a restart settles an unfinished Attempt to (§16.1)."""

    outcome: str
    reason: str


def decide_restart_outcome(gate_result: GateResult | None) -> RestartDecision:
    """Decide the restart outcome: withdrawn, drift, or interrupted (REC-12).

    A recorded outcome is finished as recorded and never reaches here. An
    input withdrawn meanwhile ends ``withdrawn``; Drift ends ``drift``;
    anything else ends ``interrupted`` / ``process-loss``. No Workflow Step
    runs on any of these paths.
    """
    if gate_result is not None:
        if gate_result.status == "withdrawn":
            return RestartDecision(
                outcome="withdrawn",
                reason=f"withdrawn: {gate_result.reason or 'withdrawn'}",
            )
        if gate_result.status == "drift":
            return RestartDecision(
                outcome="drift", reason=f"drift: {gate_result.reason or 'drift'}"
            )
    return RestartDecision(outcome="interrupted", reason=PROCESS_LOSS_REASON)


def close_open_executions(ledger: RunLedger, attempt_id: str, *, reason: str) -> int:
    """Close every open Step Execution as interrupted (REC-12) (§13.2).

    A runtime fact, never a Step Result, never routed; no Workflow Step runs.
    """
    return ledger.close_interrupted_step_executions(attempt_id, reason=reason)


@dataclass
class RecoveryFailureTracker:
    """Count consecutive recovery write failures toward the halt (REC-18).

    Three consecutive recovery write failures halt the Worker with
    ``recovery-write-failure`` (§16.3). A success resets the count.
    """

    consecutive_failures: int = 0

    def note_success(self) -> None:
        """Reset the count after a recovery pass with no write failure."""
        self.consecutive_failures = 0

    def note_failure(self) -> bool:
        """Count one failed recovery pass; ``True`` where the Worker halts."""
        self.consecutive_failures += 1
        return self.consecutive_failures >= MAX_CONSECUTIVE_RECOVERY_FAILURES


def should_halt_after_failures(consecutive_failures: int) -> bool:
    """Whether the halt-after-three bound is reached (REC-18) (§16.3)."""
    return consecutive_failures >= MAX_CONSECUTIVE_RECOVERY_FAILURES


@dataclass(frozen=True)
class RecoveredAttempt:
    """What one restart recovery did with an unfinished Attempt (§16.1)."""

    attempt_id: str
    outcome: str | None
    reason: str | None
    released: bool = False
    finished: bool = False


def recover_unconfirmed_attempt(
    *,
    tracker: SettlementTracker,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
) -> RecoveredAttempt:
    """Release an unconfirmed Claim found at restart (REC-10) (§16.2).

    Deletes the Worker's own Claim Comment by key, removes the assignment
    if no other Claim is in force, and marks the record ``released``. There
    is no Terminal Outcome and no terminal comment.
    """
    recover_unconfirmed_claim(tracker, ledger, attempt_id, number, input_type)  # type: ignore[arg-type]
    return RecoveredAttempt(
        attempt_id=attempt_id, outcome=None, reason=None, released=True
    )


def recover_attempt(
    *,
    tracker: SettlementTracker,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels: Sequence[str] = (),
    gate: GateCheck | None = None,
    claim_active: bool = True,
    uncommitted_count: int = 0,
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    limit_fact: str = "",
) -> RecoveredAttempt:
    """Settle and finalise one unfinished Attempt at restart (REC-12) (§16.1).

    Never enters the Workflow graph and never runs a Workflow Step, never
    calls the model: settles the effect that was in flight (§14.2), closes
    any open Step Execution as interrupted by process loss (§13.2), then
    finishes the lifecycle (§15.2). A recorded outcome is finished as
    recorded and never re-decided; otherwise the Attempt ends
    ``interrupted`` / ``process-loss``, ``withdrawn`` where the input was
    withdrawn, or ``drift`` where the Gate finds Drift. The dirty tree is
    never committed or pushed; the terminal comment reports the
    uncommitted-file count (REC-21). Landed effects are never reverted
    (REC-15, §14.3). A :class:`CredentialFailure` records
    ``interrupted`` / ``credential-failure`` before propagating so the next
    start finishes the terminal writes after the credential is fixed
    (REC-17).
    """
    try:
        settle_open_step_effects(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt_id,
            number=number,
            input_type=input_type,
            gate=gate,
            claim_active=claim_active,
        )
    except CredentialFailure:
        close_open_executions(
            ledger, attempt_id, reason=CREDENTIAL_FAILURE_REASON
        )
        _record_credential_failure(
            ledger,
            attempt_id,
            ending_step=ending_step,
            step_result=step_result,
            workflow_end=workflow_end,
            limit_fact=limit_fact,
            uncommitted_count=uncommitted_count,
        )
        raise
    close_open_executions(ledger, attempt_id, reason=PROCESS_LOSS_REASON)
    stored = ledger.get_outcome(attempt_id)
    if stored is not None:
        try:
            perform_terminal_writes(
                tracker=tracker,
                ledger=ledger,
                attempt_id=attempt_id,
                number=number,
                input_type=input_type,
                selection_label=selection_label,
                end_labels=end_labels,
                comment_body=stored.terminal_comment_body,
                writes=stored.writes,
            )
        except CredentialFailure:
            raise
        ledger.set_attempt_state(attempt_id, "finished")
        return RecoveredAttempt(
            attempt_id=attempt_id,
            outcome=stored.outcome,
            reason=stored.reason,
            finished=True,
        )
    gate_result: GateResult | None = None
    if gate is not None:
        try:
            gate_result = gate()
        except CredentialFailure:
            raise
        except Exception:
            gate_result = None
    decision = decide_restart_outcome(gate_result)
    landed = _landed_summaries(ledger, attempt_id)
    not_performed = _not_performed_summaries(ledger, attempt_id)
    if decision.outcome == "withdrawn":
        ending = ending_step or "gate"
        result = step_result or "withdrawn"
        end = workflow_end or "-"
    elif decision.outcome == "drift":
        ending = ending_step or "gate"
        result = step_result or "drift"
        end = workflow_end or "-"
    else:
        ending = ending_step
        result = step_result
        end = workflow_end
    comment = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=decision.outcome,
        ending_step=ending,
        step_result=result,
        workflow_end=end,
        limit_fact=limit_fact or decision.reason,
        landed=landed,
        not_performed=not_performed,
        uncommitted_count=uncommitted_count,
    )
    record_terminal_outcome(
        ledger,
        attempt_id,
        outcome=decision.outcome,
        reason=decision.reason
        if decision.outcome == "interrupted"
        else f"{decision.outcome}: {decision.reason}",
        terminal_comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    try:
        perform_terminal_writes(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt_id,
            number=number,
            input_type=input_type,
            selection_label=selection_label,
            end_labels=end_labels,
            comment_body=comment,
            writes=terminal_writes_for_outcome(),
        )
    except CredentialFailure:
        _record_credential_failure(
            ledger,
            attempt_id,
            ending_step=ending,
            step_result=result,
            workflow_end=end,
            limit_fact=limit_fact,
            uncommitted_count=uncommitted_count,
        )
        raise
    ledger.set_attempt_state(attempt_id, "finished")
    return RecoveredAttempt(
        attempt_id=attempt_id,
        outcome=decision.outcome,
        reason=decision.reason,
        finished=True,
    )


def _record_credential_failure(
    ledger: RunLedger,
    attempt_id: str,
    *,
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    limit_fact: str = "",
    uncommitted_count: int = 0,
) -> None:
    stored = ledger.get_outcome(attempt_id)
    if stored is not None:
        return
    comment = render_terminal_comment(
        attempt_id=attempt_id,
        outcome="interrupted",
        ending_step=ending_step,
        step_result=step_result,
        workflow_end=workflow_end,
        limit_fact=limit_fact or CREDENTIAL_FAILURE_REASON,
        uncommitted_count=uncommitted_count,
        detail="credential-failure: the Worker halts; recovery finishes "
        "the terminal writes after the credential is fixed",
    )
    record_terminal_outcome(
        ledger,
        attempt_id,
        outcome="interrupted",
        reason=CREDENTIAL_FAILURE_REASON,
        terminal_comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )


def _landed_summaries(ledger: RunLedger, attempt_id: str) -> list[str]:
    summaries: list[str] = []
    for effect in ledger.list_step_effects(attempt_id):
        if effect.status != "receipt":
            continue
        if effect.step_id == FINALISATION_STEP:
            continue
        operation = _operation_of(effect)
        remote = effect.remote_id or "-"
        summaries.append(f"{operation or 'effect'} {remote}")
    return summaries


def _not_performed_summaries(ledger: RunLedger, attempt_id: str) -> list[str]:
    summaries: list[str] = []
    for effect in ledger.list_step_effects(attempt_id):
        if effect.status != "not-performed":
            continue
        if effect.step_id == FINALISATION_STEP:
            continue
        operation = _operation_of(effect)
        summaries.append(f"{operation or 'effect'}: {effect.rendered_payload}")
    return summaries


def finalise_shutdown(
    *,
    tracker: SettlementTracker,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels: Sequence[str] = (),
    gate: GateCheck | None = None,
    uncommitted_count: int = 0,
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    workspace: Path | None = None,
) -> RecoveredAttempt:
    """Finalise the live Attempt on SIGTERM within the shutdown grace (REC-24).

    The in-flight model request is discarded by the caller (no new model
    call starts once the signal is observed); the in-flight write is
    settled here (§12, REC-16). The dirty tree is never committed or
    pushed; the terminal comment reports the uncommitted-file count. Ends
    ``interrupted`` / ``shutdown``, or ``withdrawn`` where the input was
    withdrawn meanwhile. Whatever the grace does not finish is finished by
    recovery at the next start (§16.1). A second signal is an ordinary
    process loss.
    """
    _ = workspace
    _ = filter_end_labels
    gate_result: GateResult | None = None
    if gate is not None:
        try:
            gate_result = gate()
        except Exception:
            gate_result = None
    if gate_result is not None and gate_result.status == "withdrawn":
        outcome = "withdrawn"
        reason = f"withdrawn: {gate_result.reason or 'withdrawn'}"
    else:
        outcome = "interrupted"
        reason = SHUTDOWN_REASON
    close_open_executions(ledger, attempt_id, reason=SHUTDOWN_REASON)
    try:
        settle_open_step_effects(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt_id,
            number=number,
            input_type=input_type,
            gate=gate,
            claim_active=True,
        )
    except CredentialFailure:
        _record_credential_failure(
            ledger,
            attempt_id,
            ending_step=ending_step,
            step_result=step_result,
            workflow_end=workflow_end,
            uncommitted_count=uncommitted_count,
        )
        raise
    landed = _landed_summaries(ledger, attempt_id)
    not_performed = _not_performed_summaries(ledger, attempt_id)
    comment = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=outcome,
        ending_step=ending_step,
        step_result=step_result,
        workflow_end=workflow_end,
        limit_fact=reason,
        landed=landed,
        not_performed=not_performed,
        uncommitted_count=uncommitted_count,
    )
    record_terminal_outcome(
        ledger,
        attempt_id,
        outcome=outcome,
        reason=reason,
        terminal_comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=attempt_id,
        number=number,
        input_type=input_type,
        selection_label=selection_label,
        end_labels=end_labels,
        comment_body=comment,
        writes=terminal_writes_for_outcome(),
    )
    ledger.set_attempt_state(attempt_id, "finished")
    return RecoveredAttempt(
        attempt_id=attempt_id, outcome=outcome, reason=reason, finished=True
    )
