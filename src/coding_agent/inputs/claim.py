"""The Claim sequence: intent, assignment, Claim Comment, read-back, pin (§6.2).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §6.1
(exclusivity is global per input; the visible ownership record is the
Agent Identity's assignment plus a Claim Comment bearing an Attempt Marker
with ``kind=claim``, the Workflow id and the Attempt id; among Claim
Comments in force the lowest comment id wins; a Claim Comment stops being
in force when its Attempt's terminal comment is published, or when recovery
deletes it), §6.2 (the Claim sequence: record the Claim intent in the
transaction of the Intake check; assign the Agent Identity; post the Claim
Comment; read the input back; pin from the read-back and record
``claim-confirmed``; a Worker whose read-back fails or loses deletes its
own Claim Comment, removes the assignment only if no other Claim Comment
is in force, and records the Claim ``released`` — no terminal comment, no
label change, and a ``released`` Claim is not a Terminal Outcome), §6.3
(Attempt identity and ``n``), §6.4 (a Claim orphaned by a permanently lost
volume is released by a human) and §16.2 (an unconfirmed Claim found at
restart is released the same way: own Claim Comment deleted by key,
assignment removed if no other Claim is in force, ``released`` recorded).

Row ids: INP-1, INP-2, INP-5, INP-6, INP-13, REC-10, REC-22.

The Agent Identity's assignment is always ours (§6.1: every assignment the
Worker makes has a Claim intent in the ledger), so recovery can always
release or finish it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coding_agent.inputs.comments import PinnedComment
    from coding_agent.inputs.pickup import PickupConfig

from coding_agent.inputs.authorisation import (
    IdentityMode,
    detect_identity_mode,
    label_authorised,
)
from coding_agent.inputs.numbering import (
    attempt_id,
    next_n,
    parse_attempt_marker,
)
from coding_agent.inputs.pinning import (
    PinnedInput,
    attempt_pin_from_pin,
    pin_from_readback,
)
from coding_agent.inputs.port import (
    CodeHostPort,
    InputComment,
    InputPort,
    InputType,
    TrackedInput,
)
from coding_agent.inputs.selection import (
    exclusion_reason,
    input_author_matches,
    open_agent_pr_for_issue,
    sort_inputs,
)
from coding_agent.ledger.store import (
    STEP_EFFECT_STATUS_RECEIPT,
    RunLedger,
    step_effect_key,
    step_effect_marker,
)
from coding_agent.operator.commands import (
    ACK_NOT_FULFILLED,
    NEXT_COMPLETED_DETAIL,
    CommandRecord,
)
from coding_agent.runtime.round_handoff import RoundHandoffOffer

CLAIM_STEP = ""
"""The reserved step position of Claim writes in Effect Intent keys (§14.1)."""

CLAIM_EXECUTION = 0
"""The reserved Step Execution number of Claim writes (§14.1)."""

ASSIGN_ORDINAL = 0
"""The write ordinal of the Claim's assignment write (§14.1)."""

CLAIM_COMMENT_ORDINAL = 1
"""The write ordinal of the Claim Comment write (§14.1)."""

CLAIM_KIND = "claim"
"""The Attempt Marker kind of the Claim Comment (§6.1)."""

TERMINAL_MARKER_KIND = "terminal"
"""The Attempt Marker kind of the trusted terminal comment (§6.1, §15.3)."""

TERMINAL_MARKER_STEP = ""
"""The reserved step position of the terminal marker checked for trust (§14.1)."""

TERMINAL_MARKER_EXECUTION = 0
"""The reserved Step Execution number of the terminal marker (§14.1)."""

TERMINAL_MARKER_ORDINAL = 0
"""The write ordinal of the terminal comment marker (§14.1)."""


def terminal_marker_for(raw_attempt_id: str) -> str:
    """The canonical terminal marker a trusted terminal comment carries (§15.3).

    Rendered by :func:`step_effect_marker` with the finalisation position
    (``step=- n=0 ordinal=0``) and ``kind=terminal``. Only a comment carrying
    exactly this marker for the Claim's exact Attempt id counts as trusted
    terminal evidence; a malformed marker never matches.
    """
    return step_effect_marker(
        raw_attempt_id,
        TERMINAL_MARKER_STEP,
        TERMINAL_MARKER_EXECUTION,
        TERMINAL_MARKER_ORDINAL,
        kind=TERMINAL_MARKER_KIND,
    )


def trusted_terminal_attempt_ids(
    comments: Sequence[InputComment], agent_id: int | None
) -> set[str]:
    """The Attempt ids a trusted terminal comment ends (§6.1, §14).

    A terminal comment is trusted only where it carries the canonical
    terminal marker for its exact Attempt id and is authored by the
    configured Agent Identity's numeric id. A forged author, a malformed
    marker or a mismatched Attempt never enters the set. ``None`` where the
    numeric id is unknown trusts nothing.
    """
    if agent_id is None:
        return set()
    trusted: set[str] = set()
    for comment in comments:
        if f"kind={TERMINAL_MARKER_KIND}" not in comment.body:
            continue
        if comment.author_id != agent_id:
            continue
        parsed = parse_attempt_marker(comment.body)
        if parsed is None:
            continue
        raw_attempt_id = str(parsed)
        if terminal_marker_for(raw_attempt_id) not in comment.body:
            continue
        trusted.add(raw_attempt_id)
    return trusted


