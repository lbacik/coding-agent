from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from coding_agent.profile.parser import (
    SUPPORTED_SCHEMA,
    MissingReadinessFacts,
    ProfileOutcome,
    UnknownSchema,
    parse_profile_yaml,
)
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber, check_service
from coding_agent.profile.toolchain import SupportedToolchainMatrix, assert_toolchain


@dataclass(frozen=True)
class ProfileSources:
    """Where readiness for one Attempt is read from (contract §7): the
    canonical `docs/agents/project-profile.yml`, consulted first, with the
    prose fallback chain (`AGENTS.md`/`CLAUDE.md` → `docs/agents/*` →
    project configuration → issue body) behind it.

    Extracting Readiness Facts out of that prose chain is not this module's
    job — S2 makes no model calls, and free-form extraction is a reading
    task for whatever produces `fallback`. This module owns only the
    precedence rule between the two sources and what happens when neither
    resolves.

    `fallback` is typed as a complete, already-resolved `ProjectProfile`
    rather than a partial, fact-by-fact structure: this module treats "read
    the profile file" and "read the fallback chain" as two whole
    alternative resolutions of the same Readiness Facts, not two sources to
    merge field by field. Nothing in contract §7 or the acceptance matrix
    asks for a merge finer than that, and the profile-wins rule (L2-15)
    reads most naturally as "prefer one whole source over the other."
    """

    profile_yaml_text: str | None
    """`docs/agents/project-profile.yml`'s content at the Base Revision, or
    `None` if the Target Repository carries no such file."""
    fallback: ProjectProfile | None = None
    """Readiness Facts already resolved from the fallback chain, if the
    canonical file is absent. Where both exist, the profile wins — so a
    fallback value is ignored whenever `profile_yaml_text` is not `None`
    (L2-15), and used only in its absence (L2-16)."""
    sources_searched: tuple[str, ...] = ()
    """Every location consulted, named in the clarification comment so a
    human knows where to add the missing fact."""


@dataclass(frozen=True)
class Ready:
    profile: ProjectProfile


@dataclass(frozen=True)
class NeedsClarification:
    """Contract §7's first failure route: a Readiness Fact unresolvable
    from any source. A human can fix this — by editing the profile, most
    permanently — so it costs a Clarification Round (T3), never a Terminal
    failure on its own."""

    missing: tuple[str, ...]
    sources_searched: tuple[str, ...]


UnsupportedEnvironmentReason = Literal["unknown-schema", "toolchain-mismatch", "unreachable-service"]


@dataclass(frozen=True)
class UnsupportedEnvironment:
    """Contract §7's second failure route: the profile is complete and
    correct, but this image cannot satisfy it. No Question Set can fix a
    deployment, so this is a classification of `failed`, not clarification."""

    reason: UnsupportedEnvironmentReason
    detail: str


EnvironmentReadiness = Ready | NeedsClarification | UnsupportedEnvironment


def _resolve_profile_outcome(sources: ProfileSources) -> ProfileOutcome | None:
    """`None` means neither source resolved anything at all."""
    if sources.profile_yaml_text is not None:
        return parse_profile_yaml(sources.profile_yaml_text)
    if sources.fallback is not None:
        return sources.fallback
    return None


def evaluate_readiness(
    sources: ProfileSources,
    matrix: SupportedToolchainMatrix,
    service_env: Mapping[str, str],
    prober: ServiceProber,
) -> EnvironmentReadiness:
    """The readiness decision contract §7 and §4's `evaluate_readiness` node
    describe, up to (never including) implementation: parse the profile,
    assert the toolchain, probe declared services — all before any command
    the profile declares is ever run.
    """
    outcome = _resolve_profile_outcome(sources)

    if outcome is None:
        return NeedsClarification(
            missing=("no Project Profile, and no fallback source resolved any Readiness Fact",),
            sources_searched=sources.sources_searched,
        )
    if isinstance(outcome, UnknownSchema):
        return UnsupportedEnvironment(
            reason="unknown-schema",
            detail=(
                f"declared schema {outcome.declared!r} is not one this image understands "
                f"(supported: {SUPPORTED_SCHEMA})"
            ),
        )
    if isinstance(outcome, MissingReadinessFacts):
        return NeedsClarification(missing=outcome.missing, sources_searched=sources.sources_searched)

    profile = outcome

    mismatch = assert_toolchain(profile.toolchain, matrix)
    if mismatch is not None:
        return UnsupportedEnvironment(reason="toolchain-mismatch", detail=mismatch.reason)

    for service in profile.services:
        unreachable = check_service(service, service_env, prober)
        if unreachable is not None:
            return UnsupportedEnvironment(reason="unreachable-service", detail=unreachable.reason)

    return Ready(profile=profile)
