#!/usr/bin/env python3
"""Generate a Supported Toolchain Matrix from provisioned toolchains.

With no ``--runtime`` flag every supported runtime found on PATH is
recorded: that auto-detect behaviour stays for local host use (README).
A derived deployment image always passes an explicit ``--runtime``
selection (repeatable), so its Matrix describes only the Target Project
toolchains that image provisioned. The base image carries Python+uv (and
Node) for coding-agent's own use, which must not leak into the Matrix of
e.g. a PHP-only deployment; inferring supported runtimes from whatever is
on PATH would do exactly that.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Collection
from pathlib import Path
from typing import Callable


DEFAULT_OUTPUT = Path("tmp/toolchain-matrix.json")


class MatrixGenerationError(Exception):
    """A selected runtime (or its package manager) is not provisioned."""


def _run_version(command: list[str], parse: Callable[[str], str]) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return parse(result.stdout.strip())


def _first_line(value: str) -> str:
    return value.splitlines()[0]


def _python_version(value: str) -> str:
    return _first_line(value).removeprefix("Python ")


def _php_version(value: str) -> str:
    return value


def _node_version(value: str) -> str:
    return value.removeprefix("v")


def _uv_version(value: str) -> str:
    return value.split()[1]


def _composer_version(value: str) -> str:
    return value.split()[2]


def _package_manager_version(name: str, parser: Callable[[str], str]) -> str | None:
    return _run_version([name, "--version"], parser)


_RuntimeDefinition = tuple[str, list[str], Callable[[str], str], str, Callable[[str], str]]

_DEFINITIONS: tuple[_RuntimeDefinition, ...] = (
    (
        "python",
        ["python3", "--version"],
        _python_version,
        "uv",
        _uv_version,
    ),
    (
        "php",
        ["php", "-r", "echo PHP_VERSION;"],
        _php_version,
        "composer",
        _composer_version,
    ),
    (
        "node",
        ["node", "--version"],
        _node_version,
        "pnpm",
        lambda value: value,
    ),
)

_DEFINITIONS_BY_RUNTIME = {definition[0]: definition for definition in _DEFINITIONS}


def build_matrix(selected: Collection[str] | None = None) -> dict[str, object]:
    """Build the Matrix for `selected` runtimes, or auto-detect when None.

    An explicit selection records exactly those runtimes and fails with
    :class:`MatrixGenerationError`, naming the cause, when a selected
    runtime or its expected package manager is absent. With no selection
    (local host use) runtimes missing from PATH are silently skipped, as
    before.
    """
    toolchains: dict[str, dict[str, object]] = {}
    if selected is None:
        wanted = [name for name, *_ in _DEFINITIONS]
    else:
        unknown = sorted(set(selected) - set(_DEFINITIONS_BY_RUNTIME))
        if unknown:
            raise MatrixGenerationError(f"unsupported runtime(s): {', '.join(unknown)}")
        wanted = [name for name, *_ in _DEFINITIONS if name in set(selected)]
    for name in wanted:
        _, runtime_command, runtime_parser, manager, manager_parser = _DEFINITIONS_BY_RUNTIME[name]
        runtime_version = _run_version(runtime_command, runtime_parser)
        if runtime_version is None:
            if selected is not None:
                raise MatrixGenerationError(
                    f"selected runtime {name!r} is not installed "
                    f"(looked for {runtime_command[0]!r} on PATH)"
                )
            continue
        manager_version = _package_manager_version(manager, manager_parser)
        if manager_version is None:
            if selected is not None:
                raise MatrixGenerationError(
                    f"selected runtime {name!r} is missing its package manager "
                    f"(looked for {manager!r} on PATH)"
                )
            continue
        toolchains[name] = {
            "version": runtime_version,
            "package_manager": {"name": manager, "version": manager_version},
        }
    return {"schema": 1, "toolchains": toolchains}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        action="append",
        choices=sorted(_DEFINITIONS_BY_RUNTIME),
        default=None,
        help="Record only this runtime (repeatable). A derived deployment "
        "image passes one flag per Target Project toolchain it provisioned.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    try:
        matrix = build_matrix(args.runtime)
    except MatrixGenerationError as exc:
        parser.error(str(exc))
    if not matrix["toolchains"]:
        parser.error("no supported runtime and package-manager pairs were found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote Supported Toolchain Matrix to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
