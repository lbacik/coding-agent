from __future__ import annotations

from coding_agent.profile.parser import (
    SUPPORTED_SCHEMA,
    MissingReadinessFacts,
    ProfileOutcome,
    UnknownSchema,
    parse_profile,
    parse_profile_yaml,
)
from coding_agent.profile.readiness import (
    EnvironmentReadiness,
    NeedsClarification,
    ProfileSources,
    Ready,
    UnsupportedEnvironment,
    UnsupportedEnvironmentReason,
    evaluate_readiness,
)
from coding_agent.profile.schema import Check, Evidence, ProjectProfile, Service, Toolchain
from coding_agent.profile.services import ServiceProber, ServiceUnreachable, SocketServiceProber, check_service
from coding_agent.profile.substitution import UnresolvedPlaceholder, render_command
from coding_agent.profile.toolchain import (
    MalformedToolchainMatrix,
    SupportedToolchain,
    SupportedToolchainMatrix,
    ToolchainMismatch,
    assert_toolchain,
    load_toolchain_matrix,
)

__all__ = [
    "SUPPORTED_SCHEMA",
    "Check",
    "EnvironmentReadiness",
    "Evidence",
    "MalformedToolchainMatrix",
    "MissingReadinessFacts",
    "NeedsClarification",
    "ProfileOutcome",
    "ProfileSources",
    "ProjectProfile",
    "Ready",
    "Service",
    "ServiceProber",
    "ServiceUnreachable",
    "SocketServiceProber",
    "SupportedToolchain",
    "SupportedToolchainMatrix",
    "Toolchain",
    "ToolchainMismatch",
    "UnknownSchema",
    "UnresolvedPlaceholder",
    "UnsupportedEnvironment",
    "UnsupportedEnvironmentReason",
    "assert_toolchain",
    "check_service",
    "evaluate_readiness",
    "load_toolchain_matrix",
    "parse_profile",
    "parse_profile_yaml",
    "render_command",
]
