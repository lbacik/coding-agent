from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


DEFAULT_COMMAND_TIMEOUT_SECONDS: float = 600.0
"""Per-command timeout for every workspace command (contract §11).

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Command timeouts
kill the command's whole process group, never only the shell").

MVP row ids: P-CMD-1, L3-ERR-14.

Every command the runtime starts in the workspace — the model's
``test_targeted`` and the runtime's own commands (readiness setup, baseline,
final checks, the validation runner) — runs in its own process group with
this timeout. A tuning constant, not part of the contract's guarantees; a
caller that needs a shorter bound (tests, for example) passes an explicit
``timeout_seconds``.
"""

COMMAND_TIMEOUT_EXIT_CODE: int = 124
"""Exit code reported for a timed-out command.

Follows the conventional ``timeout(1)`` status so a timeout is red wherever a
node already judges a non-zero exit as a failure, while ``timed_out`` keeps
the machine-readable fact.
"""


@dataclass(frozen=True)
class ExecutedCommand:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    """Whether the per-command timeout killed the command's process group.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (a timeout kills
    the whole process group and, for the model, returns as a tool result).

    MVP row ids: P-CMD-1, L3-ERR-14.
    """


class CommandRunner(Protocol):
    """A port so tests can fake command execution without shelling out,
    mirroring `ServiceProber` (`coding_agent.profile.services`)."""

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand: ...


def kill_process_group(pid: int, *, sig: int = signal.SIGKILL) -> None:
    """Kill the whole process group of ``pid`` (contract §11).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Command
    timeouts kill the command's whole process group, never only the shell").

    MVP row ids: P-CMD-1.

    Every workspace command starts with ``start_new_session=True``, so its
    ``pid`` leads its own group and grandchildren stay inside it unless they
    explicitly leave the session. A missing group (already reaped) is not an
    error. Kept as a module-level helper so the SIGTERM ticket can reuse it
    for the running command's group kill.
    """
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _timeout_note(command: str, timeout_seconds: float) -> str:
    return (
        f"command timed out after {timeout_seconds:g}s; "
        f"killed the whole process group: {command!r}"
    )


def _require_positive_timeout(timeout_seconds: float) -> float:
    """Share one positivity guard across the ``CommandRunner`` adapters."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    return timeout_seconds


def run_command_with_timeout(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    timeout_seconds: float,
) -> ExecutedCommand:
    """Run one shell command in its own process group with a timeout.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Command
    timeouts kill the command's whole process group, never only the shell").

    MVP row ids: P-CMD-1, L3-ERR-14.

    Starts the shell with ``start_new_session=True`` so the command owns its
    process group, waits ``timeout_seconds``, then on expiry kills the whole
    group (grandchildren included) and reports ``timed_out=True`` with
    ``COMMAND_TIMEOUT_EXIT_CODE`` instead of raising. A timeout is therefore
    a result the caller classifies — a tool result for the model
    (L3-ERR-14), a red command for a runtime node — never a process death.
    """
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        kill_process_group(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        note = _timeout_note(command, timeout_seconds)
        previous = (stdout or "").rstrip("\n")
        stdout = f"{previous}\n{note}\n" if previous else f"{note}\n"
        return ExecutedCommand(
            command=command,
            exit_code=COMMAND_TIMEOUT_EXIT_CODE,
            stdout=stdout or note + "\n",
            stderr=stderr or "",
            timed_out=True,
        )
    return ExecutedCommand(
        command=command,
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
    )


class SubprocessCommandRunner:
    """The real adapter: a Project-Profile-declared command, run as a shell
    string in the given working directory. Wiring this against a real
    checkout inside the image — the three real repositories and the
    in-image run — is S2c's job, not this harness's.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (every workspace
    command runs in its own process group with a per-command timeout; a
    timeout kills the whole group and returns as a result, never as a
    process death).

    MVP row ids: P-CMD-1, L3-ERR-14.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = _require_positive_timeout(timeout_seconds)

    @property
    def timeout_seconds(self) -> float:
        """This runner's per-command timeout (contract §11, P-CMD-1)."""
        return self._timeout_seconds

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        return run_command_with_timeout(
            command, cwd=cwd, env=None, timeout_seconds=self._timeout_seconds
        )


class CredentialStrippedCommandRunner:
    """A `CommandRunner` whose subprocess environment has the named
    credential variables removed, at the environment level rather than only
    by leaving a GitHub tool out of the toolset (`L3-IMP-9`, re-founded on
    the environment after #25 found a test file mutate source code as a
    side effect of collection). Bound to `test_targeted`, the one Validation
    Contract command the model's own tool loop runs.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (every workspace
    command runs in its own process group with a per-command timeout; a
    timeout kills the whole group and returns as a tool result the model can
    react to, L3-ERR-14).

    MVP row ids: P-CMD-1, L3-ERR-14.
    """

    def __init__(
        self,
        credential_env_vars: Sequence[str],
        timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._credential_env_vars = frozenset(credential_env_vars)
        self._timeout_seconds = _require_positive_timeout(timeout_seconds)

    @property
    def timeout_seconds(self) -> float:
        """This runner's per-command timeout (contract §11, P-CMD-1)."""
        return self._timeout_seconds

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in self._credential_env_vars
        }
        return run_command_with_timeout(
            command, cwd=cwd, env=env, timeout_seconds=self._timeout_seconds
        )
