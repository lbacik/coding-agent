"""Round Handoff publication: the versioned terminal record (§8).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (an
opted-in issue Workflow publishes a pushed Hard Handoff on its Target
Issue; another Worker claims the offer under the same Agent Identity and
opens a Continuation within the limit fixed by the human-authorised
Attempt) and §15 (the terminal comment and the finalisation sequence).

The Target Issue's single terminal comment carries one versioned Round
Handoff record: the source Attempt Marker, the full work commit SHA, the
named ref to fetch the work from, the full Base Revision SHA, the note
kind, the bounded redacted note, the exact Pickup Label and the portable
authorisation and counter evidence. The comment is self-contained for
pickup from another machine; the Run Ledger row holding the comment is
the local evidence. A failed push, a missing note or an unavailable
revision leaves a diagnostic and no pickup signal.

This module is model-free: the ``hard`` note is composed from available
Execution Summaries, the ``ai-assisted`` note is the resolved Step
Artifact document, both bounded and already redacted at rest. Verification
of the remote revisions runs through caller-supplied evidence — this
module never touches the network or the git checkout itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ROUND_HANDOFF_RECORD_VERSION = 1
"""The version of the terminal record this build writes and reads (§8).

Intake accepts only this version: a newer writer's record reads as
absent, never as a guess.
"""

ROUND_HANDOFF_NOTE_MAX_CHARS = 8_000
"""The bound on the published handoff note (§8).

Both note kinds are cut here with an explicit marker (and, for an
``ai-assisted`` note, a pointer at the Step Artifact holding the full
text), so the terminal comment stays far below GitHub's body limit and
the Continuation's opening request stays bounded.
"""

ROUND_HANDOFF_FENCE = "<!-- round-handoff-record v1"
"""The opening line of the machine-readable record block (§8)."""

ROUND_HANDOFF_FENCE_CLOSE = "-->"
"""The closing line of the machine-readable record block (§8)."""

ROUND_HANDOFF_DIAGNOSTICS: tuple[str, ...] = (
    "failed-push",
    "missing-note",
    "unavailable-revision",
    "missing-authorisation",
    "missing-handoff",
)
"""The closed diagnostic reason set (§8).

