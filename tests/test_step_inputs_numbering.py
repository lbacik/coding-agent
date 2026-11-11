"""Attempt identity and ``n`` rebuilt after a ledger loss (§6.3, §16.5).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §6.3 (the
``<workflow>#<N>/<n>`` identity, the Attempt branch and the marker) and
§16.5 (``n`` rebuilt from the Attempt branches and markers on GitHub).

Row ids: INP-13, REC-22.
"""

from __future__ import annotations

from coding_agent.inputs.numbering import (
    attempt_branch,
    attempt_id,
    commit_trailer,
    next_n,
    parse_attempt_branch,
    parse_attempt_id,
    parse_attempt_marker,
)


def test_attempt_id_format() -> None:
    """INP-13: ``<workflow>#<N>/<n>``; the type is not part of the id."""
    assert attempt_id("implement", 42, 1) == "implement#42/1"
    assert attempt_id("review", 42, 1) == "review#42/1"


def test_attempt_branch_format() -> None:
    """INP-13: ``agent/<workflow>/<issue>/<n>-<slug>``."""
    assert attempt_branch("implement", 42, 2, "Add a Greeting!") == (
        "agent/implement/42/2-add-a-greeting"
    )


def test_commit_trailer_carries_the_attempt_id() -> None:
    """§6.3: the Workflow id appears in the commit trailer."""
    assert commit_trailer("implement", 42, 2) == "Attempt: implement#42/2"


def test_branch_and_marker_round_trip() -> None:
    """§16.5: branches and markers parse back to (workflow, number, n)."""
    assert parse_attempt_branch("agent/implement/42/2-add-a-greeting") == (
        "implement",
        42,
        2,
    )
    assert parse_attempt_branch("agent/implement/42-x") is None
    assert parse_attempt_branch("feature/something") is None
    assert parse_attempt_marker(
        "<!-- coding-agent attempt=implement#42/2 kind=claim step=- n=0 ordinal=1 -->"
    ) == ("implement", 42, 2)
    assert parse_attempt_marker("no marker here") is None
    assert parse_attempt_id("review#42/1") == ("review", 42, 1)


def test_next_n_counts_per_workflow_and_input() -> None:
    """INP-13: ``n`` counts per (Workflow, input); numbers are never reused."""
    assert next_n("implement", 42, ledger_n=2) == 3
    assert next_n("review", 42, ledger_n=0) == 1


def test_next_n_rebuilt_from_branches_and_markers_after_ledger_loss() -> None:
    """REC-22: after a ledger loss ``n`` is rebuilt so nothing collides."""
    branches = [
        "agent/implement/42/1-add-a-greeting",
        "agent/implement/42/2-more-work",
        "agent/review/42/1-look",
    ]
    markers = [
        "<!-- coding-agent attempt=implement#42/3 kind=terminal step=- n=0 ordinal=0 -->",
        "<!-- coding-agent attempt=implement#7/1 kind=terminal step=- n=0 ordinal=0 -->",
    ]
    assert (
        next_n("implement", 42, ledger_n=0, branches=branches, markers=markers) == 4
    )
    assert next_n("review", 42, ledger_n=0, branches=branches, markers=markers) == 2
    assert next_n("implement", 7, ledger_n=0, branches=branches, markers=markers) == 2
