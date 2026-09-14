from __future__ import annotations

import re

_SEAM_VERBS = (
    "is",
    "are",
    "does",
    "performs",
    "wires",
    "takes",
    "raises",
    "returns",
    "resolves",
    "confirms",
    "asserts",
)

# This repository's own acceptance-criteria convention *is* the text test:
# a checklist item about a symbol opens with that symbol in backticks,
# immediately followed by a verb describing it ("`derive_seam_set` is a
# text test...", "`confirm_seam` performs..."). That position-plus-verb
# shape is what tells a seam apart from an incidental backtick-quoted
# mention elsewhere in the body — a shell command, a row id, a path cited
# only for context — without needing a model to tell the two apart.
_CHECKLIST_ITEM_SEAM = re.compile(
    r"(?m)^[ \t]*(?:[-*]|\d+\.)[ \t]*(?:\[[ xX]\][ \t]*)?"
    r"`([^`\n]+)`"
    r"[ \t]+(?:" + "|".join(_SEAM_VERBS) + r")\b"
)

# The title carries no such convention to key off, but it is a single short
# line a human wrote to name the task -- not free-form prose accumulating
# incidental references the way a body is -- so any symbol it backtick-quotes
# ("Fix `parse_receipt` off-by-one") is trusted outright.
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def derive_seam_set(title: str, body: str) -> tuple[str, ...]:
    """ADR 0008's Seam Set as a Readiness Fact: a text test over the Target
    Issue's title and body, decidable without any model call. Returns every
    distinct public symbol, path or endpoint the issue names -- backtick-quoted
    anywhere in the title, or as the subject of an acceptance-criteria item in
    the body -- in the order first seen; the empty tuple where it names none,
    which is a missing Readiness Fact."""
    seen: dict[str, None] = {}

    def record(name: str) -> None:
        name = name.strip()
        if name and name not in seen:
            seen[name] = None

    for match in _BACKTICKED.finditer(title):
        record(match.group(1))
    for match in _CHECKLIST_ITEM_SEAM.finditer(body):
        record(match.group(1))

    return tuple(seen)


class UnconfirmedSeam(Exception):
    """ADR 0008, `L3-IMP-11`: `confirm_seam` entered with no Seam Set in
    state. A correct run never reaches this — the caller checks
    `derive_seam_set`'s result before calling `confirm_seam` — so raising
    here is an internal invariant violation, never a request for
    clarification."""


def confirm_seam(seam_set: tuple[str, ...]) -> tuple[str, ...]:
    """Performs no model call and takes no branch a correct run can reach
    when a Seam Set is present: it asserts `seam_set` is non-empty and
    hands it back unchanged. Refuses — raises `UnconfirmedSeam` — rather
    than asking, when it is not."""
    if not seam_set:
        raise UnconfirmedSeam("confirm_seam entered with no Seam Set in state")
    return seam_set
