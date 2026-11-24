"""Projected operator native events for Round Handoff (§16 v1.2.1).

Contract: the operator contract's ``native.*`` Contract Events. Every
function here reads the Run Ledger's lifecycle tables (``attempts``,
``outcomes``, ``step_effects``, ``run_ledger_events``) only through a
:class:`~coding_agent.ledger.reader.RunLedgerReader`. Nothing here writes:
the events are pure projections of durable recorded facts — actual runtime
threshold crossings, the AI-assisted Handoff result or refusal, Hard
Handoff entry, verified Round Handoff readiness and pickup exhaustion —
so they hold after restart, on an independent Worker reading the same
ledger, and during idle Intake from the last finished Attempt. An ordinary
Step Result that happens to use a reserved spelling emits no crossing
event: crossings project only from recorded runtime threshold events,
never from result names.

The terminal comment body the projection parses is already redacted at
rest (the Terminal Outcome write redacts it once); crossing and readiness
texts otherwise carry only token counts, labels and allowlisted reasons,
capped at 300 characters like every Contract Event. Consumers MAY ignore
native events.
"""

from __future__ import annotations

import re
from typing import Any

from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.operator.archive import (
    NATIVE_AI_HANDOFF_REFUSED,
    NATIVE_AI_HANDOFF_RESULT,
    NATIVE_CONTEXT_THRESHOLD_CROSSED,
    NATIVE_HARD_HANDOFF_ENTERED,
    NATIVE_PICKUP_EXHAUSTED,
    NATIVE_ROUND_HANDOFF_READY,
)

__all__ = [
    "NATIVE_CONTEXT_THRESHOLD_CROSSED",
    "NATIVE_AI_HANDOFF_RESULT",
    "NATIVE_AI_HANDOFF_REFUSED",
    "NATIVE_HARD_HANDOFF_ENTERED",
    "NATIVE_ROUND_HANDOFF_READY",
    "NATIVE_PICKUP_EXHAUSTED",
    "NATIVE_EVENT_TYPES",
    "native_events_for_attempt",
]
"""The projected event names are the archive's canonical constants.

:mod:`coding_agent.operator.archive` owns the single definition of each
``native.*`` Contract Event type (it is a leaf module and cannot import
this projection); this module re-exports them for projection consumers.
"""

NATIVE_EVENT_TYPES = (
    NATIVE_CONTEXT_THRESHOLD_CROSSED,
    NATIVE_AI_HANDOFF_RESULT,
    NATIVE_AI_HANDOFF_REFUSED,
    NATIVE_HARD_HANDOFF_ENTERED,
    NATIVE_ROUND_HANDOFF_READY,
    NATIVE_PICKUP_EXHAUSTED,
)
"""Every projected ``native.*`` event type, oldest evidence first."""

_MAX_EVENT_TEXT_LEN = 300
"""Projected native event texts cap at 300 characters, like every event."""

_CROSSING_KINDS = (
    "context_threshold",
    "soft_context_threshold",
    "hard_context_threshold",
)
"""Run Ledger event kinds recording an actual runtime threshold crossing."""

_CROSSING_LABELS = {
    "context_threshold": "context threshold",
    "soft_context_threshold": "soft context threshold",
    "hard_context_threshold": "hard context threshold",
}
"""The human phrase each crossing kind reports under."""

# Terminal-write receipt keys confirming a verified round (§15.2, §15.3):
# the terminal comment itself (``TerminalCommentTarget`` records
# ``{"terminal-write": "terminal"}``), the Pickup Label removal and the
# assignment release. Must match ``runtime.step_finalisation``.
_TERMINAL_RECEIPT_KEYS = frozenset(
    {"terminal", "remove-selection-label", "release-assignment"}
)
"""Step-effect receipt keys proving the terminal writes landed."""

_ENDING_RE = re.compile(r"^Ending: step (\S+) result (\S+) end (\S+)\s*$", re.MULTILINE)
"""The terminal comment's machine-shaped ending line, when recorded."""

_DIAGNOSTIC_PREFIX = "Round Handoff: no pickup ("
"""The operator-visible prefix of a recorded no-pickup diagnostic."""


