from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from coding_agent.profile.schema import (
    SUPPORTED_EVIDENCE_FORMAT,
    TOOLCHAIN_RUNTIME_FOR_LANGUAGE,
    Check,
    Evidence,
    ProjectProfile,
    Service,
    Toolchain,
)

SUPPORTED_SCHEMA = 2


@dataclass(frozen=True)
class UnknownSchema:
    """`schema` names a version this parser does not know.

    Routes to `unsupported-environment`, never to clarification (L2-13):
    no Question Set can fix an image that does not understand a schema a
    human already declared correctly. An *absent* `schema` key is a
    different case — a missing Readiness Fact the human can supply, like
    any other gap in the profile — and is reported as one instead.
    """

    declared: object


@dataclass(frozen=True)
class MissingReadinessFacts:
    """One or more Readiness Facts could not be resolved from the profile.

    `missing` carries every gap found, not just the first — the Worker
    publishes one clarification comment listing every open question
    (contract §9), so the parser must not stop at the first defect.
    """

    missing: tuple[str, ...]


ProfileOutcome = ProjectProfile | MissingReadinessFacts | UnknownSchema


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _require_str(source: Mapping[str, Any], key: str, *, label: str, missing: list[str]) -> str | None:
    """Fetch `source[key]` if it's a non-empty string, else record `label`
    as a missing Readiness Fact and return `None`."""
    value = source.get(key)
    if isinstance(value, str) and value:
        return value
    missing.append(label)
    return None


def _parse_toolchain(
    data: Mapping[str, Any], language: str | None, missing: list[str]
) -> Toolchain | None:
    toolchain_data = data.get("toolchain")
    if not isinstance(toolchain_data, dict):
        missing.append("toolchain")
        return None
    if language is None or language not in TOOLCHAIN_RUNTIME_FOR_LANGUAGE:
        # Can't validate a runtime key against an unknown language; the
        # "language" gap is already recorded by the caller.
        return None

    runtime = TOOLCHAIN_RUNTIME_FOR_LANGUAGE[language]
    version = _require_str(toolchain_data, runtime, label=f"toolchain.{runtime}", missing=missing)
    package_manager = _require_str(
        toolchain_data, "package_manager", label="toolchain.package_manager", missing=missing
    )
    if version is None or package_manager is None:
        return None
    return Toolchain(runtime=runtime, version=version, package_manager=package_manager)


def _parse_commands(data: Mapping[str, Any], missing: list[str]) -> tuple[str, str, str] | None:
    commands = data.get("commands")
    if not isinstance(commands, dict):
        missing.append("commands")
        return None

    bootstrap = _require_str(commands, "bootstrap", label="commands.bootstrap", missing=missing)
    test_all = _require_str(commands, "test_all", label="commands.test_all", missing=missing)
    test_targeted = _require_str(
        commands, "test_targeted", label="commands.test_targeted", missing=missing
    )
    if bootstrap is None or test_all is None or test_targeted is None:
        return None
    return bootstrap, test_all, test_targeted


def _parse_evidence(data: Mapping[str, Any], missing: list[str]) -> Evidence | None:
    """`evidence` is required for the test commands (contract §7, L2-11):
    its absence is a missing Readiness Fact, not something the harness can
    infer or skip."""
    evidence_data = data.get("evidence")
    if not isinstance(evidence_data, dict):
        missing.append("evidence")
        return None

    fmt = evidence_data.get("format")
    if fmt != SUPPORTED_EVIDENCE_FORMAT:
        missing.append(
            f"evidence.format: only {SUPPORTED_EVIDENCE_FORMAT!r} is supported in v1, got {fmt!r}"
        )

    ev_test_all = _require_str(
        evidence_data, "test_all", label="evidence.test_all", missing=missing
    )
    ev_test_targeted = _require_str(
        evidence_data, "test_targeted", label="evidence.test_targeted", missing=missing
    )
    if fmt != SUPPORTED_EVIDENCE_FORMAT or ev_test_all is None or ev_test_targeted is None:
        return None
    return Evidence(format=fmt, test_all=ev_test_all, test_targeted=ev_test_targeted)