def has_trusted_terminal_comment(
    comments: Sequence[InputComment], raw_attempt_id: str, agent_id: int | None
) -> bool:
    """Whether a trusted terminal comment ends ``raw_attempt_id`` (§6.1).

    Matches the published terminal comment to the Claim's exact Attempt id
    and requires the configured Agent Identity's numeric author id plus the
    canonical terminal marker. A forged author, malformed marker or
    mismatched Attempt reads as absent.
    """
    return raw_attempt_id in trusted_terminal_attempt_ids(comments, agent_id)


@dataclass(frozen=True)
class ClaimConfig:
    """What one Selection-and-Claim sweep runs under (§5, §6).

    ``selection_label`` is the Workflow's Selection Label;
    ``require_all``, ``require_any`` and ``exclude`` are its filters,
    re-evaluated at the Claim read-back and never at a later Gate;
    ``drafts`` (``include``/``exclude``) applies to pull-request inputs
    only; ``skip_open_agent_pr`` applies to issue inputs only;
    ``base_branch`` is the configured base branch the Base Revision is
    resolved from; ``agent_login`` and ``agent_id`` are the Agent Identity;
    ``effective_configuration`` is the record the Claim pins on the
    Attempt (contract §2.10, CFG-41). ``prompt_texts``,
    ``activation_deliveries`` and ``activation_components`` carry the exact
    startup bytes the Attempt Archive digest-addresses before the ledger
    transaction the Claim depends on — every prompt the Workflow needs and
    every available activation's components and delivery bytes, including
    on-demand-only members.
    """

    workflow_id: str
    input_type: InputType
    selection_label: str
    require_all: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    drafts: str = "exclude"
    skip_open_agent_pr: bool = False
    base_branch: str = "main"
    agent_login: str = ""
    agent_id: int | None = None
    effective_configuration: Mapping[str, object] = field(default_factory=dict)
    prompt_texts: Mapping[str, str] = field(default_factory=dict)
    activation_deliveries: Mapping[str, str] = field(default_factory=dict)
    activation_components: Mapping[str, Mapping[str, bytes]] = field(
        default_factory=dict
    )
    profile_text: str | None = None
    comments_declared: bool = False
    """Whether an agent step of the Workflow declares ``input.comments`` (§7.4).

    Where true, an issue Claim reads the input's comments back, pins the
    Trusted Comments (ADR 0035) and carries them on the confirmed Claim;
    where false no comment is read and nothing is archived or recorded.
    """


@dataclass(frozen=True)
class ConfirmedClaim:
    """The Attempt one sweep claimed and confirmed (contract §6.2)."""

    attempt_id: str
    workflow_id: str
    input_number: int
    n: int
    pin: PinnedInput
    pickup_offer: RoundHandoffOffer | None = None
    """The Round Handoff offer this pickup Claim consumed, if any (§8).

    ``None`` for ordinary selection Claims. A pickup Claim carries the
    live offer so the Continuation opens at the exact published work
    commit with the pinned note as input data.
    """
    pinned_comments: tuple[PinnedComment, ...] = ()
    """The Trusted Comments pinned at Claim, oldest first (§7.4, ADR 0035).

    Empty where the Workflow declares no ``input.comments`` step: the
    input is always present, and an empty set is present, not absent.
    """


@dataclass(frozen=True)
class ReleasedClaim:
    """A Claim the read-back refused: released, never an Attempt (§6.2)."""

    input_number: int
    reason: str


def claim_comment_body(attempt: str, intent_key: str) -> str:
    """Render the Claim Comment body: the Attempt id and its marker (§6.1)."""
    marker = step_effect_marker(
        attempt, CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL, kind=CLAIM_KIND
    )
    return (
        f"coding-agent claims {attempt} for the {CLAIM_KIND}.\n\n{marker}\n"
        f"<!-- effect-intent {intent_key} -->\n"
    )


def attempt_finished_in_ledger(ledger: RunLedger, raw_attempt_id: str) -> bool:
    """Whether an Attempt id parsed from a marker is over in the ledger (§6.1).

    A Claim Comment stops being in force when its Attempt ends: the state
    is ``finished`` or ``released``, or an outcome is recorded. Unknown ids
    — a ledger loss, another volume — stay in force: a human releases them
    (§6.4, §16.5).
    """
    try:
        record = ledger.get_attempt(raw_attempt_id)
    except AssertionError:
        return False
    if record.state in ("finished", "released"):
        return True
    return ledger.get_outcome(raw_attempt_id) is not None


