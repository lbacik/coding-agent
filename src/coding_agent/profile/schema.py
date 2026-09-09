from __future__ import annotations

from dataclasses import dataclass

#: `language:` values the schema recognises, and the `toolchain:` key each one
#: is declared under. TypeScript's runtime is Node, so its key differs from
#: its language name; Python and PHP's do not.
TOOLCHAIN_RUNTIME_FOR_LANGUAGE = {"python": "python", "php": "php", "typescript": "node"}

#: The only Validation Evidence format v1 understands (contract §7): pytest,
#: PHPUnit and vitest all emit it natively, so one parser serves all three
#: languages.
SUPPORTED_EVIDENCE_FORMAT = "junit-xml"


@dataclass(frozen=True)
class Toolchain:
    """A Project Profile's declared runtime requirement, asserted against a
    Supported Toolchain Matrix by `coding_agent.profile.toolchain`."""

    runtime: str
    """The Supported Toolchain Matrix key: "python", "php" or "node"."""
    version: str
    package_manager: str


@dataclass(frozen=True)
class Evidence:
    format: str
    test_all: str
    test_targeted: str


@dataclass(frozen=True)
class Check:
    name: str
    command: str
    evidence: str | None = None
    """Optional (contract §7): without it, a check red at the Delivery
    Snapshot cannot establish a Baseline Failure, so a red baseline no
    longer excuses it."""


@dataclass(frozen=True)
class Service:
    name: str
    url_env: str


@dataclass(frozen=True)
class ProjectProfile:
    """The Validation Contract: the profile as it stands at the Base Revision,
    pinned at Claim (contract §7). Frozen so that value, once obtained, is
    the pin — a later edit to `project-profile.yml` cannot reach back and
    change an `Attempt`'s own contract (L2-17)."""

    schema: int
    language: str
    working_directory: str
    toolchain: Toolchain
    bootstrap: str
    test_all: str
    test_targeted: str
    evidence: Evidence
    checks: tuple[Check, ...]
    """Empty means `checks: none` was declared — satisfied, not skipped
    (L2-8). An empty *list* in the source YAML is rejected before a
    `ProjectProfile` is ever constructed (L2-9), so this tuple being empty
    is never ambiguous."""
    services: tuple[Service, ...]
