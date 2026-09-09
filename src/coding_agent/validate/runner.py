from __future__ import annotations

import subprocess
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
