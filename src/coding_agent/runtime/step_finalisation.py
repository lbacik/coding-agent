"""Step-workflow finalisation: the terminal comment, end labels and write-once outcome.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §15 (the
Terminal Outcome set; the finalisation sequence: settle in-flight effects,
record the Terminal Outcome with the rendered terminal comment and the
terminal-write list once and immutably, then perform the terminal writes;
the terminal writes: exactly one terminal comment from the fixed template,
the Selection Label removed, the Workflow's end labels for that outcome
applied, the assignment removed when no other Claim is in force; then the
Attempt is marked ``finished``) and §18 (the Worker never writes its own
Workflow's Selection Label: static validation refuses it and a runtime guard
filters it).

Row ids: REC-13, REC-14, REC-15, REC-21, REC-26.

Reuses the ledger's write-once ``record_terminal_outcome`` (the second
record is refused: the first record wins) and the ``step_effects`` intent
sequence for the terminal writes. Reuses :mod:`coding_agent.runtime.shutdown`
for the SIGTERM flag the live path observes; this module never installs a
handler.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Union

from coding_agent.inputs.claim import attempt_finished_in_ledger
from coding_agent.inputs.port import InputType
from coding_agent.ledger.store import (
    STEP_EFFECT_STATUS_INTENT,
    STEP_TRANSITION_END,
    RunLedger,
    StepEffectRecord,
    StepTransitionRecord,
    step_effect_marker,
)
from coding_agent.operator.step_projection import describe_limit_fact
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_DETAIL,
    PIN_VIOLATION_REASON,
)

TERMINAL_COMMENT_KIND = "terminal"
"""The Attempt Marker kind of the terminal comment (§14.4, §15.3)."""

FINALISATION_STEP = ""
"""The reserved step position of terminal writes in Effect Intent keys (§14.1)."""

FINALISATION_EXECUTION = 0
"""The reserved Step Execution number of terminal writes (§14.1)."""

TERMINAL_COMMENT_ORDINAL = 0
REMOVE_SELECTION_LABEL_ORDINAL = 1
ADD_END_LABELS_ORDINAL = 2
RELEASE_ASSIGNMENT_ORDINAL = 3
"""Write ordinals of the terminal writes (§14.1)."""

TERMINAL_OUTCOMES: tuple[str, ...] = (
    "completed",
    "blocked",
    "unfinished",
    "failed",
    "withdrawn",
    "drift",
    "interrupted",
    "internal-failure",
)
"""The closed Terminal Outcome set (§15.1). There is no ``canceled``."""

RUNTIME_ONLY_OUTCOMES: tuple[str, ...] = (
    "withdrawn",
    "drift",
    "interrupted",
    "internal-failure",
)
"""Outcomes only the runtime selects; never a Route target (§15.1)."""

TERMINAL_WRITE_COMMENT = "terminal-comment"
TERMINAL_WRITE_REMOVE_LABEL = "remove-selection-label"
TERMINAL_WRITE_ADD_LABELS = "add-end-labels"
TERMINAL_WRITE_RELEASE = "release-assignment"
"""Terminal-write list entries (§15.2, §15.3)."""


class FinalisationTracker(Protocol):
    """The tracker surface finalisation needs (§14, §15.3)."""

    @property
    def agent_login(self) -> str: ...
    @property
    def agent_id(self) -> int | None: ...

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> object: ...
    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: str
    ) -> object | None: ...
    def remove_label(self, number: int, input_type: InputType, label: str) -> None: ...
    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None: ...
    def unassign_input(self, number: int, input_type: InputType) -> None: ...
    def list_comments(
        self, number: int, input_type: InputType
    ) -> Sequence[object]: ...


def terminal_writes_for_outcome() -> tuple[str, ...]:
    """The terminal-write list, in perform order (§15.2, §15.3).

    Always the same four entries: the terminal comment, the Selection Label
    removal, the end labels, then the assignment release. An empty end-label
    set is a no-op entry, never a missing one, so the recorded list is
    stable and recovery finishes the same writes (REC-13).
    """
    return (
        TERMINAL_WRITE_COMMENT,
        TERMINAL_WRITE_REMOVE_LABEL,
        TERMINAL_WRITE_ADD_LABELS,
        TERMINAL_WRITE_RELEASE,
    )


def filter_end_labels(
    end_labels: Sequence[str], selection_label: str
) -> tuple[str, ...]:
    """Drop the Workflow's own Selection Label from end labels (REC-26) (§18).

    Static validation refuses such a configuration, but the runtime guard
    holds whatever the bypass wrote: the Worker never writes its own
    Workflow's Selection Label.
    """
    return tuple(label for label in end_labels if label != selection_label)


def render_terminal_comment(
    *,
    attempt_id: str,
    outcome: str,
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    limit_fact: str = "",
    landed: Sequence[str] = (),
    not_performed: Sequence[str] = (),
    uncommitted_count: int = 0,
    detail: str = "",
) -> str:
    """Render the one terminal comment from the fixed template (§15.3).

    No model call: the runtime states the Attempt id and the Terminal
    Outcome; the ending step, Step Result and Workflow End; the limit fact;
    the landed effects with links; the ``not-performed`` effects; and the
    uncommitted-file count. Carries the Attempt Marker with ``kind=terminal``
    so the Claim Comment stops being in force (§6.1).
    """
    marker = step_effect_marker(
        attempt_id,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        TERMINAL_COMMENT_ORDINAL,
        kind=TERMINAL_COMMENT_KIND,
    )
    lines = [
        f"coding-agent {attempt_id}: {outcome}",
        "",
        f"Attempt: {attempt_id}",
        f"Outcome: {outcome}",
    ]
    if ending_step or step_result or workflow_end:
        lines.append(
            f"Ending: step {ending_step or '-'} "
            f"result {step_result or '-'} "
            f"end {workflow_end or '-'}"
        )
    if limit_fact:
        lines.append(f"Limit: {limit_fact}")
    if landed:
        lines.append("")
        lines.append("Landed effects:")
        for entry in landed:
            lines.append(f"- {entry}")
    if not_performed:
        lines.append("")
        lines.append("Not performed:")
        for entry in not_performed:
            lines.append(f"- {entry}")
    lines.append("")
    if uncommitted_count:
        lines.append(
            f"Uncommitted files: {uncommitted_count} "
            "(the dirty tree was never committed or pushed)"
        )
    else:
        lines.append("Uncommitted files: 0")
    if detail:
        lines.append("")
        lines.append(detail)
    lines.append("")
    lines.append(marker)
    return "\n".join(lines) + "\n"


def count_uncommitted_files(workspace: Path) -> int:
    """Count uncommitted files without committing or pushing (REC-21) (§16.1).

    The dirty tree is never committed or pushed; the terminal comment
    reports the count and the workspace stays local. An unreadable tree
    reports ``0`` rather than failing finalisation.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
    except OSError:
        return 0
    if completed.returncode != 0:
        return 0
    count = 0
    for line in completed.stdout.splitlines():
        if line.strip():
            count += 1
    return count


def _tracker_comments(
    tracker: FinalisationTracker, number: int, input_type: InputType
) -> list[object]:
    try:
        result = tracker.list_comments(number, input_type)
    except Exception:
        return []
    return list(result)


def other_claim_in_force(
    tracker: FinalisationTracker,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
) -> bool:
    """Whether another Claim Comment is still in force (§15.3, §6.1).

    The assignment is removed only when no other Claim is in force. A
    Claim Comment stops being in force when its Attempt is over
    (``finished``/``released`` or an outcome recorded); unknown ids stay in
    force.
    """
    for comment in _tracker_comments(tracker, number, input_type):
        body = getattr(comment, "body", "")
        if not isinstance(body, str) or "kind=claim" not in body:
            continue
        from coding_agent.inputs.numbering import parse_attempt_marker

        parsed = parse_attempt_marker(body)
        if parsed is None:
            return True
        raw = f"{parsed[0]}#{parsed[1]}/{parsed[2]}"
        if raw == attempt_id:
            continue
        if not attempt_finished_in_ledger(ledger, raw):
            return True
    return False


def _record_terminal_intent(
    ledger: RunLedger, attempt_id: str, ordinal: int, payload: str
) -> None:
    ledger.record_step_effect_intent(
        attempt_id,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        ordinal,
        rendered_payload=payload,
        expected_target={"terminal-write": TERMINAL_COMMENT_KIND},
    )


