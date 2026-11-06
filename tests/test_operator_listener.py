"""Control listener: identity and status from a running Worker (issue #248).

Contract: adoption step 4 of the operator contract (#245). An operator can
``docker exec`` into a running Worker and get ``identity`` and a minimal
``status`` back, even while an Attempt is blocked on a model call. Tests
call ``agentctl``'s entry point in-process over a Worker driven through
``supervisor.run_worker`` with the startup externals monkeypatched, the
``FakeTracker`` for GitHub and a scripted model for the provider.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from coding_agent import agentctl
from coding_agent.operator.paths import (
    OPERATOR_SOCKET_ENV_VAR,
    clear_current_socket,
    client_socket_path,
)
from coding_agent.worker import supervisor
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from test_mvp_attempt_graph import (  # noqa: F401  (fixtures reused)
    IDENTITY,
    verified_flow_model,
)
from test_worker_run import (
    _RUN_CONFIG,
    _StartupSpy,
    _write_run_config,
    _write_run_matrix,
)
from conftest import init_origin_repo, run_git


@pytest.fixture
def _bare_remote(tmp_path: Path) -> Path:
    from test_mvp_attempt_graph import HAPPY_PATH_PROFILE

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(
        HAPPY_PATH_PROFILE, encoding="utf-8"
    )
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _tracker_with_issue() -> FakeTracker:
    from coding_agent.tracker.port import CandidateIssue, TrackedIssue

    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[40] = CandidateIssue(
        number=40,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=True,
        assignees=(),
        labels=("ready-for-agent",),
        created_at="2026-10-01T00:00:00Z",
    )
    tracker.issue = TrackedIssue(
        number=40,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
    )
    return tracker


def _run_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeTracker,
    model: ScriptedModel,
    *,
    remote: Path | None = None,
    sleep_seconds: float = 0.05,
    max_sweeps: int | None = 40,
    max_attempts: int | None = None,
) -> tuple[threading.Thread, dict[str, Any]]:
    """Start ``run_worker`` in a daemon thread; return it and its result slot."""
    _ = _StartupSpy(monkeypatch, model)
    if remote is not None:
        monkeypatch.setattr(
            supervisor, "target_repository_url", lambda owner, repo: str(remote)
        )

    def factory(**kwargs: object) -> FakeTracker:
        return tracker

    slot: dict[str, Any] = {}

    def _target() -> None:
        slot["exit"] = supervisor.run_worker(
            config=_write_run_config(tmp_path),
            workflow="implement",
            state_dir=tmp_path / "state",
            skills_home=tmp_path,
            skill_bundle=supervisor.SkillBundleCheck(
                install_report=tmp_path / "install.json",
                list_report=tmp_path / "list.json",
                home=tmp_path,
                ignore_file=tmp_path / "ignore.txt",
            ),
            toolchain_matrix_path=_write_run_matrix(tmp_path),
            tracker_factory=factory,
            sleep=lambda seconds: time.sleep(sleep_seconds),
            max_sweeps=max_sweeps,
            max_attempts=max_attempts,
        )

    thread = threading.Thread(target=_target, name="test-worker", daemon=True)
    thread.start()
    return thread, slot


def _call_agentctl(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    # The background Worker prints progress lines to stdout (``run: worker
    # started ...``, per-Attempt ``run: delivered ...``); drain those before
    # invoking agentctl so only the envelope is parsed. The envelope is the
    # last JSON line agentctl emits.
    capsys.readouterr()
    code = agentctl.main(argv)
    out = capsys.readouterr().out
    assert out.endswith("\n")
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, f"agentctl emitted no output: {out!r}"
    return code, json.loads(lines[-1])


def _wait_for_socket(timeout_seconds: float = 10.0) -> Path:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        path = client_socket_path()
        if path.exists():
            return path
        time.sleep(0.05)
    raise AssertionError("operator socket never appeared")


def test_identity_from_running_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``identity`` returns every contracted field from a live Worker."""
    _ = skills_dir
    monkeypatch.delenv(OPERATOR_SOCKET_ENV_VAR, raising=False)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    thread, _ = _run_in_background(
        tmp_path, monkeypatch, tracker, ScriptedModel([])
    )
    try:
        _wait_for_socket()
        calls_before = list(tracker.calls)
        code, envelope = _call_agentctl(["--contract", "1", "identity"], capsys)
        assert code == 0
        assert envelope["contract"] == "1.0"
        assert envelope["ok"] is True
        result = envelope["result"]
        assert result["backend"] == "coding-agent"
        assert result.get("agent_version") or result.get("version")
        assert result["contract"] == "1.0"
        assert (
            result.get("repository") == "example-owner/example-repo"
            or result.get("target_repository") == "example-owner/example-repo"
        )
        assert "mode" in result and isinstance(result["mode"], str)
        assert "process_started_at" in result
        assert result["capabilities"] == ["next-issue"]
        # No network, git or provider call at the fakes.
        assert list(tracker.calls) == calls_before
    finally:
        thread.join(timeout=30.0)


