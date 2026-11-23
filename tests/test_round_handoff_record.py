"""The versioned Round Handoff terminal record (contract §8).

Issue #612, acceptance bullets 2-3. The terminal comment carries one
fenced record block with the source Attempt Marker, the full work commit
SHA, the named ref, the full Base Revision SHA, the note kind, the
bounded redacted note, the exact Pickup Label and the portable
authorisation and counter evidence. Parsing is strict: a forged or
truncated block reads as absent and the round stays ineligible.
"""

from __future__ import annotations

import pytest

from coding_agent.runtime.round_handoff import (
    ROUND_HANDOFF_DIAGNOSTICS,
    ROUND_HANDOFF_NOTE_MAX_CHARS,
    HandoffDiagnostic,
    HandoffPreparation,
    RoundHandoffOffer,
    SummaryInput,
    decide_handoff,
    hard_handoff_note,
    parse_handoff_block,
    render_diagnostic_section,
    render_handoff_block,
    render_handoff_section,
    truncate_note,
)

WORK = "a" * 40
BASE = "b" * 40


def _offer(**overrides: object) -> RoundHandoffOffer:
    fields: dict[str, object] = {
        "workflow_id": "implement",
        "issue": 7,
        "attempt": "implement#7/1",
        "work_commit": WORK,
        "ref": "agent/implement-7-1-work",
        "base_revision": BASE,
        "note_kind": "hard",
        "note": "did the thing",
        "pickup_label": "round-finished",
        "used": 0,
        "limit": 2,
    }
    fields.update(overrides)
    return RoundHandoffOffer(**fields)  # type: ignore[arg-type]


def _preparation(**overrides: object) -> HandoffPreparation:
    fields: dict[str, object] = {
        "workflow_id": "implement",
        "issue": 7,
        "attempt": "implement#7/1",
        "outcome": "unfinished",
        "pickup_label": "round-finished",
        "limit": 2,
        "annotation_kind": "hard",
        "annotation_note": None,
        "note_text": "did the thing",
        "work_commit": WORK,
        "ref": "agent/implement-7-1-work",
        "base_revision": BASE,
        "push_landed": True,
        "remote_verified": True,
        "landed_revision": WORK,
        "selection_actor": "alice",
        "selection_actor_id": 123,
        "selection_actor_type": "User",
        "selection_actor_at": "2026-11-23T16:00:00+00:00",
        "identity_mode": "org-member",
        "used": 0,
    }
    fields.update(overrides)
    return HandoffPreparation(**fields)  # type: ignore[arg-type]


# --- render / parse round-trip ------------------------------------------------------


def test_block_round_trips_a_full_offer() -> None:
    """Every field survives the comment and back."""
    offer = _offer(
        note_kind="ai-assisted",
        selection_actor="alice",
        selection_actor_id=123,
        selection_actor_type="User",
        selection_actor_at="2026-11-23T16:00:00+00:00",
        identity_mode="org-member",
    )

    assert parse_handoff_block(render_handoff_block(offer)) == offer


def test_block_round_trips_an_exhausted_offer() -> None:
    """The exhaustion flag and reason are machine-readable."""
    offer = _offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached")

    parsed = parse_handoff_block(render_handoff_block(offer))

    assert parsed == offer
    assert parsed is not None and parsed.exhausted


def test_block_round_trips_a_note_with_a_fence_line() -> None:
    """A note line closing the fence escapes and restores."""
    offer = _offer(note="first\n-->\nlast")

    parsed = parse_handoff_block("prefix\n" + render_handoff_block(offer) + "\n")

    assert parsed is not None
    assert parsed.note == "first\n-->\nlast"


def test_block_ignores_unknown_fields() -> None:
    """An older reader skips what a newer writer adds."""
    block = render_handoff_block(_offer()) + ""
    injected = block.replace("note:\n", "future_field: yes\nnote:\n")

    parsed = parse_handoff_block(injected)

    assert parsed == _offer()


