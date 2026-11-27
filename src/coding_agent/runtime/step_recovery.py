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

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from coding_agent.github.errors import CredentialFailure
from coding_agent.inputs.claim import recover_unconfirmed_claim
from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import (
    RunLedger,
    StepEffectRecord,
)
from coding_agent.runtime.effects.driver import (
    Failed,
    Landed,
    NotPerformed,
    Performed,
    drive_effect,
)
from coding_agent.runtime.effects.registry import module_for_intent
from coding_agent.runtime.step_finalisation import (
    FINALISATION_EXECUTION,
    FINALISATION_STEP,
    AttemptDecision,
    CREDENTIAL_FAILURE_REASON,
    CredentialFailed,
    FinalisationTarget,
    FinalisedAttempt,
    GateStopped,
    Interrupted,
    WorkflowEnded,
    finish_attempt,
    limit_fact_text,
    recorded_workflow_end,
    workflow_end_reason,
)

PROCESS_LOSS_REASON = "process-loss"
"""The ``interrupted`` reason for a process loss settled at restart (§16.1)."""

SHUTDOWN_REASON = "shutdown"
"""The ``interrupted`` reason for a SIGTERM-ended live Attempt (§12)."""

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


def settle_step_effect(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    intent: StepEffectRecord,
    number: int,
    input_type: InputType,
    gate: GateCheck | None = None,
    claim_active: bool = True,
) -> str:
    """Settle one intent without a receipt by key lookup (REC-5 … REC-8) (§14.2).

    Delegates to the shared effect driver over the registry module the
    stored intent row resolves to. Returns the settlement: ``"receipt"``
    where the write had landed (the receipt is recorded, nothing written
    twice), ``"performed"`` where the absent write was performed once after
    the preconditions held, or ``"not-performed"`` with its reason recorded.
    A human-closed own pull request is found by the lookup and settles
    ``"receipt"``: it is never re-created (REC-8). A lookup or write failure
    propagates and leaves the intent open; the restart loop counts it toward
    the consecutive-failure halt. :class:`CredentialFailure` propagates: the
    caller records ``interrupted`` / ``credential-failure`` and halts
    (REC-17).
    """
    if intent.status != "intent":
        return intent.status
    module = module_for_intent(intent)
    if module is None:
        # Assignment, label and push intents: the terminal path replays them
        # idempotently; unknown step kinds have no Effect module.
        return intent.status
    target = module.target_from_dict(dict(intent.expected_target or {}))
    outcome = drive_effect(
        tracker=tracker,
        ledger=ledger,
        attempt_id=intent.attempt_id,
        step_id=intent.step_id,
        seq=intent.execution_number,
        ordinal=intent.ordinal,
        number=number,
        input_type=input_type,
        module=module,
        target=target,
        body=_intent_body(intent),
        intent_payload=intent.rendered_payload,
        record_intent=False,
        gate=gate,
        claim_active=claim_active,
    )
    if isinstance(outcome, Landed):
        return "receipt"
    if isinstance(outcome, Performed):
        return "performed"
    if isinstance(outcome, NotPerformed):
        return "not-performed"
    assert isinstance(outcome, Failed)
    raise outcome.error


def settle_open_step_effects(
    *,
    tracker: StepTrackerPort,
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


def _finish_after_settling(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels_for: Mapping[str, Sequence[str]],
    uncommitted_count: int,
    decision: AttemptDecision | None,
    gate: GateCheck | None,
) -> FinalisedAttempt:
    """Finish a settled Attempt through the finalisation entry point (§15.2).

    A recorded outcome is finished as recorded and never re-decided. An
    unfinished Attempt with a recorded Workflow End but no recorded outcome
    — the crash window between the end node and the Supervisor's record —
    ends with that category, and a withdrawal the restart Gate sees does
    not override it. Otherwise the caller's decision stands, or the restart
    outcome is decided from the Gate: ``interrupted`` / ``process-loss``,
    ``withdrawn``, or ``drift``. No Workflow Step runs on any of these
    paths.
    """
    stored = ledger.get_outcome(attempt_id)
    resolved = decision
    if stored is None:
        end = recorded_workflow_end(ledger, attempt_id)
        if end is not None and end.to_end:
            category = str(end.to_end)
            resolved = WorkflowEnded(
                category=category,
                reason=workflow_end_reason(
                    from_step=end.from_step or "",
                    result=end.result or "",
                    end=category,
                    limit_fact=limit_fact_text(ledger, attempt_id),
                ),
                ending_step=end.from_step or "",
                step_result=end.result or "",
                workflow_end=category,
            )
        elif resolved is None:
            gate_result: GateResult | None = None
            if gate is not None:
                try:
                    gate_result = gate()
                except CredentialFailure:
                    raise
                except Exception:
                    gate_result = None
            restart = decide_restart_outcome(gate_result)
            if restart.outcome in ("withdrawn", "drift"):
                resolved = GateStopped(
                    outcome=restart.outcome, reason=restart.reason
                )
            else:
                resolved = Interrupted(reason=restart.reason)
    if resolved is None:
        # A recorded outcome replays whatever the decision would have been:
        # the close below is a no-op where executions already closed, and
        # the crash window closed them as process loss.
        resolved = Interrupted(reason=PROCESS_LOSS_REASON)
    return finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=attempt_id,
        target=FinalisationTarget(
            number=number, input_type=input_type, selection_label=selection_label
        ),
        decision=resolved,
        end_labels_for=end_labels_for,
        uncommitted_count=uncommitted_count,
    )


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
    tracker: StepTrackerPort,
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
    recover_unconfirmed_claim(tracker, ledger, attempt_id, number, input_type)
    return RecoveredAttempt(
        attempt_id=attempt_id, outcome=None, reason=None, released=True
    )


def recover_attempt(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels_for: Mapping[str, Sequence[str]],
    gate: GateCheck | None = None,
    claim_active: bool = True,
    uncommitted_count: int = 0,
    decision: AttemptDecision | None = None,
) -> RecoveredAttempt:
    """Settle and finalise one unfinished Attempt at restart (REC-12) (§16.1).

    Never enters the Workflow graph and never runs a Workflow Step, never
    calls the model: settles the effect that was in flight (§14.2), then
    finishes the lifecycle through the finalisation entry point (§15.2). A
    recorded outcome is finished as recorded and never re-decided;
    otherwise the caller's ``decision`` stands, or the Attempt ends
    ``interrupted`` / ``process-loss``, ``withdrawn`` where the input was
    withdrawn, or ``drift`` where the Gate finds Drift — unless a Workflow
    End was recorded without an outcome, which ends with that category. The
    dirty tree is never committed or pushed; the terminal comment reports
    the uncommitted-file count (REC-21). Landed effects are never reverted
    (REC-15, §14.3). A :class:`CredentialFailure` records
    ``interrupted`` / ``credential-failure`` before propagating so the next
    start finishes the terminal writes after the credential is fixed
    (REC-17).

    The Attempt Archive is validated first (SKV3-29/30): settlement and
    finalisation proceed from the pinned identity and the archived blobs,
    never from current files, and a missing, corrupt, escaping, symlinked
    or incompatible core or blob refuses before any write (no partial
    success). The Worker-loop entry validates as well, so the gate cannot
    be skipped by calling this entry directly.
    """
    from coding_agent.skillbundle.recovery import ensure_archive_for_recovery

    ensure_archive_for_recovery(ledger=ledger, attempt_id=attempt_id)
    target = FinalisationTarget(
        number=number, input_type=input_type, selection_label=selection_label
    )
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
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt_id,
            target=target,
            decision=CredentialFailed(),
            end_labels_for=end_labels_for,
            uncommitted_count=uncommitted_count,
        )
        raise
    finalised = _finish_after_settling(
        tracker=tracker,
        ledger=ledger,
        attempt_id=attempt_id,
        number=number,
        input_type=input_type,
        selection_label=selection_label,
        end_labels_for=end_labels_for,
        uncommitted_count=uncommitted_count,
        decision=decision,
        gate=gate,
    )
    return RecoveredAttempt(
        attempt_id=attempt_id,
        outcome=finalised.outcome,
        reason=finalised.reason,
        finished=finalised.finished,
    )


def finalise_shutdown(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels_for: Mapping[str, Sequence[str]],
    gate: GateCheck | None = None,
    uncommitted_count: int = 0,
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    workspace: Path | None = None,
    deadline: float | None = None,
) -> RecoveredAttempt:
    """Finalise the live Attempt on SIGTERM within the shutdown grace (REC-24).

    The in-flight model request is discarded by the caller (no new model
    call starts once the signal is observed); the in-flight write is
    settled here (§12, REC-16), then the lifecycle finishes through the
    finalisation entry point (§15.2). The dirty tree is never committed or
    pushed; the terminal comment reports the uncommitted-file count. Ends
    ``interrupted`` / ``shutdown``, or ``withdrawn`` where the input was
    withdrawn meanwhile. Whatever the grace does not finish is finished by
    recovery at the next start (§16.1): where ``deadline`` (a
    :func:`shutdown_grace_deadline` bound) is already past — at entry or
    after settling — the Attempt is returned unfinished with that ending
    named but unrecorded, so the next start finishes it. A second signal is
    an ordinary process loss.
    """
    _ = workspace
    if deadline is not None and time.monotonic() >= deadline:
        return RecoveredAttempt(
            attempt_id=attempt_id,
            outcome="interrupted",
            reason=SHUTDOWN_REASON,
            finished=False,
        )
    gate_result: GateResult | None = None
    if gate is not None:
        try:
            gate_result = gate()
        except Exception:
            gate_result = None
    if gate_result is not None and gate_result.status == "withdrawn":
        decision: AttemptDecision = GateStopped(
            outcome="withdrawn",
            reason=f"withdrawn: {gate_result.reason or 'withdrawn'}",
        )
    else:
        decision = Interrupted(
            reason=SHUTDOWN_REASON,
            ending_step=ending_step,
            step_result=step_result,
            workflow_end=workflow_end,
        )
    target = FinalisationTarget(
        number=number, input_type=input_type, selection_label=selection_label
    )
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
        finish_attempt(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt_id,
            target=target,
            decision=CredentialFailed(),
            end_labels_for=end_labels_for,
            uncommitted_count=uncommitted_count,
        )
        raise
    if deadline is not None and time.monotonic() >= deadline:
        # The grace ran out settling the in-flight write: the settled write
        # stays landed and the next start finishes the Attempt (§16.1).
        return RecoveredAttempt(
            attempt_id=attempt_id,
            outcome="interrupted",
            reason=SHUTDOWN_REASON,
            finished=False,
        )
    finalised = _finish_after_settling(
        tracker=tracker,
        ledger=ledger,
        attempt_id=attempt_id,
        number=number,
        input_type=input_type,
        selection_label=selection_label,
        end_labels_for=end_labels_for,
        uncommitted_count=uncommitted_count,
        decision=decision,
        gate=gate,
    )
    return RecoveredAttempt(
        attempt_id=attempt_id,
        outcome=finalised.outcome,
        reason=finalised.reason,
        finished=finalised.finished,
    )