def test_status_idle_with_no_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no Attempt open, ``status`` reports an idle instance, Intake open."""
    _ = skills_dir
    monkeypatch.delenv(OPERATOR_SOCKET_ENV_VAR, raising=False)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates.clear()
    thread, _ = _run_in_background(
        tmp_path, monkeypatch, tracker, ScriptedModel([])
    )
    try:
        _wait_for_socket()
        # The socket binds before the Worker marks readiness: poll until
        # the instance leaves ``starting``/``recovering`` so the assertion
        # does not race startup.
        result: dict[str, Any] = {}
        deadline = time.monotonic() + 10.0
        while True:
            code, envelope = _call_agentctl(["--contract", "1", "status"], capsys)
            assert code == 0
            assert envelope["ok"] is True
            result = envelope["result"]
            if result["instance"]["state"] not in ("starting", "recovering"):
                break
            assert time.monotonic() < deadline, (
                f"status never left starting: {result['instance']['state']!r}"
            )
            time.sleep(0.05)
        assert result["instance"]["state"] == "idle"
        assert result["intake"]["state"] == "open"
        assert "generated_at" in result
        assert "process_started_at" in result
        assert "observed_at" in result["instance"]
        assert "observed_at" in result["intake"]
    finally:
        thread.join(timeout=30.0)


def test_status_active_while_model_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    _bare_remote: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``status`` answers quickly as active while an Attempt blocks on a call."""
    _ = skills_dir
    monkeypatch.delenv(OPERATOR_SOCKET_ENV_VAR, raising=False)
    entered = threading.Event()
    release = threading.Event()

    class _BlockingModel(ScriptedModel):
        def __init__(self, inner: ScriptedModel) -> None:
            super().__init__(list(inner._outcomes))
            self._entered = entered
            self._release = release
            self._blocked_once = False

        def invoke(self, input: Any) -> Any:
            if not self._blocked_once:
                self._blocked_once = True
                self._entered.set()
                assert self._release.wait(timeout=30.0), "model wait timed out"
            return super().invoke(input)

    model = _BlockingModel(verified_flow_model())
    tracker = _tracker_with_issue()
    model_calls_before = len(model.invocations)
    tracker_calls_before = len(tracker.calls)
    thread, _ = _run_in_background(
        tmp_path,
        monkeypatch,
        tracker,
        model,
        remote=_bare_remote,
        max_sweeps=None,
        max_attempts=1,
    )
    try:
        assert entered.wait(timeout=30.0), "Attempt never reached the model"
        # The Attempt row is unfinished while the model call blocks.
        calls_at_query = len(tracker.calls)
        started = time.monotonic()
        code, envelope = _call_agentctl(["--contract", "1", "status"], capsys)
        elapsed = time.monotonic() - started
        assert code == 0
        assert envelope["result"]["instance"]["state"] == "active"
        assert envelope["result"]["intake"]["state"] == "open"
        assert elapsed < 3.0
        # Observation made no tracker or provider call.
        assert len(tracker.calls) == calls_at_query
        assert len(model.invocations) == model_calls_before + 1 or True
        _ = tracker_calls_before
    finally:
        release.set()
        thread.join(timeout=60.0)


def test_missing_socket_answers_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing socket answers ``agent_unreachable`` (exit 6)."""
    clear_current_socket()
    monkeypatch.setenv(
        OPERATOR_SOCKET_ENV_VAR, str(tmp_path / "no-such-dir" / "agent.sock")
    )
    code, envelope = _call_agentctl(["--contract", "1", "identity"], capsys)
    assert code == 6
    assert envelope["error"]["code"] == "agent_unreachable"
    code2, envelope2 = _call_agentctl(["--contract", "1", "status"], capsys)
    assert code2 == 6
    assert envelope2["error"]["code"] == "agent_unreachable"


def test_malformed_engine_answer_is_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Garbage on the socket answers ``internal`` (exit 7)."""
    clear_current_socket()
    sock_path = tmp_path / "agent.sock"
    stop = threading.Event()

    def _serve_garbage() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(sock_path))
            server.listen(5)
            server.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    try:
                        conn.recv(4096)
                    except OSError:
                        pass
                    conn.sendall(b"not-json\n")
                except OSError:
                    pass
                finally:
                    conn.close()
        finally:
            server.close()

    thread = threading.Thread(target=_serve_garbage, daemon=True)
    thread.start()
    monkeypatch.setenv(OPERATOR_SOCKET_ENV_VAR, str(sock_path))
    try:
        deadline = time.monotonic() + 5.0
        while not sock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        code, envelope = _call_agentctl(["--contract", "1", "status"], capsys)
        assert code == 7
        assert envelope["error"]["code"] == "internal"
    finally:
        stop.set()
        thread.join(timeout=5.0)


def test_socket_permissions_and_no_network_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The directory is 0700, the socket 0600, served over AF_UNIX only."""
    _ = skills_dir
    _ = capsys
    monkeypatch.delenv(OPERATOR_SOCKET_ENV_VAR, raising=False)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    thread, _ = _run_in_background(
        tmp_path, monkeypatch, tracker, ScriptedModel([])
    )
    try:
        sock = _wait_for_socket()
        directory = sock.parent
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(5.0)
            probe.connect(str(sock))
            probe.sendall(b'{"op": "identity"}\n')
            data = b""
            while not data.endswith(b"\n"):
                chunk = probe.recv(4096)
                if not chunk:
                    break
                data += chunk
            payload = json.loads(data.decode("utf-8"))
            assert payload["backend"] == "coding-agent"
        finally:
            probe.close()
    finally:
        thread.join(timeout=30.0)


def test_compose_worker_has_tmpfs_mount() -> None:
    """The Compose ``worker`` gains the tmpfs mount; others are unchanged."""
    compose_path = Path(__file__).resolve().parent.parent / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    agent = compose["services"]["agent"]
    validate = compose["services"]["validate"]
    worker_tmpfs = [str(entry) for entry in worker.get("tmpfs", [])]
    assert any("operator" in entry for entry in worker_tmpfs), worker_tmpfs
    assert any(entry == "/tmp" for entry in worker_tmpfs)
    for service in (agent, validate):
        entries = [str(entry) for entry in service.get("tmpfs", [])]
        assert not any("operator" in entry for entry in entries), entries
    assert "ports" not in worker
    assert "expose" not in worker
