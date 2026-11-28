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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coding_agent.inputs.comments import SelectedComment

from coding_agent.inputs.claim import attempt_finished_in_ledger, terminal_marker_for
from coding_agent.inputs.port import CodeHostPort, InputPort, InputType
from coding_agent.ledger.store import (
    STEP_EFFECT_STATUS_INTENT,
    STEP_TRANSITION_END,
    RunLedger,
    StepEffectRecord,
    StepTransitionRecord,
)
from coding_agent.operator.step_projection import describe_limit_fact
from coding_agent.runtime.effects.driver import Failed, drive_effect
from coding_agent.runtime.effects.modules import TerminalCommentTarget
from coding_agent.runtime.effects.registry import TERMINAL_COMMENT, module_for_intent
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_DETAIL,
    PIN_VIOLATION_REASON,
)
from coding_agent.runtime.relay import (
    RELAY_MISSING_AUTHORISATION,
    RelayRecord,
    parse_relay_block,
    render_relay_diagnostic,
    render_relay_section,
)
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    parse_handoff_block,
    render_diagnostic_section,
    render_handoff_section,
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


def render_comments_disclosure(late: Sequence[SelectedComment]) -> str:
    """Render the post-Claim Trusted Comments disclosure (§7.4, ADR 0035).

    Counts the Trusted Comments posted after the Claim read-back — the
    ones the Attempt never saw — with an ``#issuecomment-`` link each,
    the same way new base commits are disclosed. The caller renders this
    only where the Attempt pinned a comment set; an empty late list still
    renders, so the comment shows the Attempt saw everything.
    """
    if not late:
        return "Trusted comments after Claim: none"
    lines = [
        "Trusted comments after Claim: "
        f"{len(late)} (posted after the Claim read-back; "
        "this Attempt never saw them)"
    ]
    for item in late:
        lines.append(f"- #issuecomment-{item.comment.comment_id}")
    return "\n".join(lines)


def late_trusted_comments(
    inputs: InputPort,
    ledger: RunLedger,
    *,
    attempt_id: str,
    number: int,
    input_type: InputType,
) -> list[SelectedComment] | None:
    """The Trusted Comments posted after the Claim read-back (§7.4).

    Re-selects over the live list and keeps what the pin does not hold, so
    the terminal comment can disclose late comments the Attempt never saw.
    ``None`` where the Attempt pinned no comment set — an older Attempt or
    a Workflow with no ``input.comments`` step — so the terminal comment
    carries no disclosure section at all. Best-effort: a failed live read
    discloses nothing rather than failing finalisation.
    """
    from coding_agent.inputs.comments import new_trusted_comments

    stored = ledger.get_pinned_comment_set(attempt_id)
    if stored is None:
        return None
    pinned = ledger.get_pinned_comments(attempt_id) or []
    pinned_ids = {comment.comment_id for comment in pinned}
    try:
        live = inputs.list_comments(number, input_type)
    except Exception:
        return []
    return new_trusted_comments(
        live,
        pinned_ids,
        issue_author_id=stored.issue_author_id,
        owner_id=stored.owner_id,
        agent_id=stored.agent_id,
        agent_login=inputs.agent_login,
    )


def render_terminal_comment(
    *,
    attempt_id: str,
    outcome: str,
    marker_text: str,
    ending_step: str = "",
    step_result: str = "",
    workflow_end: str = "",
    limit_fact: str = "",
    landed: Sequence[str] = (),
    not_performed: Sequence[str] = (),
    uncommitted_count: int = 0,
    detail: str = "",
    handoff_section: str = "",
    comments_section: str = "",
) -> str:
    """Render the one terminal comment from the fixed template (§15.3).

    No model call: the runtime states the Attempt id and the Terminal
    Outcome; the ending step, Step Result and Workflow End; the limit fact;
    the landed effects with links; the ``not-performed`` effects; and the
    uncommitted-file count. The optional Round Handoff section (the
    operator-visible offer account with its machine-readable record block,
    or the no-pickup diagnostic) and the optional post-Claim Trusted
    Comments disclosure ride above the Attempt Marker, which stays
    the last line, so the Claim Comment stops being in force (§6.1).

    ``marker_text`` is the adapter's rendering of the terminal Attempt
    Marker; core never formats marker text itself.
    """
    marker = marker_text
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
    if handoff_section:
        lines.append("")
        lines.append(handoff_section)
    if comments_section:
        lines.append("")
        lines.append(comments_section)
    lines.append("")
    lines.append(marker)
    return "\n".join(lines) + "\n"


