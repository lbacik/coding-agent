"""Round Handoff pickup: claim a pushed offer ahead of the Selection Label (§8).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (an
opted-in issue Workflow publishes a pushed Hard Handoff on its Target
Issue; another Worker claims the offer under the same Agent Identity and
opens a Continuation within the limit fixed by the human-authorised
Attempt) and §6 (the Claim sequence: intent, assignment, Claim Comment,
read-back, pin).

The Pickup Label is tried ahead of the Selection Label: an issue carrying
the Workflow's Pickup Label whose newest trusted terminal record for this
Workflow and Target Issue is a live (non-exhausted) Round Handoff offer is
claimed as a pickup before model work. The claim pins the recorded Base
Revision (retained even when the configured base branch advances) and the
record's portable authorisation evidence, and revalidates the same source
at the Claim read-back: a replaced, stale, invalid or exhausted signal
stays ineligible — including where the issue carries both labels.

Authorisation note: the §5.1 ``labeled``-event actor test does not apply
to the pickup lane. The Pickup Label is applied by the runtime's own
terminal writes, so its actor is the Agent Identity itself and would fail
that test by design. The pickup's authorisation is the trusted terminal
record instead: authored by the Agent Identity's numeric id, carrying the
canonical terminal marker for the source Attempt, and naming this
Workflow, this Target Issue, a matching marker, full revision SHAs and
counter evidence (``used < limit``). A human cannot forge it — only the
Agent Identity's numeric author id verifies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from coding_agent.inputs.authorisation import IdentityMode

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    _execute_claim,
    in_force_claim_comments,
    terminal_marker_for,
)
from coding_agent.inputs.marker import AttemptMarker
from coding_agent.inputs.numbering import parse_attempt_marker
from coding_agent.inputs.pinning import PinnedInput, pin_from_readback
from coding_agent.inputs.port import (
    CodeHostPort,
    InputComment,
    InputPort,
    TrackedInput,
)
from coding_agent.inputs.selection import open_agent_pr_for_issue, sort_inputs
from coding_agent.ledger.store import RunLedger
from coding_agent.operator.commands import CommandRecord
from coding_agent.runtime.round_handoff import RoundHandoffOffer, parse_handoff_block

if TYPE_CHECKING:
    from coding_agent.inputs.comments import PinnedComment

PICKUP_REASONS: tuple[str, ...] = (
    "pickup-label",
    "pickup-replaced",
    "pickup-exhausted",
    "pickup-unavailable",
)
"""Pickup-lane ineligibility reasons (§8).

``pickup-label`` names a missing Pickup Label signal; ``pickup-replaced``
names an absent, invalid, mismatched or superseded record (a newer trusted
terminal for the Workflow and Target Issue carries no live offer);
``pickup-exhausted`` names a live record whose counter reached its limit;
``pickup-unavailable`` names a live offer whose work commit, named ref or
Base Revision the remote cannot serve. Shared guard reasons (``closed``,
``assigned``, ``blocker``, ``claim``, ``agent-pr``) read the same as
selection.
"""


PickupRemoteVerifier = Callable[[RoundHandoffOffer], bool]
"""Whether the remote still serves an offer's revisions (§8).

