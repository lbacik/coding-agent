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
from coding_agent.identity.startup import Identity, StartupCheckFailed
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.claim import sweep_and_claim
from coding_agent.inputs.port import InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import ProviderCapabilityRefused
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.step_finalisation import (
    FinalisationTarget,
    WorkflowEnded,
    finish_attempt,
)
from coding_agent.skillbundle.deployment import (
    DeploymentFault,
    RunVerificationRefusal,
)
from coding_agent.worker import supervisor
from coding_agent.worker.run import (
    POLL_SLEEP_SLICE_SECONDS,
    WorkerLoopConfig,
    run_worker_loop,
)
from coding_agent.worker.startup import StartupRefusal
from worker_fakes import WorkerFakes, make_worker_ports
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    WorkflowStep,
    Workflow,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker
from support.run_inputs import make_run_inputs
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

LABEL = "agent:implement"
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


def _claim_config(
    poll_label: str = LABEL, workflow: Workflow | None = None
) -> ClaimConfig:
    selected = workflow if workflow is not None else _workflow()
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=poll_label,
        base_branch="main",
        agent_login=IDENTITY.login,
        agent_id=IDENTITY.account_id,
        effective_configuration=full_effective_configuration(selected),
    )


def _agent_step(
    step_id: str,
    results: dict[str, dict[str, Any]],
    routes: dict[str, Any],
) -> WorkflowStep:
    parsed = {
        name: (target if isinstance(target, RouteTarget) else RouteTarget(step=target))
        for name, target in routes.items()
    }
    return WorkflowStep(
        id=step_id,
        agent=Agent(results=results),
        max_executions=1,
        routes=parsed,
    )


def _end(category: str) -> RouteTarget:
    return RouteTarget(end=category)


def _workflow(
    outcomes: dict[str, tuple[str, ...]] | None = None,
) -> Workflow:
    """One agent step routed straight to ``delivered``: the loop-test Workflow."""
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=LABEL),
        ceiling=AttemptCeiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        outcomes=outcomes if outcomes is not None else {},
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