def pinned_handoff_eligibility(
    ledger: RunLedger, attempt_id: str, ending_step: str, step_result: str
) -> tuple[str, int] | None:
    """The pinned Pickup Label and series limit for an eligible end, if any (§8).

    Reads the Attempt's pinned Effective Configuration (``version: 4``) from
    the Run Ledger, never the currently loaded file: the ``round_handoff``
    enablement section (the resolved ``pickup_label`` and the positive
    ``max_automatic_pickups`` limit) plus the taken Route's own
    ``round_handoff`` annotation (``ai-assisted`` or ``hard``). ``None``
    where the Attempt pins no full-shape record, Round Handoff stays
    disabled, or the taken end carries no publishing annotation — such a
    round keeps its ordinary terminal path.
    """
    try:
        record = ledger.get_attempt(attempt_id).effective_configuration
    except Exception:
        return None
    try:
        from coding_agent.workflow.effective import (
            is_full_effective_configuration as _is_full,
        )
        from coding_agent.workflow.effective import (
            route_round_handoff_kind as _route_kind,
        )
        from coding_agent.workflow.effective import (
            round_handoff_policy as _round_handoff_policy,
        )
    except Exception:
        return None
    try:
        if not _is_full(record):
            return None
        if not isinstance(record, Mapping):
            return None
        policy = _round_handoff_policy(record)
        if policy is None:
            return None
        if not ending_step or not step_result:
            return None
        if _route_kind(record, ending_step, step_result) not in (
            "ai-assisted",
            "hard",
        ):
            return None
        return policy
    except Exception:
        return None


def _replay_pickup_label(stored_comment: str) -> str | None:
    """The Pickup Label a stored terminal comment still offers, if any (§8).

    Parses the recorded comment's own machine-readable block: a valid
    non-exhausted record replays its exact Pickup Label, while an exhausted
    record, a diagnostic, or an ordinary comment replays the configured end
    labels. Forged or truncated blocks read as absent and never verify.
    """
    offer = parse_handoff_block(stored_comment)
    if offer is None or offer.exhausted:
        return None
    return offer.pickup_label


def _missing_handoff_diagnostic(
    attempt_id: str,
    ending_step: str,
    step_result: str,
    pickup_label: str,
) -> HandoffDiagnostic:
    """The diagnostic for an eligible end with no prepared offer (§8).

    The publishing push landed before preparation ran (a crash between the
    push and the terminal record, or a build that never prepared one), so
    the round keeps ordinary labels and offers no pickup after restart.
    """
    return HandoffDiagnostic(
        reason="missing-handoff",
        detail=(
            f"round-handoff: the {ending_step}/{step_result} end of "
            f"{attempt_id} is eligible for pickup under {pickup_label}, "
            "but no offer was prepared (the push landed before "
            "preparation); no pickup is offered"
        ),
    )


