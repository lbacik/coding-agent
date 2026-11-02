"""Acceptance Criteria parsing from the Target Issue body.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (Acceptance
Criteria are enumerated from the Target Issue body by the §66 text
convention: exactly one level-2 heading whose normalised text is
``Acceptance criteria``, case-insensitively, reading its non-empty direct
checklist items up to the next heading of the same or higher level; each
direct item with its continued lines and nested content is one criterion;
nested checklist items never create criteria; a checked box still declares
one; the criteria are numbered ``AC-1``, ``AC-2``, … in source order; no
section, more than one, or a section without direct checklist items yields
no criteria).

MVP row ids: P-RDY-1.
"""

from __future__ import annotations

from coding_agent.runtime.acceptance import parse_acceptance_criteria


def test_no_section_yields_no_criteria() -> None:
    assert parse_acceptance_criteria("Just a title and some prose.\n") == ()


def test_two_sections_yield_no_criteria() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] first\n"
        "## Acceptance criteria\n"
        "- [ ] second\n"
    )
    assert parse_acceptance_criteria(body) == ()


def test_section_without_direct_checklist_items_yields_no_criteria() -> None:
    body = "## Acceptance criteria\n\nSome prose, no checklist.\n"
    assert parse_acceptance_criteria(body) == ()


def test_section_with_only_nested_items_yields_no_criteria() -> None:
    body = "## Acceptance criteria\n  - [ ] `seam` is nested\n"
    assert parse_acceptance_criteria(body) == ()


def test_section_with_only_plain_bullets_yields_no_criteria() -> None:
    body = "## Acceptance criteria\n- not a checklist item\n"
    assert parse_acceptance_criteria(body) == ()


def test_heading_match_is_case_insensitive() -> None:
    body = "## ACCEPTANCE CRITERIA\n- [ ] `greet` returns a greeting\n"
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1"]


def test_items_number_in_source_order() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] `alpha` does one thing\n"
        "- [ ] `beta` does another\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert [(criterion.id, criterion.text) for criterion in criteria] == [
        ("AC-1", "`alpha` does one thing"),
        ("AC-2", "`beta` does another"),
    ]


def test_reading_stops_at_the_next_level_two_heading() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] `alpha` does one thing\n"
        "## Notes\n"
        "- [ ] `beta` is not a criterion\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1"]


def test_reading_stops_at_a_level_one_heading() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] `alpha` does one thing\n"
        "# Notes\n"
        "- [ ] `beta` is not a criterion\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1"]


def test_a_level_three_heading_does_not_end_the_section() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] `alpha` does one thing\n"
        "### Detail\n"
        "- [ ] `beta` does another\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1", "AC-2"]


def test_nested_items_never_become_criteria() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] `alpha` does one thing\n"
        "  - [ ] `beta` is nested detail\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1"]


def test_checked_boxes_still_declare_a_criterion() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [x] `alpha` is already done\n"
        "- [X] `beta` is also done\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1", "AC-2"]


def test_multiline_items_join_their_continued_lines() -> None:
    body = (
        "## Acceptance criteria\n"
        "- [ ] `alpha` does one thing\n"
        "  with a continued line\n"
    )
    criteria = parse_acceptance_criteria(body)
    assert len(criteria) == 1
    assert "continued line" in criteria[0].text


def test_empty_items_are_skipped() -> None:
    body = "## Acceptance criteria\n- [ ]\n- [ ] `alpha` does one thing\n"
    criteria = parse_acceptance_criteria(body)
    assert [criterion.id for criterion in criteria] == ["AC-1"]
    assert "`alpha`" in criteria[0].text
