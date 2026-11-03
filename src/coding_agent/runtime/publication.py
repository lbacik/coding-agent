"""The Publication Gate verdict, the Delivery Intent and the tail rendering.

Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the Publication
Gate re-reads the Target Issue and decides the verdict; a pull request
requires, in order, the Implementation Outcome ``verified completion``, no
Drift, the review gate, green final checks and a clean tree; the first unmet
condition names the reason and every unmet condition is recorded for the
terminal comment; base-branch movement does not block but is recorded for
disclosure; ``delivery_intent`` records the outcome ``delivered`` as a
Delivery Intent before any delivery write: the Attempt id, branch, head sha,
verdict and the writes the outcome calls for; writing the Intent is the point
of no return), §13 (the write sequence keyed by ``(attempt_id, write_kind)``;
a pull request opens only once, found by head branch plus the Attempt Marker),
§14 (the Implementation Outcome → Terminal Outcome table) and §15 (the pull
request and terminal comment contents, with the Attempt Marker), and
[ADR 0003](../adr/0003-delivery-intent.md) (the Intent is written before the
first delivery write and a resumption finishes it rather than re-deciding).

MVP row ids: P-DEL-1, P-DEL-2, P-DEL-6, P-DEL-7, P-DEL-8, P-DEL-9, C-PUB-1,
C-PUB-2, G-TDD-1, L3-DEL-5, L3-REV-10, C-OUT-1.
"""

from __future__ import annotations

from dataclasses import dataclass


PUBLICATION_GATE_KIND = "publication_gate"
"""Run Ledger event kind for the Publication Gate verdict (§12)."""

DELIVERY_INTENT_KIND = "delivery_intent"
"""Run Ledger event kind for the Delivery Intent (§12, ADR 0003)."""

PUBLISH_WRITE_KIND = "open-pull-request"
"""The ``effects`` write kind for opening the pull request (§13).

Recorded through the same intent/receipt sequence as every other external
write. Over the ``local`` tracker the write is recorded without being
performed; the GitHub adapter performs it in M6.
"""

DRIFT_REASON = "drift"
REVIEW_MISSING_REASON = "review-missing"
FAILING_VALIDATION_REASON = "failing-validation"
DIRTY_TREE_REASON = "dirty-tree"

VERIFIED_COMPLETION_REASON = "verified-completion"

TERMINAL_COMMENT_KIND = "terminal"
DELIVERY_COMMENT_KIND = "delivery"


@dataclass(frozen=True)
class PublicationVerdict:
    """The Publication Gate's deterministic verdict (§12).

    ``ready`` is true only when every gate condition holds. ``reason`` is the
    Terminal Outcome reason: ``verified-completion`` when ready, else the
    first unmet condition in the contract order Drift → review → final checks
    → clean tree. ``unmet`` carries every unmet condition for the terminal
    comment. ``base_moved`` records whether the base head differs from the
    Base Revision; it never blocks and the Worker never rebases or merges.
    """

    ready: bool
    reason: str
    unmet: tuple[str, ...]
    base_moved: bool
    base_revision: str
    base_head: str | None


def evaluate_publication_gate(
    *,
    is_verified_completion: bool,
    fingerprint_match: bool,
    review_gate_met: bool,
    final_checks_green: bool,
    dirty_files: tuple[str, ...],
    base_revision: str,
    base_head: str | None,
) -> PublicationVerdict:
    """Decide the Publication Gate verdict from its ordered conditions (§12).

    The order is Drift → review → final checks → clean tree. Base-branch
    movement is compared and recorded but never blocks. Where the
    Implementation Outcome is not ``verified completion`` there is no verdict
    to decide here: the caller passes that through untouched, so this helper
    only runs for ``verified completion`` and ``is_verified_completion`` is
    always true at the call site. It is kept explicit so the ordering proof
    reads the contract order.
    """
    _ = is_verified_completion
    unmet: list[str] = []
    if not fingerprint_match:
        unmet.append(DRIFT_REASON)
    if not review_gate_met:
        unmet.append(REVIEW_MISSING_REASON)
    if not final_checks_green:
        unmet.append(FAILING_VALIDATION_REASON)
    if dirty_files:
        unmet.append(DIRTY_TREE_REASON)
    base_moved = base_head is not None and base_head != base_revision
    if not unmet:
        return PublicationVerdict(
            ready=True,
            reason=VERIFIED_COMPLETION_REASON,
            unmet=(),
            base_moved=base_moved,
            base_revision=base_revision,
            base_head=base_head,
        )
    return PublicationVerdict(
        ready=False,
        reason=unmet[0],
        unmet=tuple(unmet),
        base_moved=base_moved,
        base_revision=base_revision,
        base_head=base_head,
    )


