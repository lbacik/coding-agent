"""Each entry module imports cleanly in a fresh interpreter.

Inside one pytest process, modules load in whatever order the collected
tests happen to import them, which can hide a circular import: the
``workflow.effective`` <-> ``skillbundle.deployment`` cycle passed the whole
suite and only failed in ``python -m coding_agent.cli``. A fresh
interpreter per module makes each one the first thing imported.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

ENTRY_MODULES = (
    "coding_agent.cli",
    "coding_agent.worker",
    "coding_agent.worker.supervisor",
    "coding_agent.workflow.effective",
    "coding_agent.skillbundle",
    "coding_agent.skillbundle.deployment",
)


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