def _record_terminal_receipt(
    ledger: RunLedger, attempt_id: str, ordinal: int, remote_id: str | None
) -> None:
    ledger.record_step_effect_receipt(
        attempt_id,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        ordinal,
        remote_id=remote_id,
    )


def perform_terminal_writes(
    *,
    tracker: FinalisationTracker,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels: Sequence[str] = (),
    comment_body: str = "",
    writes: Sequence[str] | None = None,
) -> None:
    """Perform the recorded terminal writes, each once (§15.2, §15.3).

    The terminal comment is posted even on a closed or merged input. The
    Selection Label is removed; the Workflow's end labels for the outcome
    are applied after the runtime guard drops the Selection Label
    (REC-26); the assignment is removed when no other Claim is in force.
    Landed effects are never reverted here (REC-15): no close, no draft
    conversion, no branch deletion, no removal of reviews or comments.
    Each write records its Effect Intent before the write and its Receipt
    after it, so a retried recovery never writes twice.
    """
    ordered = tuple(writes) if writes is not None else terminal_writes_for_outcome()
    safe_end_labels = filter_end_labels(end_labels, selection_label)
    for write_kind in ordered:
        if write_kind == TERMINAL_WRITE_COMMENT:
            if not comment_body:
                continue
            marker = step_effect_marker(
                attempt_id,
                FINALISATION_STEP,
                FINALISATION_EXECUTION,
                TERMINAL_COMMENT_ORDINAL,
                kind=TERMINAL_COMMENT_KIND,
            )
            _record_terminal_intent(
                ledger, attempt_id, TERMINAL_COMMENT_ORDINAL, comment_body
            )
            existing = tracker.find_comment_by_marker(number, input_type, marker)
            existing_id = getattr(existing, "comment_id", None)
            if existing is not None and isinstance(existing_id, int):
                _record_terminal_receipt(
                    ledger, attempt_id, TERMINAL_COMMENT_ORDINAL, str(existing_id)
                )
                continue
            posted = tracker.post_comment(number, input_type, comment_body)
            posted_id = getattr(posted, "comment_id", None)
            _record_terminal_receipt(
                ledger,
                attempt_id,
                TERMINAL_COMMENT_ORDINAL,
                str(posted_id) if isinstance(posted_id, int) else None,
            )
        elif write_kind == TERMINAL_WRITE_REMOVE_LABEL:
            _record_terminal_intent(
                ledger,
                attempt_id,
                REMOVE_SELECTION_LABEL_ORDINAL,
                f"remove label {selection_label}",
            )
            tracker.remove_label(number, input_type, selection_label)
            _record_terminal_receipt(
                ledger, attempt_id, REMOVE_SELECTION_LABEL_ORDINAL, selection_label
            )
        elif write_kind == TERMINAL_WRITE_ADD_LABELS:
            _record_terminal_intent(
                ledger,
                attempt_id,
                ADD_END_LABELS_ORDINAL,
                f"add labels {','.join(safe_end_labels)}",
            )
            if safe_end_labels:
                tracker.add_labels(number, input_type, safe_end_labels)
            _record_terminal_receipt(
                ledger, attempt_id, ADD_END_LABELS_ORDINAL, ",".join(safe_end_labels)
            )
        elif write_kind == TERMINAL_WRITE_RELEASE:
            _record_terminal_intent(
                ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL, "release assignment"
            )
            if not other_claim_in_force(tracker, ledger, attempt_id, number, input_type):
                tracker.unassign_input(number, input_type)
            _record_terminal_receipt(
                ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL, tracker.agent_login
            )
        else:
            raise AssertionError(f"unknown terminal write kind: {write_kind!r}")


# --- Attempt finalisation: one entry point ---------------------------------------

CREDENTIAL_FAILURE_REASON = "credential-failure"
"""The ``interrupted`` reason for a Credential Failure (REC-17) (§16.3)."""

