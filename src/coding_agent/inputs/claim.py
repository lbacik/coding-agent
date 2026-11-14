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

from coding_agent.inputs.authorisation import (
    IdentityMode,
    detect_identity_mode,
    label_authorised,
)
from coding_agent.inputs.numbering import (
    attempt_id,
    next_n,
    parse_attempt_id,
    parse_attempt_marker,
)
from coding_agent.inputs.pinning import PinnedInput, pin_from_readback
from coding_agent.inputs.port import InputComment, InputType, StepTrackerPort, TrackedInput
from coding_agent.inputs.selection import (
    exclusion_reason,
    input_author_matches,
    open_agent_pr_for_issue,
    sort_inputs,
)
from coding_agent.ledger.store import (
    STEP_EFFECT_STATUS_RECEIPT,
    AttemptPin,
    RunLedger,
    step_effect_key,
    step_effect_marker,
)
from coding_agent.operator.commands import (
    ACK_NOT_FULFILLED,
    NEXT_COMPLETED_DETAIL,
    CommandRecord,
)

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
    Attempt (contract §2.10, CFG-41).
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


@dataclass(frozen=True)
class ConfirmedClaim:
    """The Attempt one sweep claimed and confirmed (contract §6.2)."""

    attempt_id: str
    workflow_id: str
    input_number: int
    n: int
    pin: PinnedInput


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
    comments: Sequence[InputComment], ledger: RunLedger
) -> list[InputComment]:
    """The Claim Comments still in force: kind=claim, Attempt not over (§6.1)."""
    in_force: list[InputComment] = []
    for comment in comments:
        if f"kind={CLAIM_KIND}" not in comment.body:
            continue
        parsed = parse_attempt_marker(comment.body)
        if parsed is None:
            in_force.append(comment)
            continue
        raw_attempt_id = f"{parsed[0]}#{parsed[1]}/{parsed[2]}"
        if not attempt_finished_in_ledger(ledger, raw_attempt_id):
            in_force.append(comment)
    return in_force


def winning_claim(
    comments: Sequence[InputComment], ledger: RunLedger
) -> InputComment | None:
    """The winning Claim Comment: the lowest comment id in force (§6.1)."""
    in_force = in_force_claim_comments(comments, ledger)
    if not in_force:
        return None
    return min(in_force, key=lambda comment: comment.comment_id)