def _parse_checks(data: Mapping[str, Any], missing: list[str]) -> tuple[Check, ...] | None:
    """`checks` is a list of named commands or the literal scalar `none`
    (contract §7). An absent key is a missing Readiness Fact (L2-10); `none`
    satisfies it without running anything (L2-8); an empty list is rejected
    as ambiguous rather than treated as either (L2-9)."""
    if "checks" not in data:
        missing.append("checks")
        return None

    raw_checks = data["checks"]
    if raw_checks == "none":
        return ()

    if not isinstance(raw_checks, list):
        missing.append("checks: must be a list of named commands or the literal scalar 'none'")
        return None
    if len(raw_checks) == 0:
        missing.append(
            "checks: an empty list is ambiguous — declare 'none' or name at least one check"
        )
        return None

    checks: list[Check] = []
    for index, entry in enumerate(raw_checks):
        if (
            not isinstance(entry, dict)
            or not _is_nonempty_str(entry.get("name"))
            or not _is_nonempty_str(entry.get("command"))
        ):
            missing.append(f"checks[{index}]: needs a non-empty name and command")
            continue
        evidence = entry.get("evidence")
        checks.append(
            Check(
                name=entry["name"],
                command=entry["command"],
                evidence=evidence if _is_nonempty_str(evidence) else None,
            )
        )
    return tuple(checks)


def _parse_services(data: Mapping[str, Any], missing: list[str]) -> tuple[Service, ...]:
    """Absent is "no declared services", not a gap: unlike `checks`, the
    contract never requires an explicit empty declaration here."""
    raw_services = data.get("services", [])
    if not isinstance(raw_services, list):
        missing.append("services: must be a list")
        return ()

    services: list[Service] = []
    for index, entry in enumerate(raw_services):
        if (
            not isinstance(entry, dict)
            or not _is_nonempty_str(entry.get("name"))
            or not _is_nonempty_str(entry.get("url_env"))
        ):
            missing.append(f"services[{index}]: needs a non-empty name and url_env")
            continue
        services.append(Service(name=entry["name"], url_env=entry["url_env"]))
    return tuple(services)


def parse_profile(data: Mapping[str, Any]) -> ProfileOutcome:
    """Decide what `docs/agents/project-profile.yml` (or an equivalent
    fallback source) means, without executing anything (contract §7).

    Every Readiness Fact gap is collected before returning, so a caller can
    publish one Question Set covering all of them (contract §9) rather than
    round-tripping one clarification per defect. The one exception is a
    *present but unrecognised* `schema`: that alone decides the outcome
    (L2-13), since the shape of every other field depends on the schema
    version and a mismatch there makes the rest of the checks meaningless.
    An *absent* `schema` carries no such ambiguity, so it is folded into
    `missing` like any other gap instead of short-circuiting.
    """
    missing: list[str] = []

    if "schema" in data:
        schema = data["schema"]
        if schema != SUPPORTED_SCHEMA:
            return UnknownSchema(declared=schema)
    else:
        missing.append("schema")

    language = data.get("language")
    if language not in TOOLCHAIN_RUNTIME_FOR_LANGUAGE:
        missing.append(f"language: must be one of python/php/typescript, got {language!r}")
        language = None

    working_directory = _require_str(
        data, "working_directory", label="working_directory", missing=missing
    )
    toolchain = _parse_toolchain(data, language, missing)
    parsed_commands = _parse_commands(data, missing)
    evidence = _parse_evidence(data, missing)
    checks = _parse_checks(data, missing)
    services = _parse_services(data, missing)

    if missing:
        return MissingReadinessFacts(tuple(missing))

    if (
        language is None
        or working_directory is None
        or toolchain is None
        or parsed_commands is None
        or evidence is None
        or checks is None
    ):
        raise AssertionError(
            "unreachable: every field above must be resolved once `missing` is empty"
        )
    bootstrap, test_all, test_targeted = parsed_commands

    return ProjectProfile(
        schema=SUPPORTED_SCHEMA,
        language=language,
        working_directory=working_directory,
        toolchain=toolchain,
        bootstrap=bootstrap,
        test_all=test_all,
        test_targeted=test_targeted,
        evidence=evidence,
        checks=checks,
        services=services,
    )


def parse_profile_yaml(text: str) -> ProfileOutcome:
    """`docs/agents/project-profile.yml`'s content, read by the caller at
    whatever revision it is pinning (the Base Revision, at Claim)."""
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        return MissingReadinessFacts(
            ("project-profile.yml must be a YAML mapping at the top level",)
        )
    return parse_profile(loaded)