def _cap(text: str) -> str:
    """A projected event text, capped at 300 characters like every event."""
    return text[:_MAX_EVENT_TEXT_LEN]


def _effective_configuration(reader: RunLedgerReader, attempt_id: str) -> dict[str, Any]:
    """The Attempt's pinned Effective Configuration, or ``{}`` unpinned."""
    try:
        record = reader.get_attempt(attempt_id)
    except Exception:
        return {}
    if record is None:
        return {}
    effective = record.effective_configuration
    return effective if isinstance(effective, dict) else {}


def _route_kind(
    effective: dict[str, Any], ending_step: str | None, step_result: str | None
) -> str | None:
    """The taken Route's ``round_handoff`` kind, if the end publishes one."""
    if not ending_step or not step_result:
        return None
    workflow = effective.get("workflow")
    if not isinstance(workflow, dict):
        return None
    steps = workflow.get("steps")
    if not isinstance(steps, dict):
        return None
    step = steps.get(ending_step)
    if not isinstance(step, dict):
        return None
    routes = step.get("routes")
    if not isinstance(routes, dict):
        return None
    route = routes.get(step_result)
    if not isinstance(route, dict):
        return None
    annotation = route.get("round_handoff")
    if not isinstance(annotation, dict):
        return None
    kind = annotation.get("kind")
    return kind if isinstance(kind, str) and kind else None


def _parse_ending(body: str) -> tuple[str | None, str | None]:
    """The ``(ending_step, step_result)`` the terminal comment records, if any."""
    match = _ENDING_RE.search(body)
    if match is None:
        return (None, None)
    ending_step = match.group(1)
    step_result = match.group(2)
    if ending_step == "-":
        ending_step = ""
    if step_result == "-":
        step_result = ""
    return (ending_step or None, step_result or None)


def _parse_diagnostic(body: str) -> tuple[str, str] | None:
    """The ``(reason, detail)`` of a recorded no-pickup diagnostic, if any."""
    for line in body.splitlines():
        if not line.startswith(_DIAGNOSTIC_PREFIX):
            continue
        rest = line[len(_DIAGNOSTIC_PREFIX) :]
        reason, separator, detail = rest.partition("):")
        if not separator or not reason:
            continue
        return (reason, detail.strip())
    return None


def _terminal_receipts(reader: RunLedgerReader, attempt_id: str) -> set[str]:
    """The terminal-write keys with a recorded receipt for the Attempt.

    Claim rows share the ``("", 0)`` key space, so only rows whose
    ``expected_target`` carries a ``terminal-write`` key count — the same
    discrimination the finalisation recovery uses.
    """
    try:
        effects = reader.list_step_effects(attempt_id)
    except Exception:
        return set()
    receipts: set[str] = set()
    for raw in effects:
        if isinstance(raw, dict):
            step_id = raw.get("step_id")
            execution_number = raw.get("execution_number")
            ordinal = raw.get("ordinal")
            status = raw.get("status")
            target = raw.get("expected_target")
        else:
            step_id = getattr(raw, "step_id", None)
            execution_number = getattr(raw, "execution_number", None)
            ordinal = getattr(raw, "ordinal", None)
            status = getattr(raw, "status", None)
            target = getattr(raw, "expected_target", None)
        if step_id != "":
            continue
        if execution_number != 0:
            continue
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            continue
        if status != "receipt":
            continue
        if not isinstance(target, dict):
            continue
        key = target.get("terminal-write")
        if isinstance(key, str) and key in _TERMINAL_RECEIPT_KEYS:
            receipts.add(key)
    return receipts


def _basis_phrase(basis: str) -> str:
    """The human phrase for a recorded Conversation Context basis."""
    if basis == "anchored":
        return "anchored on provider-reported input"
    return "local estimate, no measured response yet"


