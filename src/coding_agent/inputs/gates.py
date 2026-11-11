"""Gates, Drift and withdrawal (contract §7.3).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §7.3 (a Gate
re-reads the input and the Attempt's limits; Gates sit before every Step
Execution and before every external write; Drift — a title/body edit, a
foreign head change, a base-branch change — ends the Attempt with the
runtime-only Terminal Outcome ``drift`` and runs no further step;
withdrawal — the Selection Label removed by a human, the input closed, a
pull request merged — ends the Attempt ``withdrawn``; new base-branch
commits are not Drift, only disclosed; a draft/ready change is ignored;
both endings run the finalisation of §15).

Row ids: INP-1 … INP-3, INP-6, INP-7, INP-16 … INP-18.

This module is pure: it compares a fresh tracker read with the pins and
answers the Gate verdict. The caller performs the read and, for a
pull-request Attempt of its own, passes the Input Head its own
compare-and-swap push advanced to — that advance is not Drift (§7.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from coding_agent.inputs.pinning import PinnedInput
from coding_agent.inputs.port import TrackedInput

GateStatus = Literal["ok", "drift", "withdrawn"]
"""A Gate's verdict: continue, Drift, or withdrawal (§7.3)."""


@dataclass(frozen=True)
class GateResult:
    """What one Gate re-read found (§7.3).

    ``reason`` names the Drift or withdrawal: ``content-changed``,
    ``head-changed``, ``base-branch-changed``, ``selection-label-removed``,
    ``closed`` or ``merged``; it is empty where the Gate passes.
    ``base_moved`` discloses base-branch commits that are not Drift
    (INP-18): the base tip differs from the Base Revision (for a
    pull-request input, from the tip the merge base was computed against).
    """

    status: GateStatus
    reason: str = ""
    base_moved: bool = False


def check_gate(
    current: TrackedInput,
    pin: PinnedInput,
    *,
    selection_label: str,
    expected_head: str | None = None,
    base_tip_now: str | None = None,
    base_tip_at_claim: str | None = None,
) -> GateResult:
    """Check one Gate re-read against the pins (§7.3).

    Withdrawal is checked first: the Selection Label removed, the input
    closed, or the pull request merged. Then Drift: a title/body edit, a
    head that differs from the Input Head, or a base-branch change. A
    draft/ready change is ignored. ``expected_head`` carries the Input Head
    the Attempt's own compare-and-swap push advanced to; without it the
    pinned head decides. ``base_tip_now`` against ``base_tip_at_claim``
    (defaulting to the pin's Base Revision) reports ``base_moved`` without
    ever drifting.
    """
    if selection_label not in current.labels:
        return GateResult(status="withdrawn", reason="selection-label-removed")
    if not current.is_open:
        return GateResult(status="withdrawn", reason="closed")
    if current.input_type == "pull-request" and current.merged:
        return GateResult(status="withdrawn", reason="merged")
    if current.title != pin.title or current.body != pin.body:
        return GateResult(status="drift", reason="content-changed")
    if current.input_type == "pull-request":
        if current.base_branch != pin.base_branch:
            return GateResult(status="drift", reason="base-branch-changed")
        wanted = expected_head if expected_head is not None else pin.input_head
        if (
            wanted is not None
            and current.head_sha is not None
            and current.head_sha != wanted
        ):
            return GateResult(status="drift", reason="head-changed")
    moved = False
    if base_tip_now is not None:
        baseline = base_tip_at_claim
        if baseline is None:
            baseline = pin.base_tip if pin.base_tip is not None else pin.base_revision
        moved = base_tip_now != baseline
    return GateResult(status="ok", base_moved=moved)
