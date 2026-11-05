"""Hidden oracle for exp-69 fixture f3-farewell (#201, part of #69).

The experiment harness runs this against a copy of a run's final working tree,
after the Attempt has ended and never inside its workspace. It imports the
package from the tree's own ``src`` and checks behaviour only: it does not run
or count the repository's tests. Exit 0 means the requested change was
delivered; exit 1 means it was not.

Requested change: ``farewell(name)`` in ``l2_fixture_py.greeting`` returns
``"Goodbye, <name>!"``, and ``greet`` and ``shout`` keep their behaviour.
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


@check
def farewell_returns_goodbye(module: ModuleType) -> None:
    expect_equal(module.farewell("World"), "Goodbye, World!")
    expect_equal(module.farewell("Ada"), "Goodbye, Ada!")


@check
def greet_and_shout_unchanged(module: ModuleType) -> None:
    expect_equal(module.greet("World"), "Hello, World!")
    expect_equal(module.shout("world"), "HELLO, WORLD!")


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
