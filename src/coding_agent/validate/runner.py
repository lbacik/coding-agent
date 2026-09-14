from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExecutedCommand:
    command: str
    exit_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """A port so tests can fake command execution without shelling out,
    mirroring `ServiceProber` (`coding_agent.profile.services`)."""

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand: ...


class SubprocessCommandRunner:
    """The real adapter: a Project-Profile-declared command, run as a shell
    string in the given working directory. Wiring this against a real
    checkout inside the image — the three real repositories and the
    in-image run — is S2c's job, not this harness's."""

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True
        )
        return ExecutedCommand(
            command=command, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )


class CredentialStrippedCommandRunner:
    """A `CommandRunner` whose subprocess environment has the named
    credential variables removed, at the environment level rather than only
    by leaving a GitHub tool out of the toolset (`L3-IMP-9`, re-founded on
    the environment after #25 found a test file mutate source code as a
    side effect of collection). Bound to `test_targeted`, the one Validation
    Contract command the model's own tool loop runs."""

    def __init__(self, credential_env_vars: Sequence[str]) -> None:
        self._credential_env_vars = frozenset(credential_env_vars)

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in self._credential_env_vars
        }
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, env=env
        )
        return ExecutedCommand(
            command=command, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )
