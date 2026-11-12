"""The Worker: ``agent run``'s startup, poll loop and intake (contract §4).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup in
order; a failure at any step exits non-zero with no Attempt opened and no
tracker write; the poll loop sleeps only when a sweep claimed nothing and
selects again immediately after an Attempt; a Credential Failure halts at
once, three consecutive infrastructure-class failures halt only after the
third Attempt is finalized), §5 (``(created_at, number)`` order) and §6 (the
Worker's Claim opens the Attempt the run then works).

Matrix row ids: REC-11 (legacy refusal is covered in
``test_step_legacy_startup.py``), OP-15/OP-16 smoke hooks, and the retained
lifecycle rows (see the acceptance matrix).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.errors import CredentialFailure, GitHubFailure
from coding_agent.identity.startup import Identity, StartupCheckFailed, StartupCheckResult
from coding_agent.identity.startup import run_startup_checks as real_run_startup_checks
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim, ReleasedClaim
from coding_agent.inputs.claim import sweep_and_claim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import ProviderCapabilityRefused
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.skillbundle.verify import VerificationReport
from coding_agent.worker import supervisor
from coding_agent.worker.run import (
    POLL_SLEEP_SLICE_SECONDS,
    WorkerLoopConfig,
    run_worker_loop,
)
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

LABEL = "agent:implement"
EFFECTIVE = {"workflow": "implement"}
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


def _assert_no_attempt_opened(ledger_path: Path) -> None:
    """A startup refusal opens no Attempt, though the operator store may exist.

    Adoption step 4 (#248): the operator store opens right after the volume
    lock — in the ledger's SQLite file — so a refusal at skill-bundle,
    github-checks or later leaves the file behind with its operator tables.
    What must still hold is P-WRK-3: no Attempt row and no tracker write.
    """
    if not ledger_path.exists():
        return
    connection = sqlite3.connect(str(ledger_path))
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "attempts" in tables:
            count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()
            assert count is not None and int(count[0]) == 0
    finally:
        connection.close()


def _issue_input(number: int, created_at: str) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=f"Add a greeting {number}",
        body=BODY,
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _human_event(label: str = LABEL) -> LabeledEvent:
    return LabeledEvent(
        label=label,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeStepTracker:
    """A step tracker with human-authorised Selection Labels on every input."""
    tracker = FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = _issue_input(number, created_at)
        tracker.labeled_events[(number, LABEL)] = _human_event()
    return tracker


def _claim_config(poll_label: str = LABEL) -> ClaimConfig:
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=poll_label,
        base_branch="main",
        agent_login=IDENTITY.login,
        agent_id=IDENTITY.account_id,
        effective_configuration=dict(EFFECTIVE),
    )


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, Any]],
    routes: dict[str, Any],
) -> V2Step:
    parsed = {
        name: (target if isinstance(target, V2RouteTarget) else V2RouteTarget(step=target))
        for name, target in routes.items()
    }
    return V2Step(
        id=step_id,
        agent=V2Agent(results=results),  # type: ignore[arg-type]
        max_executions=1,
        routes=parsed,
    )


def _end(category: str) -> V2RouteTarget:
    return V2RouteTarget(end=category)  # type: ignore[arg-type]


def _workflow() -> V2Workflow:
    """One agent step routed straight to ``delivered``: the loop-test Workflow."""
    return V2Workflow(
        id="implement",
        model="sonnet",
        input=V2Input(type="issue", label=LABEL),  # type: ignore[arg-type]
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        defaults_routes={"error": _end("failed"), "limit-exceeded": _end("unfinished")},
        steps={
            "work": _agent_step("work", {"done": {}}, {"done": _end("delivered")}),
        },
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _loop_config(poll: float = 60.0) -> WorkerLoopConfig:
    return WorkerLoopConfig(
        claim_config=_claim_config(), workflow=_workflow(), poll_interval_seconds=poll
    )


def _recording_runner(
    ledger: RunLedger, reasons: dict[int, tuple[str, str]], ran: list[int]
) -> Callable[[ConfirmedClaim], WorkflowResult]:
    """Records each claimed Attempt's scripted outcome, as the graph would."""

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        outcome, reason = reasons.get(
            claimed.input_number, ("delivered", "verified-completion")
        )
        ledger.record_terminal_outcome(claimed.attempt_id, outcome=outcome, reason=reason)
        return WorkflowResult(
            attempt_id=claimed.attempt_id, outcome=outcome, reason=reason
        )

    return run_one


def _release_on_finish(
    tracker: FakeStepTracker,
    runner: Callable[[ConfirmedClaim], WorkflowResult],
) -> Callable[[ConfirmedClaim], WorkflowResult]:
    """The terminal writes a stub runner skips: label off, assignment off."""

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        result = runner(claimed)
        tracker.remove_label(claimed.input_number, "issue", LABEL)
        tracker.unassign_input(claimed.input_number, "issue")
        return result

    return run_one


def test_sweep_claims_the_oldest_authorised_input_first(ledger: RunLedger) -> None:
    """§5: the sweep tries candidates in ``(created_at, number)`` order."""
    tracker = _tracker_with((31, "2026-10-02T00:00:00Z"), (32, "2026-10-01T00:00:00Z"))

    claimed = sweep_and_claim(tracker, ledger, _claim_config())

    assert isinstance(claimed, ConfirmedClaim)
    assert (claimed.attempt_id, claimed.input_number, claimed.n) == (
        "implement#32/1",
        32,
        1,
    )


def test_a_claim_without_authorisation_selects_nothing(ledger: RunLedger) -> None:
    """§5.1: without a human ``labeled`` event the input is never claimed."""
    tracker = FakeStepTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    tracker.inputs[7] = _issue_input(7, "2026-10-01T00:00:00Z")

    assert sweep_and_claim(tracker, ledger, _claim_config()) is None
    assert not ledger.has_unfinished_attempt()


# --- The poll loop (§4), driven with a stub Attempt runner ---------------


def test_p_wrk_4_idle_the_worker_sleeps_for_the_poll_interval(ledger: RunLedger) -> None:
    """P-WRK-4: a sweep that claims nothing sleeps exactly the poll interval.

    The idle sleep is interruptible (§12, REC-SIG-1): it yields every
    0.5 s so a SIGTERM mid-sleep stops intake at once, keeping the same
    totals. The assertion is therefore on the per-sweep sums, not the
    chunking.
    """
    tracker = _tracker_with()
    sleeps: list[float] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(poll=42.0),
        run_one_attempt=_recording_runner(ledger, {}, []),
        sleep=sleeps.append,
        max_sweeps=3,
    )

    assert exit_code == 0
    assert len(sleeps) == 2 * int(42.0 / POLL_SLEEP_SLICE_SECONDS)
    assert all(chunk <= POLL_SLEEP_SLICE_SECONDS + 1e-9 for chunk in sleeps)
    assert abs(sum(sleeps) - 2 * 42.0) < 1e-9
    assert "assign_input" not in tracker.call_names()