def _guard_reason(
    tracker: StepTrackerPort,
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
    re-evaluates the same predicate the intake did.
    """
    event = tracker.read_labeled_event(
        candidate.number, candidate.input_type, config.selection_label
    )
    authorised = label_authorised(
        event,
        agent_login=config.agent_login,
        agent_id=config.agent_id,
        identity_mode=identity_mode,
    )
    in_force = in_force_claim_comments(
        tracker.list_comments(candidate.number, candidate.input_type), ledger
    )
    agent_pr_open = False
    if config.input_type == "issue" and config.skip_open_agent_pr:
        agent_pr_open = (
            open_agent_pr_for_issue(
                tracker.list_open_pull_requests(),
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
        blocker_open=tracker.has_open_blocker(candidate.number, candidate.input_type),
        claim_in_force=bool(in_force),
        target_repo=tracker.target_repo,
        agent_pr_open=agent_pr_open,
        require_all=config.require_all,
        require_any=config.require_any,
        exclude=config.exclude,
        drafts=config.drafts,
    )


def check_eligibility(
    tracker: StepTrackerPort,
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
        candidate = tracker.fetch_input(number, config.input_type)
    except Exception:
        return "unreadable"
    return _guard_reason(
        tracker, ledger, config, candidate, identity_mode=identity_mode
    )


def _release_tracker_state(
    tracker: StepTrackerPort,
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
            tracker.delete_comment(own_comment_id)
        except Exception:
            pass
    remaining = [
        comment
        for comment in in_force_claim_comments(
            tracker.list_comments(number, input_type), ledger
        )
        if comment.comment_id != own_comment_id
    ]
    if not remaining:
        try:
            tracker.unassign_input(number, input_type)
        except Exception:
            pass


def release_claim(
    tracker: StepTrackerPort,
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
    _release_tracker_state(tracker, ledger, number, input_type, own_comment_id)
    ledger.set_attempt_state(attempt, "released")


def release_priority_claim(
    tracker: StepTrackerPort,
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
    _release_tracker_state(tracker, ledger, number, input_type, own_comment_id)
    ledger.release_priority_claim(
        attempt,
        request_id,
        detail=f"priority for #{number} not fulfilled: {reason}",
    )


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


def attempt_claim(
    tracker: StepTrackerPort,
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

    The Attempt row opens in the transaction of the Intake check, before
    the assignment is made, so recovery can always release it. A failed
    read-back — or a lost race, where another Claim Comment wins — deletes
    the Worker's own Claim Comment, removes the assignment only when no
    other Claim is in force, and records the Claim ``released``.

    Where ``priority_request_id`` carries a ``next issue N`` command, the
    Claim settles it at the boundary: a confirmed Claim completes the
    priority, a failed read-back releases it ``not_fulfilled``, both in
    the same write transaction as the lifecycle write. Intake closed
    (``None``) never resolves the priority — it stays ``accepted`` and is
    never used to reopen Intake.
    """
    ledger_n = ledger.next_workflow_attempt_number(
        config.workflow_id, candidate.number
    )
    n = next_n(
        config.workflow_id,
        candidate.number,
        ledger_n=ledger_n - 1,
        branches=known_branches,
        markers=known_markers,
    )
    attempt = attempt_id(config.workflow_id, candidate.number, n)
    opened = ledger.open_attempt_if_intake_open(
        attempt,
        effective_configuration=config.effective_configuration,
        agent_identity_id=config.agent_id,
    )
    if opened is None:
        if priority_request_id is not None:
            return None
        return ReleasedClaim(input_number=candidate.number, reason="intake-closed")
    assign_key = step_effect_key(attempt, CLAIM_STEP, CLAIM_EXECUTION, ASSIGN_ORDINAL)
    ledger.record_step_effect_intent(
        attempt,
        CLAIM_STEP,
        CLAIM_EXECUTION,
        ASSIGN_ORDINAL,
        rendered_payload=f"assign {config.agent_login} on {candidate.input_type} #{candidate.number}",
        expected_target={
            "input": f"{candidate.input_type}#{candidate.number}",
            "assignee": config.agent_login,
        },
    )
    tracker.assign_input(candidate.number, candidate.input_type)
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
    posted = tracker.post_claim_comment(
        candidate.number,
        candidate.input_type,
        claim_comment_body(attempt, comment_key),
    )
    _record_claim_receipts(
        ledger, attempt, comment_id=posted.comment_id, agent_login=config.agent_login
    )

    failure = _readback_failure(
        tracker,
        ledger,
        config,
        candidate,
        attempt=attempt,
        own_comment_id=posted.comment_id,
        identity_mode=identity_mode,
    )
    if failure is not None:
        if priority_request_id is not None:
            release_priority_claim(
                tracker,
                ledger,
                attempt,
                candidate.number,
                candidate.input_type,
                posted.comment_id,
                request_id=priority_request_id,
                reason=failure,
            )
        else:
            release_claim(
                tracker, ledger, attempt, candidate.number, candidate.input_type, posted.comment_id
            )
        return ReleasedClaim(input_number=candidate.number, reason=failure)

    readback = tracker.fetch_input(candidate.number, candidate.input_type)
    base_tip = tracker.fetch_base_tip(config.base_branch)
    merge_base: str | None = None
    if candidate.input_type == "pull-request":
        if readback.head_sha is None:
            if priority_request_id is not None:
                release_priority_claim(
                    tracker,
                    ledger,
                    attempt,
                    candidate.number,
                    candidate.input_type,
                    posted.comment_id,
                    request_id=priority_request_id,
                    reason="head-missing",
                )
            else:
                release_claim(
                    tracker,
                    ledger,
                    attempt,
                    candidate.number,
                    candidate.input_type,
                    posted.comment_id,
                )
            return ReleasedClaim(input_number=candidate.number, reason="head-missing")
        if merge_base_resolver is None:
            raise ValueError("a pull-request Claim needs a merge-base resolver")
        merge_base = merge_base_resolver(readback.head_sha, base_tip)
    event = tracker.read_labeled_event(
        candidate.number, candidate.input_type, config.selection_label
    )
    pin = pin_from_readback(
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
    ledger.record_attempt_pin(
        AttemptPin(
            attempt_id=attempt,
            workflow_id=config.workflow_id,
            input_type=readback.input_type,
            input_number=readback.number,
            title=readback.title,
            body=readback.body,
            fingerprint=pin.fingerprint,
            base_revision=pin.base_revision,
            base_tip=pin.base_tip,
            input_head=pin.input_head,
            head_branch=pin.head_branch,
            base_branch=pin.base_branch,
            selection_actor=pin.selection_actor,
            selection_actor_id=pin.selection_actor_id,
            selection_actor_type=pin.selection_actor_type,
            selection_actor_at=pin.selection_actor_at,
            identity_mode=pin.identity_mode,
        )
    )
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
        workflow_id=config.workflow_id,
        input_number=candidate.number,
        n=n,
        pin=pin,
    )


def _readback_failure(
    tracker: StepTrackerPort,
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
    readback = tracker.fetch_input(candidate.number, candidate.input_type)
    if not readback.is_open:
        return "closed"
    if readback.input_type == "pull-request" and readback.merged:
        return "merged"
    if config.selection_label not in readback.labels:
        return "selection-label"
    event = tracker.read_labeled_event(
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
    winner = winning_claim(tracker.list_comments(candidate.number, candidate.input_type), ledger)
    if winner is None or winner.comment_id != own_comment_id:
        return "claim-lost"
    agent_pr_open = False
    if config.input_type == "issue" and config.skip_open_agent_pr:
        agent_pr_open = (
            open_agent_pr_for_issue(
                tracker.list_open_pull_requests(),
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
        blocker_open=tracker.has_open_blocker(candidate.number, candidate.input_type),
        claim_in_force=False,
        target_repo=tracker.target_repo,
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
    tracker: StepTrackerPort,
    ledger: RunLedger,
    config: ClaimConfig,
    *,
    priority: CommandRecord | int | None = None,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    merge_base_resolver: Callable[[str, str], str] | None = None,
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
    """
    try:
        owner_id = tracker.fetch_repository_owner_id()
        agent = tracker.fetch_agent_identity()
        identity_mode = detect_identity_mode(agent.account_id, owner_id)
    except Exception:
        return None
    if config.input_type == "issue":
        listed = tracker.list_issue_candidates(config.selection_label)
    else:
        listed = tracker.list_pull_request_candidates(config.selection_label)
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
                    tracker, ledger, config, candidate, identity_mode=identity_mode
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
                        tracker,
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
            _guard_reason(tracker, ledger, config, candidate, identity_mode=identity_mode)
            is not None
        ):
            continue
        claimed = attempt_claim(
            tracker,
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
    tracker: StepTrackerPort,
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
    for comment in tracker.list_comments(number, input_type):
        if key in comment.body and input_author_matches_comment(
            tracker, comment
        ):
            own = comment
            break
    release_claim(
        tracker,
        ledger,
        attempt,
        number,
        input_type,
        own.comment_id if own is not None else None,
    )


def input_author_matches_comment(
    tracker: StepTrackerPort, comment: InputComment
) -> bool:
    """Whether a comment is authored by the Agent Identity (§14)."""
    if tracker.agent_id is not None:
        return comment.author_id == tracker.agent_id
    return comment.author_login == tracker.agent_login


def parsed_attempt_numbers(attempt_ids: Iterable[str]) -> list[tuple[str, int, int]]:
    """Parse ledger Attempt ids for numbering repair (REC-22) (§16.5)."""
    parsed: list[tuple[str, int, int]] = []
    for raw in attempt_ids:
        match = parse_attempt_id(raw)
        if match is not None:
            parsed.append(match)
    return parsed
