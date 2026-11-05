"""Hidden oracle for exp-69 fixture f4-greet-strip (#201, part of #69).

The experiment harness runs this against a copy of a run's final working tree,
after the Attempt has ended and never inside its workspace. It imports the
package from the tree's own ``src`` and checks behaviour only: it does not run
or count the repository's tests. Exit 0 means the requested change was
delivered; exit 1 means it was not.

Requested change: ``greet`` strips leading and trailing whitespace from the
name, keeps inner whitespace, refuses an empty or blank name with a
``ValueError`` naming ``name``, and ``shout`` inherits both through ``greet``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

sys.path.insert(0, str(Path.cwd() / "src"))

Check = Callable[[ModuleType], None]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


def expect_equal(actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def expect_value_error_naming(
    argument: str, call: Callable[..., object], *args: object, **kwargs: object
) -> None:
    try:
        result = call(*args, **kwargs)
    except ValueError as exc:
        if argument not in str(exc):
            raise AssertionError(
                f"ValueError message {str(exc)!r} does not name {argument!r}"
            ) from None
        return
    raise AssertionError(f"expected ValueError, got result {result!r}")


@check
def ordinary_names_unchanged(module: ModuleType) -> None:
    expect_equal(module.greet("World"), "Hello, World!")
    expect_equal(module.shout("world"), "HELLO, WORLD!")


@check
def surrounding_whitespace_stripped(module: ModuleType) -> None:
    expect_equal(module.greet("  Ada Lovelace "), "Hello, Ada Lovelace!")
    expect_equal(module.greet("\tAda\n"), "Hello, Ada!")


@check
def inner_whitespace_kept(module: ModuleType) -> None:
    expect_equal(module.greet(" Ada  Lovelace "), "Hello, Ada  Lovelace!")


@check
def empty_or_blank_name_raises_value_error_naming_argument(module: ModuleType) -> None:
    for value in ("", "   ", "\t\n"):
        expect_value_error_naming("name", module.greet, value)


@check
def shout_inherits_both_behaviours(module: ModuleType) -> None:
    expect_equal(module.shout("  ada "), "HELLO, ADA!")
    expect_value_error_naming("name", module.shout, "  ")


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
