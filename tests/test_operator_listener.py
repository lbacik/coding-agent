"""Control listener: identity and status from a running Worker (issue #248).

Contract: adoption step 4 of the operator contract (#245). An operator can
``docker exec`` into a running Worker and get ``identity`` and a minimal
``status`` back, even while an Attempt is blocked on a model call. Tests
call ``agentctl``'s entry point in-process over a Worker driven through
``supervisor.run_worker`` with the startup externals monkeypatched, the
``FakeStepTracker`` for GitHub and a blocking ``run_workflow`` stub for the
provider.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from coding_agent import agentctl
from coding_agent.identity.startup import Identity, StartupCheckResult
from coding_agent.inputs.authorisation import IdentityMode
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim, sweep_and_claim
from coding_agent.inputs.port import LabeledEvent, StepTrackerPort, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import (
    OPERATOR_SOCKET_ENV_VAR,
    clear_current_socket,
    client_socket_path,
)
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.skillbundle.verify import VerificationReport
from coding_agent.worker import supervisor
from conftest import init_origin_repo
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

LABEL = "agent:implement"
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"

_RUN_CONFIG = """\
version: 2

target:
  kind: github
  repository: example-owner/example-repo
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: sonnet
    input: {type: issue, label: agent:implement}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      work:
        agent:
          tools: [workspace-read]
          results:
            done: {}
        routes: {done: {end: completed}}
"""


def _write_run_config(tmp_path: Path, text: str = _RUN_CONFIG) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_run_matrix(tmp_path: Path) -> Path:
    path = tmp_path / "toolchain-matrix.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_manager": {"name": "uv", "version": "0.12.8"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _passing_bundle_report() -> VerificationReport:
    report = VerificationReport()
    report.add("bundle", True, "verified")
    return report


class _StartupSpy:
    """Patches every startup collaborator and records the order they ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, model: ScriptedModel) -> None:
        self.order: list[str] = []
        self.model = model
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(supervisor, "verify_installed_skill_bundle", self._bundle)
        monkeypatch.setattr(supervisor, "run_startup_checks", self._github)
        monkeypatch.setattr(supervisor, "build_chat_model", self._build)
        monkeypatch.setattr(supervisor, "assert_provider_capability", self._provider)
        self.bundle_report = _passing_bundle_report()
        self.github_error: Exception | None = None
        self.provider_error: Exception | None = None

    def _bundle(self, check: supervisor.SkillBundleCheck) -> VerificationReport:
        self.order.append("skill-bundle")
        return self.bundle_report

    def _github(self, *args: object, **kwargs: object) -> StartupCheckResult:
        self.order.append("github")
        if self.github_error is not None:
            raise self.github_error
        return StartupCheckResult(
            identity=IDENTITY,
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        )

    def _build(self, pin: object, **kwargs: object) -> ScriptedModel:
        return self.model

    def _provider(
        self, pin: object, model: object, table: object, **kwargs: object
    ) -> None:
        self.order.append("provider")
        if self.provider_error is not None:
            raise self.provider_error


@pytest.fixture
def _bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _tracker_with_issue() -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    tracker.inputs[40] = TrackedInput(
        number=40,
        input_type="issue",
        title="Add a greeting",
        body=BODY,
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
    )
    tracker.labeled_events[(40, LABEL)] = LabeledEvent(
        label=LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )
    return tracker


def _run_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
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

    def factory(**kwargs: object) -> FakeStepTracker:
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


def _calls_besides_intake_polling(tracker: FakeStepTracker) -> list[tuple[str, tuple[object, ...]]]:
    """Tracker calls other than the idle Worker's background Intake sweeps."""
    ignored = {
        "fetch_repository_owner_id",
        "fetch_agent_identity",
        "list_issue_candidates",
        "list_pull_request_candidates",
    }
    return [call for call in list(tracker.calls) if call[0] not in ignored]


