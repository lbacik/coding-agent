"""Profile readiness: the Project Profile alone decides.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (the Project Profile
is the only Target-Project-owned input; there is no prose fallback —
``AGENTS.md``, ``CLAUDE.md`` and ``docs/agents/*`` are never read for
Readiness Facts; an absent profile is a missing Readiness Fact; the profile
is read at the Base Revision and pinned as the Validation Contract).

MVP row ids: P-RDY-1, P-RDY-2, P-RDY-3, C-RDY-1, C-RDY-2, L2-12, L2-13,
L2-14.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from coding_agent.profile.parser import (
    SUPPORTED_SCHEMA,
    InvalidProfile,
    MissingReadinessFacts,
    UnknownSchema,
    parse_profile_yaml,
)
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber, check_service
from coding_agent.profile.toolchain import SupportedToolchainMatrix, assert_toolchain


@dataclass(frozen=True)
class ProfileSources:
    """Where readiness for one Attempt is read from (contract §7): the
    canonical `docs/agents/project-profile.yml` at the Base Revision. There
    is no prose fallback: no other file is ever consulted for a Readiness
    Fact (C-RDY-2).
    """

    profile_yaml_text: str | None
    """`docs/agents/project-profile.yml`'s content at the Base Revision, or
    `None` if the Target Repository carries no such file at that revision."""
    sources_searched: tuple[str, ...] = ()
    """Every location consulted, named in the refusal reason so a human
    knows where to add the missing fact."""


@dataclass(frozen=True)
class Ready:
    profile: ProjectProfile


@dataclass(frozen=True)
class NeedsClarification:
    """Retained for compatibility: superseded by ``MissingReadinessFacts``.

    The MVP ends a missing Readiness Fact terminally as ``failed`` /
    ``missing-readiness-fact`` with no model call (P-RDY-1, P-RDY-2,
    C-RDY-1, C-RDY-2) instead of clarifying. New code returns
    ``MissingReadinessFacts`` from the parser directly.
    """

    missing: tuple[str, ...]
    sources_searched: tuple[str, ...]


UnsupportedEnvironmentReason = Literal["unknown-schema", "toolchain-mismatch", "unreachable-service"]


@dataclass(frozen=True)
class UnsupportedEnvironment:
    """Contract §7's deployment failure route: the profile is complete and
    correct, but this image cannot satisfy it — a toolchain outside the
    Supported Toolchain Matrix (L2-12), an unknown `schema` version (L2-13),
    or an unreachable declared service (L2-14)."""

    reason: UnsupportedEnvironmentReason
    detail: str


EnvironmentReadiness = Ready | MissingReadinessFacts | InvalidProfile | UnsupportedEnvironment


def evaluate_readiness(
    sources: ProfileSources,
    matrix: SupportedToolchainMatrix,
    service_env: Mapping[str, str],
    prober: ServiceProber,
) -> EnvironmentReadiness:
    """The readiness decision contract §7 describes, up to (never including)
    implementation: parse the profile, assert the toolchain, probe declared
    services — all before any command the profile declares is ever run, and
    with no model call.

    There is no prose fallback (C-RDY-2): ``AGENTS.md``, ``CLAUDE.md`` and
    ``docs/agents/*`` are never read here. ``profile_yaml_text=None`` is a
    missing Readiness Fact, returned as ``MissingReadinessFacts`` naming the
    profile. Content that does not parse or violates the schema is
    ``InvalidProfile`` (P-RDY-3); an unknown ``schema`` is
    ``UnsupportedEnvironment`` (L2-13), never a missing fact.
    """
    if sources.profile_yaml_text is None:
        return MissingReadinessFacts(
            ("docs/agents/project-profile.yml: no Project Profile at the Base Revision",)
        )

    outcome = parse_profile_yaml(sources.profile_yaml_text)

    if isinstance(outcome, InvalidProfile):
        return outcome
    if isinstance(outcome, UnknownSchema):
        return UnsupportedEnvironment(
            reason="unknown-schema",
            detail=(
                f"declared schema {outcome.declared!r} is not one this image understands "
                f"(supported: {SUPPORTED_SCHEMA})"
            ),
        )
    if isinstance(outcome, MissingReadinessFacts):
        return outcome

    profile = outcome

    mismatch = assert_toolchain(profile.toolchain, matrix)
    if mismatch is not None:
        return UnsupportedEnvironment(reason="toolchain-mismatch", detail=mismatch.reason)

    for service in profile.services:
        unreachable = check_service(service, service_env, prober)
        if unreachable is not None:
            return UnsupportedEnvironment(reason="unreachable-service", detail=unreachable.reason)

    return Ready(profile=profile)