def test_section_carries_the_visible_account_and_the_block() -> None:
    """Operators read the summary; Workers parse the fence."""
    section = render_handoff_section(_offer())

    assert "Round Handoff" in section
    assert WORK in section
    assert "round-finished" in section
    assert "(0/2)" in section
    assert parse_handoff_block(section) == _offer()


def test_exhausted_section_says_so_plainly() -> None:
    """The last round publishes its note with the exhaustion reason."""
    section = render_handoff_section(
        _offer(used=2, exhausted=True, exhaustion_reason="pickup-limit-reached")
    )

    assert "exhausted" in section
    assert "pickup-limit-reached" in section
    assert parse_handoff_block(section) is not None


# --- strict parsing ------------------------------------------------------------------


def test_missing_block_reads_as_absent() -> None:
    """An ordinary terminal comment carries no offer."""
    assert parse_handoff_block("coding-agent implement#7/1: unfinished\n") is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda block: block.replace("round-handoff-record v1", "round-handoff-record v2"),
        lambda block: block.replace(f"work_commit: {WORK}", "work_commit: " + "a" * 39),
        lambda block: block.replace(f"work_commit: {WORK}", "work_commit: " + "A" * 40),
        lambda block: block.replace("used: 0", "used: -1"),
        lambda block: block.replace("limit: 2", "limit: 0"),
        lambda block: block.replace("note_kind: hard", "note_kind: soft"),
        lambda block: block.replace("exhausted: false", "exhausted: maybe"),
        lambda block: block.replace("issue: 7", "issue: seven"),
        lambda block: block.replace("pickup_label: round-finished\n", ""),
        lambda block: block.replace("note:\n", ""),
        lambda block: block + "extra line without colon\n",
        lambda block: block.replace("used: 0\n", "used: 0\nused: 1\n"),
        lambda block: block.rsplit("-->", 1)[0],
        lambda block: "garbage\n" + block,
    ],
)
def test_malformed_block_reads_as_absent(mutate: object) -> None:
    """A forged or truncated block never verifies."""
    assert callable(mutate)
    block = render_handoff_block(_offer())

    assert parse_handoff_block(mutate(block)) is None


def test_block_for_another_comment_is_ignored() -> None:
    """The fence must open the block, not merely appear in prose."""
    assert parse_handoff_block("see <!-- round-handoff-record v1 later") is None


# --- the bound --------------------------------------------------------------------------


def test_short_note_is_untouched() -> None:
    """A note within the bound keeps its bytes."""
    text, cut = truncate_note("hello")

    assert (text, cut) == ("hello", False)


def test_long_note_is_cut_with_a_marker() -> None:
    """A note past the bound is cut with an explicit marker."""
    text, cut = truncate_note("x" * (ROUND_HANDOFF_NOTE_MAX_CHARS + 1))

    assert cut is True
    assert len(text) > ROUND_HANDOFF_NOTE_MAX_CHARS
    assert "truncated" in text
    assert str(ROUND_HANDOFF_NOTE_MAX_CHARS) in text


def test_truncation_marker_names_the_artifact_pointer() -> None:
    """The marker points at the Step Artifact holding the full text."""
    text, _ = truncate_note("x" * (ROUND_HANDOFF_NOTE_MAX_CHARS + 1), pointer="ai.1.note")

    assert "ai.1.note" in text


# --- the hard note -----------------------------------------------------------------------


def test_hard_note_without_content_is_missing() -> None:
    """No summaries and no context is a missing note, never an empty offer."""
    assert hard_handoff_note([]) is None
    assert hard_handoff_note([], context=[]) is None
    assert hard_handoff_note([], context=["", ""]) is None