def test_p_wrk_4_after_an_attempt_the_worker_selects_again_immediately(
    ledger: RunLedger,
) -> None:
    """P-WRK-4 and §5: two Attempts back to back, oldest first, no sleep."""
    tracker = _tracker_with((31, "2026-10-02T00:00:00Z"), (32, "2026-10-01T00:00:00Z"))
    ran: list[int] = []
    sleeps: list[float] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(tracker, _recording_runner(ledger, {}, ran)),
        sleep=sleeps.append,
        max_attempts=2,
    )

    assert exit_code == 0
    assert ran == [32, 31]
    assert sleeps == []
    assert ledger.get_attempt("implement#32/1").state == "finished"
    assert ledger.get_attempt("implement#31/1").state == "finished"


def test_p_wrk_1_a_recorded_credential_failure_halts_at_once(ledger: RunLedger) -> None:
    """P-WRK-1: non-zero exit, the next candidate is never claimed."""
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"), (32, "2026-10-02T00:00:00Z"))
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(
            ledger, {31: ("failed", "credential-failure")}, ran
        ),
        sleep=lambda seconds: None,
        max_attempts=5,
    )

    assert exit_code == 1
    assert ran == [31]
    assert ledger.get_attempt("implement#31/1").state == "finished"
    assert ledger.get_outcome("implement#31/1") is not None
    assert ledger.get_outcome("implement#31/1").reason == "credential-failure"
    assert not ledger.has_unfinished_attempt()