def _loop_config(
    poll: float = 60.0, workflow: Workflow | None = None
) -> WorkerLoopConfig:
    selected = workflow if workflow is not None else _workflow()
    return WorkerLoopConfig(
        claim_config=_claim_config(workflow=selected),
        workflow=selected,
        poll_interval_seconds=poll,
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
    outcome = ledger.get_outcome("implement#31/1")
    assert outcome is not None
    assert outcome.reason == "credential-failure"
    assert not ledger.has_unfinished_attempt()


def test_p_wrk_1_a_credential_failure_escaping_the_run_halts_at_once(
    ledger: RunLedger,
) -> None:
    """P-WRK-1: a Credential Failure escaping the run is recorded and halts.

    The live Credential Failure records ``interrupted`` /
    ``credential-failure`` (REC-17) with no terminal writes, leaving the
    Attempt unfinished for recovery after the credential is fixed.
    """
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
    assert (outcome.outcome, outcome.reason) == (
        "interrupted",
        "credential-failure",
    )
    assert "post_comment" not in tracker.call_names()
    assert ledger.get_attempt("implement#31/1").state != "finished"


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
version: 3

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
                "schema": 2,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_managers": {"uv": {"version": "0.12.8"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _fakes(
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    model: ScriptedModel,
    *,
    remote: Path | None = None,
    skill_refusal: RunVerificationRefusal | None = None,
    github_error: Exception | None = None,
    provider_error: Exception | None = None,
    real_github_checks: bool = False,
) -> WorkerFakes:
    """Fake startup ports handing out ``tracker`` and ``model``."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return make_worker_ports(
        model=model,
        tracker=tracker,
        skill_refusal=skill_refusal,
        github_error=github_error,
        provider_error=provider_error,
        real_github_checks=real_github_checks,
        repository_url=str(remote) if remote is not None else None,
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    *,
    config: Path | None = None,
    remote: Path | None = None,
    max_attempts: int | None = None,
    max_sweeps: int | None = None,
    fakes: WorkerFakes | None = None,
    workflow_runner: Callable[..., WorkflowResult] | None = None,
) -> tuple[int, WorkerFakes]:
    active = fakes if fakes is not None else _fakes(
        monkeypatch, tracker, ScriptedModel([]), remote=remote
    )
    if active.github.tracker is None:
        active.github.tracker = tracker
    if remote is not None and active.github.repository_url is None:
        active.github.repository_url = str(remote)
    exit_code = supervisor.run_worker(
        config=config if config is not None else _write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=active.ports,
        sleep=lambda seconds: None,
        max_attempts=max_attempts,
        max_sweeps=max_sweeps,
        workflow_runner=workflow_runner,
    )
    return exit_code, active


def _bare_remote(tmp_path: Path) -> Path:
    """A local bare remote the Worker can mirror and check out offline."""
    from conftest import commit_validation_profile, init_origin_repo, run_git

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    commit_validation_profile(src)
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
    model = ScriptedModel([])
    tracker = _tracker_with((41, "2026-10-02T00:00:00Z"), (40, "2026-10-01T00:00:00Z"))
    remote = _bare_remote(tmp_path)
    recorded: list[str] = []

    def _stub_workflow(**kwargs: Any) -> WorkflowResult:
        attempt_id = str(kwargs["attempt_id"])
        recorded.append(attempt_id)
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
            ended=True,
        )

    exit_code, fakes = _run(
        tmp_path, monkeypatch, tracker, remote=remote, max_attempts=2,
        fakes=_fakes(monkeypatch, tracker, model, remote=remote),
        workflow_runner=_stub_workflow,
    )

    assert exit_code == 0
    assert fakes.order == ["skill-bundle", "github", "provider"]
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
    assert fakes.github.tracker_calls
    assert fakes.github.tracker_calls[0]["identity"] == IDENTITY


def _halt_row(state_dir: Path) -> tuple[str, str]:
    """The durable Halt recorded in the operator store's SQLite file."""
    connection = sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        row = connection.execute(
            "SELECT reason, detail FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0]), str(row[1])


@pytest.mark.parametrize(
    ("refusal", "expected_order", "phase", "message"),
    [
        (
            "skill-bundle",
            ["skill-bundle"],
            "skill-bundle",
            "Skill Bundle deployment verification failed",
        ),
        (
            "github",
            ["skill-bundle", "github"],
            "github-checks",
            "GitHub startup check failed",
        ),
        (
            "provider",
            ["skill-bundle", "github", "provider"],
            "provider-capability",
            "provider-capability-refused",
        ),
    ],
)
def test_p_wrk_3_a_startup_refusal_exits_nonzero_with_no_attempt_and_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refusal: str,
    expected_order: list[str],
    phase: str,
    message: str,
) -> None:
    """P-WRK-3: the Skill Bundle, the GitHub checks or the Provider
    Capability Assertion refuses; nothing after it runs."""
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))
    if refusal == "skill-bundle":
        skill_refusal: RunVerificationRefusal | None = RunVerificationRefusal(
            faults=(DeploymentFault(phase="bundle", path="", detail="missing"),),
        )
        github_error: Exception | None = None
        provider_error: Exception | None = None
    elif refusal == "github":
        skill_refusal = None
        github_error = StartupCheckFailed("GET /user returned 401: Bad credentials")
        provider_error = None
    else:
        skill_refusal = None
        github_error = None
        provider_error = ProviderCapabilityRefused("no tool call arrived")
    fakes = _fakes(
        monkeypatch,
        tracker,
        ScriptedModel([]),
        skill_refusal=skill_refusal,
        github_error=github_error,
        provider_error=provider_error,
    )

    result = supervisor.start(
        config=_write_run_config(tmp_path),
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=fakes.ports,
    )

    assert isinstance(result, StartupRefusal)
    assert result.phase == phase
    assert message in result.message
    assert fakes.order == expected_order
    if refusal == "provider":
        # The owner lookup runs inside github-checks, before
        # provider-capability, so the tracker is already built.
        assert len(fakes.github.tracker_calls) == 1
        assert tracker.call_names() == ["fetch_repository_owner_id"]
    else:
        assert fakes.github.tracker_calls == []
        assert tracker.calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    reason, detail = _halt_row(tmp_path / "state")
    assert reason == "startup-check-refused"
    assert message in detail
    assert f"run: startup refused at {phase} after " in capsys.readouterr().err


