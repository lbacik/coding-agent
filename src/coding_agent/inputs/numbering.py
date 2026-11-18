"""Attempt identity and ``n`` rebuilt after a ledger loss (§6.3, §16.5).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §6.3 (an
Attempt is ``(workflow-id, input number, n)``, rendered
``<workflow>#<N>/<n>``; ``n`` counts per (Workflow, input) from the Run
Ledger, one more than the highest recorded ``n``; numbers are never reused,
not even after a ``released`` Claim; the Workflow id appears in every
Attempt Marker, in the commit trailer and, for an issue input, in the
Attempt branch ``agent/<workflow>/<issue>/<n>-<slug>``) and §16.5 (after a
ledger loss ``n`` is rebuilt from the Attempt branches and the Attempt
Markers on GitHub, so no branch or marker collides).

Row ids: INP-13, REC-22.

This module is pure: it formats Attempt ids, branches and markers and
takes the maximum over the ledger's ``n``, the branch names and the
markers. Attempt Id strings parse in
:mod:`coding_agent.attempt_id` and only there; this module builds them
from parts and reads them back as :class:`AttemptId` values. The caller
reads those sources; nothing here touches the network, the working tree
or the ledger.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from coding_agent.attempt_id import AttemptId

_BRANCH_RE = re.compile(
    r"^agent/(?P<workflow>[a-z0-9]+(?:-[a-z0-9]+)*)/(?P<number>\d+)/(?P<n>\d+)-"
)
_MARKER_RE = re.compile(r"attempt=(?P<workflow>\S+?)#(?P<number>\d+)/(?P<n>\d+)")


def attempt_id(workflow_id: str, input_number: int, n: int) -> str:
    """The Attempt id: ``<workflow>#<N>/<n>`` (§6.3)."""
    return str(AttemptId(workflow_id, input_number, n))


def attempt_branch(
    workflow_id: str, input_number: int, n: int, input_title: str
) -> str:
    """The Attempt branch for an issue input (§6.3, §17).

    ``agent/<workflow>/<issue>/<n>-<slug>``: one branch per Attempt, never
    force-pushed and never deleted.
    """
    return AttemptId(workflow_id, input_number, n).branch(input_title)


def commit_trailer(attempt: AttemptId) -> str:
    """The Attempt id as a commit trailer (§6.3, §14.4)."""
    return f"Attempt: {attempt}"


def parse_attempt_branch(branch: str) -> tuple[str, int, int] | None:
    """Split ``agent/<workflow>/<issue>/<n>-<slug>`` into its parts, or ``None``."""
    match = _BRANCH_RE.match(branch)
    if match is None:
        return None
    return (match.group("workflow"), int(match.group("number")), int(match.group("n")))


def parse_attempt_marker(text: str) -> AttemptId | None:
    """Split the ``attempt=<workflow>#<N>/<n>`` of an Attempt Marker, or ``None``.

    The first marker in the text wins; a body without one is not ours (§14.4:
    the rule reads in one direction only — no marker means not ours). The
    marker parts parse as an Attempt Id, so a marker whose parts are not
    an Attempt Id is not ours either.
    """
    match = _MARKER_RE.search(text)
    if match is None:
        return None
    raw = f"{match.group('workflow')}#{match.group('number')}/{match.group('n')}"
    return AttemptId.try_parse(raw)


def highest_n(
    workflow_id: str,
    input_number: int,
    candidates: Iterable[AttemptId | tuple[str, int, int] | None],
) -> int:
    """The highest ``n`` for a (Workflow, input) among parsed sources."""
    highest = 0
    for parsed in candidates:
        if parsed is None:
            continue
        if isinstance(parsed, AttemptId):
            if parsed.workflow is None:
                continue
            source_workflow, source_number, source_n = (
                parsed.workflow,
                parsed.input_number,
                parsed.sequence,
            )
        else:
            source_workflow, source_number, source_n = parsed
        if source_workflow == workflow_id and source_number == input_number:
            highest = max(highest, source_n)
    return highest


def next_n(
    workflow_id: str,
    input_number: int,
    *,
    ledger_n: int = 0,
    branches: Iterable[str] = (),
    markers: Iterable[str] = (),
) -> int:
    """The next ``n`` for a (Workflow, input) over every source (§16.5).

    One more than the highest of the ledger's ``n``, the Attempt branches
    and the Attempt Markers on GitHub — so after a ledger loss no branch
    or marker collides (REC-22). The caller reads the branches (the working
    tree or the remote) and the markers (GitHub); nothing here does. Only
    step Attempt ids count: finished Legacy Attempt Ids remain history
    "for numbering" but repair ignores them.
    """
    from_branches = highest_n(
        workflow_id, input_number, (parse_attempt_branch(name) for name in branches)
    )
    from_markers = highest_n(
        workflow_id, input_number, (parse_attempt_marker(text) for text in markers)
    )
    return max(ledger_n, from_branches, from_markers) + 1