``failed-push`` names a publishing step whose push never landed;
``missing-note`` names an absent handoff note; ``unavailable-revision``
names a work commit or Base Revision the remote cannot serve;
``missing-authorisation`` names absent Claim authorisation evidence;
``missing-handoff`` names a recovery that found an eligible end but no
durably prepared offer. Every diagnostic leaves ordinary labels and no
pickup signal.
"""

_SHA_RE = re.compile(r"[0-9a-f]{40}")
"""What a full published SHA looks like."""


@dataclass(frozen=True)
class RoundHandoffOffer:
    """A durably-prepared Round Handoff offer: the terminal record (§8).

    ``used`` counts the pickups the series consumed before this round
    (zero for a fresh human-authorised series); ``limit`` is the
    human-authorised limit the series keeps even when the deployed file
    later changes. ``exhausted`` marks the last permitted round: the note
    and revisions still publish, but the terminal carries the exhaustion
    reason with ordinary labels and no pickup signal. The selection actor
    fields are the portable authorisation evidence pinned at Claim.
    """

    workflow_id: str
    issue: int
    attempt: str
    work_commit: str
    ref: str
    base_revision: str
    note_kind: str
    note: str
    pickup_label: str
    used: int
    limit: int
    exhausted: bool = False
    exhaustion_reason: str = ""
    selection_actor: str = ""
    selection_actor_id: int = 0
    selection_actor_type: str = ""
    selection_actor_at: str = ""
    identity_mode: str = ""


@dataclass(frozen=True)
class HandoffDiagnostic:
    """Why an eligible round publishes no pickup signal (§8).

    ``reason`` is one of :data:`ROUND_HANDOFF_DIAGNOSTICS`; ``detail``
    names the landed revision where one exists, so the operator can tell
    work that landed without an offer from work that never landed.
    """

    reason: str
    detail: str


@dataclass(frozen=True)
class HandoffPreparation:
    """Everything preparation needs, made explicit (§8).

    ``pickup_label``/``limit`` are the enabled Workflow's resolved
    configuration (``None`` where Round Handoff stays disabled);
    ``annotation_kind``/``annotation_note`` ride the taken publishing
    Route (``None`` where the end carries no annotation); ``note_text``
    is the resolved bounded note (``None`` where no note could be built);
    ``push_landed`` reports whether a ``commit-and-push`` receipt landed
    a remote id; ``remote_verified`` reports whether the remote serves
    ``work_commit`` at ``ref`` and ``base_revision``; ``landed_revision``
    names the revision the push left behind, for diagnostics.
    """

    workflow_id: str
    issue: int
    attempt: str
    outcome: str
    pickup_label: str | None
    limit: int | None
    annotation_kind: str | None
    annotation_note: str | None
    note_text: str | None
    work_commit: str | None
    ref: str | None
    base_revision: str | None
    push_landed: bool
    remote_verified: bool
    landed_revision: str | None = None
    selection_actor: str | None = None
    selection_actor_id: int | None = None
    selection_actor_type: str | None = None
    selection_actor_at: str | None = None
    identity_mode: str | None = None
    used: int = 0


def truncate_note(text: str, pointer: str | None = None) -> tuple[str, bool]:
    """Bound a handoff note, marking the cut (§8).

    Returns the bounded text and whether it was cut. The marker names the
    bound and, where the caller knows one, the Step Artifact holding the
    full document — mirroring the publication truncation rule (OP-19).
    """
    if len(text) <= ROUND_HANDOFF_NOTE_MAX_CHARS:
        return text, False
    marker = (
        f"\n[truncated: note exceeded {ROUND_HANDOFF_NOTE_MAX_CHARS} characters"
    )
    if pointer:
        marker += f"; full text in Step Artifact {pointer}"
    marker += "]"
    return text[:ROUND_HANDOFF_NOTE_MAX_CHARS] + marker, True


def _render_commands(commands: object) -> list[str]:
    """One compact line per recorded command, generically shaped (§9.7)."""
    if not isinstance(commands, list):
        return []
    lines: list[str] = []
    for entry in commands[:50]:
        if not isinstance(entry, Mapping):
            continue
        lines.append(
            "; ".join(f"{key}={value}" for key, value in entry.items())[:500]
        )
    return lines


@dataclass(frozen=True)
class SummaryInput:
    """One available Execution Summary for the model-free note (§8, §9.7)."""

    step_id: str
    seq: int
    facts: Mapping[str, object]


def hard_handoff_note(
    summaries: Sequence[SummaryInput],
    context: Sequence[str] = (),
) -> str | None:
    """Compose the model-free ``hard`` note from Execution Summaries (§8).

    Renders the threshold crossing (or receiving-step failure) context and
    each summary's changed files, commands, evidence, limit and spend —
    already redacted at rest — then bounds the whole note. ``None`` where
    no content exists at all: an empty note is a missing note, never an
    empty offer.
    """
    lines: list[str] = [
        "Hard Handoff: the working conversation crossed its Hard Context "
        "Threshold (or the AI-assisted note could not be produced), so the "
        "Worker ended the Step Execution with no further model request."
    ]
    for entry in context:
        if entry:
            lines.append(entry)
    for summary in summaries:
        facts = summary.facts
        lines.append(f"## {summary.step_id} #{summary.seq}")
        changed = facts.get("changed_files")
        if isinstance(changed, list) and changed:
            names = [str(name) for name in changed[:100]]
            lines.append(f"Changed files: {', '.join(names)}")
        for command_line in _render_commands(facts.get("commands")):
            lines.append(f"- {command_line}")
        limit = facts.get("limit")
        if isinstance(limit, Mapping) and limit:
            lines.append(
                "Limit: "
                + "; ".join(f"{key}={value}" for key, value in limit.items())[:500]
            )
        failure = facts.get("provider_failure")
        if isinstance(failure, Mapping) and failure:
            lines.append(
                "Provider failure: "
                + "; ".join(f"{key}={value}" for key, value in failure.items())[:500]
            )
        evidence = facts.get("evidence")
        if isinstance(evidence, Mapping) and evidence:
            keys = sorted(str(key) for key in evidence.keys())[:50]
            lines.append(f"Evidence: {', '.join(keys)}")
        spend = facts.get("spend")
        if isinstance(spend, Mapping) and spend:
            lines.append(
                "Spend: "
                + "; ".join(f"{key}={value}" for key, value in spend.items())[:500]
            )
    if len(lines) == 1:
        return None
    text = "\n".join(lines)
    bounded, _ = truncate_note(text)
    return bounded


def _is_full_sha(value: object) -> bool:
    """Whether ``value`` is a full lowercase commit SHA."""
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def build_live_preparation(
    *,
    workflow_id: str,
    issue: int,
    attempt: str,
    outcome: str,
    ending_step: str,
    step_result: str,
    pickup_label: str | None,
    limit: int | None,
    annotation_kind: str | None,
    note_text: str | None,
    work_commit: str | None,
    ref: str | None,
    base_revision: str | None,
    push_landed: bool,
    remote_verified: bool,
    landed_revision: str | None = None,
    selection_actor: str | None = None,
    selection_actor_id: int | None = None,
    selection_actor_type: str | None = None,
    selection_actor_at: str | None = None,
    identity_mode: str | None = None,
    used: int = 0,
) -> HandoffPreparation | None:
    """Assemble the live round's preparation, or ``None`` off the path (§8).

    ``None`` where the round never publishes — a non-``unfinished``
    outcome, a disabled Workflow, or a taken end carrying no publishing
    annotation — so the Attempt keeps its ordinary terminal path with no
    diagnostic section. Every other ineligibility (a failed push, a missing
    note, missing authorisation, an unavailable revision) stays visible:
    it builds the preparation and :func:`decide_handoff` diagnoses it.
    """
    if outcome != "unfinished":
        return None
    if pickup_label is None or limit is None:
        return None
    if annotation_kind not in ("ai-assisted", "hard"):
        return None
    return HandoffPreparation(
        workflow_id=workflow_id,
        issue=issue,
        attempt=attempt,
        outcome=outcome,
        pickup_label=pickup_label,
        limit=limit,
        annotation_kind=annotation_kind,
        annotation_note=None,
        note_text=note_text,
        work_commit=work_commit,
        ref=ref,
        base_revision=base_revision,
        push_landed=push_landed,
        remote_verified=remote_verified,
        landed_revision=landed_revision,
        selection_actor=selection_actor,
        selection_actor_id=selection_actor_id,
        selection_actor_type=selection_actor_type,
        selection_actor_at=selection_actor_at,
        identity_mode=identity_mode,
        used=used,
    )


def decide_handoff(
    preparation: HandoffPreparation,
) -> RoundHandoffOffer | HandoffDiagnostic | None:
    """Decide the round: offer, diagnostic, or no handoff at all (§8).

    ``None`` where the round never publishes — Round Handoff disabled, the
    taken end carries no annotation, or the outcome is not ``unfinished`` —
    so the Attempt keeps its ordinary terminal path with no diagnostic
    section. A failed push, a missing note, missing authorisation or an
    unavailable revision yields a diagnostic with ordinary labels and no
    pickup signal. Otherwise the prepared offer carries the exact Pickup
    Label; an exhausted series still publishes its note and revisions with
    the exhaustion reason and ordinary labels.
    """
    if preparation.outcome != "unfinished":
        return None
    if preparation.pickup_label is None or preparation.limit is None:
        return None
    if preparation.annotation_kind not in ("ai-assisted", "hard"):
        return None
    if not preparation.push_landed:
        return HandoffDiagnostic(
            reason="failed-push",
            detail=_failed_push_detail(preparation),
        )
    if preparation.note_text is None:
        return HandoffDiagnostic(
            reason="missing-note",
            detail=(
                f"round-handoff: no {preparation.annotation_kind} note could "
                f"be built for {preparation.attempt}; no pickup is offered"
                + _landed_suffix(preparation)
            ),
        )
    if preparation.selection_actor is None or not isinstance(
        preparation.selection_actor_id, int
    ):
        return HandoffDiagnostic(
            reason="missing-authorisation",
            detail=(
                f"round-handoff: no Claim authorisation evidence for "
                f"{preparation.attempt}; no pickup is offered"
                + _landed_suffix(preparation)
            ),
        )
    if (
        not _is_full_sha(preparation.work_commit)
        or not preparation.ref
        or not _is_full_sha(preparation.base_revision)
        or not preparation.remote_verified
    ):
        return HandoffDiagnostic(
            reason="unavailable-revision",
            detail=(
                f"round-handoff: the remote cannot serve work commit "
                f"{preparation.work_commit} at ref {preparation.ref!r} or "
                f"Base Revision {preparation.base_revision}; "
                f"no pickup is offered{_landed_suffix(preparation)}"
            ),
        )
    if not preparation.workflow_id or not preparation.attempt:
        raise ValueError("round-handoff: a prepared offer needs its workflow and attempt")
    if preparation.issue < 1:
        raise ValueError("round-handoff: a prepared offer needs its target issue")
    if preparation.used < 0 or preparation.limit < 1:
        raise ValueError("round-handoff: a prepared offer needs a valid used count and limit")
    assert preparation.work_commit is not None
    assert preparation.base_revision is not None
    assert preparation.ref is not None
    assert preparation.note_text is not None
    exhausted = preparation.used >= preparation.limit
    return RoundHandoffOffer(
        workflow_id=preparation.workflow_id,
        issue=preparation.issue,
        attempt=preparation.attempt,
        work_commit=preparation.work_commit,
        ref=preparation.ref,
        base_revision=preparation.base_revision,
        note_kind=preparation.annotation_kind,
        note=preparation.note_text,
        pickup_label=preparation.pickup_label,
        used=preparation.used,
        limit=preparation.limit,
        exhausted=exhausted,
        exhaustion_reason="pickup-limit-reached" if exhausted else "",
        selection_actor=preparation.selection_actor,
        selection_actor_id=preparation.selection_actor_id,
        selection_actor_type=preparation.selection_actor_type or "",
        selection_actor_at=preparation.selection_actor_at or "",
        identity_mode=preparation.identity_mode or "",
    )


def _landed_suffix(preparation: HandoffPreparation) -> str:
    """Name the landed revision in a diagnostic, where one exists (§8)."""
    if preparation.landed_revision:
        return f"; landed revision {preparation.landed_revision} carries no offer"
    return ""


def _failed_push_detail(preparation: HandoffPreparation) -> str:
    """The failed-push diagnostic: the push never landed (§8)."""
    return (
        f"round-handoff: the publishing push for {preparation.attempt} "
        f"never landed{_landed_suffix(preparation)}; no pickup is offered"
    )


def render_handoff_block(offer: RoundHandoffOffer) -> str:
    """Render the machine-readable terminal record block (§8).

    The single fenced block the terminal comment carries: every field the
    next Worker verifies before model work. The note rides inside the
    block so machines never parse prose; note lines that would close the
    fence early are escaped on render and restored on parse.
    """
    fields = [
        f"workflow: {offer.workflow_id}",
        f"issue: {offer.issue}",
        f"attempt: {offer.attempt}",
        f"work_commit: {offer.work_commit}",
        f"ref: {offer.ref}",
        f"base_revision: {offer.base_revision}",
        f"note_kind: {offer.note_kind}",
        f"pickup_label: {offer.pickup_label}",
        f"used: {offer.used}",
        f"limit: {offer.limit}",
        f"exhausted: {'true' if offer.exhausted else 'false'}",
    ]
    if offer.exhaustion_reason:
        fields.append(f"exhaustion_reason: {offer.exhaustion_reason}")
    fields.extend(
        [
            f"selection_actor: {offer.selection_actor}",
            f"selection_actor_id: {offer.selection_actor_id}",
        ]
    )
    if offer.selection_actor_type:
        fields.append(f"selection_actor_type: {offer.selection_actor_type}")
    if offer.selection_actor_at:
        fields.append(f"selection_actor_at: {offer.selection_actor_at}")
    if offer.identity_mode:
        fields.append(f"identity_mode: {offer.identity_mode}")
    fields.append("note:")
    escaped = "\n".join(
        "-- >" if line == ROUND_HANDOFF_FENCE_CLOSE else line
        for line in offer.note.split("\n")
    )
    return (
        ROUND_HANDOFF_FENCE
        + "\n"
        + "\n".join(fields)
        + "\n"
        + escaped
        + "\n"
        + ROUND_HANDOFF_FENCE_CLOSE
    )


def parse_handoff_block(body: str) -> RoundHandoffOffer | None:
    """Parse the terminal record block, or ``None`` where absent (§8).

    Strict: the fence, every required field and every SHA must be exactly
    right, or the block reads as absent and the round stays ineligible —
    a forged or truncated record never verifies. Unknown fields are
    ignored so an older reader skips what a newer writer adds; the
    version line itself must match exactly.
    """
    start = body.find(ROUND_HANDOFF_FENCE)
    if start < 0:
        return None
    rest = body[start + len(ROUND_HANDOFF_FENCE):]
    if not rest.startswith("\n"):
        return None
    lines = rest[1:].splitlines()
    try:
        close_at = lines.index(ROUND_HANDOFF_FENCE_CLOSE)
    except ValueError:
        return None
    lines = lines[:close_at]
    fields: dict[str, str] = {}
    note_lines: list[str] | None = None
    for line in lines:
        if note_lines is not None:
            note_lines.append(
                ROUND_HANDOFF_FENCE_CLOSE if line == "-- >" else line
            )
            continue
        if line == "note:":
            note_lines = []
            continue
        key, separator, value = line.partition(": ")
        if not separator or not key or " " in key or key in fields:
            return None
        fields[key] = value
    if note_lines is None:
        return None
    try:
        issue = int(fields["issue"])
        used = int(fields["used"])
        limit = int(fields["limit"])
        selection_actor_id = int(fields.get("selection_actor_id", "0"))
    except (KeyError, ValueError):
        return None
    try:
        workflow_id = fields["workflow"]
        attempt = fields["attempt"]
        work_commit = fields["work_commit"]
        ref = fields["ref"]
        base_revision = fields["base_revision"]
        note_kind = fields["note_kind"]
        pickup_label = fields["pickup_label"]
        exhausted_raw = fields["exhausted"]
    except KeyError:
        return None
    if (
        not workflow_id
        or issue < 1
        or not attempt
        or not _is_full_sha(work_commit)
        or not ref
        or not _is_full_sha(base_revision)
        or note_kind not in ("ai-assisted", "hard")
        or not pickup_label
        or used < 0
        or limit < 1
        or exhausted_raw not in ("true", "false")
    ):
        return None
    note = "\n".join(note_lines)
    return RoundHandoffOffer(
        workflow_id=workflow_id,
        issue=issue,
        attempt=attempt,
        work_commit=work_commit,
        ref=ref,
        base_revision=base_revision,
        note_kind=note_kind,
        note=note,
        pickup_label=pickup_label,
        used=used,
        limit=limit,
        exhausted=exhausted_raw == "true",
        exhaustion_reason=fields.get("exhaustion_reason", ""),
        selection_actor=fields.get("selection_actor", ""),
        selection_actor_id=selection_actor_id,
        selection_actor_type=fields.get("selection_actor_type", ""),
        selection_actor_at=fields.get("selection_actor_at", ""),
        identity_mode=fields.get("identity_mode", ""),
    )


def render_handoff_section(offer: RoundHandoffOffer) -> str:
    """Render the operator-visible section plus the machine record (§8).

    The visible lines name the round, the revisions, the note kind and
    the pickup count; the fenced block below carries the exact verified
    fields the next Worker parses. An exhausted round says so plainly:
    the note and revisions publish, but no pickup is offered.
    """
    if offer.exhausted:
        status = (
            f"Round Handoff: {offer.attempt} publishes its final round "
            f"({offer.used}/{offer.limit}, {offer.exhaustion_reason}); "
            "automatic pickup is exhausted"
        )
    else:
        status = (
            f"Round Handoff: {offer.attempt} offers pickup under "
            f"{offer.pickup_label} ({offer.used}/{offer.limit})"
        )
    lines = [
        status,
        f"Work commit: {offer.work_commit} (ref {offer.ref})",
        f"Base Revision: {offer.base_revision}",
        f"Note ({offer.note_kind}, {len(offer.note)} chars):",
        offer.note,
        render_handoff_block(offer),
    ]
    return "\n".join(lines)


def render_diagnostic_section(diagnostic: HandoffDiagnostic) -> str:
    """Render the operator-visible diagnostic for a round with no offer (§8)."""
    return f"Round Handoff: no pickup ({diagnostic.reason}): {diagnostic.detail}"
