#!/usr/bin/env python3
"""Generate a Supported Toolchain Matrix from tools installed on the host."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable


DEFAULT_OUTPUT = Path("tmp/toolchain-matrix.json")


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


def build_matrix() -> dict[str, object]:
    toolchains: dict[str, dict[str, object]] = {}
    definitions = (
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
    for runtime, runtime_command, runtime_parser, manager, manager_parser in definitions:
        runtime_version = _run_version(runtime_command, runtime_parser)
        manager_version = _package_manager_version(manager, manager_parser)
        if runtime_version is None or manager_version is None:
            continue
        toolchains[runtime] = {
            "version": runtime_version,
            "package_manager": {"name": manager, "version": manager_version},
        }
    return {"schema": 1, "toolchains": toolchains}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    matrix = build_matrix()
    if not matrix["toolchains"]:
        parser.error("no supported runtime and package-manager pairs were found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote Supported Toolchain Matrix to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