def in_force_claim_comments(
    comments: Sequence[InputComment],
    ledger: RunLedger,
    agent_id: int | None = None,
) -> list[InputComment]:
    """The Claim Comments still in force: kind=claim, Attempt not over (§6.1).

    An Attempt is over where the local Run Ledger says so (``finished`` /
    ``released`` or a recorded outcome) or — where the Worker holds no local
    record of the old Claim, e.g. an independent state volume — where a
    trusted terminal comment on the Workflow Input ends its exact Attempt id
    (the Agent Identity's numeric author id plus the canonical terminal
    marker). Unknown ids without trusted terminal evidence stay in force.
    """
    trusted = trusted_terminal_attempt_ids(comments, agent_id)
    in_force: list[InputComment] = []
    for comment in comments:
        if f"kind={CLAIM_KIND}" not in comment.body:
            continue
        parsed = parse_attempt_marker(comment.body)
        if parsed is None:
            in_force.append(comment)
            continue
        raw_attempt_id = str(parsed)
        if attempt_finished_in_ledger(ledger, raw_attempt_id):
            continue
        if raw_attempt_id in trusted:
            continue
        in_force.append(comment)
    return in_force


def winning_claim(
    comments: Sequence[InputComment],
    ledger: RunLedger,
    agent_id: int | None = None,
) -> InputComment | None:
    """The winning Claim Comment: the lowest comment id in force (§6.1)."""
    in_force = in_force_claim_comments(comments, ledger, agent_id)
    if not in_force:
        return None
    return min(in_force, key=lambda comment: comment.comment_id)


def _guard_reason(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    config: ClaimConfig,
    candidate: TrackedInput,
    *,
    identity_mode: IdentityMode,
) -> str | None:
    """The reason ``candidate`` is excluded, or ``None`` where selectable.

    Every guard input is read fresh — the ``labeled`` event and its §5.1
    actor test, the native blocker, another Workflow's Claim in force, the
    Agent Identity's open pull request for this issue — so the read-back
    re-evaluates the same predicate the intake did. The ``skip-open-agent-pr``
    scan is explicit orchestration across both ports: ``code_host``
    lists the open pull requests while ``inputs`` checks the per-input
    blocker.
    """
    event = inputs.read_labeled_event(
        candidate.number, candidate.input_type, config.selection_label
    )
    authorised = label_authorised(
        event,
        agent_login=config.agent_login,
        agent_id=config.agent_id,
        identity_mode=identity_mode,
    )
    claim_agent_id = (
        config.agent_id if config.agent_id is not None else inputs.agent_id
    )
    in_force = in_force_claim_comments(
        inputs.list_comments(candidate.number, candidate.input_type),
        ledger,
        claim_agent_id,
    )
    agent_pr_open = False
    if config.input_type == "issue" and config.skip_open_agent_pr:
        agent_pr_open = (
            open_agent_pr_for_issue(
                code_host.list_open_pull_requests(),
                candidate.number,
                agent_login=config.agent_login,
                agent_id=config.agent_id,
            )
            is not None
        )
    return exclusion_reason(
        candidate,
        selection_label=config.selection_label,
        label_authorised=authorised,
        blocker_open=inputs.has_open_blocker(candidate.number, candidate.input_type),
        claim_in_force=bool(in_force),
        target_repo=inputs.target_repo,
        agent_pr_open=agent_pr_open,
        require_all=config.require_all,
        require_any=config.require_any,
        exclude=config.exclude,
        drafts=config.drafts,
    )


def check_eligibility(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    config: ClaimConfig,
    number: int,
    *,
    identity_mode: IdentityMode,
) -> str | None:
    """The guard reason for a ``next issue N`` priority input (§5.4).

    Applies the same guards the sweep uses, read fresh through the
    caller's own tracker instance. Returns the guard reason where N is
    definitely ineligible, ``None`` where it may be claimed.
    """
    try:
        candidate = inputs.fetch_input(number, config.input_type)
    except Exception:
        return "unreadable"
    return _guard_reason(
        inputs, code_host, ledger, config, candidate, identity_mode=identity_mode
    )


def _release_tracker_state(
    inputs: InputPort,
    ledger: RunLedger,
    number: int,
    input_type: InputType,
    own_comment_id: int | None,
) -> None:
    """Undo the Claim's tracker writes: delete, unassign where free (§6.2).

    Deletes the Worker's own Claim Comment and removes the assignment only
    if no other Claim Comment is in force. The ledger write — a plain
    release or a priority-resolving one — is the caller's.
    """
    if own_comment_id is not None:
        try:
            inputs.delete_comment(own_comment_id)
        except Exception:
            pass
    remaining = [
        comment
        for comment in in_force_claim_comments(
            inputs.list_comments(number, input_type), ledger, inputs.agent_id
        )
        if comment.comment_id != own_comment_id
    ]
    if not remaining:
        try:
            inputs.unassign_input(number, input_type)
        except Exception:
            pass


def release_claim(
    inputs: InputPort,
    ledger: RunLedger,
    attempt: str,
    number: int,
    input_type: InputType,
    own_comment_id: int | None,
) -> None:
    """Release an unconfirmed Claim: delete, unassign, record (REC-10) (§16.2).

    Deletes the Worker's own Claim Comment, removes the assignment only if
    no other Claim Comment is in force, and marks the Attempt ``released``
    — the same path the Claim read-back failure and restart recovery share.
    There is no Terminal Outcome and no terminal comment.
    """
    _release_tracker_state(inputs, ledger, number, input_type, own_comment_id)
    ledger.set_attempt_state(attempt, "released")