def test_identity_from_running_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``identity`` returns every contracted field from a live Worker."""
    _ = skills_dir
    monkeypatch.delenv(OPERATOR_SOCKET_ENV_VAR, raising=False)
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    thread, _ = _run_in_background(
        tmp_path, monkeypatch, tracker, ScriptedModel([])
    )
    try:
        _wait_for_socket()
        calls_before = _calls_besides_intake_polling(tracker)
        code, envelope = _call_agentctl(["--contract", "1", "identity"], capsys)
        assert code == 0
        assert envelope["contract"] == "1.0"
        assert envelope["ok"] is True
        result = envelope["result"]
        assert result["backend"] == "coding-agent"
        assert result.get("agent_version") or result.get("version")
        assert result["contract"] == {"major": 1, "minor": 0}
        assert (
            result.get("repository") == "example-owner/example-repo"
            or result.get("target_repository") == "example-owner/example-repo"
        )
        assert result["mode"] == "online"
        assert "process_started_at" in result
        assert result["process_started_at"] is not None
        assert str(result["process_started_at"]).endswith("Z")
        assert result["capabilities"] == ["next-issue", "stop-after"]
        # No network, git or provider call at the fakes. The Worker's own
        # Intake loop keeps polling in the background and
        # may sweep at any moment during the call, so its polling is not
        # attributed to ``identity``.
        assert _calls_besides_intake_polling(tracker) == calls_before
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
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    tracker.inputs.clear()
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
        assert str(result["generated_at"]).endswith("Z")
        assert "process_started_at" in result["identity"]
        assert result["identity"]["mode"] == "online"
        assert result["identity"]["contract"] == {"major": 1, "minor": 0}
        assert "identity" in result
        assert "observed_at" in result["instance"]
        assert str(result["instance"]["observed_at"]).endswith("Z")
        assert "observed_at" in result["intake"]
        assert str(result["intake"]["observed_at"]).endswith("Z")
        assert result["intake"]["stop_plan"] is None
        assert result["intake"]["priority"] is None
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

    def _blocking_workflow(**kwargs: Any) -> WorkflowResult:
        attempt_id = str(kwargs["attempt_id"])
        ledger: RunLedger = kwargs["ledger"]
        entered.set()
        assert release.wait(timeout=30.0), "workflow wait timed out"
        ledger.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
        )

    monkeypatch.setattr(supervisor, "run_workflow", _blocking_workflow)
    tracker = _tracker_with_issue()
    thread, _ = _run_in_background(
        tmp_path,
        monkeypatch,
        tracker,
        ScriptedModel([]),
        remote=_bare_remote,
        max_sweeps=None,
        max_attempts=1,
    )
    try:
        assert entered.wait(timeout=30.0), "Attempt never reached the workflow"
        # The Attempt row is unfinished while the workflow blocks.
        calls_at_query = len(tracker.calls)
        started = time.monotonic()
        code, envelope = _call_agentctl(["--contract", "1", "status"], capsys)
        elapsed = time.monotonic() - started
        assert code == 0
        assert envelope["result"]["instance"]["state"] == "active"
        assert envelope["result"]["intake"]["state"] == "open"
        assert elapsed < 3.0
        # Observation made no tracker call.
        assert len(tracker.calls) == calls_at_query
        current = envelope["result"]["current_attempt"]
        assert current is not None
        assert current["attempt_id"] == "implement#40/1"
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
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
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


def test_compose_worker_discovery_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Compose ``worker`` is the one Managed Instance: four discovery labels.

    Contract spec §2.1 (adoption step 4, #245): the labels sit on the managed
    service through the deployment, never as an image ``LABEL``. The instance
    name resolves from ``AGENT_INSTANCE_NAME`` with a safe default, so the
    service is discoverable with nothing set.
    """
    compose_path = Path(__file__).resolve().parent.parent / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]
    worker = services["worker"]
    labels = worker.get("labels", {})
    assert isinstance(labels, dict), labels
    assert labels.get("io.github.lbacik.agent-operator.contract") == "1"
    instance = labels.get("io.github.lbacik.agent-operator.instance", "")
    assert isinstance(instance, str), instance
    match = re.fullmatch(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([a-z0-9][a-z0-9-]{0,62})\}",
        instance,
    )
    assert match is not None, instance
    assert labels.get("io.github.lbacik.agent-operator.exec-user") == "agent"
    assert labels.get("io.github.lbacik.agent-operator.backend") == "coding-agent"
    assert worker.get("restart") == "no"
    var_name, default = match.group(1), match.group(2)
    monkeypatch.delenv(var_name, raising=False)
    resolved = os.environ.get(var_name, default)
    assert resolved == default
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", resolved) is not None
    for name in ("agent", "validate"):
        other = services[name].get("labels", {})
        assert not any(
            str(key).startswith("io.github.lbacik.agent-operator.")
            for key in other
        ), (name, other)


# --- ``next issue N`` priority: commit path and Claim boundary (S10) -----------


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore]:
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository="example-owner/example-repo",
        process_started_at=_started_at(),
    )
    return ledger, store


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login=IDENTITY.login,
        agent_id=IDENTITY.account_id,
        effective_configuration={"workflow": "implement"},
    )


