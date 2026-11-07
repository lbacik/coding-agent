"""Producer conformance: the harness API over fakes (issue #260).

Contract: ``agent-operator-contract`` major 1 at ``06ee46c`` (parent
#245). The upstream ``v1.0.0`` harness API does not exist yet, so
:mod:`coding_agent.conformance.harness` implements the five operations
the issue names over the fake tracker and the scripted model, and this
suite proves each one works plus the image/deployment assertions the
producer suite runs in CI against the built image:

- make an issue eligible;
- hold an Attempt at a phase;
- finish an Attempt with a given outcome;
- crash the process (a real ``SIGKILL``ed OS process);
- stop the container (a real ``SIGTERM``ed OS process);
- ``docker exec`` / ``docker run`` argv, read-only rootfs flags, the
  discovery labels and the socket permissions.
"""

from __future__ import annotations

import signal
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from coding_agent.conformance import harness as harness_module
from coding_agent.conformance.harness import (
    HARNESS_OPERATIONS,
    ConformanceHarness,
    assert_socket_permissions,
    discovery_labels_for,
    docker_exec_agentctl,
    docker_run_agentctl,
    spawn_sleeper,
)
from support.fake_tracker import FakeTracker


def test_harness_implements_every_operation() -> None:
    """The harness API is exactly the five operations the issue names."""
    assert HARNESS_OPERATIONS == (
        "make_issue_eligible",
        "hold_attempt_at_phase",
        "finish_attempt_with_outcome",
        "crash_process",
        "stop_container",
    )
    harness = ConformanceHarness()
    for operation in HARNESS_OPERATIONS:
        assert callable(getattr(harness, operation)), operation


def test_make_issue_eligible_is_claimable() -> None:
    """A planted issue is open, unassigned and carries the Selection Label."""
    harness = ConformanceHarness()
    tracker = FakeTracker()
    candidate = harness.make_issue_eligible(tracker, 42)
    assert tracker.candidates[42] is candidate
    assert candidate.is_open is True
    assert candidate.assignees == ()
    assert "ready-for-agent" in candidate.labels
    assert "ready-for-human" not in candidate.labels
    assert "wontfix" not in candidate.labels


def test_hold_and_finish_attempt_roundtrip() -> None:
    """Holding parks the phase; finishing releases it with the outcome."""
    harness = ConformanceHarness()
    gate = harness.hold_attempt_at_phase("implement")
    assert gate.phase == "implement"
    assert gate.outcome is None
    assert gate.wait(timeout_seconds=0.05) is False
    harness.finish_attempt_with_outcome("delivered")
    assert gate.wait(timeout_seconds=5.0) is True
    assert gate.outcome == "delivered"


def test_crash_process_kills_a_real_process() -> None:
    """``crash_process`` SIGKILLs a real OS process (not a thread)."""
    harness = ConformanceHarness()
    target = spawn_sleeper(300.0)
    try:
        code = harness.crash_process(target)
    finally:
        try:
            target.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            target.kill()
    assert code == -signal.SIGKILL


def test_stop_container_terminates_a_real_process() -> None:
    """``stop_container`` SIGTERMs a real OS process (``docker stop``)."""
    harness = ConformanceHarness()
    target = spawn_sleeper(300.0)
    try:
        code = harness.stop_container(target)
    finally:
        if target.poll() is None:
            target.kill()
            target.wait(timeout=5.0)
    assert code == -signal.SIGTERM


def test_docker_exec_invocation_shape() -> None:
    """``docker exec`` runs ``agentctl`` as ``--user agent``, no TTY."""
    argv = docker_exec_agentctl(
        "coding-agent",
        ["--contract", "1", "status"],
    )
    assert argv[:5] == ["docker", "exec", "--user", "agent", "coding-agent"]
    assert argv[5:] == ["agentctl", "--contract", "1", "status"]
    assert harness_module.EXEC_USER == "agent"


def test_docker_run_invocation_shape() -> None:
    """``docker run`` is read-only with ``/tmp`` and the state volume."""
    argv = docker_run_agentctl(
        "coding-agent:dev",
        ["--contract", "1", "status"],
        state_volume="coding-agent-state",
    )
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in argv
    assert "/tmp" in argv
    assert "--user" in argv
    assert "coding-agent-state:/var/lib/coding-agent" in argv
    assert argv[-3:] == ["--contract", "1", "status"]


def test_compose_worker_is_the_only_managed_instance() -> None:
    """Discovery labels sit on the ``worker`` service, never the image."""
    compose_path = Path(__file__).resolve().parent.parent / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    labels = worker.get("labels", {})
    for key, value in discovery_labels_for("coding-agent").items():
        if key.endswith(".instance"):
            assert labels[key].endswith("coding-agent}")
        else:
            assert labels[key] == value
    dockerfile = (compose_path.parent / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        assert not line.strip().startswith("LABEL")
    assert harness_module.DISCOVERY_LABEL_PREFIX not in dockerfile


def test_socket_permissions_live_cycle(tmp_path: Path) -> None:
    """0700 directory and 0600 socket, asserted through the helper."""
    socket_dir = tmp_path / "operator"
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "agent.sock"
    socket_path.touch(mode=0o600)
    socket_dir.chmod(0o700)
    socket_path.chmod(0o600)
    assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    assert_socket_permissions(socket_dir, socket_path)
    with pytest.raises(AssertionError):
        socket_path.chmod(0o644)
        try:
            assert_socket_permissions(socket_dir, socket_path)
        finally:
            socket_path.chmod(0o600)
