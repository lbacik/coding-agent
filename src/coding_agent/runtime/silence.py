"""Deterministic silence continuations for the step subgraph.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 ("Exit": silence is
answered, not obeyed — a response with no tool calls and no accepted exit
draws one deterministic continuation message composed by the runtime from
the unmet obligations; the budget is two continuations per work node,
monotonic and never reset by progress; once exhausted the node ends
``completion not established`` / ``undeclared``; no continuation is issued
in the verification reserve phase or after a hard ceiling) and §9
(``report_completion`` is the only non-exceptional exit), and
[ADR 0014](../../docs/adr/0014-completion-is-declared-and-substantiated-never-inferred.md)
(silence is answered, not obeyed).

MVP row ids: L3-IMP-24, L3-IMP-25, L3-IMP-26.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

MAX_SILENCE_CONTINUATIONS = 2
"""Continuations per work node (contract §8, ADR 0014).

The budget only ever goes down: progress never resets it. A fresh work node
after a step boundary or Context-Window Handoff opens with both restored,
because the Continuation Snapshot carries work facts and never runtime
policy state.
"""

SILENCE_CONTINUATION_MARKER = "Implementation cannot be inferred from silence"
"""Marker prefix of every continuation message.

Tests count continuations in the bytes sent to the scripted model by this
prefix, never by node internals. The marker is stable so a reordered
criterion list or a new test id cannot hide a continuation.
"""

SILENCE_EXHAUSTED_REASON = "undeclared"
"""Reason when the continuation budget is spent (contract §8, §9).

The Implementation Outcome is ``completion not established`` with this
reason, whether or not a candidate diff exists, because silence is not an
external cause.
"""


def compose_silence_continuation(
    *,
    sequence_number: int,
    acceptance_criteria: tuple[tuple[str, str], ...],
    uncovered_ids: tuple[str, ...],
    has_diff: bool,
    executed_tests: tuple[tuple[str, bool], ...],
) -> str:
    """Compose one deterministic continuation message (contract §8).

    Pure function of its arguments: no model call, no timestamp, no
    randomness. The same state always yields the same bytes. ``sequence_number``
    is 1-indexed within ``MAX_SILENCE_CONTINUATIONS``. ``uncovered_ids`` are
    the Acceptance Criteria with no passing Criterion Evidence; with no
    accepted exit every criterion the Attempt holds is uncovered. ``has_diff``
    names whether a candidate diff exists. ``executed_tests`` are
    ``(test_id, passed)`` pairs from Criterion Evidence, sorted by the caller.
    """
    total = MAX_SILENCE_CONTINUATIONS
    criteria_text = "; ".join(
        f"{criterion_id}: {text}"
        for criterion_id, text in acceptance_criteria
        if criterion_id in set(uncovered_ids)
    )
    if not criteria_text and uncovered_ids:
        criteria_text = "; ".join(sorted(uncovered_ids))
    if not criteria_text:
        criteria_text = "(none)"
    if executed_tests:
        executed_text = "; ".join(
            f"{test_id} ({'pass' if passed else 'fail'})" for test_id, passed in executed_tests
        )
    else:
        executed_text = "(none)"
    diff_text = (
        "present" if has_diff else "absent (the working tree is identical to the Base Revision)"
    )
    return (
        f"{SILENCE_CONTINUATION_MARKER} "
        f"(continuation {sequence_number}/{total}).\n"
        f"Uncovered Acceptance Criteria ({len(uncovered_ids)}): {criteria_text}.\n"
        f"Candidate diff: {diff_text}.\n"
        f"Targeted tests executed ({len(executed_tests)}): {executed_text}.\n"
        "Call report_completion to declare completion, or use the work tools "
        "to make progress. Silence alone never establishes completion."
    )


def is_silence_continuation(message: BaseMessage) -> bool:
    """Whether ``message`` is a runtime-composed silence continuation."""
    content = message.content
    text = content if isinstance(content, str) else str(content)
    return SILENCE_CONTINUATION_MARKER in text


def count_continuations(messages: list[BaseMessage]) -> int:
    """Count continuation messages in a message list (test seam)."""
    return sum(1 for message in messages if is_silence_continuation(message))


def continuation_message(sequence_number: int, text: str) -> HumanMessage:
    """Wrap composed continuation ``text`` as the next model input."""
    _ = sequence_number
    return HumanMessage(content=text)