CREDENTIAL_FAILURE_DETAIL = (
    "credential-failure: the Worker halts; recovery finishes "
    "the terminal writes after the credential is fixed"
)
"""The terminal comment detail for a Credential Failure (REC-17) (§15.3)."""


class UnsettledEffectsError(Exception):
    """An Effect Intent is still unsettled: settle first, then finish (§14.2)."""


@dataclass(frozen=True)
class FinalisationTarget:
    """What the ledger cannot know: the tracker input and its Selection Label."""

    number: int
    input_type: InputType
    selection_label: str


@dataclass(frozen=True)
class WorkflowEnded:
    """The Workflow reached an end: the outcome is the end category (§8.2)."""

    category: str
    reason: str
    ending_step: str = ""
    step_result: str = ""
    workflow_end: str = ""
    detail: str = ""


@dataclass(frozen=True)
class GateStopped:
    """A Gate before a write found Drift or withdrawal (§7.3, §16.1)."""

    outcome: str
    """``drift`` or ``withdrawn``."""
    reason: str
    """The full outcome reason, kind prefix included."""


@dataclass(frozen=True)
class PinViolated:
    """An asserted pin violation: ``interrupted`` / ``pin-violation`` (§9.1)."""

    step: str = ""
    """The step whose response carried the wrong model, where known."""


@dataclass(frozen=True)
class InternalFailure:
    """The Workflow graph failed without reaching an end (§8.4)."""

    reason: str


@dataclass(frozen=True)
class Interrupted:
    """A runtime-only ending with no Workflow End (§12, §16.1)."""

    reason: str
    """``process-loss`` for a crash, ``shutdown`` for SIGTERM."""
    ending_step: str = ""
    step_result: str = ""
    workflow_end: str = ""


@dataclass(frozen=True)
class CredentialFailed:
    """A Credential Failure: record, halt, and let recovery finish (REC-17)."""


AttemptDecision = Union[
    WorkflowEnded, GateStopped, PinViolated, InternalFailure, Interrupted, CredentialFailed
]
"""Why the Attempt ended, never who is calling (§15.2)."""


@dataclass(frozen=True)
class FinalisedAttempt:
    """What one finalisation pass did with an Attempt (§15.2)."""

    attempt_id: str
    outcome: str
    reason: str
    finished: bool


@dataclass(frozen=True)
class _ResolvedDecision:
    """One decision flattened to the finalisation sequence's fields."""

    outcome: str
    reason: str
    close_reason: str
    ending_step: str
    step_result: str
    workflow_end: str
    detail: str
    limit_fact: str


_SETTLEABLE_OPERATIONS = frozenset(
    {"post-comment", "publish-review", "open-pull-request", "terminal", ""}
)
"""Intent operations restart settlement resolves by remote lookup (§14.2)."""


def _effect_operation(intent: StepEffectRecord) -> str:
    target = intent.expected_target
    operation = target.get("operation")
    return str(operation) if isinstance(operation, str) else ""


def _is_claim_row(intent: StepEffectRecord) -> bool:
    return (
        intent.step_id == ""
        and intent.execution_number == 0
        and intent.ordinal in (0, 1)
    )


def _needs_settlement(intent: StepEffectRecord) -> bool:
    """Whether an intent must be settled before finalisation may start."""
    if intent.status != STEP_EFFECT_STATUS_INTENT:
        return False
    if intent.step_id == FINALISATION_STEP:
        return False
    if _is_claim_row(intent):
        return False
    return _effect_operation(intent) in _SETTLEABLE_OPERATIONS


def _refuse_unsettled(ledger: RunLedger, attempt_id: str) -> None:
    """Refuse to finish while a settleable Effect Intent has no receipt (§14.2).

    Settling stays the caller's job: a restarted Worker settles before it
    finishes, and the live path settles every write synchronously. Terminal
    and Claim rows belong to the finalisation path itself and never trip
    this; anything else unsettled is a programming error, never retried.
    """
    unsettled = [
        f"{effect.step_id}#{effect.execution_number}/{effect.ordinal}"
        for effect in ledger.list_step_effects(attempt_id)
        if _needs_settlement(effect)
    ]
    if unsettled:
        raise UnsettledEffectsError(
            f"{attempt_id} still has unsettled Effect Intents "
            f"({', '.join(unsettled)}): settle first, then finish"
        )