def _prepare_relay(
    ledger: RunLedger, attempt_id: str, outcome: str
) -> tuple[str, str | None]:
    """The Relay section and the receiver's Relay Label for ``outcome`` (§19).

    Reads the Attempt's pinned Effective Configuration, never the loaded
    file: the outcome's declared Relay target with its Relay Label. The
    record carries the originating human authorisation pinned at the
    chain's first Claim; an Attempt that pinned none records no Relay, only
    the ``missing-authorisation`` diagnostic, because a Relay never creates
    an authorisation. ``("", None)`` where the outcome declares no Relay.
    """
    try:
        attempt = ledger.get_attempt(attempt_id)
        record = attempt.effective_configuration
        from coding_agent.workflow.effective import (
            effective_relay_target,
            effective_workflow_id,
            is_full_effective_configuration,
        )

        if not isinstance(record, Mapping) or not is_full_effective_configuration(
            record
        ):
            return "", None
        declared = effective_relay_target(record, outcome)
        if declared is None:
            return "", None
        target_workflow, relay_label = declared
        source_workflow = effective_workflow_id(record)
        pin = ledger.get_attempt_pin(attempt_id)
    except Exception:
        return "", None
    if (
        pin is None
        or not pin.selection_actor
        or not pin.selection_actor_id
    ):
        return (
            render_relay_diagnostic(
                RELAY_MISSING_AUTHORISATION,
                f"{attempt_id} pinned no human authorisation to carry to "
                f"workflow {target_workflow}; no Relay Label is applied",
            ),
            None,
        )
    relay = RelayRecord(
        source_attempt=attempt_id,
        source_workflow=source_workflow,
        target_workflow=target_workflow,
        issue=pin.input_number,
        outcome=outcome,
        relay_label=relay_label,
        origin_actor=pin.selection_actor,
        origin_actor_id=pin.selection_actor_id,
        origin_actor_type=pin.selection_actor_type or "",
        origin_actor_at=pin.selection_actor_at or "",
        identity_mode=pin.identity_mode or "",
    )
    return render_relay_section(relay), relay_label


def _replay_relay_label(stored_comment: str) -> str | None:
    """The Relay Label a stored terminal comment still carries, if any (§19).

    Recovery re-applies the label of the record the comment already holds,
    so a Relay finishes unchanged after a crash between the comment and the
    label write. A forged or truncated block reads as absent.
    """
    record = parse_relay_block(stored_comment)
    return record.relay_label if record is not None else None


def _pinned_relay_label(ledger: RunLedger, attempt_id: str) -> str | None:
    """The Workflow's own pinned Relay Label, removed when its Attempt ends (§19)."""
    try:
        record = ledger.get_attempt(attempt_id).effective_configuration
        from coding_agent.workflow.effective import (
            effective_relay_label,
            is_full_effective_configuration,
        )

        if not isinstance(record, Mapping) or not is_full_effective_configuration(
            record
        ):
            return None
        return effective_relay_label(record)
    except Exception:
        return None


def _relay_consumed(ledger: RunLedger, attempt_id: str) -> tuple[str, ...]:
    """The Relay Label this Attempt's own Workflow consumes, as a tuple (§19)."""
    label = _pinned_relay_label(ledger, attempt_id)
    return (label,) if label is not None else ()


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
    inputs: InputPort, number: int, input_type: InputType
) -> list[object]:
    try:
        result = inputs.list_comments(number, input_type)
    except Exception:
        return []
    return list(result)


def other_claim_in_force(
    inputs: InputPort,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
) -> bool:
    """Whether another Claim Comment is still in force (§15.3, §6.1).

    The assignment is removed only when no other Claim is in force. A
    Claim Comment stops being in force when its Attempt is over
    (``finished``/``released`` or an outcome recorded) or — where the Worker
    holds no local record, e.g. an independent state volume — where a trusted
    terminal comment ends its exact Attempt id; unknown ids without trusted
    terminal evidence stay in force.
    """
    from coding_agent.inputs.claim import trusted_terminal_attempt_ids
    from coding_agent.inputs.numbering import parse_attempt_marker
    from coding_agent.inputs.port import InputComment

    comments = _tracker_comments(inputs, number, input_type)
    narrowed: list[InputComment] = [
        comment for comment in comments if isinstance(comment, InputComment)
    ]
    trusted: set[str] = trusted_terminal_attempt_ids(
        narrowed, inputs.agent_id, inputs.format_marker
    )
    for comment in comments:
        body = getattr(comment, "body", "")
        if not isinstance(body, str) or "kind=claim" not in body:
            continue
        parsed = parse_attempt_marker(body)
        if parsed is None:
            return True
        raw = str(parsed)
        if raw == attempt_id:
            continue
        if attempt_finished_in_ledger(ledger, raw):
            continue
        if raw in trusted:
            continue
        return True
    return False


