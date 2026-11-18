"""The Attempt Id value type: the single owner of the Attempt Id format.

Glossary (CONTEXT.md, "Attempts and their durable record"):

- An **Attempt Id** is the canonical text form of an Attempt's identity:
  its Workflow, its input number and its sequence number, written
  ``<workflow>#<N>/<n>``.
- A **Legacy Attempt Id** is an Attempt Id written by the MVP runtime,
  ``#<N>/<n>``, with no Workflow. It identifies only finished history,
  or an unfinished MVP Attempt, which refuses startup; no Attempt is
  opened under one.
- A string that has neither form is not an Attempt Id and never
  identifies an Attempt.

Every Attempt Id string in the inputs, operator and ledger layers parses
here and only here. The grammar is strict — no leading zeros, ``N`` and
``n`` both at least 1, no surrounding whitespace — so for every string
that parses, ``str(AttemptId.parse(s)) == s``.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §6.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ATTEMPT_ID_RE = re.compile(
    r"^(?:(?P<workflow>[a-z0-9]+(?:-[a-z0-9]+)*))?#(?P<N>[1-9]\d*)/(?P<n>[1-9]\d*)$"
)
"""The Attempt Id grammar: an optional Workflow id, then ``#<N>/<n>``.

The Workflow segment matches a Workflow id (``[a-z0-9]+(-[a-z0-9]+)*``);
``N`` and ``n`` have no leading zeros and are both at least 1.
"""

_WORKFLOW_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""A Workflow id on its own: lowercase alphanumerics, hyphens inside only."""


@dataclass(frozen=True)
class AttemptId:
    """One parsed Attempt Id: its Workflow, input number and sequence number.

    A step Attempt Id carries a Workflow (``implement#44/1``); a Legacy
    Attempt Id carries none (``#44/1``, ``workflow is None``). The
    constructor validates its values against the same grammar
    :func:`parse` enforces, so a constructed value always round-trips
    through :func:`str`.
    """

    workflow: str | None
    input_number: int
    sequence: int

    def __post_init__(self) -> None:
        """Reject values the Attempt Id grammar rejects."""
        if self.workflow is not None:
            if not isinstance(self.workflow, str) or _WORKFLOW_RE.match(self.workflow) is None:
                raise ValueError(f"malformed Attempt id workflow {self.workflow!r}")
        for name in ("input_number", "sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"malformed Attempt id {name} {value!r}")

    @classmethod
    def parse(cls, raw: str) -> AttemptId:
        """Parse ``raw`` as an Attempt Id, raising :class:`ValueError` where malformed.

        A malformed string is never a Legacy Attempt Id: only a string
        with the ``#<N>/<n>`` shape and no Workflow segment is legacy.
        """
        if not isinstance(raw, str):
            raise ValueError(f"malformed Attempt id {raw!r}")
        match = _ATTEMPT_ID_RE.match(raw)
        if match is None:
            raise ValueError(f"malformed Attempt id {raw!r}")
        return cls(
            workflow=match.group("workflow"),
            input_number=int(match.group("N")),
            sequence=int(match.group("n")),
        )

    @classmethod
    def try_parse(cls, raw: str) -> AttemptId | None:
        """Parse ``raw`` as an Attempt Id, or ``None`` where malformed."""
        try:
            return cls.parse(raw)
        except ValueError:
            return None

    @property
    def is_legacy(self) -> bool:
        """Whether this is a Legacy Attempt Id: no Workflow segment."""
        return self.workflow is None

    def branch(self, title: str) -> str:
        """The Attempt branch for an issue input: ``agent/<workflow>/<N>/<n>-<slug>``.

        Raises :class:`ValueError` for a Legacy Attempt Id: a Legacy
        Attempt Id has no Workflow, so it names no branch.
        """
        if self.workflow is None:
            raise ValueError(f"a Legacy Attempt Id names no branch: {self!s}")
        from coding_agent.identity.commit_identity import slugify

        return (
            f"agent/{self.workflow}/{self.input_number}/{self.sequence}-{slugify(title)}"
        )

    def __str__(self) -> str:
        """The canonical Attempt Id string: ``<workflow>#<N>/<n>``."""
        if self.workflow is None:
            return f"#{self.input_number}/{self.sequence}"
        return f"{self.workflow}#{self.input_number}/{self.sequence}"