def recorded_workflow_end(
    ledger: RunLedger, attempt_id: str
) -> StepTransitionRecord | None:
    """The last recorded Workflow End fact for one Attempt, if any (§8.2)."""
    last: StepTransitionRecord | None = None
    for transition in ledger.list_step_transitions(attempt_id):
        if transition.kind == STEP_TRANSITION_END:
            last = transition
    return last


def limit_fact_text(ledger: RunLedger, attempt_id: str) -> str:
    """The ledger's limit-fact description for the terminal comment (§15.3)."""
    try:
        for record in ledger.list_step_executions(attempt_id):
            if record.limit_fact:
                return describe_limit_fact(record.limit_fact)
    except Exception:
        return ""
    return ""


def workflow_end_reason(
    *, from_step: str, result: str, end: str, limit_fact: str = ""
) -> str:
    """The Workflow End reason: the limit fact where one was recorded (§8.5)."""
    if limit_fact:
        return limit_fact
    return f"ended at end {end} after result {result} of step {from_step}"


def _resolve_decision(
    decision: AttemptDecision, limit_fact: str
) -> _ResolvedDecision:
    """Flatten one outcome decision to the finalisation sequence's fields."""
    if isinstance(decision, WorkflowEnded):
        return _ResolvedDecision(
            outcome=decision.category,
            reason=decision.reason,
            close_reason="workflow-end",
            ending_step=decision.ending_step,
            step_result=decision.step_result,
            workflow_end=decision.workflow_end,
            detail=decision.detail,
            limit_fact=limit_fact,
        )
    if isinstance(decision, GateStopped):
        return _ResolvedDecision(
            outcome=decision.outcome,
            reason=decision.reason,
            close_reason=decision.outcome,
            ending_step="gate",
            step_result=decision.outcome,
            workflow_end="-",
            detail="",
            limit_fact=limit_fact or decision.reason,
        )
    if isinstance(decision, PinViolated):
        return _ResolvedDecision(
            outcome="interrupted",
            reason=PIN_VIOLATION_REASON,
            close_reason=PIN_VIOLATION_REASON,
            ending_step=decision.step,
            step_result="",
            workflow_end="",
            detail=PIN_VIOLATION_DETAIL,
            limit_fact=limit_fact or PIN_VIOLATION_REASON,
        )
    if isinstance(decision, InternalFailure):
        return _ResolvedDecision(
            outcome="internal-failure",
            reason=decision.reason,
            close_reason="internal-failure",
            ending_step="",
            step_result="",
            workflow_end="",
            detail="",
            limit_fact=limit_fact or decision.reason,
        )
    if isinstance(decision, Interrupted):
        return _ResolvedDecision(
            outcome="interrupted",
            reason=decision.reason,
            close_reason=decision.reason,
            ending_step=decision.ending_step,
            step_result=decision.step_result,
            workflow_end=decision.workflow_end,
            detail="",
            limit_fact=limit_fact or decision.reason,
        )
    assert isinstance(decision, CredentialFailed)
    return _ResolvedDecision(
        outcome="interrupted",
        reason=CREDENTIAL_FAILURE_REASON,
        close_reason=CREDENTIAL_FAILURE_REASON,
        ending_step="",
        step_result="",
        workflow_end="",
        detail=CREDENTIAL_FAILURE_DETAIL,
        limit_fact=limit_fact or CREDENTIAL_FAILURE_REASON,
    )


def _landed_summaries(ledger: RunLedger, attempt_id: str) -> list[str]:
    summaries: list[str] = []
    for effect in ledger.list_step_effects(attempt_id):
        if effect.status != "receipt":
            continue
        if effect.step_id == FINALISATION_STEP:
            continue
        operation = _effect_operation(effect)
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
        operation = _effect_operation(effect)
        summaries.append(f"{operation or 'effect'}: {effect.rendered_payload}")
    return summaries


