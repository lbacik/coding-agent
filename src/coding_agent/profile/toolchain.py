from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from coding_agent.profile.schema import Toolchain


class MalformedToolchainMatrix(Exception):
    """`/opt/coding-agent/toolchain-matrix.json` (published by S1a) does not
    have the shape this module expects."""


@dataclass(frozen=True)
class SupportedToolchain:
    version: str
    package_manager_name: str


@dataclass(frozen=True)
class SupportedToolchainMatrix:
    """The image's own declaration of what it actually provides (ADR 0001),
    published at `/opt/coding-agent/toolchain-matrix.json` by S1a. Keyed by
    the same runtime names a `Toolchain` uses: "python", "php", "node"."""

    schema: int
    toolchains: Mapping[str, SupportedToolchain]


def load_toolchain_matrix(data: Mapping[str, Any]) -> SupportedToolchainMatrix:
    try:
        schema = data["schema"]
        if not isinstance(schema, int) or isinstance(schema, bool):
            raise TypeError("schema must be an integer")
        raw_toolchains = data["toolchains"]
        if not isinstance(raw_toolchains, Mapping):
            raise TypeError("toolchains must be an object")
        toolchains = {
            runtime: _parse_supported_toolchain(runtime, entry)
            for runtime, entry in raw_toolchains.items()
        }
    except (KeyError, TypeError, AttributeError) as exc:
        raise MalformedToolchainMatrix(f"toolchain matrix is missing an expected field: {exc}") from exc
    return SupportedToolchainMatrix(schema=schema, toolchains=toolchains)


def _parse_supported_toolchain(runtime: object, entry: object) -> SupportedToolchain:
    if not isinstance(runtime, str):
        raise TypeError("runtime names must be strings")
    if not isinstance(entry, Mapping):
        raise TypeError(f"toolchain entry for {runtime!r} must be an object")
    version = entry["version"]
    package_manager = entry["package_manager"]
    if not isinstance(version, str):
        raise TypeError(f"toolchain version for {runtime!r} must be a string")
    if not isinstance(package_manager, Mapping):
        raise TypeError(f"package_manager for {runtime!r} must be an object")
    manager_name = package_manager["name"]
    manager_version = package_manager["version"]
    if not isinstance(manager_name, str) or not isinstance(manager_version, str):
        raise TypeError(f"package_manager fields for {runtime!r} must be strings")
    return SupportedToolchain(version=version, package_manager_name=manager_name)


@dataclass(frozen=True)
class ToolchainMismatch:
    reason: str


def _version_satisfies(declared: str, available: str) -> bool:
    """Prefix match on dotted segments: declared "3.13" is satisfied by
    available "3.13.9", but declared "3.1" is not — a naive string
    `.startswith` would wrongly accept "3.1" against "3.13.9"."""
    declared_parts = declared.split(".")
    available_parts = available.split(".")
    return available_parts[: len(declared_parts)] == declared_parts


def assert_toolchain(toolchain: Toolchain, matrix: SupportedToolchainMatrix) -> ToolchainMismatch | None:
    """`None` means the declared toolchain is satisfiable by this image.

    A mismatch is always Unsupported Environment (contract §7, L2-12): the
    Project Profile is correct, the deployment is what falls short, and no
    Question Set can fix a toolchain version the image does not carry.
    """
    available = matrix.toolchains.get(toolchain.runtime)
    if available is None:
        return ToolchainMismatch(
            f"the image carries no {toolchain.runtime} toolchain at all "
            f"(profile declares {toolchain.runtime} {toolchain.version})"
        )
    if not _version_satisfies(toolchain.version, available.version):
        return ToolchainMismatch(
            f"profile declares {toolchain.runtime} {toolchain.version}, "
            f"image's Supported Toolchain Matrix carries {available.version}"
        )
    if available.package_manager_name != toolchain.package_manager:
        return ToolchainMismatch(
            f"profile declares package manager {toolchain.package_manager!r}, "
            f"image's Supported Toolchain Matrix carries {available.package_manager_name!r} "
            f"for {toolchain.runtime}"
        )
    return None
