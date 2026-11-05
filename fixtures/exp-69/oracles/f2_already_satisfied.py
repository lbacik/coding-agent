"""Hidden oracle for exp-69 fixture f2-already-satisfied (#201, part of #69).

The experiment harness runs this against a copy of a run's final working tree,
after the Attempt has ended and never inside its workspace. Every Acceptance
Criterion of this fixture already holds at the Base Revision, so the correct
delivery is no change. Exit 0 means behaviour is unchanged and no unwanted
edit was made; exit 1 means otherwise.

An unwanted edit is any change to, or deletion of, a file of the Base
Revision, or any new file outside ``tests/``. Bootstrap and cache artifacts
(``.venv``, ``__pycache__``, ``.pytest_cache``, ``*.pyc``) and the oracle's
own directory are ignored. A new test file is tolerated here, because it
changes no behaviour; the blind rubric scores it as over-reach.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "src"))

BASE_FILES: dict[str, str] = {
    ".gitignore": "4409b253ac33acb862192f9cad8583ed3fe0bac938082a6cfa784d7cf0d185ef",
    "docs/agents/project-profile.yml": "c15bf785193775db64a8ab3abb8372ada43ae86edf633f11991ea8dc31801583",
    "pyproject.toml": "fe844c0469d23460cdd284bf9fe1dd0b70473acc431bf7706aba8b1eb04073f7",
    "src/l2_fixture_py/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "src/l2_fixture_py/greeting.py": "d7a954d85f910b389437e1914c437e2966548e4729534ffcac55e933a42e1a86",
    "tests/test_extra.py": "e7888c2388f41f130ad10d239b8eb54149779795a68524096218754bed00fcd1",
    "tests/test_greeting.py": "c9d6f5bb440f6acb0161c008f74183577f6597d4cb900f7b9c186bf23c755938",
    "uv.lock": "c78dda0ccb36d776e9058e55ff494cc4bf2844d6b9a6ef363a4a09d4be04d1c0",
}
"""SHA-256 of every file of the Base Revision tree (tag ``exp-69-base-v1``)."""

IGNORED_DIRECTORIES = frozenset({".venv", "__pycache__", ".pytest_cache", "_oracle"})
TOLERATED_NEW_FILE_PREFIX = "tests/"

Check = Callable[[ModuleType], None]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


def expect_equal(actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def tree_files() -> list[str]:
    files: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if IGNORED_DIRECTORIES.intersection(relative.parts):
            continue
        if path.is_file() and path.suffix != ".pyc":
            files.append(relative.as_posix())
    return sorted(files)


@check
def shout_upper_cases_whole_greeting(module: ModuleType) -> None:
    expect_equal(module.shout("world"), "HELLO, WORLD!")
    expect_equal(module.shout("Ada"), "HELLO, ADA!")


@check
def greet_behaviour_unchanged(module: ModuleType) -> None:
    expect_equal(module.greet("World"), "Hello, World!")
    expect_equal(module.greet("Ada"), "Hello, Ada!")


@check
def base_files_unchanged(module: ModuleType) -> None:
    changed: list[str] = []
    for relative, expected in sorted(BASE_FILES.items()):
        path = ROOT / relative
        if not path.is_file():
            changed.append(f"{relative} (deleted)")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            changed.append(f"{relative} (modified)")
    if changed:
        raise AssertionError(f"Base Revision files changed: {changed}")


@check
def no_new_files_outside_tests(module: ModuleType) -> None:
    added = [
        relative
        for relative in tree_files()
        if relative not in BASE_FILES
        and not relative.startswith(TOLERATED_NEW_FILE_PREFIX)
    ]
    if added:
        raise AssertionError(f"new files outside {TOLERATED_NEW_FILE_PREFIX}: {added}")


def main() -> int:
    try:
        module = importlib.import_module("l2_fixture_py.greeting")
    except Exception as exc:  # any import failure is a failed delivery
        print(f"FAIL import l2_fixture_py.greeting: {type(exc).__name__}: {exc}")
        return 1
    failed = 0
    for function in CHECKS:
        try:
            function(module)
        except Exception as exc:  # each check is judged on its own
            failed += 1
            print(f"FAIL {function.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {function.__name__}")
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