def test_p_wrk_1_a_credential_failure_escaping_the_run_halts_at_once(
    ledger: RunLedger,
) -> None:
    """P-WRK-1: a Credential Failure escaping the run is recorded and halts."""
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise CredentialFailure("GET", "/repos/o/r/issues/31")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=5,
    )

    assert exit_code == 1
    outcome = ledger.get_outcome("implement#31/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("failed", "credential-failure")
    assert "post_comment" not in tracker.call_names()


def test_p_wrk_1_a_revoked_credential_during_selection_halts(ledger: RunLedger) -> None:
    """A credential revoked while the Worker idles halts it non-zero (§4)."""

    class RevokedTracker(FakeStepTracker):
        def list_issue_candidates(self, selection_label: str) -> list[TrackedInput]:
            raise CredentialFailure("GET", "/repos/o/r/issues")

    exit_code = run_worker_loop(
        tracker=RevokedTracker(
            agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
        ),
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(ledger, {}, []),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1


def test_c_err_2_a_failed_sweep_is_retried_at_the_next_poll(
    ledger: RunLedger,
) -> None:
    """§4: a failed sweep is not a crash: the Worker sleeps and selects again."""
    calls = 0
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

    class FlakyTracker(FakeStepTracker):
        def list_issue_candidates(self, selection_label: str) -> list[TrackedInput]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise GitHubFailure(
                    "GET",
                    "/repos/o/r/issues",
                    502,
                    reason="server-error",
                    failure_class="github",
                )
            return super().list_issue_candidates(selection_label)

    flaky = FlakyTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    flaky.inputs.update(tracker.inputs)
    flaky.labeled_events.update(tracker.labeled_events)
    ran: list[int] = []
    slept: list[float] = []

    exit_code = run_worker_loop(
        tracker=flaky,
        ledger=ledger,
        loop_config=_loop_config(poll=60.0),
        run_one_attempt=_release_on_finish(flaky, _recording_runner(ledger, {}, ran)),
        sleep=slept.append,
        max_attempts=1,
    )

    assert exit_code == 0
    assert ran == [31]
    assert len(slept) == int(60.0 / POLL_SLEEP_SLICE_SECONDS)
    assert all(chunk <= POLL_SLEEP_SLICE_SECONDS + 1e-9 for chunk in slept)
    assert abs(sum(slept) - 60.0) < 1e-9


def test_a_claim_interrupted_after_the_attempt_opened_halts_rather_than_idling(
    ledger: RunLedger,
) -> None:
    """§4: a failure mid-Claim leaves an open Attempt, which closes intake.

    Retrying the sweep would idle forever with nothing said, so the Worker
    halts non-zero and leaves the Attempt to startup recovery (§16.1).
    """
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    class FailingAssignTracker(FakeStepTracker):
        fail_next_assign: bool = True

        def assign_input(self, number: int, input_type: Any) -> None:
            if self.fail_next_assign:
                self.fail_next_assign = False
                raise GitHubFailure(
                    "PUT",
                    f"/repos/o/r/issues/{number}/assignees",
                    502,
                    reason="server-error",
                    failure_class="github",
                )
            super().assign_input(number, input_type)

    failing = FailingAssignTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    failing.inputs.update(tracker.inputs)
    failing.labeled_events.update(tracker.labeled_events)

    exit_code = run_worker_loop(
        tracker=failing,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(ledger, {}, []),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    assert ledger.has_unfinished_attempt()


INFRA = ("failed", "infrastructure-failure: provider timeout")


def test_p_wrk_2_three_infrastructure_failures_halt_after_the_third_is_finalized(
    ledger: RunLedger,
) -> None:
    """P-WRK-2: halt only after the third Attempt is finalized; a restart
    finds nothing unfinished, so it does not loop."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
        (34, "2026-10-04T00:00:00Z"),
    )
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(
            tracker, _recording_runner(ledger, {31: INFRA, 32: INFRA, 33: INFRA}, ran)
        ),
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    assert ran == [31, 32, 33]
    assert ledger.get_attempt("implement#33/1").state == "finished"
    assert not ledger.has_unfinished_attempt()


def test_p_wrk_2_the_counter_resets_after_an_attempt_without_such_a_failure(
    ledger: RunLedger,
) -> None:
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
        (34, "2026-10-04T00:00:00Z"),
    )
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(
            tracker, _recording_runner(ledger, {31: INFRA, 32: INFRA, 34: INFRA}, ran)
        ),
        sleep=lambda seconds: None,
        max_attempts=4,
    )

    assert exit_code == 0
    assert ran == [31, 32, 33, 34]


# --- ``run_worker``: startup in order, then the step runtime (§4) ----------

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


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    *,
    config: Path | None = None,
    remote: Path | None = None,
    skills_home: Path | None = None,
    max_attempts: int | None = None,
    max_sweeps: int | None = None,
) -> tuple[int, list[dict[str, object]]]:
    built: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeStepTracker:
        built.append(kwargs)
        return tracker

    if remote is not None:
        monkeypatch.setattr(
            supervisor, "target_repository_url", lambda owner, repo: str(remote)
        )
    exit_code = supervisor.run_worker(
        config=config if config is not None else _write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skills_home=skills_home if skills_home is not None else tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        tracker_factory=factory,
        sleep=lambda seconds: None,
        max_attempts=max_attempts,
        max_sweeps=max_sweeps,
    )
    return exit_code, built


def _bare_remote(tmp_path: Path) -> Path:
    """A local bare remote the Worker can mirror and check out offline."""
    from conftest import init_origin_repo, run_git

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    return bare


def test_agent_run_takes_two_inputs_in_order_to_recorded_terminal_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A scripted end-to-end run over the step tracker (issue #153).

    Two eligible inputs are claimed in ``(created_at, number)`` order, one
    at a time, each through the stubbed Workflow run to ``delivered``; the
    real ``_finalise_live_attempt`` performs the terminal writes.
    """
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    tracker = _tracker_with((41, "2026-10-02T00:00:00Z"), (40, "2026-10-01T00:00:00Z"))
    remote = _bare_remote(tmp_path)
    recorded: list[str] = []

    def _stub_workflow(**kwargs: Any) -> WorkflowResult:
        attempt_id = str(kwargs["attempt_id"])
        ledger = kwargs["ledger"]
        ledger.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        recorded.append(attempt_id)
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
        )

    monkeypatch.setattr(supervisor, "run_workflow", _stub_workflow)

    exit_code, built = _run(
        tmp_path, monkeypatch, tracker, remote=remote, max_attempts=2
    )

    assert exit_code == 0
    assert spy.order == ["skill-bundle", "github", "provider"]
    assert recorded == ["implement#40/1", "implement#41/1"]
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        for attempt_id in ("implement#40/1", "implement#41/1"):
            outcome = ledger.get_outcome(attempt_id)
            assert outcome is not None
            assert (outcome.outcome, outcome.reason) == (
                "completed",
                "verified-completion",
            )
            assert ledger.get_attempt(attempt_id).state == "finished"
    finally:
        ledger.close()
    # The real finalisation posted one terminal comment per Attempt.
    assert tracker.call_names().count("post_comment") == 2
    out = capsys.readouterr().out
    assert "run: completed (verified-completion); attempt=implement#40/1" in out
    assert "run: completed (verified-completion); attempt=implement#41/1" in out
    assert built and built[0]["identity"] == IDENTITY


@pytest.mark.parametrize(
    ("refusal", "expected_order"),
    [
        ("skill-bundle", ["skill-bundle"]),
        ("github", ["skill-bundle", "github"]),
        ("provider", ["skill-bundle", "github", "provider"]),
    ],
)
def test_p_wrk_3_a_startup_refusal_exits_nonzero_with_no_attempt_and_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refusal: str,
    expected_order: list[str],
) -> None:
    """P-WRK-3: the Skill Bundle, the GitHub checks or the Provider
    Capability Assertion refuses; nothing after it runs."""
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    if refusal == "skill-bundle":
        spy.bundle_report = VerificationReport()
        spy.bundle_report.add("closure", False, "missing /tdd")
    elif refusal == "github":
        spy.github_error = StartupCheckFailed("GET /user returned 401: Bad credentials")
    else:
        spy.provider_error = ProviderCapabilityRefused("no tool call arrived")
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))

    exit_code, built = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 1
    assert spy.order == expected_order
    assert built == []
    assert tracker.calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    assert "run: startup refused" in capsys.readouterr().err


