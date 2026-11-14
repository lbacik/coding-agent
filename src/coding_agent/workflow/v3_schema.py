"""Typed `version: 3` Workflow Configuration objects.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the file,
the Workflow keys, steps, results, payload types, inputs, operation
parameters) and the skills-declaration resolution behind issue #372 (a
configuration-wide ``skills`` declaration names the Skill Bundle membership:
the exact skill name -> source entry and optional ref).

A `version: 3` file is a `version: 2` file plus the optional configuration-wide
``skills`` block: a list of ``{source, include, ref?}`` entries. Everything
else — target, models, worker, workflows — keeps the `version: 2` shape and
meaning, so this module reuses the ``V2*`` types for those blocks and only
adds the skills declaration. These dataclasses are the typed shape the
strict v3 loader (``coding_agent.workflow.v3_validate``) returns. Frozen so
that a value, once loaded, is the pin — a later edit to the YAML cannot reach
back and change an open Attempt's own configuration (§2.10).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coding_agent.workflow.v2_schema import (
    V2ModelDecl,
    V2Target,
    V2Worker,
    V2Workflow,
)


@dataclass(frozen=True)
class V3SkillDecl:
    """One ``skills`` entry: where its members come from (§2.1, SKV3-02).

    ``source`` is the normalised installer Source Identity (always the
    ``git+https://`` spelling): both spellings of one URL normalise to it,
    and there is no equivalence by redirect, ``.git`` suffix or case.
    ``include`` names the exact member skills (case, spaces and underscores
    preserved — never trimmed, folded or matched against a Workflow-id
    regex). ``ref`` pins the entry (a branch, tag or commit); ``None`` floats
    on the source's default.
    """

    source: str
    include: tuple[str, ...]
    ref: str | None = None


@dataclass(frozen=True)
class V3Config:
    """A validated `version: 3` Workflow Configuration file (§2.1–§2.3).

    Structural shape only at the type level; reference, trust, environment
    and graph checks live in `coding_agent.workflow.v3_validate` and run
    before one of these is returned. A missing ``skills`` field and
    ``skills: []`` both mean an empty Skill Bundle: there is no implicit
    four-skill default (SKV3-04, static part).
    """

    version: int
    target: V2Target
    models: dict[str, V2ModelDecl]
    worker: V2Worker
    workflows: dict[str, V2Workflow]
    skills: tuple[V3SkillDecl, ...]
    source: Path


def skill_membership(config: V3Config) -> dict[str, tuple[str, str | None]]:
    """The normalised membership: exact skill name -> (source, ref).

    This is what static ``instructions`` and on-demand availability consume:
    the exact declared name maps to its source entry's normalised Source
    Identity and optional ref. Names are exact strings — lookup never trims,
    folds or pattern-matches them.
    """
    membership: dict[str, tuple[str, str | None]] = {}
    for entry in config.skills:
        for name in entry.include:
            membership[name] = (entry.source, entry.ref)
    return membership


def normalize_skill_source(raw: str) -> str:
    """Normalise a ``skills`` entry's ``source`` to its Source Identity.

    Accepts HTTPS Git only, in the ``https://`` or ``git+https://`` spelling;
    the normalised form always carries the ``git+`` prefix (the installer's
    Source Identity). Anything else — SSH, local directories, other schemes —
    and any user-info, query or fragment component is refused with
    ``ValueError``.

    Static and offline: no subprocess runs and nothing is logged. Diagnostics
    never echo the raw value, so a credential-bearing component (user-info)
    cannot leak through an error message.
    """
    from urllib.parse import urlsplit

    if not isinstance(raw, str) or not raw:
        raise ValueError("skill source must be a non-empty string")
    if raw.startswith("git+https://"):
        https_part = raw[len("git+") :]
    elif raw.startswith("https://"):
        https_part = raw
    else:
        raise ValueError(
            "skill source must use 'https://' or 'git+https://' (HTTPS Git only)"
        )
    # Presence, not truthiness: an empty but present component
    # (`https://host/repo?`, `https://host/repo#`, `https://@host/repo`)
    # is still a query, fragment or user-info component (SKV3-03).
    if "#" in https_part:
        raise ValueError("skill source must not include a fragment component")
    if "?" in https_part:
        raise ValueError("skill source must not include a query component")
    try:
        parts = urlsplit(https_part)
    except ValueError as exc:
        raise ValueError("skill source does not parse") from exc
    if "@" in parts.netloc:
        raise ValueError("skill source must not include userinfo")
    host = parts.hostname or ""
    if not host:
        raise ValueError("skill source must include a host")
    if parts.path in ("", "/"):
        raise ValueError("skill source must include a repository path")
    return f"git+{https_part}"