def _record_terminal_intent(
    ledger: RunLedger,
    attempt_id: str,
    ordinal: int,
    payload: str,
    expected_target: Mapping[str, object] | None = None,
) -> None:
    ledger.record_step_effect_intent(
        attempt_id,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        ordinal,
        rendered_payload=payload,
        expected_target=dict(expected_target)
        if expected_target is not None
        else {"terminal-write": TERMINAL_COMMENT_KIND},
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


def _terminal_effect(
    ledger: RunLedger, attempt_id: str, ordinal: int
) -> StepEffectRecord | None:
    """The terminal intent row for one ordinal, if it is a terminal row.

    Claim rows share the reserved ``("", 0)`` key space (claim ordinals 0,1
    vs terminal ordinals 0-3): only a row whose ``expected_target`` carries
    ``terminal-write`` belongs to the terminal path. Anything else (a Claim
    row, a step row) reads as absent so the terminal write still performs.
    """
    effect = ledger.get_step_effect(
        attempt_id, FINALISATION_STEP, FINALISATION_EXECUTION, ordinal
    )
    if effect is None:
        return None
    try:
        target = effect.expected_target or {}
    except Exception:
        return None
    if not isinstance(target, dict) or "terminal-write" not in target:
        return None
    return effect


def _has_terminal_receipt(
    ledger: RunLedger, attempt_id: str, ordinal: int
) -> bool:
    """Whether a terminal write already holds its Effect Receipt (§15.2).

    A receipted write is never written again: recovery skips it without a
    tracker call, so an interrupted finalisation resumes instead of
    duplicating. Claim rows sharing the key never count: only a terminal
    row's receipt skips its write.
    """
    effect = _terminal_effect(ledger, attempt_id, ordinal)
    return effect is not None and effect.status == "receipt"


def _has_terminal_intent(
    ledger: RunLedger, attempt_id: str, ordinal: int
) -> bool:
    """Whether a terminal write already holds an Effect Intent without receipt.

    An ambiguous retry (intent recorded, write may have landed, receipt
    missing) reads the remote back before writing again; a first run with no
    intent performs directly, preserving the long-standing call shape.
    """
    effect = _terminal_effect(ledger, attempt_id, ordinal)
    return effect is not None and effect.status == "intent"


def _remote_labels_and_assignees(
    inputs: InputPort, number: int, input_type: InputType
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    """Best-effort read-back of the input's labels and assignees (§14.2).

    ``None`` where the read fails: the caller performs the write instead of
    blocking finalisation on a read. :class:`CredentialFailure` propagates so
    the Worker halts and recovery retries after the credential is fixed.
    """
    from coding_agent.inputs.errors import CredentialFailure as _CredentialFailure

    try:
        current = inputs.fetch_input(number, input_type)
    except _CredentialFailure:
        raise
    except Exception:
        return None, None
    try:
        labels = tuple(current.labels)
    except Exception:
        labels = None
    try:
        assignees = tuple(current.assignees)
    except Exception:
        assignees = None
    return labels, assignees


def _pinned_terminal_identity(
    ledger: RunLedger, attempt_id: str
) -> tuple[str | None, dict[str, tuple[str, ...]] | None]:
    """The pinned Selection Label and end-labels map, where pinned (§2.10).

    Reads the Attempt's pinned Effective Configuration (``version: 4``) from
    the Run Ledger, never the currently loaded file. ``(None, None)`` where
    the Attempt holds no full-shape record: the caller falls back to its own
    values so minimal test rows and legacy opens still finalise.
    """
    try:
        attempt = ledger.get_attempt(attempt_id)
    except Exception:
        return None, None
    record = attempt.effective_configuration
    try:
        from coding_agent.workflow.effective import (
            effective_end_labels_for_all as _end_labels_for_all,
        )
        from coding_agent.workflow.effective import (
            effective_selection_label as _selection_label,
        )
        from coding_agent.workflow.effective import (
            is_full_effective_configuration as _is_full,
        )
    except Exception:
        return None, None
    try:
        if not _is_full(record):
            return None, None
        return _selection_label(record), _end_labels_for_all(record)
    except Exception:
        return None, None


def _resolve_terminal_plan(
    ledger: RunLedger,
    attempt_id: str,
    outcome: str,
    selection_label: str,
    end_labels_for: Mapping[str, Sequence[str]],
) -> tuple[str, tuple[str, ...]]:
    """Resolve the terminal write plan for ``outcome`` from pinned data (§16.1).

    The pinned Selection Label wins where the Attempt pins a full Effective
    Configuration; the pinned end labels win where they name ``outcome``.
    Otherwise the caller's values stand, so legacy minimal rows still
    finalise. Recovery therefore replays the exact label the Attempt
    consumed and the exact labels it applies even when the current Workflow
    Configuration differs.
    """
    pinned_selection, pinned_map = _pinned_terminal_identity(ledger, attempt_id)
    resolved_selection = pinned_selection or selection_label
    if pinned_map is not None and outcome in pinned_map:
        return resolved_selection, tuple(pinned_map[outcome])
    return resolved_selection, tuple(end_labels_for.get(outcome, ()))


def perform_terminal_writes(
    *,
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    attempt_id: str,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels: Sequence[str] = (),
    comment_body: str = "",
    writes: Sequence[str] | None = None,
    also_remove_labels: Sequence[str] = (),
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

    ``also_remove_labels`` are further consumed intake labels (the
    Workflow's own Relay Label, §19) removed in the same write as the
    Selection Label, so the write list keeps its four stable entries.
    """
    ordered = tuple(writes) if writes is not None else terminal_writes_for_outcome()
    safe_end_labels = filter_end_labels(end_labels, selection_label)
    for write_kind in ordered:
        if write_kind == TERMINAL_WRITE_COMMENT:
            if not comment_body:
                continue
            if _has_terminal_receipt(ledger, attempt_id, TERMINAL_COMMENT_ORDINAL):
                continue
            outcome = drive_effect(
                inputs=inputs,
                code_host=code_host,
                ledger=ledger,
                attempt_id=attempt_id,
                step_id=FINALISATION_STEP,
                seq=FINALISATION_EXECUTION,
                ordinal=TERMINAL_COMMENT_ORDINAL,
                number=number,
                input_type=input_type,
                module=TERMINAL_COMMENT,
                target=TerminalCommentTarget(),
                body=comment_body,
                intent_payload=comment_body,
                record_intent=True,
            )
            if isinstance(outcome, Failed):
                raise outcome.error
            continue
        elif write_kind == TERMINAL_WRITE_REMOVE_LABEL:
            if _has_terminal_receipt(
                ledger, attempt_id, REMOVE_SELECTION_LABEL_ORDINAL
            ):
                continue
            retrying = _has_terminal_intent(
                ledger, attempt_id, REMOVE_SELECTION_LABEL_ORDINAL
            )
            consumed = (selection_label, *(
                label
                for label in also_remove_labels
                if label and label != selection_label
            ))
            _record_terminal_intent(
                ledger,
                attempt_id,
                REMOVE_SELECTION_LABEL_ORDINAL,
                f"remove label {selection_label}"
                + (
                    f" and {', '.join(consumed[1:])}" if len(consumed) > 1 else ""
                ),
                {"terminal-write": "remove-selection-label", "label": selection_label}
                | ({"also": list(consumed[1:])} if len(consumed) > 1 else {}),
            )
            if retrying:
                labels, _ = _remote_labels_and_assignees(inputs, number, input_type)
                if labels is not None and not any(
                    label in labels for label in consumed
                ):
                    _record_terminal_receipt(
                        ledger, attempt_id, REMOVE_SELECTION_LABEL_ORDINAL, selection_label
                    )
                    continue
            for label in consumed:
                inputs.remove_label(number, input_type, label)
            _record_terminal_receipt(
                ledger, attempt_id, REMOVE_SELECTION_LABEL_ORDINAL, selection_label
            )
        elif write_kind == TERMINAL_WRITE_ADD_LABELS:
            if _has_terminal_receipt(ledger, attempt_id, ADD_END_LABELS_ORDINAL):
                continue
            retrying = _has_terminal_intent(ledger, attempt_id, ADD_END_LABELS_ORDINAL)
            _record_terminal_intent(
                ledger,
                attempt_id,
                ADD_END_LABELS_ORDINAL,
                f"add labels {','.join(safe_end_labels)}",
                {
                    "terminal-write": "add-end-labels",
                    "labels": list(safe_end_labels),
                },
            )
            if not safe_end_labels:
                _record_terminal_receipt(
                    ledger, attempt_id, ADD_END_LABELS_ORDINAL, ""
                )
                continue
            if retrying:
                labels, _ = _remote_labels_and_assignees(inputs, number, input_type)
                if labels is not None and all(
                    label in labels for label in safe_end_labels
                ):
                    _record_terminal_receipt(
                        ledger,
                        attempt_id,
                        ADD_END_LABELS_ORDINAL,
                        ",".join(safe_end_labels),
                    )
                    continue
            inputs.add_labels(number, input_type, safe_end_labels)
            _record_terminal_receipt(
                ledger, attempt_id, ADD_END_LABELS_ORDINAL, ",".join(safe_end_labels)
            )
        elif write_kind == TERMINAL_WRITE_RELEASE:
            if _has_terminal_receipt(ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL):
                continue
            retrying = _has_terminal_intent(
                ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL
            )
            _record_terminal_intent(
                ledger,
                attempt_id,
                RELEASE_ASSIGNMENT_ORDINAL,
                "release assignment",
                {"terminal-write": "release-assignment"},
            )
            if other_claim_in_force(inputs, ledger, attempt_id, number, input_type):
                _record_terminal_receipt(
                    ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL, inputs.agent_login
                )
                continue
            if retrying:
                _, assignees = _remote_labels_and_assignees(
                    inputs, number, input_type
                )
                if assignees is not None and inputs.agent_login not in assignees:
                    _record_terminal_receipt(
                        ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL, inputs.agent_login
                    )
                    continue
            inputs.unassign_input(number, input_type)
            _record_terminal_receipt(
                ledger, attempt_id, RELEASE_ASSIGNMENT_ORDINAL, inputs.agent_login
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


AttemptDecision = (
    WorkflowEnded
    | GateStopped
    | PinViolated
    | InternalFailure
    | Interrupted
    | CredentialFailed
)
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
    return module_for_intent(intent) is not None


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
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    attempt_id: str,
    target: FinalisationTarget,
    decision: AttemptDecision,
    end_labels_for: Mapping[str, Sequence[str]],
    uncommitted_count: int = 0,
    handoff: RoundHandoffOffer | HandoffDiagnostic | None = None,
) -> FinalisedAttempt:
    """End one Attempt through its outcome decision: the single entry point.

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §15 (the
    Terminal Outcome set; the finalisation sequence: settle in-flight
    effects, record the Terminal Outcome with the rendered terminal comment
    and the terminal-write list once and immutably, then perform the
    terminal writes; then the Attempt is marked ``finished``), §14.2
    (settlement on restart), §16 (recovery finishes a recorded outcome
    and never re-decides it) and §8 (a prepared Round Handoff offer settles
    its terminal comment, the consumed intake label and the Pickup Label
    before releasing assignment; the Pickup Label replaces the configured
    ``unfinished`` labels for an eligible round).

    The sequence, enforced here and tested here: refuse where a settleable
    Effect Intent is still unsettled (settling stays the caller's job);
    close open Step Executions with the decision's close reason; where an
    outcome is already recorded, ignore the decision — and the passed
    handoff — and replay the recorded row's comment and writes (ADR 0017:
    finish a recorded outcome, never re-decide it), with the stored
    comment's own valid non-exhausted record replaying its Pickup Label;
    otherwise render the terminal comment (carrying the offer's record
    block or the no-pickup diagnostic above the terminal marker) and record
    the full Terminal Outcome row once — outcome, reason, comment, writes —
    before the first tracker write (ADR 0017: recorded before published);
    perform the terminal writes with the end labels of the outcome actually
    recorded or replayed; mark the Attempt ``finished``.

    A live non-exhausted offer replaces the configured end labels with its
    exact Pickup Label; an exhausted offer or a diagnostic keeps the
    configured labels. A live ``unfinished`` end that is eligible from the
    pinned configuration but carries no prepared offer records the
    ``missing-handoff`` diagnostic (the push landed before preparation) and
    keeps ordinary labels with no pickup after restart.

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
        replay_selection, replay_end_labels = _resolve_terminal_plan(
            ledger, attempt_id, stored.outcome, target.selection_label, end_labels_for
        )
        if stored.outcome == "unfinished":
            replay_pickup = _replay_pickup_label(stored.terminal_comment_body)
            if replay_pickup is not None:
                replay_end_labels = (replay_pickup,)
        replay_relay = _replay_relay_label(stored.terminal_comment_body)
        if replay_relay is not None:
            replay_end_labels = (*replay_end_labels, replay_relay)
        perform_terminal_writes(
            inputs=inputs,
            code_host=code_host,
            ledger=ledger,
            attempt_id=attempt_id,
            number=target.number,
            input_type=target.input_type,
            selection_label=replay_selection,
            end_labels=replay_end_labels,
            comment_body=stored.terminal_comment_body,
            writes=stored.writes,
            also_remove_labels=_relay_consumed(ledger, attempt_id),
        )
        ledger.set_attempt_state(attempt_id, "finished")
        return FinalisedAttempt(
            attempt_id=attempt_id,
            outcome=stored.outcome,
            reason=stored.reason,
            finished=True,
        )
    handoff_section = ""
    live_pickup: tuple[str, ...] | None = None
    if resolved.outcome == "unfinished":
        if isinstance(handoff, RoundHandoffOffer):
            handoff_section = render_handoff_section(handoff)
            if not handoff.exhausted:
                live_pickup = (handoff.pickup_label,)
        elif isinstance(handoff, HandoffDiagnostic):
            handoff_section = render_diagnostic_section(handoff)
        elif handoff is None:
            eligibility = pinned_handoff_eligibility(
                ledger, attempt_id, resolved.ending_step, resolved.step_result
            )
            if eligibility is not None:
                handoff_section = render_diagnostic_section(
                    _missing_handoff_diagnostic(
                        attempt_id,
                        resolved.ending_step,
                        resolved.step_result,
                        eligibility[0],
                    )
                )
    relay_section, live_relay_label = _prepare_relay(
        ledger, attempt_id, resolved.outcome
    )
    if relay_section:
        handoff_section = (
            handoff_section + "\n\n" + relay_section
            if handoff_section
            else relay_section
        )
    comments_section = ""
    try:
        late = late_trusted_comments(
            inputs,
            ledger,
            attempt_id=attempt_id,
            number=target.number,
            input_type=target.input_type,
        )
    except Exception:
        late = None
    if late is not None:
        comments_section = render_comments_disclosure(late)
    comment = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=resolved.outcome,
        marker_text=inputs.format_marker(terminal_marker_for(attempt_id)),
        ending_step=resolved.ending_step,
        step_result=resolved.step_result,
        workflow_end=resolved.workflow_end,
        limit_fact=resolved.limit_fact,
        landed=_landed_summaries(ledger, attempt_id),
        not_performed=_not_performed_summaries(ledger, attempt_id),
        uncommitted_count=uncommitted_count,
        detail=resolved.detail,
        handoff_section=handoff_section,
        comments_section=comments_section,
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
    live_selection, live_end_labels = _resolve_terminal_plan(
        ledger, attempt_id, resolved.outcome, target.selection_label, end_labels_for
    )
    if live_pickup is not None:
        live_end_labels = live_pickup
    if live_relay_label is not None:
        live_end_labels = (*live_end_labels, live_relay_label)
    perform_terminal_writes(
        inputs=inputs,
        code_host=code_host,
        ledger=ledger,
        attempt_id=attempt_id,
        number=target.number,
        input_type=target.input_type,
        selection_label=live_selection,
        end_labels=live_end_labels,
        comment_body=comment,
        writes=writes,
        also_remove_labels=_relay_consumed(ledger, attempt_id),
    )
    ledger.set_attempt_state(attempt_id, "finished")
    return FinalisedAttempt(
        attempt_id=attempt_id,
        outcome=resolved.outcome,
        reason=resolved.reason,
        finished=True,
    )