def test_l3_err_8_a_restart_after_a_credential_failure_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """L3-ERR-8: the revoked token fails ``GET /user``; startup refuses."""
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))
    requests_mock.get(
        "https://api.github.com/user",
        status_code=401,
        json={"message": "Bad credentials"},
    )

    exit_code, fakes = _run(
        tmp_path,
        monkeypatch,
        tracker,
        max_sweeps=1,
        fakes=_fakes(
            monkeypatch, tracker, ScriptedModel([]), real_github_checks=True
        ),
    )

    assert exit_code == 1
    assert fakes.github.tracker_calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")


def test_an_invalid_configuration_refuses_before_any_other_startup_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4 step 1: the YAML is validated before anything else (model refs are
    statically checked, so a broken model ref refuses here)."""
    config = _write_run_config(tmp_path, _RUN_CONFIG.replace("model: sonnet", "model: nope"))

    exit_code, fakes = _run(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, max_sweeps=1
    )

    assert exit_code == 1
    assert fakes.order == []
    assert fakes.github.tracker_calls == []


_TWO_MODEL_RUN_CONFIG = """\
version: 3

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
  gpt:
    provider: openai
    model: gpt-5.6-luna
    endpoint: responses
    effort: medium
    api-key-env: OPENAI_API_KEY
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
  review:
    model: gpt
    input: {type: pull-request, label: agent:review}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: triage
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      triage:
        agent:
          tools: [workspace-read]
          results:
            done: {summary: document}
        routes: {done: publish}
      publish:
        operation: publish-review
        with: {body: triage.done.summary}
        routes: {published: {end: completed}, stale-head: {end: failed}}