def _crossing_events(
    reader: RunLedgerReader,
    attempt_id: str,
    effective: dict[str, Any],
    process_started_at: str,
    observed_at: str,
    issue: int | None,
) -> list[dict[str, Any]]:
    """One event per actual runtime threshold crossing, in ledger order."""
    from coding_agent.runtime.conversation_context import (
        recorded_conversation_context,
    )

    try:
        pairs = reader.list_event_payloads(attempt_id, _CROSSING_KINDS)
    except Exception:
        return []
    try:
        from coding_agent.operator.snapshots import _selected_usable_thresholds
    except Exception:
        _selected_usable_thresholds = None  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    for kind, payload in pairs:
        if not isinstance(payload, dict):
            continue
        recorded = recorded_conversation_context(payload)
        if recorded.value is None:
            continue
        usable = payload.get("usable_threshold")
        if isinstance(usable, bool) or not isinstance(usable, (int, float)):
            continue
        label = _CROSSING_LABELS.get(kind, kind)
        step_id = payload.get("step_id")
        step_text = ""
        if isinstance(step_id, str) and step_id:
            seq = payload.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1:
                step_text = f" at step {step_id} (execution {seq})"
            else:
                step_text = f" at step {step_id}"
        text = (
            f"main conversation crossed the {label}{step_text} "
            f"at {recorded.value} tokens "
            f"({_basis_phrase(recorded.basis)}); "
            f"usable threshold {usable}"
        )
        if _selected_usable_thresholds is not None and isinstance(step_id, str):
            try:
                _, hard = _selected_usable_thresholds(
                    {"effective_configuration": effective}, step_id
                )
            except Exception:
                hard = None
            if (
                isinstance(hard, (int, float))
                and not isinstance(hard, bool)
                and hard != usable
            ):
                text += f"; hard usable threshold {hard}"
        record: dict[str, Any] = {
            "at": observed_at,
            "level": "info",
            "event": NATIVE_CONTEXT_THRESHOLD_CROSSED,
            "text": _cap(text),
            "process_started_at": process_started_at,
            "attempt_id": attempt_id,
        }
        if issue is not None:
            record["issue"] = issue
        events.append(record)
    return events


def _handoff_events(
    reader: RunLedgerReader,
    attempt_id: str,
    effective: dict[str, Any],
    process_started_at: str,
    observed_at: str,
    issue: int | None,
) -> list[dict[str, Any]]:
    """Result, refusal, entry, readiness and exhaustion from terminal facts."""
    from coding_agent.runtime.round_handoff import parse_handoff_block

    try:
        outcome = reader.get_outcome(attempt_id)
    except Exception:
        outcome = None
    body = getattr(outcome, "terminal_comment_body", "") if outcome is not None else ""
    if not isinstance(body, str) or not body:
        return []
    try:
        offer = parse_handoff_block(body)
    except Exception:
        offer = None
    diagnostic = _parse_diagnostic(body)
    ending_step, step_result = _parse_ending(body)
    kind = _route_kind(effective, ending_step, step_result)

    def _record(
        name: str, text: str, reason: str | None = None
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "at": observed_at,
            "level": "info",
            "event": name,
            "text": _cap(text),
            "process_started_at": process_started_at,
            "attempt_id": attempt_id,
        }
        if issue is not None:
            record["issue"] = issue
        if reason:
            record["reason"] = reason
        return record

    events: list[dict[str, Any]] = []
    if offer is not None and offer.note_kind == "hard":
        if offer.exhausted:
            events.append(
                _record(
                    NATIVE_HARD_HANDOFF_ENTERED,
                    f"Hard Handoff entered: {attempt_id} publishes its final "
                    f"round ({offer.used}/{offer.limit}, "
                    f"{offer.exhaustion_reason or 'pickup-limit-reached'})",
                )
            )
        else:
            events.append(
                _record(
                    NATIVE_HARD_HANDOFF_ENTERED,
                    f"Hard Handoff entered: {attempt_id} carries a hard "
                    f"Handoff note ({offer.used}/{offer.limit})",
                )
            )
    elif diagnostic is not None and kind == "hard":
        where = ""
        if ending_step and step_result:
            where = f" at step {ending_step} (result {step_result})"
        events.append(
            _record(
                NATIVE_HARD_HANDOFF_ENTERED,
                f"Hard Handoff entered{where} with no pickup offered "
                f"({diagnostic[0]}): {diagnostic[1]}",
                reason=diagnostic[0],
            )
        )
    if offer is not None and offer.note_kind == "ai-assisted" and not offer.exhausted:
        events.append(
            _record(
                NATIVE_AI_HANDOFF_RESULT,
                f"AI-assisted Handoff result: {attempt_id} offers pickup "
                f"under {offer.pickup_label} ({offer.used}/{offer.limit})",
            )
        )
    if diagnostic is not None and (kind is None or kind == "ai-assisted"):
        where = ""
        if ending_step and step_result:
            where = f" at step {ending_step} (result {step_result})"
        events.append(
            _record(
                NATIVE_AI_HANDOFF_REFUSED,
                f"AI-assisted Handoff refused{where}: no pickup "
                f"({diagnostic[0]}): {diagnostic[1]}",
                reason=diagnostic[0],
            )
        )
    if offer is not None and not offer.exhausted:
        receipts = _terminal_receipts(reader, attempt_id)
        if _TERMINAL_RECEIPT_KEYS.issubset(receipts):
            events.append(
                _record(
                    NATIVE_ROUND_HANDOFF_READY,
                    f"Round Handoff ready: pickup under {offer.pickup_label} "
                    f"({offer.used}/{offer.limit}) verified: terminal "
                    f"comment, Pickup Label and assignment release confirmed",
                )
            )
    if offer is not None and offer.exhausted:
        events.append(
            _record(
                NATIVE_PICKUP_EXHAUSTED,
                f"pickup exhausted ({offer.used}/{offer.limit}, "
                f"{offer.exhaustion_reason or 'pickup-limit-reached'}); "
                f"a human must reauthorise a new series",
                reason=offer.exhaustion_reason or "pickup-limit-reached",
            )
        )
    return events