def finish_attempt(
    *,
    tracker: FinalisationTracker,
    ledger: RunLedger,
    attempt_id: str,
    target: FinalisationTarget,
    decision: AttemptDecision,
    end_labels_for: Mapping[str, Sequence[str]],
    uncommitted_count: int = 0,
) -> FinalisedAttempt:
    """End one Attempt through its outcome decision: the single entry point.

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §15 (the
    Terminal Outcome set; the finalisation sequence: settle in-flight
    effects, record the Terminal Outcome with the rendered terminal comment
    and the terminal-write list once and immutably, then perform the
    terminal writes; then the Attempt is marked ``finished``), §14.2
    (settlement on restart) and §16 (recovery finishes a recorded outcome
    and never re-decides it).

    The sequence, enforced here and tested here: refuse where a settleable
    Effect Intent is still unsettled (settling stays the caller's job);
    close open Step Executions with the decision's close reason; where an
    outcome is already recorded, ignore the decision and replay the
    recorded row's comment and writes (ADR 0017: finish a recorded outcome,
    never re-decide it); otherwise render the terminal comment and record
    the full Terminal Outcome row once — outcome, reason, comment, writes —
    before the first tracker write (ADR 0017: recorded before published);
    perform the terminal writes with the end labels of the outcome actually
    recorded or replayed; mark the Attempt ``finished``.

    :class:`CredentialFailed` records ``interrupted`` /
    ``credential-failure`` but performs no terminal writes and leaves the
    Attempt unfinished, so recovery finishes it after the credential is
    fixed (REC-17).
    """
    if not isinstance(decision, CredentialFailed):
        _refuse_unsettled(ledger, attempt_id)
    resolved = _resolve_decision(decision, limit_fact_text(ledger, attempt_id))
    ledger.close_interrupted_step_executions(
        attempt_id, reason=resolved.close_reason
    )
    stored = ledger.get_outcome(attempt_id)
    if stored is not None:
        if isinstance(decision, CredentialFailed):
            return FinalisedAttempt(
                attempt_id=attempt_id,
                outcome=stored.outcome,
                reason=stored.reason,
                finished=False,
            )
        end_labels = tuple(end_labels_for.get(stored.outcome, ()))
        perform_terminal_writes(
            tracker=tracker,
            ledger=ledger,
            attempt_id=attempt_id,
            number=target.number,
            input_type=target.input_type,
            selection_label=target.selection_label,
            end_labels=end_labels,
            comment_body=stored.terminal_comment_body,
            writes=stored.writes,
        )
        ledger.set_attempt_state(attempt_id, "finished")
        return FinalisedAttempt(
            attempt_id=attempt_id,
            outcome=stored.outcome,
            reason=stored.reason,
            finished=True,
        )
    comment = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=resolved.outcome,
        ending_step=resolved.ending_step,
        step_result=resolved.step_result,
        workflow_end=resolved.workflow_end,
        limit_fact=resolved.limit_fact,
        landed=_landed_summaries(ledger, attempt_id),
        not_performed=_not_performed_summaries(ledger, attempt_id),
        uncommitted_count=uncommitted_count,
        detail=resolved.detail,
    )
    writes = terminal_writes_for_outcome()
    ledger.record_terminal_outcome(
        attempt_id,
        outcome=resolved.outcome,
        reason=resolved.reason,
        terminal_comment_body=comment,
        writes=list(writes),
    )
    if isinstance(decision, CredentialFailed):
        return FinalisedAttempt(
            attempt_id=attempt_id,
            outcome=resolved.outcome,
            reason=resolved.reason,
            finished=False,
        )
    end_labels = tuple(end_labels_for.get(resolved.outcome, ()))
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=attempt_id,
        number=target.number,
        input_type=target.input_type,
        selection_label=target.selection_label,
        end_labels=end_labels,
        comment_body=comment,
        writes=writes,
    )
    ledger.set_attempt_state(attempt_id, "finished")
    return FinalisedAttempt(
        attempt_id=attempt_id,
        outcome=resolved.outcome,
        reason=resolved.reason,
        finished=True,
    )