"""


def test_run_refuses_a_missing_selected_model_key_before_any_live_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SKV3-21: the credentials phase refuses a missing selected-Workflow
    model key before any live call, Attempt or GitHub write."""
    fakes = _fakes(monkeypatch, FakeStepTracker(), ScriptedModel([]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    exit_code, fakes = _run(
        tmp_path, monkeypatch, FakeStepTracker(), max_sweeps=1, fakes=fakes
    )

    assert exit_code == 1
    assert fakes.order == ["skill-bundle"]
    assert fakes.github.tracker_calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    err = capsys.readouterr().err
    assert "run: startup refused at credentials" in err
    assert "ANTHROPIC_API_KEY" in err
    assert "is not set" in err


def test_run_refuses_a_missing_target_token_before_any_live_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SKV3-21: the credentials phase refuses a missing target token before
    any live call, Attempt or GitHub write."""
    fakes = _fakes(monkeypatch, FakeStepTracker(), ScriptedModel([]))
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.delenv("GITHUB_TOKEN")

    exit_code, fakes = _run(
        tmp_path, monkeypatch, FakeStepTracker(), max_sweeps=1, fakes=fakes
    )

    assert exit_code == 1
    assert fakes.order == ["skill-bundle"]
    assert fakes.github.tracker_calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    err = capsys.readouterr().err
    assert "run: startup refused at credentials" in err
    assert "GITHUB_TOKEN" in err
    assert "is not set" in err


def test_run_ignores_a_missing_key_used_only_by_another_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKV3-21: a key used only by an unselected Workflow never blocks
    startup: with OPENAI_API_KEY unset, the implement Workflow (sonnet)
    still reaches the GitHub and provider checks."""
    fakes = _fakes(monkeypatch, FakeStepTracker(), ScriptedModel([]))
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    monkeypatch.delenv("OPENAI_API_KEY")
    config = _write_run_config(tmp_path, _TWO_MODEL_RUN_CONFIG)

    exit_code, fakes = _run(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, max_sweeps=1,
        fakes=fakes,
    )

    assert exit_code == 0
    assert fakes.order == ["skill-bundle", "github", "provider"]
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")


_GATEWAY_RUN_CONFIG = """\
version: 3

target:
  kind: github
  repository: example-owner/example-repo
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  muse:
    gateway: {kind: litellm, url: HTTP://PI.LAN:4000/}
    model: muse-spark-1.3-contributor
    endpoint: responses
    effort: medium
    api-key-env: LITELLM_PROXY_API_KEY
    output-cap-tokens: 32000
    context-threshold-tokens: 100000
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: muse
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


def test_worker_startup_with_a_gateway_pin_runs_the_capability_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.1/§3: a gateway pin starts up like a direct pin — the Provider
    Capability Assertion runs for the gateway pin with its output cap."""
    tracker = FakeStepTracker()
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "test-gateway-key")
    config = _write_run_config(tmp_path, _GATEWAY_RUN_CONFIG)

    exit_code, fakes = _run(
        tmp_path, monkeypatch, tracker, config=config, max_sweeps=1,
        fakes=_fakes(monkeypatch, tracker, ScriptedModel([])),
    )

    assert exit_code == 0
    assert fakes.order == ["skill-bundle", "github", "provider"]
    assert [pin.key for pin in fakes.model.build_calls] == [
        "litellm:muse-spark-1.3-contributor@responses"
    ]
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")


def test_pinned_configuration_records_the_gateway_pin_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CFG-41 (§2.10): the Effective Configuration's Pinned Model record
    carries the gateway kind, the canonical URL, the output cap, the
    threshold and its prices."""
    from coding_agent.workflow.effective import build_effective_configuration
    from coding_agent.workflow.configuration import load_workflow_configuration

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)
    config = _write_run_config(tmp_path, _GATEWAY_RUN_CONFIG)

    loaded, faults = load_workflow_configuration(config)

    assert not faults
    assert loaded is not None
    workflow = loaded.config.workflows["implement"]
    model_decl = loaded.config.models[workflow.model]
    pin, prices = supervisor.configured_pin(model_decl)
    record = build_effective_configuration(
        make_run_inputs(loaded),
        workflow=workflow,
        pin=pin,
        prices=prices,
    )
    pinned = record["pinned_model"]
    assert isinstance(pinned, dict)
    assert pinned["key"] == "litellm:muse-spark-1.3-contributor@responses"
    assert pinned["gateway_kind"] == "litellm"
    assert pinned["gateway_url"] == "http://pi.lan:4000"
    assert pinned["model"] == "muse-spark-1.3-contributor"
    assert pinned["endpoint"] == "responses"
    assert pinned["effort"] == "medium"
    assert pinned["output_cap_tokens"] == 32000
    assert pinned["context_threshold_tokens"] == 100000
    assert pinned["prices"] == {
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.20,
        "cache_write": 2.50,
    }


_GATEWAY_URL_ENV_RUN_CONFIG = _GATEWAY_RUN_CONFIG.replace(
    "gateway: {kind: litellm, url: HTTP://PI.LAN:4000/}",
    "gateway: {kind: litellm, url-env: LITELLM_PROXY_URL}",
)


@pytest.mark.parametrize(
    ("value", "marker"),
    [
        (None, None),
        ("", None),
        ("ftp://pi.lan:4000", None),
        ("http://user:SEKRET@pi.lan:4000", "SEKRET"),
        ("http://pi.lan:4000?key=value", None),
        ("http://pi.lan:4000#section", None),
    ],
)
def test_run_refuses_an_unusable_gateway_url_env_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
    marker: str | None,
) -> None:
    """§4.1: an unset, empty or malformed ``url-env`` value refuses startup
    with no Attempt opened. The refusal names the variable, never the value —
    a refused value may carry userinfo."""
    tracker = FakeStepTracker()
    fakes = _fakes(monkeypatch, tracker, ScriptedModel([]))
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("LITELLM_PROXY_URL", "placeholder")
    if value is None:
        monkeypatch.delenv("LITELLM_PROXY_URL")
    else:
        monkeypatch.setenv("LITELLM_PROXY_URL", value)
    config = _write_run_config(tmp_path, _GATEWAY_URL_ENV_RUN_CONFIG)

    exit_code, fakes = _run(
        tmp_path, monkeypatch, tracker, config=config, max_sweeps=1, fakes=fakes
    )

    assert exit_code == 1
    # The owner lookup runs inside github-checks, before
    # provider-capability, so the tracker is already built.
    assert len(fakes.github.tracker_calls) == 1
    assert tracker.call_names() == ["fetch_repository_owner_id"]
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    err = capsys.readouterr().err
    assert "run: startup refused at provider-capability" in err
    assert "LITELLM_PROXY_URL" in err
    if marker is not None:
        assert marker not in err
    if value:
        assert value not in err


def test_run_with_a_valid_gateway_url_env_records_the_canonical_url_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.10/§4.1: with a valid value the Attempt's ``pinned_model`` record
    holds the canonical resolved URL plus ``gateway_url_source``."""
    from coding_agent.workflow.effective import build_effective_configuration
    from coding_agent.workflow.configuration import load_workflow_configuration

    monkeypatch.setenv("LITELLM_PROXY_URL", "HTTP://Pi.LAN:4000/")
    config = _write_run_config(tmp_path, _GATEWAY_URL_ENV_RUN_CONFIG)

    loaded, faults = load_workflow_configuration(config)

    assert not faults
    assert loaded is not None
    workflow = loaded.config.workflows["implement"]
    model_decl = loaded.config.models[workflow.model]
    assert model_decl.gateway_url is None
    assert model_decl.gateway_url_env == "LITELLM_PROXY_URL"
    pin, price = supervisor.configured_pin(model_decl)
    assert pin.gateway_url == "http://pi.lan:4000"
    record = build_effective_configuration(
        make_run_inputs(loaded),
        workflow=workflow,
        pin=pin,
        prices=price,
    )
    pinned = record["pinned_model"]
    assert isinstance(pinned, dict)
    assert pinned["gateway_url"] == "http://pi.lan:4000"
    assert pinned["gateway_url_source"] == "env:LITELLM_PROXY_URL"


def test_run_with_a_valid_gateway_url_env_starts_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.1: a valid ``url-env`` value reaches the Provider Capability
    Assertion like a literal pin."""
    tracker = FakeStepTracker()
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("LITELLM_PROXY_URL", "HTTP://Pi.LAN:4000/")
    config = _write_run_config(tmp_path, _GATEWAY_URL_ENV_RUN_CONFIG)

    exit_code, fakes = _run(
        tmp_path, monkeypatch, tracker, config=config, max_sweeps=1,
        fakes=_fakes(monkeypatch, tracker, ScriptedModel([])),
    )

    assert exit_code == 0
    assert fakes.order == ["skill-bundle", "github", "provider"]
    assert [pin.key for pin in fakes.model.build_calls] == [
        "litellm:muse-spark-1.3-contributor@responses"
    ]
    assert [pin.gateway_url for pin in fakes.model.build_calls] == [
        "http://pi.lan:4000"
    ]
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")


def test_gateway_url_env_value_moves_the_effective_configuration_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.10: two runs differing only in the variable's value have different
    Effective Configuration digests, as #321 requires."""
    from coding_agent.ledger.store import digest_effective_configuration
    from coding_agent.workflow.effective import build_effective_configuration
    from coding_agent.workflow.configuration import load_workflow_configuration

    config = _write_run_config(tmp_path, _GATEWAY_URL_ENV_RUN_CONFIG)
    loaded, faults = load_workflow_configuration(config)
    assert not faults
    assert loaded is not None
    workflow = loaded.config.workflows["implement"]
    model_decl = loaded.config.models[workflow.model]

    def _digest_for(value: str) -> str:
        pin, price = supervisor.configured_pin(
            model_decl, env={"LITELLM_PROXY_URL": value}
        )
        record = build_effective_configuration(
            make_run_inputs(loaded),
            workflow=workflow,
            pin=pin,
            prices=price,
        )
        return digest_effective_configuration(record)

    assert _digest_for("http://pi.lan:4000") != _digest_for("http://other.lan:4000")


def test_recovery_uses_the_recorded_gateway_url_after_an_env_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.10/§16: an open Attempt claimed with value A is recovered after a
    restart with value B. Its pinned record still holds A, its digest is
    unchanged, and recovery never consults the variable."""
    from coding_agent.ledger.store import RunLedger, digest_effective_configuration
    from coding_agent.provider.pinned_model import (
        resolve_gateway_url as real_resolve_gateway_url,
    )
    from coding_agent.workflow.effective import build_effective_configuration
    from coding_agent.workflow.configuration import load_workflow_configuration

    config = _write_run_config(tmp_path, _GATEWAY_URL_ENV_RUN_CONFIG)
    loaded, faults = load_workflow_configuration(config)
    assert not faults
    assert loaded is not None
    workflow = loaded.config.workflows["implement"]
    model_decl = loaded.config.models[workflow.model]

    pin_a, price_a = supervisor.configured_pin(
        model_decl, env={"LITELLM_PROXY_URL": "http://pi.lan:4000"}
    )
    record_a = build_effective_configuration(
        make_run_inputs(loaded),
        workflow=workflow,
        pin=pin_a,
        prices=price_a,
    )
    digest_a = digest_effective_configuration(record_a)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    seed = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        seed.open_attempt("implement#7/1", effective_configuration=record_a)
    finally:
        seed.close()

    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("LITELLM_PROXY_URL", "http://other.lan:4000")

    exit_code, fakes = _run(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, max_sweeps=1
    )

    assert exit_code == 0
    # Startup resolves the new pin once; recovery consults no variable.
    assert fakes.model.resolve_calls == ["LITELLM_PROXY_URL"]
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        stored = ledger.get_attempt("implement#7/1")
        assert stored.effective_configuration == record_a
        assert stored.effective_configuration_digest == digest_a
        pinned = stored.effective_configuration["pinned_model"]
        assert isinstance(pinned, dict)
        assert pinned["gateway_url"] == "http://pi.lan:4000"
    finally:
        ledger.close()


def test_configured_pin_refuses_both_url_and_url_env() -> None:
    """The validator refuses both keys together; the defensive startup check
    agrees instead of preferring one silently."""
    from coding_agent.workflow.schema import ModelDecl, ModelPrices

    decl = ModelDecl(
        id="muse",
        provider=None,
        model="muse-spark-1.3-contributor",
        endpoint="responses",
        effort="medium",
        api_key_env="LITELLM_PROXY_API_KEY",
        prices=ModelPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5),
        gateway_kind="litellm",
        gateway_url="http://pi.lan:4000",
        gateway_url_env="LITELLM_PROXY_URL",
        output_cap_tokens=32000,
        context_threshold_tokens=100000,
    )

    with pytest.raises(ValueError, match="exactly one of 'url' or 'url-env'"):
        supervisor.configured_pin(decl, env={})


def test_a_setup_failure_outside_the_run_is_recorded_finalized_and_counted(
    ledger: RunLedger,
) -> None:
    """§4: setup is an infrastructure class. An ``OSError`` escaping the run
    ends ``internal-failure`` through the finalisation entry point — the
    terminal comment is posted and the Attempt is finalized — and it counts
    toward the halt-after-three; intake never stays closed."""
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
    for number in (31, 32, 33):
        stored = ledger.get_outcome(f"implement#{number}/1")
        assert stored is not None
        assert stored.outcome == "internal-failure"
        assert stored.reason.startswith("infrastructure-failure: setup")
        assert ledger.get_attempt(f"implement#{number}/1").state == "finished"
    assert not ledger.has_unfinished_attempt()
    assert tracker.call_names().count("post_comment") == 3


@pytest.mark.parametrize(
    ("error", "failure_class"),
    [
        (OSError(28, "No space left on device"), "setup"),
        (GitFailure("git checkout exited 128"), "git"),
        (
            GitHubFailure(
                "GET",
                "/repos/o/r/issues/31",
                502,
                reason="server-error",
                failure_class="github",
            ),
            "github",
        ),
    ],
)
def test_an_infrastructure_escape_ends_internal_failure_with_terminal_writes(
    ledger: RunLedger, error: Exception, failure_class: str
) -> None:
    """§4/§15: an infrastructure-class escape ends ``internal-failure``
    through the finalisation entry point: the terminal comment is posted,
    the Selection Label removed, the end labels applied, the assignment
    removed, and the Attempt finished."""
    workflow = _workflow(outcomes={"internal-failure": ("ops-internal",)})
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise error

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(workflow=workflow),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    stored = ledger.get_outcome("implement#31/1")
    assert stored is not None
    assert stored.outcome == "internal-failure"
    assert stored.reason.startswith(f"infrastructure-failure: {failure_class}")
    assert ledger.get_attempt("implement#31/1").state == "finished"
    assert not ledger.has_unfinished_attempt()
    terminal = [comment for comment in tracker.comments if "kind=terminal" in comment.body]
    assert len(terminal) == 1
    assert "coding-agent implement#31/1: internal-failure" in terminal[0].body
    assert LABEL not in tracker.inputs[31].labels
    assert "ops-internal" in tracker.inputs[31].labels
    assert tracker.inputs[31].assignees == ()


class _FlakyCommentTracker(FakeStepTracker):
    """A tracker whose first ``failures_left`` comment posts fail (§14.4)."""

    failures_left: int = 0

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> Any:
        if self.failures_left > 0:
            self.failures_left -= 1
            self._record("post_comment", number, input_type, body)
            raise GitHubFailure(
                "POST",
                f"/repos/o/r/issues/{number}/comments",
                502,
                reason="server-error",
                failure_class="github",
            )
        return super().post_comment(number, input_type, body)


def _issue_tracker_with(
    flaky: _FlakyCommentTracker, *numbers_and_dates: tuple[int, str]
) -> _FlakyCommentTracker:
    """Seed ``flaky`` with labelled issue inputs like :func:`_tracker_with`."""
    seed = _tracker_with(*numbers_and_dates)
    flaky.inputs.update(seed.inputs)
    flaky.labeled_events.update(seed.labeled_events)
    return flaky


def test_a_failed_finalisation_leaves_the_attempt_recorded_and_unfinished(
    ledger: RunLedger,
) -> None:
    """§15/§16: where the terminal writes fail, the Attempt stays recorded
    and unfinished and no new Claim opens while it is unfinished."""
    tracker = _issue_tracker_with(
        _FlakyCommentTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id),
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
    )
    tracker.failures_left = 100

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise OSError(28, "No space left on device")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_sweeps=2,
    )

    assert exit_code == 0
    stored = ledger.get_outcome("implement#31/1")
    assert stored is not None
    assert stored.outcome == "internal-failure"
    assert stored.reason.startswith("infrastructure-failure: setup")
    assert ledger.get_attempt("implement#31/1").state == "outcome-recorded"
    assert ledger.has_unfinished_attempt()
    # The terminal comment never landed and the input is untouched.
    assert [comment for comment in tracker.comments if "kind=terminal" in comment.body] == []
    assert LABEL in tracker.inputs[31].labels
    # Intake stayed closed: no Claim for the second input opened.
    assert not ledger.has_attempt("implement#32/1")


def test_a_write_failure_after_the_record_is_finished_as_recorded_by_recovery(
    ledger: RunLedger,
) -> None:
    """§15/§16: the live finalisation records the outcome, the loop replay
    fails the same way, and the next cycle's recovery pass finishes the
    recorded outcome as recorded — never re-decided."""
    workflow = _workflow(outcomes={"completed": ("done-label",)})
    tracker = _issue_tracker_with(
        _FlakyCommentTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id),
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
    )
    # The live finalisation inside the run and the loop's replay both fail;
    # the recovery pass on the next cycle succeeds.
    tracker.failures_left = 2

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        if claimed.input_number == 31:
            finish_attempt(
                tracker=tracker,
                ledger=ledger,
                attempt_id=claimed.attempt_id,
                target=FinalisationTarget(
                    number=31, input_type="issue", selection_label=LABEL
                ),
                decision=WorkflowEnded(
                    category="completed",
                    reason="verified-completion",
                    ending_step="work",
                    step_result="done",
                    workflow_end="completed",
                ),
                end_labels_for={"completed": ("done-label",)},
            )
            raise AssertionError("unreachable: the comment post above raises")
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=claimed.attempt_id,
            outcome="completed",
            reason="verified-completion",
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(workflow=workflow),
        run_one_attempt=_release_on_finish(tracker, run_one),
        sleep=lambda seconds: None,
        max_sweeps=6,
    )

    assert exit_code == 0
    stored = ledger.get_outcome("implement#31/1")
    assert stored is not None
    # The recorded outcome and reason are unchanged by the loop replay.
    assert (stored.outcome, stored.reason) == ("completed", "verified-completion")
    assert ledger.get_attempt("implement#31/1").state == "finished"
    assert not ledger.has_unfinished_attempt()
    terminal = [comment for comment in tracker.comments if "kind=terminal" in comment.body]
    assert [comment.body for comment in terminal] == [stored.terminal_comment_body]
    assert LABEL not in tracker.inputs[31].labels
    assert "done-label" in tracker.inputs[31].labels
    assert tracker.inputs[31].assignees == ()
    # The second input was claimed only after recovery finished the first:
    # no ``#31/2`` and no duplicate ``#32/2`` exists.
    assert not ledger.has_attempt("implement#31/2")
    assert not ledger.has_attempt("implement#32/2")
    assert ledger.get_attempt("implement#32/1").state == "finished"


def test_an_unsettled_effect_intent_leaves_the_attempt_for_recovery(
    ledger: RunLedger,
) -> None:
    """§14/§16: an unsettled Effect Intent refuses live finalisation; the
    Attempt stays unfinished and the next cycle settles it, then finishes
    it ``interrupted`` / ``process-loss``."""
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ledger.record_step_effect_intent(
            claimed.attempt_id,
            "work",
            1,
            0,
            rendered_payload="a settlement probe",
            expected_target={
                "operation": "post-comment",
                "input_number": claimed.input_number,
                "input_type": "issue",
            },
        )
        raise OSError(28, "No space left on device")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_sweeps=3,
    )

    assert exit_code == 0
    # Live finalisation refused before recording: recovery settled the open
    # intent, then finished the Attempt as a process loss.
    stored = ledger.get_outcome("implement#31/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "process-loss")
    assert ledger.get_attempt("implement#31/1").state == "finished"
    assert not ledger.has_unfinished_attempt()


def test_a_credential_failure_during_finalisation_halts_leaving_recovery(
    ledger: RunLedger,
) -> None:
    """§4/§16: a Credential Failure inside the infrastructure finalisation
    records the halt at once, leaving the recorded Attempt for recovery."""

    class _RevokedTracker(FakeStepTracker):
        def post_comment(
            self, number: int, input_type: InputType, body: str
        ) -> Any:
            self._record("post_comment", number, input_type, body)
            raise CredentialFailure("POST", f"/repos/o/r/issues/{number}/comments")

    tracker = _RevokedTracker(agent_login=IDENTITY.login, agent_id=IDENTITY.account_id)
    seed = _tracker_with((31, "2026-10-01T00:00:00Z"))
    tracker.inputs.update(seed.inputs)
    tracker.labeled_events.update(seed.labeled_events)

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        raise OSError(28, "No space left on device")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=5,
    )

    assert exit_code == 1
    stored = ledger.get_outcome("implement#31/1")
    assert stored is not None
    assert stored.outcome == "internal-failure"
    assert stored.reason.startswith("infrastructure-failure: setup")
    assert ledger.get_attempt("implement#31/1").state != "finished"
    assert ledger.has_unfinished_attempt()
    assert [comment for comment in tracker.comments if "kind=terminal" in comment.body] == []
    halt_reason, _ = _halt_row(ledger.path.parent)
    assert halt_reason == "credential-failure"


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


def test_run_with_version_1_config_refuses_with_drain_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `version: 1` file refuses startup with `<path>: version: <CLI drain note>`."""
    config = _write_run_config(tmp_path, "version: 1\n")

    exit_code, fakes = _run(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, max_sweeps=1
    )

    assert exit_code == 1
    assert fakes.order == []
    assert fakes.github.tracker_calls == []
    err = capsys.readouterr().err
    assert f"{config}: version: {config} does not declare 'version: 3'" in err
    assert "§16.4" in err