def _hard_entry_from_events(
    reader: RunLedgerReader,
    attempt_id: str,
    process_started_at: str,
    observed_at: str,
    issue: int | None,
) -> list[dict[str, Any]]:
    """Hard Handoff entry while the Attempt still runs, before any terminal."""
    try:
        pairs = reader.list_event_payloads(attempt_id, ("hard_context_threshold",))
    except Exception:
        return []
    first: dict[str, Any] | None = None
    for _, payload in pairs:
        if isinstance(payload, dict):
            first = payload
            break
    if first is None:
        return []
    step_id = first.get("step_id")
    step_text = ""
    if isinstance(step_id, str) and step_id:
        seq = first.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1:
            step_text = f" at step {step_id} (execution {seq})"
        else:
            step_text = f" at step {step_id}"
    record: dict[str, Any] = {
        "at": observed_at,
        "level": "info",
        "event": NATIVE_HARD_HANDOFF_ENTERED,
        "text": _cap(f"Hard Handoff entered{step_text}: hard threshold crossed"),
        "process_started_at": process_started_at,
        "attempt_id": attempt_id,
    }
    if issue is not None:
        record["issue"] = issue
    return [record]


def native_events_for_attempt(
    reader: RunLedgerReader,
    attempt_id: str,
    *,
    process_started_at: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    """The projected ``native.*`` events for one Attempt, oldest evidence first.

    Crossings come from recorded runtime threshold events; the Handoff
    result, refusal, entry, readiness and exhaustion come from the recorded
    Terminal Outcome (the terminal comment's offer block, diagnostic and
    ending line against the pinned Route annotation) plus the terminal
    step-effect receipts. Readiness reports only after the terminal
    comment, Pickup Label and assignment release receipts are all
    confirmed. Never raises: an unreadable ledger reads as no events.
    """
    from coding_agent.attempt_id import AttemptId

    issue: int | None = None
    try:
        parsed = AttemptId.try_parse(attempt_id)
        number = parsed.input_number if parsed is not None else None
        if isinstance(number, int) and not isinstance(number, bool) and number >= 1:
            issue = number
    except Exception:
        issue = None
    try:
        effective = _effective_configuration(reader, attempt_id)
        events = _crossing_events(
            reader, attempt_id, effective, process_started_at, observed_at, issue
        )
        handoff = _handoff_events(
            reader, attempt_id, effective, process_started_at, observed_at, issue
        )
        if not any(
            event.get("event") == NATIVE_HARD_HANDOFF_ENTERED for event in handoff
        ):
            events.extend(
                _hard_entry_from_events(
                    reader, attempt_id, process_started_at, observed_at, issue
                )
            )
        events.extend(handoff)
        return events
    except Exception:
        return []