def release_priority_claim(
    inputs: InputPort,
    ledger: RunLedger,
    attempt: str,
    number: int,
    input_type: InputType,
    own_comment_id: int | None,
    *,
    request_id: str,
    reason: str,
) -> None:
    """Release a priority Claim and resolve it ``not_fulfilled`` (§8.3).

    The same tracker cleanup as :func:`release_claim`, but the ledger
    write also resolves the ``next issue N`` priority ``not_fulfilled``
    with the reason, in the same write transaction as the release — so
    the Claim boundary settles the command it tried.
    """
    _release_tracker_state(inputs, ledger, number, input_type, own_comment_id)
    ledger.release_priority_claim(
        attempt,
        request_id,
        detail=f"priority for #{number} not fulfilled: {reason}",
    )


def _settle_failed_claim(
    inputs: InputPort,
    ledger: RunLedger,
    candidate: TrackedInput,
    attempt: str,
    own_comment_id: int | None,
    *,
    reason: str,
    priority_request_id: str | None,
) -> ReleasedClaim:
    """Release a refused Claim, settling a tried priority where one rides (§6.2, §8.3).

    The tracker cleanup is the same either way; the ledger write resolves
    the ``next issue N`` priority ``not_fulfilled`` with the reason where
    the sweep tried one, and records a plain release otherwise.
    """
    if priority_request_id is not None:
        release_priority_claim(
            inputs,
            ledger,
            attempt,
            candidate.number,
            candidate.input_type,
            own_comment_id,
            request_id=priority_request_id,
            reason=reason,
        )
    else:
        release_claim(
            inputs,
            ledger,
            attempt,
            candidate.number,
            candidate.input_type,
            own_comment_id,
        )
    return ReleasedClaim(input_number=candidate.number, reason=reason)


def _record_claim_receipts(
    ledger: RunLedger,
    attempt: str,
    *,
    comment_id: int | None,
    agent_login: str,
) -> None:
    """Record the receipts of the Claim's two writes (§14.1)."""
    ledger.record_step_effect_receipt(
        attempt, CLAIM_STEP, CLAIM_EXECUTION, ASSIGN_ORDINAL, remote_id=agent_login
    )
    if comment_id is not None:
        ledger.record_step_effect_receipt(
            attempt,
            CLAIM_STEP,
            CLAIM_EXECUTION,
            CLAIM_COMMENT_ORDINAL,
            remote_id=str(comment_id),
        )


