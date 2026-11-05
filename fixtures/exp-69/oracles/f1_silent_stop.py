"""Hidden oracle for exp-69 fixture f1-silent-stop (#201, part of #69).

The experiment harness runs this against a copy of a run's final working tree,
after the Attempt has ended and never inside its workspace. It imports the
package from the tree's own ``src`` and checks behaviour only: it does not run
or count the repository's tests. Exit 0 means the requested change was
delivered; exit 1 means it was not.

Requested change: ``shout(name, exclamations=1)`` ends the shouted greeting
with exactly ``exclamations`` exclamation marks, validates the argument to
1..5 with a ``ValueError`` naming it, keeps its default result, and leaves
``greet`` unchanged.
"""

from __future__ import annotations

import importlib
import inspect
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
def default_result_unchanged(module: ModuleType) -> None:
    expect_equal(module.shout("world"), "HELLO, WORLD!")
    expect_equal(module.shout("Ada"), "HELLO, ADA!")


@check
def exclamations_one_matches_default(module: ModuleType) -> None:
    expect_equal(module.shout("world", exclamations=1), "HELLO, WORLD!")


@check
def exclamations_set_the_count(module: ModuleType) -> None:
    expect_equal(module.shout("world", exclamations=2), "HELLO, WORLD!!")
    expect_equal(module.shout("world", exclamations=3), "HELLO, WORLD!!!")
    expect_equal(module.shout("Ada", exclamations=5), "HELLO, ADA!!!!!")


@check
def out_of_range_raises_value_error_naming_argument(module: ModuleType) -> None:
    for value in (0, 6, -1, 100):
        expect_value_error_naming(
            "exclamations", module.shout, "world", exclamations=value
        )


@check
def greet_signature_unchanged(module: ModuleType) -> None:
    expect_equal(list(inspect.signature(module.greet).parameters), ["name"])


@check
def greet_behaviour_unchanged(module: ModuleType) -> None:
    expect_equal(module.greet("World"), "Hello, World!")
    expect_equal(module.greet("Ada"), "Hello, Ada!")


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