def _tracker_with_two_inputs() -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    for number, created in ((40, "2026-10-01T00:00:00Z"), (41, "2026-10-02T00:00:00Z")):
        tracker.inputs[number] = TrackedInput(
            number=number,
            input_type="issue",
            title="Add a greeting",
            body=BODY,
            is_open=True,
            labels=(LABEL,),
            assignees=(),
            created_at=created,
            author_login="human",
            author_id=7,
        )
        tracker.labeled_events[(number, LABEL)] = LabeledEvent(
            label=LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at=created,
        )
    return tracker


def _next_request(request_id: str, issue: int) -> bytes:
    body: dict[str, Any] = {
        "op": "next",
        "request_id": request_id,
        "expect_repo": "example-owner/example-repo",
        "issue": issue,
    }
    return (json.dumps(body) + "\n").encode("utf-8")


def _tracker_factory_for(tracker: FakeStepTracker) -> Callable[[], StepTrackerPort]:
    def factory() -> StepTrackerPort:
        return tracker

    return factory


def test_next_issue_commits_eligible_and_priority_claims_it(tmp_path: Path) -> None:
    """An eligible ``next`` commits; the priority sweep claims it first."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with_two_inputs()
        live = LiveView(process_started_at=_started_at())
        live.set_ready()
        config = _claim_config()
        mode: IdentityMode = "shared-identity"
        reply = operator_listener._handle_one(
            _next_request("req-next-1", 41),
            ledger_path=ledger.path,
            live=live,
            claim_config=config,
            tracker_factory=_tracker_factory_for(tracker),
            ledger=ledger,
            identity_mode=mode,
        )
        assert reply is not None
        assert reply.get("replayed") is False
        assert reply["command"]["arguments"] == {"issue": 41}
        claimed = sweep_and_claim(tracker, ledger, config, priority=41)
        assert isinstance(claimed, ConfirmedClaim)
        assert claimed.input_number == 41
        assert claimed.attempt_id == "implement#41/1"
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="completed", reason="verified-completion"
        )
        ledger.set_attempt_state(claimed.attempt_id, "finished")
        tracker.remove_label(41, "issue", LABEL)
        tracker.unassign_input(41, "issue")
        following = sweep_and_claim(tracker, ledger, config)
        assert isinstance(following, ConfirmedClaim)
        assert following.input_number == 40
    finally:
        store.close()
        ledger.close()


def test_next_issue_rejects_definitely_ineligible(tmp_path: Path) -> None:
    """A closed priority input is rejected with its guard reason."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with_two_inputs()
        held = tracker.inputs[41]
        tracker.inputs[41] = TrackedInput(
            number=held.number,
            input_type=held.input_type,
            title=held.title,
            body=held.body,
            is_open=False,
            labels=held.labels,
            assignees=held.assignees,
            created_at=held.created_at,
            author_login=held.author_login,
            author_id=held.author_id,
        )
        live = LiveView(process_started_at=_started_at())
        live.set_ready()
        reply = operator_listener._handle_one(
            _next_request("req-next-closed", 41),
            ledger_path=ledger.path,
            live=live,
            claim_config=_claim_config(),
            tracker_factory=_tracker_factory_for(tracker),
            ledger=ledger,
            identity_mode="shared-identity",
        )
        assert reply is not None
        assert reply["error_code"] == "rejected"
        assert reply["details"] == {"issue": 41, "reason": "closed"}
    finally:
        store.close()
        ledger.close()


def test_next_issue_rejects_unreadable_commit_time_read(tmp_path: Path) -> None:
    """A failed commit-time read rejects as ``unreadable`` (S10 §5.4)."""

    class _UnreadableTracker(FakeStepTracker):
        def fetch_input(self, number: int, input_type: Any) -> TrackedInput:
            raise RuntimeError("commit-time read failed")

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _UnreadableTracker(
            agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
        )
        live = LiveView(process_started_at=_started_at())
        live.set_ready()
        reply = operator_listener._handle_one(
            _next_request("req-next-unreadable", 41),
            ledger_path=ledger.path,
            live=live,
            claim_config=_claim_config(),
            tracker_factory=_tracker_factory_for(tracker),
            ledger=ledger,
            identity_mode="shared-identity",
        )
        assert reply is not None
        assert reply["error_code"] == "rejected"
        assert reply["details"] == {"issue": 41, "reason": "unreadable"}
    finally:
        store.close()
        ledger.close()