``True`` where the named ref serves exactly the recorded work commit with
the recorded Base Revision in its history; ``False`` (or a raise, which
reads as ``False``) where the ref moved or a revision is unavailable. The
intake refuses such an offer without a pickup: the signal stays untouched
and no Claim opens.
"""


@dataclass(frozen=True)
class PickupConfig:
    """What one pickup sweep runs under (§8).

    ``workflow_id`` is the claiming Workflow; ``pickup_label`` its
    resolved Pickup Label; ``limit`` its positive pickup limit (carried
    for the series state the confirmation pins — the in-flight offer's
    own ``used``/``limit`` counter governs eligibility, so a changed
    deployed limit applies to the next human-authorised series, never to
    this one). ``agent_login``/``agent_id`` are the Agent Identity;
    ``base_branch`` the configured base branch the Base Revision is read
    against; ``effective_configuration`` the pinned record the Attempt
    opens with; the prompt/activation/profile blobs are the startup bytes
    the Claim archives before its ledger transaction.
    """

    workflow_id: str
    pickup_label: str
    limit: int
    agent_login: str = ""
    agent_id: int | None = None
    base_branch: str = "main"
    effective_configuration: Mapping[str, object] = field(default_factory=dict)
    prompt_texts: Mapping[str, str] = field(default_factory=dict)
    activation_deliveries: Mapping[str, str] = field(default_factory=dict)
    activation_components: Mapping[str, Mapping[str, bytes]] = field(
        default_factory=dict
    )
    profile_text: str | None = None
    verify_remote: PickupRemoteVerifier | None = None
    """The intake availability check, ``None`` where the build skips it (§8).

    Production verifies the offer's work commit, named ref and Base Revision
    against the remote before any Claim; tests pass a stub or ``None``. A
    ``False`` (or raising) verifier refuses the offer as
    ``pickup-unavailable``: no Claim opens and the Pickup Label is left
    untouched.
    """


def _valid_pickup_config(config: PickupConfig) -> bool:
    """Whether a pickup sweep may run under ``config`` (§8)."""
    return (
        isinstance(config.workflow_id, str)
        and bool(config.workflow_id)
        and isinstance(config.pickup_label, str)
        and bool(config.pickup_label)
        and isinstance(config.limit, int)
        and not isinstance(config.limit, bool)
        and config.limit >= 1
    )


def newest_pickup_offer(
    comments: Sequence[InputComment],
    *,
    agent_id: int | None,
    workflow_id: str,
    issue_number: int,
    format_marker: Callable[[AttemptMarker], str] | None = None,
) -> tuple[RoundHandoffOffer, InputComment] | None:
    """The newest trusted terminal record's live offer, if any (§8).

    Scans for trusted terminal comments — authored by the Agent
    Identity's numeric id and carrying the canonical terminal marker for
    their exact Attempt id — parsed to this Workflow and Target Issue.
    The newest such comment wins: where it carries a valid record naming
    this Workflow, this issue and its own source Attempt, the offer
    returns with its source comment (exhausted or not — the caller
    reports the exhaustion). ``None`` where no trusted terminal record
    names this Workflow and issue, where the newest one carries no valid
    record (replaced by an ordinary terminal), or where the numeric
    identity is unknown. A forged author, a malformed marker, a mismatched
    record or a newer writer's version all read as absent.

    ``format_marker`` renders the canonical terminal marker; the GitHub
    rendering stands in where none is supplied. Production passes the
    tracker's own renderer.
    """
    if format_marker is None:
        from coding_agent.inputs.github import format_attempt_marker as _format

        format_marker = _format
    if agent_id is None:
        return None
    best_id: int | None = None
    best_offer: RoundHandoffOffer | None = None
    best_comment: InputComment | None = None
    for comment in comments:
        if comment.author_id != agent_id:
            continue
        parsed = parse_attempt_marker(comment.body)
        if parsed is None:
            continue
        raw_attempt = str(parsed)
        if format_marker(terminal_marker_for(raw_attempt)) not in comment.body:
            continue
        if parsed.workflow != workflow_id or parsed.input_number != issue_number:
            continue
        offer = parse_handoff_block(comment.body)
        if offer is not None and (
            offer.workflow_id != workflow_id
            or offer.issue != issue_number
            or offer.attempt != raw_attempt
        ):
            offer = None
        if best_id is None or comment.comment_id > best_id:
            best_id = comment.comment_id
            best_offer = offer
            best_comment = comment
    if best_offer is None or best_comment is None:
        return None
    return (best_offer, best_comment)


def pickup_exclusion_reason(
    candidate: TrackedInput,
    *,
    offer: RoundHandoffOffer | None,
    pickup_label: str,
    blocker_open: bool,
    claim_in_force: bool,
    agent_pr_open: bool = False,
) -> str | None:
    """The reason ``candidate`` is ineligible for pickup, or ``None`` (§8).

    Pure: the caller reads every guard input fresh. The Pickup Label
    signal comes first (a consumed label is ``pickup-label`` whatever the
    record says), then the record's currency (absent, mismatched or
    superseded is ``pickup-replaced``; a reached counter is
    ``pickup-exhausted``), then the shared guards. An invalid, stale,
    replaced or exhausted signal stays ineligible — including where the
    issue carries both the Selection and the Pickup Label.
    """
    if not candidate.is_open:
        return "closed"
    if pickup_label not in candidate.labels:
        return "pickup-label"
    if offer is None or offer.pickup_label != pickup_label:
        return "pickup-replaced"
    if offer.exhausted or offer.used >= offer.limit:
        return "pickup-exhausted"
    if candidate.assignees:
        return "assigned"
    if blocker_open:
        return "blocker"
    if claim_in_force:
        return "claim"
    if agent_pr_open:
        return "agent-pr"
    return None


def _pickup_guard_reason(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    pickup: PickupConfig,
    candidate: TrackedInput,
    offer: RoundHandoffOffer | None,
    *,
    agent_id: int | None,
    exclude_comment_id: int | None = None,
) -> str | None:
    """The pickup-lane guard reason for ``candidate``, read fresh (§8).

    The ``skip-open-agent-pr`` scan is explicit orchestration across both
    ports: ``code_host`` lists the open pull requests while ``inputs``
    checks the per-input blocker.
    """
    comments = inputs.list_comments(candidate.number, candidate.input_type)
    in_force = [
        comment
        for comment in in_force_claim_comments(
            comments, ledger, agent_id, inputs.format_marker
        )
        if comment.comment_id != exclude_comment_id
    ]
    agent_pr_open = False
    if claim_config.input_type == "issue" and claim_config.skip_open_agent_pr:
        agent_pr_open = (
            open_agent_pr_for_issue(
                code_host.list_open_pull_requests(),
                candidate.number,
                agent_login=claim_config.agent_login,
                agent_id=claim_config.agent_id,
            )
            is not None
        )
    return pickup_exclusion_reason(
        candidate,
        offer=offer,
        pickup_label=pickup.pickup_label,
        blocker_open=inputs.has_open_blocker(candidate.number, candidate.input_type),
        claim_in_force=bool(in_force),
        agent_pr_open=agent_pr_open,
    )


def _remote_available(
    pickup: PickupConfig,
    offer: RoundHandoffOffer,
) -> bool:
    """Whether the offer's revisions are still available from the remote (§8).

    ``True`` where no verifier is configured (builds that skip the check)
    or the verifier confirms the named ref still serves the recorded work
    commit with the recorded Base Revision in history. A ``False`` verifier
    — or one that raises — reads as unavailable: intake refuses the offer as
    ``pickup-unavailable`` without a pickup.
    """
    verifier = pickup.verify_remote
    if verifier is None:
        return True
    try:
        return bool(verifier(offer))
    except Exception:
        return False


def _pickup_readback_failure(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    pickup: PickupConfig,
    candidate: TrackedInput,
    *,
    offer: RoundHandoffOffer,
    source_comment_id: int,
    own_comment_id: int,
    agent_id: int | None,
) -> str | None:
    """The reason the pickup Claim read-back fails, or ``None`` (§8, §6.2).

    Re-runs the lane's guards and revalidates the same source: the
    newest trusted terminal record for the Workflow and Target Issue must
    still be the offered source comment. A replaced source (a newer
    terminal record landed first), a consumed Pickup Label or a lost race
    releases the Claim — never model work on a stale offer.
    """
    from coding_agent.inputs.claim import winning_claim

    readback = inputs.fetch_input(candidate.number, candidate.input_type)
    if not readback.is_open:
        return "closed"
    if pickup.pickup_label not in readback.labels:
        return "pickup-label"
    others = tuple(
        login for login in readback.assignees if login != pickup.agent_login
    )
    if others:
        return "assigned"
    if pickup.agent_login not in readback.assignees:
        return "assignment-missing"
    comments = inputs.list_comments(candidate.number, candidate.input_type)
    winner = winning_claim(comments, ledger, agent_id, inputs.format_marker)
    if winner is None or winner.comment_id != own_comment_id:
        return "claim-lost"
    current = newest_pickup_offer(
        comments,
        agent_id=agent_id,
        workflow_id=pickup.workflow_id,
        issue_number=candidate.number,
        format_marker=inputs.format_marker,
    )
    if (
        current is None
        or current[0].attempt != offer.attempt
        or current[1].comment_id != source_comment_id
    ):
        return "pickup-replaced"
    # The source is still current, but its revisions may have become
    # unavailable since the sweep: a moved ref or a missing work commit or
    # Base Revision releases the Claim with no pickup, never model work.
    if not _remote_available(pickup, offer):
        return "pickup-unavailable"
    guard = _pickup_guard_reason(
        inputs,
        code_host,
        ledger,
        claim_config,
        pickup,
        candidate,
        offer,
        agent_id=agent_id,
        exclude_comment_id=own_comment_id,
    )
    # The read-back already proved openness, the signal, the assignment
    # and the race above; only the shared guards can still refuse here.
    if guard in ("blocker", "claim", "agent-pr"):
        return guard
    return None


def attempt_pickup_claim(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    pickup: PickupConfig,
    candidate: TrackedInput,
    *,
    identity_mode: IdentityMode,
    offer: RoundHandoffOffer,
    source_comment: InputComment,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    priority_request_id: str | None = None,
) -> ConfirmedClaim | ReleasedClaim | None:
    """Claim one pickup candidate: intent, assign, comment, revalidate, pin (§8, §6.2).

    Mirrors :func:`coding_agent.inputs.claim.attempt_claim` on the pickup
    lane: the Attempt Archive holds the startup blobs before the ledger
    transaction the Claim depends on; the Attempt row opens in the
    transaction of the Intake check; a failed read-back — a lost race, a
    consumed Pickup Label or a replaced source — deletes the Worker's own
    Claim Comment, removes the assignment only when no other Claim is in
    force, and records the Claim ``released``.

    The pin retains the recorded Base Revision even when the configured
    base branch advanced, and carries the record's portable authorisation
    evidence as the selection actor. The confirmed Claim carries the
    offer so the Continuation opens at the exact published work commit
    with the pinned note as input data.

    A thin pickup-lane wrapper around :func:`claim._execute_claim`: the
    lane supplies the Round Handoff offer evidence, the pickup
    source-comment revalidation and the pickup pin finish (retained Base
    Revision, no merge-base resolution); the core owns the ordered
    ledger-plus-tracker sequence.
    """
    claim_agent_id = (
        pickup.agent_id if pickup.agent_id is not None else inputs.agent_id
    )

    def revalidate(attempt: str, own_comment_id: int) -> str | None:
        return _pickup_readback_failure(
            inputs,
            code_host,
            ledger,
            claim_config,
            pickup,
            candidate,
            offer=offer,
            source_comment_id=source_comment.comment_id,
            own_comment_id=own_comment_id,
            agent_id=claim_agent_id,
        )

    def finish_pin(readback: TrackedInput, base_tip: str) -> PinnedInput:
        pin = pin_from_readback(
            readback,
            workflow_id=pickup.workflow_id,
            base_tip=base_tip,
            merge_base=None,
            selection_actor=offer.selection_actor or None,
            selection_actor_id=offer.selection_actor_id,
            selection_actor_type=offer.selection_actor_type or None,
            selection_actor_at=offer.selection_actor_at or None,
            identity_mode=identity_mode,
        )
        return replace(pin, base_revision=offer.base_revision)

    return _execute_claim(
        inputs,
        code_host,
        ledger,
        candidate=candidate,
        content=claim_config,
        workflow_id=pickup.workflow_id,
        agent_login=pickup.agent_login,
        agent_id=pickup.agent_id,
        effective_configuration=pickup.effective_configuration,
        base_branch=pickup.base_branch,
        known_branches=known_branches,
        known_markers=known_markers,
        priority_request_id=priority_request_id,
        revalidate=revalidate,
        finish_pin=finish_pin,
        pickup_offer=offer,
    )


def _priority_issue_and_request(
    priority: CommandRecord | int | None,
) -> tuple[int | None, str | None]:
    """Split a sweep priority into its issue and settling request id."""
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


def sweep_pickup(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    pickup: PickupConfig,
    *,
    identity_mode: IdentityMode,
    priority: CommandRecord | int | None = None,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
) -> ConfirmedClaim | ReleasedClaim | None:
    """Run one pickup sweep: the first eligible offer wins (§8).

    Issue candidates carrying the Pickup Label are tried in contract
    §5.4 order under the pickup-lane guards; the first eligible offer is
    claimed and confirmed. A ``priority`` (``next issue N``) riding on a
    picked-up issue settles at the Claim boundary the same way a
    selection Claim settles it. ``None`` where nothing is pickup-eligible
    — the caller falls through to the Selection Label sweep. Selection
    writes nothing; an ineligible signal is left untouched.
    """
    if not _valid_pickup_config(pickup):
        return None
    if claim_config.input_type != "issue":
        return None
    claim_agent_id = (
        pickup.agent_id if pickup.agent_id is not None else inputs.agent_id
    )
    listed = inputs.list_issue_candidates(pickup.pickup_label)
    ordered = sort_inputs(listed)
    priority_issue, priority_request_id = _priority_issue_and_request(priority)
    for candidate in ordered:
        comments = inputs.list_comments(candidate.number, candidate.input_type)
        found = newest_pickup_offer(
            comments,
            agent_id=claim_agent_id,
            workflow_id=pickup.workflow_id,
            issue_number=candidate.number,
            format_marker=inputs.format_marker,
        )
        offer = found[0] if found is not None else None
        guard = _pickup_guard_reason(
            inputs,
            code_host,
            ledger,
            claim_config,
            pickup,
            candidate,
            offer,
            agent_id=claim_agent_id,
        )
        if guard is not None:
            continue
        # Intake refuses a previously advertised revision the remote can no
        # longer serve: a missing or moved ref, or an unavailable work commit
        # or Base Revision, leaves the signal untouched with no Claim.
        if found is not None and offer is not None and not _remote_available(
            pickup, offer
        ):
            continue
        assert found is not None and offer is not None
        claimed = attempt_pickup_claim(
            inputs,
            code_host,
            ledger,
            claim_config,
            pickup,
            candidate,
            identity_mode=identity_mode,
            offer=offer,
            source_comment=found[1],
            known_branches=known_branches,
            known_markers=known_markers,
            priority_request_id=(
                priority_request_id
                if candidate.number == priority_issue
                else None
            ),
        )
        if isinstance(claimed, ConfirmedClaim):
            return claimed
        if claimed is None:
            return None
    return None
