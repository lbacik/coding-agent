"""The Attempt Gates' stop signals: cancellation today, SIGTERM next.

Contract: ``docs/contract/mvp-runtime-contract.md`` §6 (the Attempt graph;
``gate`` is the Gate between steps), §8 (cancellation at a step-boundary
Gate yields ``abandoned``), §10 (the in-tool Gate in front of the
review-snapshot push: a stop found there ends the Attempt at that Gate,
keeping whatever is already pushed) and §11 (a Gate re-reads GitHub state,
re-evaluates the Attempt's limits and may stop the Attempt cooperatively;
a human cancels by closing the issue or removing the Selection Label; a
Gate before the Delivery Intent that sees either stops the model loop,
commits the existing edits and pushes the branch, and ends the Attempt
``abandoned`` with no pull request; after the Delivery Intent cancellation
does not stop publication).

MVP row ids: P-CAN-1, P-CAN-2, W-STP-3, G-REV-3.

A Gate raises :class:`GateStop` carrying the Terminal Outcome and reason
the Attempt ends with. Cancellation is the first stop signal
(``abandoned`` / ``cancelled``); the SIGTERM ticket adds its own stop
signal — ``failed`` / ``interrupted``, or ``abandoned`` where the issue
was cancelled meanwhile — raising the same ``GateStop`` from the same
Gates, without changing this module's cancellation shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.tracker.port import IssueState, TrackerPort

CANCELLED_REASON = "cancelled"
"""The Terminal Outcome reason for a cancellation before the Delivery Intent.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 with the §14
route "Cancellation before the Delivery Intent" → ``abandoned``.
MVP row ids: P-CAN-1, W-STP-3, G-REV-3.
"""

ABANDONED_OUTCOME = "abandoned"
"""The Terminal Outcome a pre-Intent cancellation ends with (§11, §14)."""


@dataclass(frozen=True)
class GateStop(Exception):
    """A stop signal a Gate detected: the Attempt ends here (§11).

    ``outcome`` and ``reason`` are the Terminal Outcome the node reports —
    for cancellation, ``abandoned`` / ``cancelled``. Raised across the
    in-tool Gate (tool results cannot carry control flow) and caught by the
    caller that owns the Attempt's end: the step loop for the in-tool Gate,
    the node itself for a graph Gate.
    """

    outcome: str
    reason: str


def cancellation_reason(state: IssueState, selection_label: str) -> str | None:
    """The cancellation reason where ``state`` shows one, else ``None`` (§11).

    A human cancels by closing the issue or removing the Selection Label:
    a closed issue and an open issue without the label both cancel
    (P-CAN-1). Anything else — open with the label present — is not a
    cancellation, so the Gate lets the Attempt through.

    MVP row ids: P-CAN-1, W-STP-3, G-REV-3.
    """
    if not state.is_open:
        return CANCELLED_REASON
    if selection_label not in state.labels:
        return CANCELLED_REASON
    return None


def check_cancellation(
    *, tracker: TrackerPort, issue_number: int, selection_label: str
) -> None:
    """Re-read the issue and raise :class:`GateStop` where it is cancelled.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (every Gate
    before the Delivery Intent re-reads the issue's open state and labels
    through the port and checks for cancellation). Read-only: the check
    writes nothing, on any outcome.

    MVP row ids: P-CAN-1, W-STP-3, G-REV-3.
    """
    state = tracker.read_issue_state(issue_number)
    reason = cancellation_reason(state, selection_label)
    if reason is not None:
        raise GateStop(outcome=ABANDONED_OUTCOME, reason=reason)
