"""The coding-agent conformance harness (issue #260).

Contract: ``agent-operator-contract`` major 1 at ``06ee46c`` (parent
#245). The upstream ``v1.0.0`` harness API does not exist yet, so this
module implements the five operations the issue names over the
existing test doubles — the fake tracker
(``tests/support/fake_tracker.py``) and the scripted model
(``tests/support/scripted_model.py``) — in the same shape the future
contract will pin:

- ``make_issue_eligible`` plants a selectable issue in the fake;
- ``hold_attempt_at_phase`` parks the scripted model behind a gate;
- ``finish_attempt_with_outcome`` releases the gate with an outcome;
- ``crash_process`` SIGKILLs a real OS process (not a thread);
- ``stop_container`` SIGTERMs one (the ``docker stop`` analogue).

The Docker halves — ``docker exec`` / ``docker run`` invocation, the
read-only root filesystem, the discovery labels and the socket
permissions — are built here as command constructors so the in-repo
tests assert the exact argv without a daemon, while
``conformance/producer-suite.sh`` runs the same argv against the built
image in CI.
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXEC_USER = "agent"
"""The Unix user ``docker exec`` must run as (image user, uid 1000)."""

DISCOVERY_LABEL_PREFIX = "io.github.lbacik.agent-operator."
"""The discovery-label prefix the deployment carries (never the image)."""

EXPECTED_DISCOVERY_LABELS: dict[str, str] = {
    "io.github.lbacik.agent-operator.contract": "1",
    "io.github.lbacik.agent-operator.exec-user": EXEC_USER,
    "io.github.lbacik.agent-operator.backend": "coding-agent",
}
"""The labels every Managed Instance carries, besides its instance name."""

OPERATOR_SOCKET_DIR_MODE = 0o700
OPERATOR_SOCKET_MODE = 0o600

HARNESS_OPERATIONS: tuple[str, ...] = (
    "make_issue_eligible",
    "hold_attempt_at_phase",
    "finish_attempt_with_outcome",
    "crash_process",
    "stop_container",
)
"""Every harness API operation this module implements (#260)."""


@dataclass
class PhaseGate:
    """A one-shot gate a held Attempt phase waits on."""

    phase: str = ""
    outcome: str | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def hold(self, phase: str) -> None:
        """Park the Attempt at ``phase`` until it is finished."""
        self.phase = phase
        self.outcome = None
        self._event.clear()

    def finish(self, outcome: str) -> None:
        """Release the gate, recording the Attempt's ``outcome``."""
        self.outcome = outcome
        self._event.set()

    def wait(self, timeout_seconds: float = 10.0) -> bool:
        """Wait for the finish; ``True`` where it arrived in time."""
        return self._event.wait(timeout_seconds)


@dataclass
class ConformanceHarness:
    """The five-operation harness over the fake tracker and scripted model."""

    gate: PhaseGate = field(default_factory=PhaseGate)

    def make_issue_eligible(
        self,
        tracker: Any,
        number: int,
        *,
        title: str = "Add a greeting",
        body: str = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        labels: tuple[str, ...] = ("ready-for-agent",),
        created_at: str = "2026-10-01T00:00:00Z",
    ) -> Any:
        """Plant issue ``number`` as an eligible selection candidate.

        The candidate is open, unassigned, carries the Selection Label and
        no exclusion label, so the Worker's guards pass and the next sweep
        claims it first in ``(created_at, number)`` order.
        """
        from coding_agent.tracker.port import CandidateIssue

        candidate = CandidateIssue(
            number=number,
            title=title,
            body=body,
            is_open=True,
            assignees=(),
            labels=labels,
            created_at=created_at,
        )
        tracker.candidates[number] = candidate
        return candidate

    def hold_attempt_at_phase(self, phase: str) -> PhaseGate:
        """Park the Attempt at ``phase``; the model unblocks on finish."""
        self.gate.hold(phase)
        return self.gate

    def finish_attempt_with_outcome(self, outcome: str) -> PhaseGate:
        """Release the held phase, recording ``outcome`` for the suite."""
        self.gate.finish(outcome)
        return self.gate

    def crash_process(self, target: subprocess.Popen[Any]) -> int:
        """SIGKILL ``target`` (a real OS process) and return its exit code."""
        target.send_signal(signal.SIGKILL)
        return target.wait(timeout=30.0)

    def stop_container(self, target: subprocess.Popen[Any]) -> int:
        """SIGTERM ``target`` (the ``docker stop`` analogue) and wait."""
        target.send_signal(signal.SIGTERM)
        try:
            return target.wait(timeout=30.0)
        except subprocess.TimeoutExpired as exc:
            target.send_signal(signal.SIGKILL)
            target.wait(timeout=30.0)
            raise exc


def spawn_sleeper(seconds: float = 300.0) -> subprocess.Popen[Any]:
    """Spawn a real OS process the harness can crash or stop.

    A ``sleep``-equivalent Python process: it dies only to a signal, so
    the suite proves real process death rather than a thread joining.
    """
    return subprocess.Popen(
        ["python3", "-c", f"import time; time.sleep({seconds!r})"],
    )


def docker_exec_agentctl(container: str, argv: list[str]) -> list[str]:
    """Build ``docker exec --user agent <container> agentctl ...``."""
    return ["docker", "exec", "--user", EXEC_USER, container, "agentctl", *argv]


def docker_run_agentctl(
    image: str,
    argv: list[str],
    *,
    state_volume: str,
    readonly: bool = True,
) -> list[str]:
    """Build ``docker run`` for one ``agentctl`` call against ``image``."""
    cmd: list[str] = ["docker", "run", "--rm"]
    if readonly:
        cmd += ["--read-only", "--tmpfs", "/tmp"]
    cmd += ["--user", "1000:1000", "-v", f"{state_volume}:/var/lib/coding-agent"]
    cmd += [image, "agentctl", *argv]
    return cmd


def discovery_labels_for(instance: str) -> dict[str, str]:
    """The discovery labels a Managed Instance named ``instance`` carries."""
    return {**EXPECTED_DISCOVERY_LABELS, "io.github.lbacik.agent-operator.instance": instance}


def assert_socket_permissions(socket_dir: Path, socket_path: Path) -> None:
    """Assert the operator socket's 0700 directory and 0600 socket."""
    dir_mode = socket_dir.stat().st_mode & 0o777
    if dir_mode != OPERATOR_SOCKET_DIR_MODE:
        raise AssertionError(f"socket dir {socket_dir} mode {oct(dir_mode)}, want 0o700")
    sock_mode = socket_path.stat().st_mode & 0o777
    if sock_mode != OPERATOR_SOCKET_MODE:
        raise AssertionError(f"socket {socket_path} mode {oct(sock_mode)}, want 0o600")


def wait_for_condition(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 10.0,
    poll_seconds: float = 0.05,
) -> bool:
    """Poll ``predicate`` until true or the timeout; ``True`` where met."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_seconds)
    return bool(predicate())


__all__ = [
    "DISCOVERY_LABEL_PREFIX",
    "EXPECTED_DISCOVERY_LABELS",
    "EXEC_USER",
    "HARNESS_OPERATIONS",
    "OPERATOR_SOCKET_DIR_MODE",
    "OPERATOR_SOCKET_MODE",
    "ConformanceHarness",
    "PhaseGate",
    "assert_socket_permissions",
    "discovery_labels_for",
    "docker_exec_agentctl",
    "docker_run_agentctl",
    "spawn_sleeper",
    "wait_for_condition",
]