class _ClaimPinFailure(Exception):
    """A lane pin-finish refusal: settle the Claim ``released`` (§6.2).

    Raised by a lane's ``finish_pin`` where the pin cannot be built (e.g.
    a pull-request read-back with no head SHA); the core settles it like
    any other failed Claim instead of propagating.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _write_claim_writes(
    inputs: InputPort,
    ledger: RunLedger,
    *,
    attempt: str,
    candidate: TrackedInput,
    agent_login: str,
) -> InputComment:
    """Perform the Claim's two writes, each one Effect Intent settled once (§6.2, ADR 0028).

    Records the assignment intent, assigns, then records the Claim Comment
    intent, posts the Claim Comment bearing the Attempt Marker with
    ``CLAIM_KIND``, and records both receipts through the shared helper.
    The single intent → write → receipt path both lanes share.
    """
    assign_key = step_effect_key(attempt, CLAIM_STEP, CLAIM_EXECUTION, ASSIGN_ORDINAL)
    ledger.record_step_effect_intent(
        attempt,
        CLAIM_STEP,
        CLAIM_EXECUTION,
        ASSIGN_ORDINAL,
        rendered_payload=f"assign {agent_login} on {candidate.input_type} #{candidate.number}",
        expected_target={
            "input": f"{candidate.input_type}#{candidate.number}",
            "assignee": agent_login,
        },
    )
    inputs.assign_input(candidate.number, candidate.input_type)
    ledger.set_assigned_by_us(attempt, True)
    comment_key = step_effect_key(
        attempt, CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL
    )
    ledger.record_step_effect_intent(
        attempt,
        CLAIM_STEP,
        CLAIM_EXECUTION,
        CLAIM_COMMENT_ORDINAL,
        rendered_payload=claim_comment_body(attempt, comment_key),
        expected_target={
            "input": f"{candidate.input_type}#{candidate.number}",
            "kind": CLAIM_KIND,
        },
    )
    posted = inputs.post_claim_comment(
        candidate.number,
        candidate.input_type,
        claim_comment_body(attempt, comment_key),
    )
    _record_claim_receipts(
        ledger, attempt, comment_id=posted.comment_id, agent_login=agent_login
    )
    return posted


def _execute_claim(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    *,
    candidate: TrackedInput,
    content: ClaimConfig,
    workflow_id: str,
    agent_login: str,
    agent_id: int | None,
    effective_configuration: Mapping[str, object],
    base_branch: str,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    priority_request_id: str | None = None,
    revalidate: Callable[[str, int], str | None],
    finish_pin: Callable[[TrackedInput, str], PinnedInput],
    pickup_offer: RoundHandoffOffer | None = None,
) -> ConfirmedClaim | ReleasedClaim | None:
    """Run the ordered Claim sequence both lanes share (§6.2).

    Numbering (ledger count plus live terminal markers) → the Attempt
    Archive holds the startup blobs → the Attempt row opens in the
    transaction of the Intake check (ADR 0026: the check stays atomic with
    the open, inside this module) → the two writes via the shared helper →
    the lane-injected read-back revalidation → the read-back plus base-tip
    fetch → the lane-injected pin finish → comment pinning where declared →
    the single pin recording → priority/settle.

    Only the lane callbacks vary: ``revalidate`` re-checks the lane's
    authorisation at the read-back (``(attempt, own_comment_id)`` to the
    failure reason, ``None`` where the Claim holds), and ``finish_pin``
    builds the lane's pin from ``(readback, base_tip)`` (raising
    ``_ClaimPinFailure`` where the pin cannot be built). Everything else —
    numbering, the Intake-atomic open, both writes, both receipts, comment
    pinning, pin recording and settling — is the one shared sequence.
    """
    ledger_n = ledger.next_workflow_attempt_number(workflow_id, candidate.number)
    claim_agent_id = agent_id if agent_id is not None else inputs.agent_id
    live_markers: list[str] = list(known_markers)
    try:
        live_comments = inputs.list_comments(
            candidate.number, candidate.input_type
        )
        for raw_finished in trusted_terminal_attempt_ids(
            live_comments, claim_agent_id
        ):
            live_markers.append(terminal_marker_for(raw_finished))
    except Exception:
        pass
    n = next_n(
        workflow_id,
        candidate.number,
        ledger_n=ledger_n - 1,
        branches=known_branches,
        markers=live_markers,
    )
    attempt = attempt_id(workflow_id, candidate.number, n)
    store_claim_content(ledger, attempt, content)
    opened = ledger.open_attempt_if_intake_open(
        attempt,
        effective_configuration=effective_configuration,
        agent_identity_id=agent_id,
    )
    if opened is None:
        if priority_request_id is not None:
            return None
        return ReleasedClaim(input_number=candidate.number, reason="intake-closed")
    posted = _write_claim_writes(
        inputs, ledger, attempt=attempt, candidate=candidate, agent_login=agent_login
    )

    failure = revalidate(attempt, posted.comment_id)
    if failure is not None:
        return _settle_failed_claim(
            inputs,
            ledger,
            candidate,
            attempt,
            posted.comment_id,
            reason=failure,
            priority_request_id=priority_request_id,
        )

    readback = inputs.fetch_input(candidate.number, candidate.input_type)
    base_tip = code_host.fetch_base_tip(base_branch)
    try:
        pin = finish_pin(readback, base_tip)
    except _ClaimPinFailure as pin_failure:
        return _settle_failed_claim(
            inputs,
            ledger,
            candidate,
            attempt,
            posted.comment_id,
            reason=pin_failure.reason,
            priority_request_id=priority_request_id,
        )
    pinned_comments: tuple[PinnedComment, ...] = ()
    if content.comments_declared and candidate.input_type == "issue":
        try:
            pinned_comments = pin_claim_comments(
                inputs,
                ledger,
                attempt=attempt,
                number=candidate.number,
                input_type=candidate.input_type,
                issue_author_id=readback.author_id,
                agent_login=agent_login,
                agent_id=claim_agent_id,
            )
        except Exception:
            return _settle_failed_claim(
                inputs,
                ledger,
                candidate,
                attempt,
                posted.comment_id,
                reason="comments-unreadable",
                priority_request_id=priority_request_id,
            )
    ledger.record_attempt_pin(attempt_pin_from_pin(pin, attempt))
    ledger.set_attempt_claim_pins(
        attempt, fingerprint=pin.fingerprint, base_revision=pin.base_revision
    )
    if priority_request_id is not None:
        ledger.confirm_priority_claim(
            attempt, priority_request_id, detail=NEXT_COMPLETED_DETAIL
        )
    else:
        ledger.set_attempt_state(attempt, "claim-confirmed")
    return ConfirmedClaim(
        attempt_id=attempt,
        workflow_id=workflow_id,
        input_number=candidate.number,
        n=n,
        pin=pin,
        pickup_offer=pickup_offer,
        pinned_comments=pinned_comments,
    )


def store_claim_content(ledger: RunLedger, attempt: str, config: ClaimConfig) -> list[str]:
    """Archive the Claim's content blobs before its ledger transaction.

    Digest-addressed blobs for every prompt the Workflow needs and every
    available activation's components and delivery bytes — including
    on-demand-only members — are written and flushed to the Attempt Archive
    before the ledger transaction the Claim depends on. ``not-delivered``
    bodies have no blob; their digests stay pinned in the content-reference
    index only. Returns the archived digests in order. A ledger without a
    resolvable archive directory (unit-test ledgers with no instance row)
    or a Claim carrying no startup bytes archives nothing.
    """
    from coding_agent.operator import archive as attempt_archives
    from coding_agent.workflow.effective import collect_claim_blobs

    if (
        not config.prompt_texts
        and not config.activation_deliveries
        and not config.activation_components
        and config.profile_text is None
    ):
        return []
    try:
        archive_dir = ledger.archive_dir_for(attempt)
    except Exception:
        return []
    if archive_dir is None:
        return []
    blobs = collect_claim_blobs(
        prompt_texts=config.prompt_texts,
        activation_deliveries=config.activation_deliveries,
        activation_components=config.activation_components,
        profile_text=config.profile_text,
    )
    if not blobs:
        return []
    attempt_archives.store_content_blobs(archive_dir, blobs)
    return sorted(blobs.keys())


def pin_claim_comments(
    inputs: InputPort,
    ledger: RunLedger,
    *,
    attempt: str,
    number: int,
    input_type: InputType,
    issue_author_id: int | None,
    agent_login: str,
    agent_id: int | None,
) -> tuple[PinnedComment, ...]:
    """Read back, select, archive and record the Trusted Comments (§7.4).

    Runs only for an issue Claim whose Workflow declares
    ``input.comments``: the live comments are read back, the Trusted
    Comments selected by numeric identity (ADR 0035), their full content
    flushed to the Attempt Archive digest-addressed, and the per-comment
    records plus the set digest recorded in the Run Ledger — before the
    ledger transaction the Claim depends on. A declared but empty set
    still records its set row, so finalisation tells "nothing pinned"
    from "nothing declared". Raises where the read-back fails, so the
    caller releases the Claim like any other read-back failure.
    """
    from coding_agent.inputs.comments import (
        comments_archive_blob,
        pin_trusted_comments,
        select_trusted_comments,
    )
    from coding_agent.operator import archive as attempt_archives

    comments = inputs.list_comments(number, input_type)
    owner_id = inputs.fetch_repository_owner_id()
    selected = select_trusted_comments(
        comments,
        issue_author_id=issue_author_id,
        owner_id=owner_id,
        agent_id=agent_id,
        agent_login=agent_login,
    )
    pinned = pin_trusted_comments(selected)
    try:
        archive_dir = ledger.archive_dir_for(attempt)
    except Exception:
        archive_dir = None
    if archive_dir is not None:
        attempt_archives.store_content_blobs(
            archive_dir, comments_archive_blob(pinned)
        )
    ledger.record_pinned_comments(
        attempt,
        pinned,
        issue_author_id=issue_author_id,
        owner_id=owner_id,
        agent_id=agent_id,
    )
    return tuple(pinned)


def attempt_claim(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    config: ClaimConfig,
    candidate: TrackedInput,
    *,
    identity_mode: IdentityMode,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    merge_base_resolver: Callable[[str, str], str] | None = None,
    priority_request_id: str | None = None,
) -> ConfirmedClaim | ReleasedClaim | None:
    """Claim one candidate: intent, assign, comment, read back, pin (§6.2).

    The Attempt Archive holds the digest-addressed content blobs before the
    ledger transaction the Claim depends on, so a crash before or after
    archive durability never leaves a confirmed Claim referencing missing or
    unflushed content; orphaned unreferenced files are permitted. The Attempt
    row opens in the transaction of the Intake check, before the assignment
    is made, so recovery can always release it. A failed read-back — or a
    lost race, where another Claim Comment wins — deletes the Worker's own
    Claim Comment, removes the assignment only when no other Claim is in
    force, and records the Claim ``released``.

    Where ``priority_request_id`` carries a ``next issue N`` command, the
    Claim settles it at the boundary: a confirmed Claim completes the
    priority, a failed read-back releases it ``not_fulfilled``, both in
    the same write transaction as the lifecycle write. Intake closed
    (``None``) never resolves the priority — it stays ``accepted`` and is
    never used to reopen Intake.

    A thin Selection-lane wrapper around :func:`_execute_claim`: the lane
    supplies the Selection Label authorisation evidence, the Selection
    read-back revalidation and the Selection pin finish (merge-base
    resolution for pull-request inputs); the core owns the ordered
    ledger-plus-tracker sequence.
    """

    def revalidate(attempt: str, own_comment_id: int) -> str | None:
        return _readback_failure(
            inputs,
            code_host,
            ledger,
            config,
            candidate,
            attempt=attempt,
            own_comment_id=own_comment_id,
            identity_mode=identity_mode,
        )

    def finish_pin(readback: TrackedInput, base_tip: str) -> PinnedInput:
        merge_base: str | None = None
        if candidate.input_type == "pull-request":
            if readback.head_sha is None:
                raise _ClaimPinFailure("head-missing")
            if merge_base_resolver is None:
                raise ValueError("a pull-request Claim needs a merge-base resolver")
            merge_base = merge_base_resolver(readback.head_sha, base_tip)
        event = inputs.read_labeled_event(
            candidate.number, candidate.input_type, config.selection_label
        )
        return pin_from_readback(
            readback,
            workflow_id=config.workflow_id,
            base_tip=base_tip,
            merge_base=merge_base,
            selection_actor=event.actor_login if event is not None else None,
            selection_actor_id=event.actor_id if event is not None else None,
            selection_actor_type=event.actor_type if event is not None else None,
            selection_actor_at=event.created_at if event is not None else None,
            identity_mode=identity_mode,
        )

    return _execute_claim(
        inputs,
        code_host,
        ledger,
        candidate=candidate,
        content=config,
        workflow_id=config.workflow_id,
        agent_login=config.agent_login,
        agent_id=config.agent_id,
        effective_configuration=config.effective_configuration,
        base_branch=config.base_branch,
        known_branches=known_branches,
        known_markers=known_markers,
        priority_request_id=priority_request_id,
        revalidate=revalidate,
        finish_pin=finish_pin,
    )


def _readback_failure(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    config: ClaimConfig,
    candidate: TrackedInput,
    *,
    attempt: str,
    own_comment_id: int,
    identity_mode: IdentityMode,
) -> str | None:
    """The reason the Claim read-back fails, or ``None`` where it holds (§6.2).

    The read-back re-checks: the input is open; the Selection Label is
    present and authorised; no other assignee; the Worker's own Claim
    Comment is winning; every guard and filter of §5.
    """
    readback = inputs.fetch_input(candidate.number, candidate.input_type)
    if not readback.is_open:
        return "closed"
    if readback.input_type == "pull-request" and readback.merged:
        return "merged"
    if config.selection_label not in readback.labels:
        return "selection-label"
    event = inputs.read_labeled_event(
        candidate.number, candidate.input_type, config.selection_label
    )
    if not label_authorised(
        event,
        agent_login=config.agent_login,
        agent_id=config.agent_id,
        identity_mode=identity_mode,
    ):
        return "label-authorisation"
    others = tuple(
        login for login in readback.assignees if login != config.agent_login
    )
    if others:
        return "assigned"
    if config.agent_login not in readback.assignees:
        return "assignment-missing"
    readback_agent_id = (
        config.agent_id if config.agent_id is not None else inputs.agent_id
    )
    winner = winning_claim(
        inputs.list_comments(candidate.number, candidate.input_type),
        ledger,
        readback_agent_id,
    )
    if winner is None or winner.comment_id != own_comment_id:
        return "claim-lost"
    agent_pr_open = False
    if config.input_type == "issue" and config.skip_open_agent_pr:
        agent_pr_open = (
            open_agent_pr_for_issue(
                code_host.list_open_pull_requests(),
                candidate.number,
                agent_login=config.agent_login,
                agent_id=config.agent_id,
            )
            is not None
        )
    guard = exclusion_reason(
        replace(
            readback,
            assignees=tuple(
                login for login in readback.assignees if login != config.agent_login
            ),
        ),
        selection_label=config.selection_label,
        label_authorised=True,
        blocker_open=inputs.has_open_blocker(candidate.number, candidate.input_type),
        claim_in_force=False,
        target_repo=inputs.target_repo,
        agent_pr_open=agent_pr_open,
        require_all=config.require_all,
        require_any=config.require_any,
        exclude=config.exclude,
        drafts=config.drafts,
    )
    if guard is not None:
        return guard
    _ = attempt
    return None


def _priority_issue_and_request(
    priority: CommandRecord | int | None,
) -> tuple[int | None, str | None]:
    """Split a sweep priority into its issue and settling request id.

    A bare int only reorders (no command settles); a ``CommandRecord``
    also settles at the Claim boundary. A malformed record reads as an
    invalid issue with its request id, so the sweep resolves it.
    """
    if priority is None:
        return (None, None)
    if isinstance(priority, bool):
        return (0, None)
    if isinstance(priority, int):
        return (priority, None)
    raw_issue = priority.arguments.get("issue")
    try:
        issue = int(str(raw_issue)) if not isinstance(raw_issue, bool) else 0
    except (TypeError, ValueError):
        issue = 0
    return (issue, priority.request_id)


def sweep_and_claim(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    config: ClaimConfig,
    *,
    priority: CommandRecord | int | None = None,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    merge_base_resolver: Callable[[str, str], str] | None = None,
    pickup: PickupConfig | None = None,
) -> ConfirmedClaim | ReleasedClaim | None:
    """Run one Selection-and-Claim sweep: the first confirmed Claim wins (§5–§6).

    Candidates are tried in contract §5.4 order. A ``priority``
    (``next issue N``) is tried first under the same guards (§5.4); where
    the priority carries its command record the sweep also settles it at
    the Claim boundary — ``completed`` on a confirmed Claim,
    ``not_fulfilled`` where N is absent, guarded or released by its
    read-back — and the sweep continues in normal order. Intake closed
    opens no Claim and leaves the priority ``accepted``. ``None`` where
    no candidate is selectable. Selection writes nothing: an excluded
    candidate is left untouched.

    Where ``pickup`` carries the Workflow's Round Handoff pickup
    configuration, the Pickup Label is tried ahead of the Selection
    Label (§8): the first pickup-eligible offer is claimed and confirmed
    before any selection candidate is considered. The pickup import stays
    lazy so this module never cycles with the pickup lane.
    """
    try:
        owner_id = inputs.fetch_repository_owner_id()
        agent = inputs.fetch_agent_identity()
        identity_mode = detect_identity_mode(agent.account_id, owner_id)
    except Exception:
        return None
    if pickup is not None:
        from coding_agent.inputs.pickup import sweep_pickup

        claimed = sweep_pickup(
            inputs,
            code_host,
            ledger,
            config,
            pickup,
            identity_mode=identity_mode,
            priority=priority,
            known_branches=known_branches,
            known_markers=known_markers,
        )
        if claimed is not None:
            return claimed
    if config.input_type == "issue":
        listed = inputs.list_issue_candidates(config.selection_label)
    else:
        listed = inputs.list_pull_request_candidates(config.selection_label)
    ordered = sort_inputs(listed)
    priority_issue, priority_request_id = _priority_issue_and_request(priority)
    if priority_issue is not None:
        if priority_issue < 1:
            if priority_request_id is not None:
                ledger.resolve_next_command(
                    priority_request_id,
                    ACK_NOT_FULFILLED,
                    detail="priority not fulfilled: invalid issue",
                )
        else:
            by_number = {candidate.number: candidate for candidate in ordered}
            candidate = by_number.get(priority_issue)
            if candidate is None:
                if priority_request_id is not None:
                    ledger.resolve_next_command(
                        priority_request_id,
                        ACK_NOT_FULFILLED,
                        detail=(
                            f"priority for #{priority_issue} not fulfilled:"
                            " issue is not a candidate"
                        ),
                    )
            else:
                guard = _guard_reason(
                    inputs,
                    code_host,
                    ledger,
                    config,
                    candidate,
                    identity_mode=identity_mode,
                )
                if guard is not None:
                    if priority_request_id is not None:
                        ledger.resolve_next_command(
                            priority_request_id,
                            ACK_NOT_FULFILLED,
                            detail=(
                                f"priority for #{priority_issue} not fulfilled:"
                                f" {guard}"
                            ),
                        )
                    ordered = [
                        other
                        for other in ordered
                        if other.number != priority_issue
                    ]
                else:
                    claimed = attempt_claim(
                        inputs,
                        code_host,
                        ledger,
                        config,
                        candidate,
                        identity_mode=identity_mode,
                        known_branches=known_branches,
                        known_markers=known_markers,
                        merge_base_resolver=merge_base_resolver,
                        priority_request_id=priority_request_id,
                    )
                    if isinstance(claimed, ConfirmedClaim):
                        return claimed
                    if claimed is None:
                        # Intake closed before the Attempt row could open:
                        # the priority stays pending and never reopens
                        # Intake, so the sweep opens nothing.
                        return None
                    ordered = [
                        other
                        for other in ordered
                        if other.number != priority_issue
                    ]
    for candidate in ordered:
        if (
            _guard_reason(
                inputs, code_host, ledger, config, candidate, identity_mode=identity_mode
            )
            is not None
        ):
            continue
        claimed = attempt_claim(
            inputs,
            code_host,
            ledger,
            config,
            candidate,
            identity_mode=identity_mode,
            known_branches=known_branches,
            known_markers=known_markers,
            merge_base_resolver=merge_base_resolver,
        )
        if isinstance(claimed, ConfirmedClaim):
            return claimed
    return None


def recover_unconfirmed_claim(
    inputs: InputPort,
    ledger: RunLedger,
    attempt: str,
    number: int,
    input_type: InputType,
) -> None:
    """Settle an unconfirmed Claim found at restart (REC-10) (§16.2).

    A Claim intent or assignment whose read-back never happened, or
    happened and lost, is not an Attempt: the recovery deletes the Worker's
    own Claim Comment by key, removes the assignment if no other Claim is
    in force, and marks the record ``released`` — the shared path with the
    live read-back failure above.
    """
    own: InputComment | None = None
    key = step_effect_key(attempt, CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL)
    for comment in inputs.list_comments(number, input_type):
        if key in comment.body and input_author_matches_comment(
            inputs, comment
        ):
            own = comment
            break
    release_claim(
        inputs,
        ledger,
        attempt,
        number,
        input_type,
        own.comment_id if own is not None else None,
    )


def input_author_matches_comment(
    inputs: InputPort, comment: InputComment
) -> bool:
    """Whether a comment is authored by the Agent Identity (§14)."""
    if inputs.agent_id is not None:
        return comment.author_id == inputs.agent_id
    return comment.author_login == inputs.agent_login


def parsed_attempt_numbers(attempt_ids: Iterable[str]) -> list[tuple[str, int, int]]:
    """Parse ledger Attempt ids for numbering repair (REC-22) (§16.5)."""
    from coding_agent.attempt_id import AttemptId

    parsed: list[tuple[str, int, int]] = []
    for raw in attempt_ids:
        attempt = AttemptId.try_parse(raw)
        if attempt is not None and attempt.workflow is not None:
            parsed.append((attempt.workflow, attempt.input_number, attempt.sequence))
    return parsed
