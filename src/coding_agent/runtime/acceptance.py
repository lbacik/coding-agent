"""Acceptance Criteria parsed from the Target Issue body.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (Readiness Facts:
Acceptance Criteria are enumerated from the Target Issue body by the §66
text convention — exactly one level-2 heading whose normalised text is
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

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*)$")
_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<marker>(?:[-*+])|(?:\d+[.)]))[ \t]+"
    r"\[(?P<check>[ xX])\][ \t]*(?P<text>.*)$"
)
_TRAILING_HASHES = re.compile(r"\s+#+\s*$")


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One Acceptance Criterion: its ``AC-N`` id and its text."""

    id: str
    text: str


def _normalise_heading(text: str) -> str:
    stripped = _TRAILING_HASHES.sub("", text.strip())
    return " ".join(stripped.split()).lower()


def _indent_width(indent: str) -> int:
    return len(indent.replace("\t", "    "))


def parse_acceptance_criteria(body: str) -> tuple[AcceptanceCriterion, ...]:
    """Enumerate the Acceptance Criteria declared by a Target Issue body.

    Returns one ``AcceptanceCriterion`` per non-empty direct checklist item
    under the single ``## Acceptance criteria`` section, numbered ``AC-1…``
    in source order. Returns the empty tuple where no section, more than
    one section, or a section without direct checklist items yields no
    criteria. Decidable with no model call.
    """
    lines = body.splitlines()
    sections: list[int] = []
    levels: list[int] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is None:
            continue
        levels.append(len(match.group("hashes")))
        if len(match.group("hashes")) == 2 and _normalise_heading(match.group("text")) == "acceptance criteria":
            sections.append(index)
    if len(sections) != 1:
        return ()
    start = sections[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        match = _HEADING.match(lines[index])
        if match is not None and len(match.group("hashes")) <= 2:
            end = index
            break
    region = lines[start:end]

    buckets: list[list[str]] = []
    for line in region:
        match = _ITEM.match(line)
        if match is not None:
            if _indent_width(match.group("indent")) <= 1:
                buckets.append([match.group("text").strip()])
            elif buckets:
                nested = match.group("text").strip()
                if nested:
                    buckets[-1].append(nested)
            continue
        if line.strip() == "":
            continue
        if buckets:
            buckets[-1].append(line.strip())

    criteria: list[AcceptanceCriterion] = []
    for position, parts in enumerate(buckets, start=1):
        text = " ".join(part for part in parts if part).strip()
        if not text:
            continue
        criteria.append(AcceptanceCriterion(id=f"AC-{position}", text=text))
    if not criteria:
        return ()
    renumbered = tuple(
        AcceptanceCriterion(id=f"AC-{number}", text=criterion.text)
        for number, criterion in enumerate(criteria, start=1)
    )
    return renumbered