def test_hard_note_renders_summaries() -> None:
    """Changed files, commands, evidence, limits and spend all render."""
    note = hard_handoff_note(
        [
            SummaryInput(
                step_id="work",
                seq=1,
                facts={
                    "changed_files": ["src/a.py"],
                    "commands": [{"command": "pytest", "result": "ok"}],
                    "evidence": {"revision": WORK},
                    "limit": {"scope": "step", "reason": "context-threshold"},
                    "spend": {"cost_usd": 1.0},
                },
            )
        ],
        context=["crossed hard at 90000 tokens"],
    )

    assert note is not None
    assert "work" in note
    assert "src/a.py" in note
    assert "pytest" in note
    assert "context-threshold" in note
    assert "crossed hard" in note


def test_hard_note_is_bounded() -> None:
    """A huge summary still publishes a bounded note."""
    note = hard_handoff_note(
        [
            SummaryInput(
                step_id="work",
                seq=1,
                facts={"changed_files": [f"src/{index}.py" for index in range(10000)]},
            )
        ]
    )

    assert note is not None
    assert len(note) <= ROUND_HANDOFF_NOTE_MAX_CHARS + 500


# --- the decision ---------------------------------------------------------------------------


def test_decide_offers_a_valid_hard_round() -> None:
    """The happy path carries the exact label and the pinned evidence."""
    decided = decide_handoff(_preparation())

    assert decided == _offer(
        selection_actor="alice",
        selection_actor_id=123,
        selection_actor_type="User",
        selection_actor_at="2026-11-23T16:00:00+00:00",
        identity_mode="org-member",
    )


def test_decide_offers_a_valid_ai_assisted_round() -> None:
    """The annotation kind rides the offer."""
    decided = decide_handoff(
        _preparation(annotation_kind="ai-assisted", annotation_note="ai.handoff.note")
    )

    assert isinstance(decided, RoundHandoffOffer)
    assert decided.note_kind == "ai-assisted"


def test_decide_marks_the_exhausted_round() -> None:
    """The last permitted round publishes with the exhaustion reason."""
    decided = decide_handoff(_preparation(used=2))

    assert isinstance(decided, RoundHandoffOffer)
    assert decided.exhausted is True
    assert decided.exhaustion_reason == "pickup-limit-reached"
    assert decided.used == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "completed"},
        {"pickup_label": None, "limit": None},
        {"annotation_kind": None},
        {"annotation_kind": "soft"},
    ],
)
def test_decide_returns_no_handoff_off_the_publishing_path(
    fields: dict[str, object],
) -> None:
    """Disabled, unannotated and non-unfinished rounds stay ordinary."""
    assert decide_handoff(_preparation(**fields)) is None


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"push_landed": False}, "failed-push"),
        ({"note_text": None}, "missing-note"),
        ({"selection_actor": None}, "missing-authorisation"),
        ({"selection_actor_id": None}, "missing-authorisation"),
        ({"work_commit": None}, "unavailable-revision"),
        ({"work_commit": "abc"}, "unavailable-revision"),
        ({"ref": None}, "unavailable-revision"),
        ({"base_revision": None}, "unavailable-revision"),
        ({"remote_verified": False}, "unavailable-revision"),
    ],
)
def test_decide_diagnoses_an_ineligible_round(
    fields: dict[str, object], reason: str
) -> None:
    """A failed push, missing note or unavailable revision leaves no offer."""
    decided = decide_handoff(_preparation(**fields))

    assert isinstance(decided, HandoffDiagnostic)
    assert decided.reason == reason
    assert decided.reason in ROUND_HANDOFF_DIAGNOSTICS
    assert "no pickup" in render_diagnostic_section(decided)


def test_failed_push_names_the_landed_revision() -> None:
    """The diagnostic tells landed-but-unoffered work from never-landed work."""
    decided = decide_handoff(_preparation(push_landed=False))

    assert isinstance(decided, HandoffDiagnostic)
    assert WORK in decided.detail


def test_decide_refuses_a_nonsense_series() -> None:
    """Corrupt counts fail fast instead of publishing a corrupt offer."""
    with pytest.raises(ValueError):
        decide_handoff(_preparation(used=-1))
    with pytest.raises(ValueError):
        decide_handoff(_preparation(limit=0))
    with pytest.raises(ValueError):
        decide_handoff(_preparation(issue=0))
