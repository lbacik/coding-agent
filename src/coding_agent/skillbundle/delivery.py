"""Deterministic skill activation deliveries (SKV3-17).

Contract: an activation delivers the complete ``SKILL.md``, then each
reviewed ``inject`` Companion File whole in policy order, with
operator-authored notices framed outside the upstream bytes (ADR 0007):
upstream bytes are only concatenated, never edited, and no notice is ever
spliced into them. ``not-delivered`` bodies appear in no delivery and no
replay. Static, on-demand and Context-Window Handoff replay deliveries are
byte-identical for the same member because every path serves the one
prebuilt delivery held by the activation registry.

The framing carries :data:`DELIVERY_FRAMING_VERSION` in every separator it
authors, so a future framing change is visible as a version change; golden
byte vectors freeze the framing, the component order and the notices.

This module is pure: it takes verified in-memory bytes and reviewed policy
records, and performs no filesystem or network access. There is no
skill-resource reader and no implicit fetch on this path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from coding_agent.skillbundle.policy import PolicyReview

DELIVERY_FRAMING_VERSION = 1
"""Version of the operator-authored delivery framing.

Every separator this module authors names this version, so the
representation a member delivers is self-describing and a future framing
change cannot pass silently.
"""


def companion_heading(name: str, path: str) -> str:
    """The operator-authored separator opening one ``inject`` companion."""
    return f"----- skill-delivery v{DELIVERY_FRAMING_VERSION}: companion {name}/{path} -----"


def notices_heading(name: str) -> str:
    """The operator-authored separator opening the notices section."""
    return f"----- skill-delivery v{DELIVERY_FRAMING_VERSION}: operator notices for {name} -----"


def compose_delivery(
    *,
    name: str,
    skill_md: str,
    companions: Sequence[tuple[str, str]] = (),
    notices: Sequence[tuple[str, str]] = (),
) -> str:
    """Compose one member's activation delivery deterministically.

    ``skill_md`` is the complete ``SKILL.md`` text verbatim and first.
    ``companions`` are ``(path, text)`` pairs — each companion whole and
    verbatim, in policy order — framed by operator-authored separators that
    carry the framing version. ``notices`` are ``(target, notice)`` pairs,
    in policy order, rendered after every companion under their own
    versioned separator, outside the upstream bytes. Where there are no
    companions or no notices, that section is absent rather than empty.
    """
    parts: list[str] = [skill_md]
    for path, text in companions:
        parts.append(f"{companion_heading(name, path)}\n{text}")
    if notices:
        rendered = "\n".join(f"- {target}: {notice}" for target, notice in notices)
        parts.append(f"{notices_heading(name)}\n{rendered}")
    return "\n\n".join(parts)


def delivery_for_review(
    *,
    name: str,
    skill_md_text: str,
    contents: Mapping[str, bytes],
    review: PolicyReview,
) -> str:
    """Compose the delivery for one reviewed member from verified bytes.

    ``inject`` companions are taken in ``review.files`` (policy) order and
    decoded as UTF-8; notices come from the ``review.references`` rulings
    with classification ``unavailable-nonrequired-reference`` in policy
    order. ``not-delivered`` files are never consulted. Raises
    ``UnicodeDecodeError`` where a delivered file is not valid UTF-8; the
    caller turns that into a binding refusal.
    """
    companions: list[tuple[str, str]] = []
    for item in review.files:
        if item.disposition != "inject":
            continue
        companions.append((item.path, contents[item.path].decode("utf-8")))
    rendered_notices: list[tuple[str, str]] = []
    for reference in review.references:
        if reference.classification != "unavailable-nonrequired-reference":
            continue
        assert reference.target is not None and reference.notice is not None
        rendered_notices.append((reference.target, reference.notice))
    return compose_delivery(
        name=name,
        skill_md=skill_md_text,
        companions=tuple(companions),
        notices=tuple(rendered_notices),
    )
