"""The Attempt Id value type: the single owner of the Attempt Id format.

Glossary (CONTEXT.md, "Attempts and their durable record"): an **Attempt
Id** is ``<workflow>#<N>/<n>``; a **Legacy Attempt Id** is the MVP
``#<N>/<n>`` with no Workflow; anything else is not an Attempt Id and
never identifies an Attempt.
"""

from __future__ import annotations

import pytest

from coding_agent.attempt_id import AttemptId


def test_table_step_legacy_and_malformed_ids() -> None:
    """Every id shape parses in exactly one module, into exactly one case."""
    rows: list[tuple[str, str | None, int | None, int | None]] = [
        # (raw, workflow, input_number, sequence); workflow None with
        # numbers set is a Legacy Attempt Id, all-None is malformed.
        ("implement#44/1", "implement", 44, 1),
        ("#44/1", None, 44, 1),
        ("44/1", None, None, None),
        ("44", None, None, None),
        ("implement#44", None, None, None),
        ("#44/x", None, None, None),
        ("#0/1", None, None, None),
        ("#1/0", None, None, None),
        ("#01/1", None, None, None),
        ("a#b#3/1", None, None, None),
        ("X#1/1", None, None, None),
        ("", None, None, None),
        (" #1/1", None, None, None),
        ("garbage", None, None, None),
    ]
    for raw, workflow, input_number, sequence in rows:
        parsed = AttemptId.try_parse(raw)
        if input_number is None or sequence is None:
            assert parsed is None, f"{raw!r} must be malformed"
            with pytest.raises(ValueError):
                AttemptId.parse(raw)
            continue
        assert parsed is not None, f"{raw!r} must parse"
        assert parsed.workflow == workflow
        assert parsed.input_number == input_number
        assert parsed.sequence == sequence
        assert parsed.is_legacy == (workflow is None)
        assert str(AttemptId.parse(raw)) == raw
        assert str(parsed) == raw


def test_branch_names_the_step_branch_and_refuses_legacy_ids() -> None:
    """``branch()`` derives the branch for step ids and raises for legacy."""
    assert (
        AttemptId("implement", 44, 1).branch("Add a Greeting!")
        == "agent/implement/44/1-add-a-greeting"
    )
    with pytest.raises(ValueError):
        AttemptId(None, 44, 1).branch("Add a Greeting!")
    with pytest.raises(ValueError):
        AttemptId.parse("#44/1").branch("Add a Greeting!")


def test_constructor_validates_against_the_grammar() -> None:
    """Constructed values always round-trip: no zeros, no bad workflows."""
    assert str(AttemptId("implement", 1, 1)) == "implement#1/1"
    assert str(AttemptId(None, 44, 2)) == "#44/2"
    with pytest.raises(ValueError):
        AttemptId("implement", 0, 1)
    with pytest.raises(ValueError):
        AttemptId("implement", 1, 0)
    with pytest.raises(ValueError):
        AttemptId("a#b", 1, 1)
    with pytest.raises(ValueError):
        AttemptId("", 1, 1)
    with pytest.raises(ValueError):
        AttemptId("Implement", 1, 1)