def attempt_marker(attempt_id: str, *, kind: str) -> str:
    """The machine-readable Attempt Marker line (§15)."""
    return f"<!-- coding-agent attempt={attempt_id} kind={kind} -->"


def _truncate_with_pointer(text: str, *, limit: int, pointer: str) -> str:
    """Truncate an oversized pull-request part with an explicit marker (§15).

    Never silently cut: the marker names the pointer (a log or artifact
    reference) where the full text lives (L3-REV-10).
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[...truncated: see {pointer}]"


def render_pull_request_body(
    *,
    attempt_id: str,
    issue_number: int,
    issue_title: str,
    workflow_id: str,
    configuration_digest: str,
    base_revision: str,
    base_head: str | None,
    base_moved: bool,
    acceptance_criteria: list[dict[str, str]],
    claimed_tests: dict[str, list[str]],
    criterion_results: dict[str, str],
    ever_failed: dict[str, bool],
    validation_commands: list[dict[str, object]],
    review_status: str,
    review_invocations: int,
    review_reports: list[dict[str, object]],
    seam_set: list[str],
    working_area: str,
    profile_changed: bool | None,
) -> str:
    """Render the pull-request body once, before any delivery write (§15).

    The body carries, in order: ``Closes #N`` and the Attempt Marker
    (``kind=delivery``); the Attempt id, Workflow id and digest; the Base
    Revision sha and a base-movement note when the base moved; per-criterion
    evidence with red history; Validation Evidence; the review status,
    invocation count and both Review Reports of the latest completed review in
    full inside ``<details>`` blocks; the Seam Set; the declared working area
    and a note when the issue changed the profile. Oversized review reports
    are truncated with an explicit marker and pointer, never silently.
    """
    lines: list[str] = [f"Closes #{issue_number}", attempt_marker(attempt_id, kind=DELIVERY_COMMENT_KIND), ""]
    lines.append(f"Attempt: {attempt_id}")
    lines.append(f"Workflow: {workflow_id} (digest `{configuration_digest}`)")
    lines.append(f"Base Revision: `{base_revision}`")
    if base_moved:
        lines.append(
            f"Base-branch movement: the base has moved since Claim "
            f"(Base Revision `{base_revision}`, base head `{base_head}`); "
            f"this work was implemented and verified against `{base_revision}`. "
            f"No rebase or merge was performed."
        )
    else:
        lines.append("Base-branch movement: none.")
    lines.append("")
    lines.append("## Acceptance Criteria")
    if not acceptance_criteria:
        lines.append("(none recorded)")
    for criterion in acceptance_criteria:
        criterion_id = str(criterion.get("id", ""))
        text = str(criterion.get("text", ""))
        claimed = claimed_tests.get(criterion_id, [])
        result = criterion_results.get(criterion_id, "unknown")
        red = ever_failed.get(criterion_id, False)
        red_note = " (observed failing before this Attempt)" if red else ""
        lines.append(f"- {criterion_id}: {text}")
        lines.append(f"  claimed tests: {claimed!r}; latest evidence: {result}{red_note}")
    lines.append("")
    lines.append("## Validation Evidence")
    if not validation_commands:
        lines.append("(none recorded)")
    for entry in validation_commands:
        lines.append(
            f"- {entry.get('name')}: exit={entry.get('exit_code')} "
            f"executed={entry.get('executed')} failures={entry.get('failure_ids')}"
        )
    lines.append("")
    lines.append(f"Review: {review_status} after {review_invocations} invocation(s)")
    for report in review_reports:
        ordinal = report.get("ordinal")
        standards = report.get("standards_report")
        spec = report.get("spec_report")
        lines.append(f"<details><summary>Review {ordinal}</summary>")
        lines.append("")
        lines.append(
            _truncate_with_pointer(
                f"Standards: {standards}",
                limit=8000,
                pointer=f"review-{ordinal}-standards",
            )
        )
        lines.append("")
        lines.append(
            _truncate_with_pointer(
                f"Spec: {spec}",
                limit=8000,
                pointer=f"review-{ordinal}-spec",
            )
        )
        lines.append("</details>")
    lines.append("")
    lines.append(f"Seam Set: {seam_set!r}")
    lines.append(f"Working area: `{working_area}`")
    if profile_changed:
        lines.append(
            "Note: the candidate edited `docs/agents/project-profile.yml`; "
            "validation still ran the Validation Contract pinned at the Base Revision."
        )
    lines.append("")
    lines.append(f"Title: {issue_title}")
    return "\n".join(lines)


def render_terminal_comment(
    *,
    attempt_id: str,
    outcome: str,
    reason: str,
    pull_request_url: str | None,
    branch_name: str | None,
    workflow_id: str,
    configuration_digest: str,
    unmet_conditions: tuple[str, ...] = (),
    next_action: str | None = None,
    detail: str | None = None,
) -> str:
    """Render the one terminal comment carrying the Attempt Marker (§15).

    It states the Terminal Outcome and its reason, a link to the pull request
    or to the pushed branch, the Attempt/Workflow/digest pinning, one sentence
    on what a human must do next for a failure, and any reason-specific
    detail: the unmet Publication Gate conditions, the failing commands, or a
    foreign push to the branch.
    """
    lines: list[str] = [attempt_marker(attempt_id, kind=TERMINAL_COMMENT_KIND), ""]
    lines.append(f"Attempt {attempt_id}: {outcome} ({reason})")
    if pull_request_url is not None:
        lines.append(f"Pull request: {pull_request_url}")
    elif branch_name is not None:
        lines.append(f"Branch (no pull request): `{branch_name}`")
    else:
        lines.append("No pull request and no pushed branch.")
    lines.append(f"Workflow: {workflow_id} (digest `{configuration_digest}`)")
    if unmet_conditions:
        lines.append(f"Unmet Publication Gate conditions: {', '.join(unmet_conditions)}")
    if detail:
        lines.append(f"Detail: {detail}")
    if outcome != "delivered":
        lines.append(f"Next: {next_action or 'a human must intervene; see the reason above.'}")
    return "\n".join(lines)


def terminal_next_action(reason: str) -> str:
    """One sentence on what a human must do next for each terminal reason (§15)."""
    actions: dict[str, str] = {
        DRIFT_REASON: "re-apply the Selection Label after reviewing the Target Issue edit; a Continuation starts from the pushed work.",
        REVIEW_MISSING_REASON: "re-apply the Selection Label; the next Attempt must complete a review before delivery.",
        FAILING_VALIDATION_REASON: "inspect the Validation Evidence in the Run Ledger and fix the failing check or test.",
        DIRTY_TREE_REASON: "inspect the leftover files named in the Validation Evidence and re-apply the Selection Label.",
        "unverified": "inspect the force-accepted claim and its missing evidence, then re-apply the Selection Label.",
        "already-satisfied": "no change was needed; re-apply the Selection Label only for new work.",
    }
    return actions.get(reason, "inspect the Run Ledger for this Attempt and re-apply the Selection Label for a Continuation.")
