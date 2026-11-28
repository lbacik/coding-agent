"""Relay: the versioned terminal record one Workflow hands the next (§19).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §19 (an issue
Workflow whose Attempt ends with a Terminal Outcome that declares ``relay``
passes the Target Issue to another Workflow of the same Workflow
Configuration through the receiver's Relay Label) and §15 (the terminal
comment).

The sender's single terminal comment carries one Relay record in a fenced
block: the source Attempt, the source and target Workflow, the Target
Issue, the Terminal Outcome, the receiver's Relay Label and the
**originating human authorisation** (the actor and time of the Selection
Label that started the chain, carried forward unchanged through every
Relay). The receiving Worker trusts the record only after the checks of
:func:`coding_agent.inputs.relay.newest_terminal_relay`; a Relay never
creates an authorisation, it only carries one.

Model-free and network-free, like :mod:`coding_agent.runtime.round_handoff`.
"""

from __future__ import annotations

from dataclasses import dataclass

RELAY_RECORD_VERSION = 1
"""The version of the Relay record this build writes and reads (§19).

A newer writer's record reads as absent, never as a guess.
"""

RELAY_FENCE = "<!-- relay-record v1"
"""The opening line of the machine-readable record block (§19)."""

RELAY_FENCE_CLOSE = "-->"
"""The closing line of the machine-readable record block (§19)."""

RELAY_REASONS: tuple[str, ...] = (
    "relay-label",
    "relay-replaced",
    "relay-undeclared",
)
"""Relay-lane ineligibility reasons (§19).

``relay-label`` names a missing Relay Label signal; ``relay-replaced`` an
absent, invalid, forged, mismatched or superseded record; ``relay-undeclared``
a record whose Relay the receiver's own pinned configuration does not
declare. The shared guard reasons (``closed``, ``assigned``, ``blocker``,
``claim``, ``agent-pr``) read the same as selection.
"""

RELAY_MISSING_AUTHORISATION = "missing-authorisation"
"""The reason a sender records no Relay: its Attempt pinned no human
authorisation to carry forward (§19)."""


@dataclass(frozen=True)
class RelayRecord:
    """One parsed Relay record (§19).

    ``origin_actor``/``origin_actor_id``/``origin_actor_type``/
    ``origin_actor_at`` are the originating human authorisation;
    ``identity_mode`` is the mode the chain's first Claim checked it under.
    """

    source_attempt: str
    source_workflow: str
    target_workflow: str
    issue: int
    outcome: str
    relay_label: str
    origin_actor: str
    origin_actor_id: int
    origin_actor_type: str = ""
    origin_actor_at: str = ""
    identity_mode: str = ""


def render_relay_block(record: RelayRecord) -> str:
    """Render the machine-readable record block (§19)."""
    fields = [
        f"source_attempt: {record.source_attempt}",
        f"source_workflow: {record.source_workflow}",
        f"target_workflow: {record.target_workflow}",
        f"issue: {record.issue}",
        f"outcome: {record.outcome}",
        f"relay_label: {record.relay_label}",
        f"origin_actor: {record.origin_actor}",
        f"origin_actor_id: {record.origin_actor_id}",
    ]
    if record.origin_actor_type:
        fields.append(f"origin_actor_type: {record.origin_actor_type}")
    if record.origin_actor_at:
        fields.append(f"origin_actor_at: {record.origin_actor_at}")
    if record.identity_mode:
        fields.append(f"identity_mode: {record.identity_mode}")
    return RELAY_FENCE + "\n" + "\n".join(fields) + "\n" + RELAY_FENCE_CLOSE


def render_relay_section(record: RelayRecord) -> str:
    """Render the operator-visible section plus the machine record (§19)."""
    return "\n".join(
        [
            f"Relay: {record.source_attempt} passes #{record.issue} to workflow "
            f"{record.target_workflow} under {record.relay_label} "
            f"({record.outcome})",
            f"Originating authorisation: {record.origin_actor} "
            f"(id {record.origin_actor_id})"
            + (f" at {record.origin_actor_at}" if record.origin_actor_at else ""),
            render_relay_block(record),
        ]
    )


def render_relay_diagnostic(reason: str, detail: str) -> str:
    """Render the operator-visible line for a Relay that is not recorded (§19)."""
    return f"Relay: no relay ({reason}): {detail}"


def parse_relay_block(body: str) -> RelayRecord | None:
    """Parse the Relay record block, or ``None`` where absent or invalid (§19).

    Strict: the fence, every required field and the integer fields must be
    exactly right, or the block reads as absent. Unknown fields are ignored
    so an older reader skips what a newer writer adds; the version line
    itself must match exactly.
    """
    start = body.find(RELAY_FENCE)
    if start < 0:
        return None
    rest = body[start + len(RELAY_FENCE):]
    if not rest.startswith("\n"):
        return None
    lines = rest[1:].splitlines()
    try:
        close_at = lines.index(RELAY_FENCE_CLOSE)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for line in lines[:close_at]:
        key, separator, value = line.partition(": ")
        if not separator or not key or " " in key or key in fields:
            return None
        fields[key] = value
    try:
        issue = int(fields["issue"])
        origin_actor_id = int(fields["origin_actor_id"])
        source_attempt = fields["source_attempt"]
        source_workflow = fields["source_workflow"]
        target_workflow = fields["target_workflow"]
        outcome = fields["outcome"]
        relay_label = fields["relay_label"]
        origin_actor = fields["origin_actor"]
    except (KeyError, ValueError):
        return None
    if (
        not source_attempt
        or not source_workflow
        or not target_workflow
        or issue < 1
        or not outcome
        or not relay_label
        or not origin_actor
        or origin_actor_id < 1
    ):
        return None
    return RelayRecord(
        source_attempt=source_attempt,
        source_workflow=source_workflow,
        target_workflow=target_workflow,
        issue=issue,
        outcome=outcome,
        relay_label=relay_label,
        origin_actor=origin_actor,
        origin_actor_id=origin_actor_id,
        origin_actor_type=fields.get("origin_actor_type", ""),
        origin_actor_at=fields.get("origin_actor_at", ""),
        identity_mode=fields.get("identity_mode", ""),
    )
