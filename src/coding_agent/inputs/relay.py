"""Relay intake: claim a relayed Target Issue ahead of the Selection Label (§19).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §19 (one issue
Workflow passes a Target Issue to another Workflow of the same Workflow
Configuration through the receiver's Relay Label), §5.2/§6 (the lane order
and the Claim sequence: intent, assignment, Claim Comment, read-back, pin).

The lane order is Pickup Label, then Relay Label, then Selection Label. An
issue carrying the receiving Workflow's Relay Label is claimed as a Relay
only when the sender's Relay record is trusted: authored by the Agent
Identity's numeric id, carrying the canonical terminal marker of its exact
source Attempt, naming its source Workflow, this receiving Workflow and this
Target Issue, being the **newest terminal comment of any Workflow** on the
issue, and declared by the receiver's own pinned Effective Configuration.
A human cannot forge it: only the Agent Identity's numeric author id
verifies. The Relay Label alone never authorises anything.

Authorisation note: the §5.1 ``labeled``-event actor test does not apply to
this lane. The Relay Label is applied by the runtime's own terminal writes,
so its actor is the Agent Identity itself and would fail that test by
design; Relay therefore works the same under shared and dedicated identity.
The record's originating human authorisation is pinned unchanged as the
relayed Attempt's selection actor.

An invalid signal is diagnosed before model work with no Claim and no
write; the label stays on the issue for a human to remove. Where an issue
carries both the Relay Label and the Selection Label, a valid record wins;
otherwise intake falls through to the Selection lane and its actor test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

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
from coding_agent.runtime.relay import RELAY_REASONS, RelayRecord, parse_relay_block
from coding_agent.workflow.effective import effective_relay_declared

__all__ = [
    "RELAY_REASONS",
    "RelayConfig",
    "attempt_relay_claim",
    "newest_terminal_relay",
    "relay_exclusion_reason",
    "sweep_relay",
]

RelayDiagnostic = Callable[[int, str], None]
"""Receives ``(issue number, reason)`` for an invalid Relay signal (§19)."""


@dataclass(frozen=True)
class RelayConfig:
    """What one Relay sweep runs under (§19).

    ``workflow_id`` is the receiving Workflow; ``relay_label`` its resolved
    Relay Label. ``agent_login``/``agent_id`` are the Agent Identity;
    ``base_branch`` the configured base branch the Base Revision is read
    against; ``effective_configuration`` the pinned record the Attempt opens
    with — it also vouches for the Relays the Workflow receives. The
    prompt/activation/profile blobs are the startup bytes the Claim archives
    before its ledger transaction.
    """

    workflow_id: str
    relay_label: str
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


def _valid_relay_config(config: RelayConfig) -> bool:
    return (
        isinstance(config.workflow_id, str)
        and bool(config.workflow_id)
        and isinstance(config.relay_label, str)
        and bool(config.relay_label)
    )


def newest_terminal_relay(
    comments: Sequence[InputComment],
    *,
    agent_id: int | None,
    workflow_id: str,
    relay_label: str,
    issue_number: int,
    format_marker: Callable[[AttemptMarker], str] | None = None,
) -> tuple[RelayRecord, InputComment] | None:
    """The newest trusted terminal comment's Relay record for this receiver (§19).

    Scans trusted terminal comments of **any** Workflow on the issue —
    authored by the Agent Identity's numeric id and carrying the canonical
    terminal marker for their exact Attempt id. The newest such comment
    wins, so any later terminal supersedes an earlier Relay. ``None`` where
    no trusted terminal exists, the numeric identity is unknown, or the
    newest one carries no valid record naming its own source Attempt and
    Workflow, this receiving Workflow, this Relay Label and this Target
    Issue. A forged author, a malformed marker, a mismatched record or a
    newer writer's version all read as absent.
    """
    if format_marker is None:
        from coding_agent.inputs.github import format_attempt_marker as _format

        format_marker = _format
    if agent_id is None:
        return None
    best_id: int | None = None
    best: tuple[RelayRecord | None, InputComment] | None = None
    for comment in comments:
        if comment.author_id != agent_id:
            continue
        parsed = parse_attempt_marker(comment.body)
        if parsed is None:
            continue
        raw_attempt = str(parsed)
        if format_marker(terminal_marker_for(raw_attempt)) not in comment.body:
            continue
        if parsed.input_number != issue_number:
            continue
        record = parse_relay_block(comment.body)
        if record is not None and (
            record.source_attempt != raw_attempt
            or record.source_workflow != parsed.workflow
            or record.target_workflow != workflow_id
            or record.relay_label != relay_label
            or record.issue != issue_number
        ):
            record = None
        if best_id is None or comment.comment_id > best_id:
            best_id = comment.comment_id
            best = (record, comment)
    if best is None or best[0] is None:
        return None
    return (best[0], best[1])


def relay_exclusion_reason(
    candidate: TrackedInput,
    *,
    record: RelayRecord | None,
    declared: bool,
    relay_label: str,
    blocker_open: bool,
    claim_in_force: bool,
    agent_pr_open: bool = False,
) -> str | None:
    """The reason ``candidate`` is ineligible for a Relay, or ``None`` (§19).

    Pure: the caller reads every guard input fresh. The Relay Label signal
    comes first (a missing label is ``relay-label``), then the record's
    currency (absent, invalid, mismatched or superseded is
    ``relay-replaced``), then the receiver's own declaration
    (``relay-undeclared``), then the shared guards.
    """
    if not candidate.is_open:
        return "closed"
    if relay_label not in candidate.labels:
        return "relay-label"
    if record is None:
        return "relay-replaced"
    if not declared:
        return "relay-undeclared"
    if candidate.assignees:
        return "assigned"
    if blocker_open:
        return "blocker"
    if claim_in_force:
        return "claim"
    if agent_pr_open:
        return "agent-pr"
    return None


def _declared(relay: RelayConfig, record: RelayRecord | None) -> bool:
    if record is None:
        return False
    return effective_relay_declared(
        relay.effective_configuration, record.source_workflow, record.outcome
    )


def _relay_guard_reason(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    relay: RelayConfig,
    candidate: TrackedInput,
    record: RelayRecord | None,
    *,
    agent_id: int | None,
    exclude_comment_id: int | None = None,
) -> str | None:
    """The Relay-lane guard reason for ``candidate``, read fresh (§19)."""
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
                workflow_id=claim_config.workflow_id,
                agent_login=claim_config.agent_login,
                agent_id=claim_config.agent_id,
            )
            is not None
        )
    return relay_exclusion_reason(
        candidate,
        record=record,
        declared=_declared(relay, record),
        relay_label=relay.relay_label,
        blocker_open=inputs.has_open_blocker(candidate.number, candidate.input_type),
        claim_in_force=bool(in_force),
        agent_pr_open=agent_pr_open,
    )


def _relay_readback_failure(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    relay: RelayConfig,
    candidate: TrackedInput,
    *,
    record: RelayRecord,
    source_comment_id: int,
    own_comment_id: int,
    agent_id: int | None,
) -> str | None:
    """The reason the Relay Claim read-back fails, or ``None`` (§19, §6.2).

    Re-runs the lane's guards and revalidates the same source: the newest
    trusted terminal comment of any Workflow must still be the relayed
    source comment. A superseded source, a removed Relay Label or a lost race
    releases the Claim — never model work on a stale signal.
    """
    from coding_agent.inputs.claim import winning_claim

    readback = inputs.fetch_input(candidate.number, candidate.input_type)
    if not readback.is_open:
        return "closed"
    if relay.relay_label not in readback.labels:
        return "relay-label"
    others = tuple(
        login for login in readback.assignees if login != relay.agent_login
    )
    if others:
        return "assigned"
    if relay.agent_login not in readback.assignees:
        return "assignment-missing"
    comments = inputs.list_comments(candidate.number, candidate.input_type)
    winner = winning_claim(comments, ledger, agent_id, inputs.format_marker)
    if winner is None or winner.comment_id != own_comment_id:
        return "claim-lost"
    current = newest_terminal_relay(
        comments,
        agent_id=agent_id,
        workflow_id=relay.workflow_id,
        relay_label=relay.relay_label,
        issue_number=candidate.number,
        format_marker=inputs.format_marker,
    )
    if (
        current is None
        or current[0] != record
        or current[1].comment_id != source_comment_id
    ):
        return "relay-replaced"
    guard = _relay_guard_reason(
        inputs,
        code_host,
        ledger,
        claim_config,
        relay,
        candidate,
        record,
        agent_id=agent_id,
        exclude_comment_id=own_comment_id,
    )
    if guard in ("blocker", "claim", "agent-pr"):
        return guard
    return None


def attempt_relay_claim(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    relay: RelayConfig,
    candidate: TrackedInput,
    *,
    identity_mode: IdentityMode,
    record: RelayRecord,
    source_comment: InputComment,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    priority_request_id: str | None = None,
) -> ConfirmedClaim | ReleasedClaim | None:
    """Claim one relayed candidate: intent, assign, comment, revalidate, pin (§19, §6.2).

    A thin Relay-lane wrapper around :func:`coding_agent.inputs.claim._execute_claim`:
    the lane supplies the source-comment revalidation and the pin finish
    (the record's originating human authorisation as the selection actor,
    the source Attempt id as the relay source); the core owns the ordered
    ledger-plus-tracker sequence.
    """
    claim_agent_id = (
        relay.agent_id if relay.agent_id is not None else inputs.agent_id
    )

    def revalidate(attempt: str, own_comment_id: int) -> str | None:
        return _relay_readback_failure(
            inputs,
            code_host,
            ledger,
            claim_config,
            relay,
            candidate,
            record=record,
            source_comment_id=source_comment.comment_id,
            own_comment_id=own_comment_id,
            agent_id=claim_agent_id,
        )

    def finish_pin(readback: TrackedInput, base_tip: str) -> PinnedInput:
        from dataclasses import replace

        pin = pin_from_readback(
            readback,
            workflow_id=relay.workflow_id,
            base_tip=base_tip,
            merge_base=None,
            selection_actor=record.origin_actor,
            selection_actor_id=record.origin_actor_id,
            selection_actor_type=record.origin_actor_type or None,
            selection_actor_at=record.origin_actor_at or None,
            identity_mode=identity_mode,
        )
        return replace(pin, relay_source_attempt=record.source_attempt)

    return _execute_claim(
        inputs,
        code_host,
        ledger,
        candidate=candidate,
        content=claim_config,
        workflow_id=relay.workflow_id,
        agent_login=relay.agent_login,
        agent_id=relay.agent_id,
        effective_configuration=relay.effective_configuration,
        base_branch=relay.base_branch,
        known_branches=known_branches,
        known_markers=known_markers,
        priority_request_id=priority_request_id,
        revalidate=revalidate,
        finish_pin=finish_pin,
    )


def _priority_issue_and_request(
    priority: CommandRecord | int | None,
) -> tuple[int | None, str | None]:
    from coding_agent.inputs.pickup import _priority_issue_and_request as split

    return split(priority)


def sweep_relay(
    inputs: InputPort,
    code_host: CodeHostPort,
    ledger: RunLedger,
    claim_config: ClaimConfig,
    relay: RelayConfig,
    *,
    identity_mode: IdentityMode,
    priority: CommandRecord | int | None = None,
    known_branches: Iterable[str] = (),
    known_markers: Iterable[str] = (),
    diagnose: RelayDiagnostic | None = None,
) -> ConfirmedClaim | ReleasedClaim | None:
    """Run one Relay sweep: the first eligible Relay wins (§19).

    Issue candidates carrying the Relay Label are tried in contract §5.4
    order under the Relay-lane guards; the first eligible Relay is claimed
    and confirmed. ``None`` where nothing is Relay-eligible — the caller
    falls through to the Selection Label sweep. An invalid signal
    (``relay-replaced``, ``relay-undeclared``) is reported through
    ``diagnose`` before any model work, with no Claim and no write; the
    label stays untouched.
    """
    if not _valid_relay_config(relay):
        return None
    if claim_config.input_type != "issue":
        return None
    claim_agent_id = (
        relay.agent_id if relay.agent_id is not None else inputs.agent_id
    )
    priority_issue, priority_request_id = _priority_issue_and_request(priority)
    for candidate in sort_inputs(inputs.list_issue_candidates(relay.relay_label)):
        comments = inputs.list_comments(candidate.number, candidate.input_type)
        found = newest_terminal_relay(
            comments,
            agent_id=claim_agent_id,
            workflow_id=relay.workflow_id,
            relay_label=relay.relay_label,
            issue_number=candidate.number,
            format_marker=inputs.format_marker,
        )
        record = found[0] if found is not None else None
        guard = _relay_guard_reason(
            inputs,
            code_host,
            ledger,
            claim_config,
            relay,
            candidate,
            record,
            agent_id=claim_agent_id,
        )
        if guard is not None:
            if diagnose is not None and guard in ("relay-replaced", "relay-undeclared"):
                diagnose(candidate.number, guard)
            continue
        assert found is not None
        claimed = attempt_relay_claim(
            inputs,
            code_host,
            ledger,
            claim_config,
            relay,
            candidate,
            identity_mode=identity_mode,
            record=found[0],
            source_comment=found[1],
            known_branches=known_branches,
            known_markers=known_markers,
            priority_request_id=(
                priority_request_id if candidate.number == priority_issue else None
            ),
        )
        if isinstance(claimed, ConfirmedClaim):
            return claimed
        if claimed is None:
            return None
    return None