def test_l3_err_8_a_restart_after_a_credential_failure_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """L3-ERR-8: the revoked token fails ``GET /user``; startup refuses."""
    _StartupSpy(monkeypatch, ScriptedModel([]))
    monkeypatch.setattr(supervisor, "run_startup_checks", real_run_startup_checks)
    requests_mock.get(
        "https://api.github.com/user",
        status_code=401,
        json={"message": "Bad credentials"},
    )
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))

    exit_code, built = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 1
    assert built == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")


def test_an_invalid_configuration_refuses_before_any_other_startup_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4 step 1: the YAML is validated before anything else (model refs are
    statically checked, so a broken model ref refuses here)."""
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    config = _write_run_config(tmp_path, _RUN_CONFIG.replace("model: sonnet", "model: nope"))

    exit_code, built = _run(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, max_sweeps=1
    )

    assert exit_code == 1
    assert spy.order == []
    assert built == []


def test_a_setup_failure_outside_the_run_is_recorded_finalized_and_counted(
    ledger: RunLedger,
) -> None:
    """§4: setup is an infrastructure class. An ``OSError`` escaping the run
    is recorded, finalized and counted toward the halt-after-three; intake
    never stays closed."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
    )

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise OSError(28, "No space left on device")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    outcome = ledger.get_outcome("implement#31/1")
    assert outcome is not None
    assert outcome.outcome == "failed"
    assert outcome.reason.startswith("infrastructure-failure: setup")
    assert not ledger.has_unfinished_attempt()


def test_a_halt_is_not_masked_by_the_attempt_bound(ledger: RunLedger) -> None:
    """A test bound reached by the halting Attempt still exits non-zero."""
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(
            ledger, {31: ("failed", "credential-failure")}, []
        ),
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 1


def test_claim_released_where_read_back_loses_is_not_an_attempt(
    ledger: RunLedger,
) -> None:
    """§6.2: a lost Claim race releases; the sweep moves on to the next input."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"), (8, "2026-10-02T00:00:00Z"))

    def _race(number: int) -> None:
        # A rival wins input 7 between our Claim writes: it loses the label.
        if number == 7:
            held = tracker.inputs[7]
            tracker.inputs[7] = TrackedInput(
                number=held.number,
                input_type=held.input_type,
                title=held.title,
                body=held.body,
                is_open=held.is_open,
                labels=(),
                assignees=held.assignees,
                created_at=held.created_at,
                author_login=held.author_login,
                author_id=held.author_id,
            )

    tracker.on_assign = _race

    claimed = sweep_and_claim(tracker, ledger, _claim_config())

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 8
    assert claimed.attempt_id == "implement#8/1"
    assert ledger.get_attempt("implement#7/1").state == "released"
