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
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from coding_agent.inputs.claim import attempt_finished_in_ledger
from coding_agent.inputs.port import InputType
from coding_agent.ledger.store import OutcomeRecord, RunLedger, step_effect_marker

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


def record_terminal_outcome(
    ledger: RunLedger,
    attempt_id: str,
    *,
    outcome: str,
    reason: str,
    terminal_comment_body: str,
    writes: Sequence[str] = (),
) -> OutcomeRecord:
    """Record the Terminal Outcome once and immutably (REC-13) (§15.2).

    The first record wins; a second call returns the stored row unchanged.
    Recovery finishes a recorded outcome and never re-decides it.
    """
    return ledger.record_terminal_outcome(
        attempt_id,
        outcome=outcome,
        reason=reason,
        terminal_comment_body=terminal_comment_body,
        writes=tuple(writes),
    )


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
