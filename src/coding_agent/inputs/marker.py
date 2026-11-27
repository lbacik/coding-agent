"""A structured Attempt Marker: the vendor-neutral Effect Intent key carrier.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14.1 (every
external write carries an Effect Intent key ``(Attempt, step, n, ordinal)``
plus the write kind in its Attempt Marker; lookups find the write back by
that key, filtered by the Agent Identity).

The marker travels as a value across every write and lookup
(``post_comment``, ``open_pull_request``, ``publish_review``,
``post_claim_comment``, ``find_comment_by_marker``,
``find_review_by_marker``, ``find_pull_request``). Each adapter decides how
to embed it and how to find it: the GitHub adapter renders today's HTML
comment line, a second vendor stores the fields in a property. Empty step
and zero execution number remain the reserved Claim/finalisation
convention. No behavior, only data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttemptMarker:
    """The structured, vendor-neutral Attempt Marker (§14.1).

    ``attempt`` is the Attempt id (``<workflow>#<N>/<n>``); ``kind`` is the
    write kind (``claim``, ``open-pull-request``, ``publish-review``,
    ``post-comment``, ``terminal``); ``step`` is the publishing step id
    (``""`` for Claim and terminal writes); ``execution_number`` is the
    Step Execution number (``0`` for Claim and terminal writes);
    ``ordinal`` is the write ordinal within the execution.
    """

    attempt: str
    kind: str
    step: str
    execution_number: int
    ordinal: int


__all__ = [
    "AttemptMarker",
]
