"""Startup recovery: unconfirmed Claims, settlement and intake (contract §16).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §16 (restart
never enters the Workflow graph and never runs a Workflow Step; an
unconfirmed Claim is released; a recorded outcome is finished as recorded and
otherwise the Attempt ends ``interrupted`` / ``process-loss``, ``withdrawn``
where the input was withdrawn, or ``drift`` where the Gate finds Drift; an
unfinished legacy Attempt refuses startup), §14.2 (restart settlement: a
landed write records the receipt and is never written twice) and §4 (intake
stays closed while an Attempt is unfinished; a failed recovery write is
retried on the next poll cycle and three in a row halt non-zero).

Matrix row ids: REC-5 … REC-12, REC-15 … REC-21, REC-24, REC-25, REC-11
(legacy refusal, with ``test_step_legacy_startup.py``).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.errors import CredentialFailure, GitHubFailure
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.claim import sweep_and_claim
from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime import step_recovery
from coding_agent.runtime.step_recovery import (
    RecoveryFailureTracker,
    decide_restart_outcome,
    recover_attempt,
    recover_unconfirmed_attempt,
    settle_open_step_effects,
    should_halt_after_failures,
)
from coding_agent.worker import run as worker_run
from coding_agent.worker import supervisor
from coding_agent.worker.run import (
    LegacyAttemptRefused,
    WorkerLoopConfig,
    recover_unfinished_step_attempts,
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
from test_worker_run import (
    _StartupSpy,
    _write_run_config,
    _write_run_matrix,
)

LABEL = "agent:implement"
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


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
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=1001)
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = _issue_input(number, created_at)
        tracker.labeled_events[(number, LABEL)] = _human_event()
    return tracker


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login="coding-agent",
        agent_id=1001,
        effective_configuration={"workflow": "implement"},
    )


def _workflow(workflow_id: str = "implement") -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type="issue", label=LABEL),  # type: ignore[arg-type]
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        defaults_routes={
            "error": V2RouteTarget(end="failed"),  # type: ignore[arg-type]
            "limit-exceeded": V2RouteTarget(end="unfinished"),  # type: ignore[arg-type]
        },
        steps={
            "work": V2Step(
                id="work",
                agent=V2Agent(results={"done": {}}),  # type: ignore[arg-type]
                max_executions=1,
                routes={"done": V2RouteTarget(end="completed")},  # type: ignore[arg-type]
            ),
        },
    )


def _loop_config() -> WorkerLoopConfig:
    return WorkerLoopConfig(
        claim_config=_claim_config(), workflow=_workflow(), poll_interval_seconds=60.0
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _stub_run(
    ledger: RunLedger, ran: list[int]
) -> Callable[[ConfirmedClaim], WorkflowResult]:
    """A stub Attempt runner recording the Terminal Outcome, as the graph would."""

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=claimed.attempt_id,
            outcome="completed",
            reason="verified-completion",
        )

    return run_one


def _release_on_finish(
    tracker: FakeStepTracker,
    runner: Callable[[ConfirmedClaim], WorkflowResult],
) -> Callable[[ConfirmedClaim], WorkflowResult]:
    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        result = runner(claimed)
        tracker.remove_label(claimed.input_number, "issue", LABEL)
        tracker.unassign_input(claimed.input_number, "issue")
        return result

    return run_one


# --- The ledger readers -----------------------------------------------------


def test_unfinished_attempts_lists_only_unfinished_in_ledger_order(
    ledger: RunLedger,
) -> None:
    """§16: recovery finds every unfinished Attempt in the ledger."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    ledger.open_attempt("implement#8/1", effective_configuration={"workflow": "x"})
    ledger.open_attempt("implement#9/1", effective_configuration={"workflow": "x"})
    ledger.set_attempt_state("implement#8/1", "released")
    ledger.set_attempt_state("implement#9/1", "finished")

    assert [record.attempt_id for record in ledger.unfinished_attempts()] == [
        "implement#7/1"
    ]


# --- REC-10: an unconfirmed Claim is released --------------------------------


def test_rec_10_crash_before_confirmation_releases_with_no_terminal_outcome(
    ledger: RunLedger,
) -> None:
    """REC-10: ``open`` settles to ``released`` with the assignment removed
    where present, no terminal comment and no label change."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    summary = recover_unfinished_step_attempts(
        tracker=tracker, ledger=ledger, workflow=_workflow(), base_branch="main"
    )

    assert summary.settled == ("implement#7/1",)
    assert summary.pending == ()
    assert ledger.get_attempt("implement#7/1").state == "released"
    assert ledger.get_outcome("implement#7/1") is None


def test_rec_10_the_input_is_claimable_as_n_plus_one(ledger: RunLedger) -> None:
    """REC-10: after recovery the input is claimed as ``implement#N/n+1``."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    recover_unfinished_step_attempts(
        tracker=tracker, ledger=ledger, workflow=_workflow(), base_branch="main"
    )

    claimed = sweep_and_claim(tracker=tracker, ledger=ledger, config=_claim_config())

    assert isinstance(claimed, ConfirmedClaim)
    assert (claimed.attempt_id, claimed.n) == ("implement#7/2", 2)


def test_rec_10_through_the_claim_halt_path(ledger: RunLedger) -> None:
    """REC-10 through the claim-interrupted halt: ``assign`` fails mid-Claim,
    the Worker halts, and the restart releases the Attempt and claims ``#N/2``."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    ran: list[int] = []

    class _FailOnceAssignTracker(FakeStepTracker):
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

    failing = _FailOnceAssignTracker(agent_login="coding-agent", agent_id=1001)
    failing.inputs.update(tracker.inputs)
    failing.labeled_events.update(tracker.labeled_events)

    halted = run_worker_loop(
        tracker=failing,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert halted == 1
    assert ledger.get_attempt("implement#7/1").state == "open"
    assert ran == []

    restarted = run_worker_loop(
        tracker=failing,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(failing, _stub_run(ledger, ran)),
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert restarted == 0
    assert ledger.get_attempt("implement#7/1").state == "released"
    assert ran == [7]
    assert ledger.get_attempt("implement#7/2").state == "finished"


# --- REC-12: settle and finalise an interrupted Attempt -----------------------


def test_rec_12_process_loss_ends_interrupted_with_terminal_writes(
    ledger: RunLedger,
) -> None:
    """REC-12: a confirmed-but-unfinished Attempt ends ``interrupted`` /
    ``process-loss`` with a terminal comment, the label removed and the
    assignment released."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
        selection_label=LABEL,
    )

    assert recovered.finished is True
    assert (recovered.outcome, recovered.reason) == ("interrupted", "process-loss")
    assert ledger.get_attempt("implement#7/1").state == "finished"
    stored = ledger.get_outcome("implement#7/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "process-loss")
    assert tracker.call_names().count("post_comment") == 1
    assert ("remove_label", (7, "issue", LABEL)) in tracker.calls
    assert ("unassign_input", (7, "issue")) in tracker.calls


def test_rec_12_a_withdrawn_input_ends_withdrawn(ledger: RunLedger) -> None:
    """REC-12: an input withdrawn meanwhile ends ``withdrawn``, never rerun."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
        selection_label=LABEL,
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )

    assert recovered.finished is True
    assert recovered.outcome == "withdrawn"
    assert ledger.get_attempt("implement#7/1").state == "finished"


def test_rec_12_drift_ends_drift(ledger: RunLedger) -> None:
    """REC-12: Drift found at restart ends ``drift``, never rerun."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
        selection_label=LABEL,
        gate=lambda: GateResult(status="drift", reason="content-changed"),
    )

    assert recovered.finished is True
    assert recovered.outcome == "drift"


def test_rec_12_a_recorded_outcome_is_finished_as_recorded(
    ledger: RunLedger,
) -> None:
    """REC-12: a recorded outcome is finished as recorded and never re-decided."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    ledger.record_terminal_outcome(
        "implement#7/1", outcome="completed", reason="verified-completion"
    )
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
        selection_label=LABEL,
        gate=lambda: GateResult(status="withdrawn", reason="closed"),
    )

    assert recovered.finished is True
    assert (recovered.outcome, recovered.reason) == (
        "completed",
        "verified-completion",
    )


def test_decide_restart_outcome_never_runs_a_step() -> None:
    """REC-12: withdrawn, Drift, or process-loss — no Workflow Step runs."""
    assert decide_restart_outcome(None) == decide_restart_outcome(None)
    assert decide_restart_outcome(
        GateResult(status="withdrawn", reason="closed")
    ).outcome == "withdrawn"
    assert decide_restart_outcome(
        GateResult(status="drift", reason="content-changed")
    ).outcome == "drift"
    assert decide_restart_outcome(GateResult(status="ok", reason="")).outcome == (
        "interrupted"
    )


# --- REC-17: a Credential Failure records interrupted and halts --------------


def test_rec_17_a_credential_failure_records_and_propagates(
    ledger: RunLedger,
) -> None:
    """REC-17: a tracker write fails on the credential; the outcome records
    ``interrupted`` / ``credential-failure`` and the error propagates so the
    Worker halts and recovery finishes after the credential is fixed."""

    class _RevokedTracker(FakeStepTracker):
        def find_comment_by_marker(
            self, number: int, input_type: Any, marker: str
        ) -> Any:
            raise CredentialFailure("GET", f"/repos/o/r/issues/{number}/comments")

        def post_comment(
            self, number: int, input_type: Any, body: str
        ) -> Any:
            raise CredentialFailure("POST", f"/repos/o/r/issues/{number}/comments")

    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    ledger.record_step_effect_intent(
        "implement#7/1",
        "work",
        1,
        0,
        rendered_payload="settlement probe\nbody",
        expected_target={"operation": "post-comment"},
    )
    tracker = _RevokedTracker(agent_login="coding-agent", agent_id=1001)
    tracker.inputs[7] = _issue_input(7, "2026-10-01T00:00:00Z")
    tracker.labeled_events[(7, LABEL)] = _human_event()

    with pytest.raises(CredentialFailure):
        recover_attempt(
            tracker=tracker,  # type: ignore[arg-type]
            ledger=ledger,
            attempt_id="implement#7/1",
            number=7,
            input_type="issue",
            selection_label=LABEL,
        )

    stored = ledger.get_outcome("implement#7/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "credential-failure")


# --- Recovery never calls the model ------------------------------------------


def test_recovery_takes_no_model() -> None:
    """§16, ADR 0017: recovery cannot call the model — no recovery entry
    point accepts one, asserted structurally on the signatures."""
    for name in (
        "recover_unconfirmed_attempt",
        "recover_attempt",
        "decide_restart_outcome",
        "settle_open_step_effects",
        "settle_step_effect",
        "finalise_shutdown",
    ):
        parameters = inspect.signature(getattr(step_recovery, name)).parameters
        assert "model" not in parameters
        assert "chat_model" not in parameters
        assert "provider" not in parameters
    worker_parameters = inspect.signature(
        worker_run.recover_unfinished_step_attempts
    ).parameters
    assert "model" not in worker_parameters
    assert "chat_model" not in worker_parameters
    assert "provider" not in worker_parameters


# --- Another Workflow's Attempts stay pending ---------------------------------


def test_another_workflows_attempt_stays_pending_with_intake_closed(
    ledger: RunLedger,
) -> None:
    """§16.1: the running Worker does not know another Workflow's labels, so
    its Attempts are left pending and reported."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    claimed = sweep_and_claim(
        tracker,
        ledger,
        ClaimConfig(
            workflow_id="review",
            input_type="issue",
            selection_label=LABEL,
            base_branch="main",
            agent_login="coding-agent",
            agent_id=1001,
            effective_configuration={"workflow": "review"},
        ),
    )
    assert isinstance(claimed, ConfirmedClaim)

    summary = recover_unfinished_step_attempts(
        tracker=tracker, ledger=ledger, workflow=_workflow(), base_branch="main"
    )

    assert summary.settled == ()
    assert summary.pending == (claimed.attempt_id,)
    assert ledger.get_attempt(claimed.attempt_id).state == "claim-confirmed"


def test_startup_settles_an_unfinished_attempt_before_selecting(
    ledger: RunLedger,
) -> None:
    """Startup recovery precedes selection: the restart settles the
    unfinished Attempt first, then idles with nothing left to claim."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    claimed = sweep_and_claim(tracker, ledger, _claim_config())
    assert isinstance(claimed, ConfirmedClaim)
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(tracker, _stub_run(ledger, ran)),
        sleep=lambda seconds: None,
        max_sweeps=2,
    )

    assert exit_code == 0
    assert ran == []
    assert ledger.get_attempt("implement#7/1").state == "finished"


# --- REC-11: legacy Attempts refuse -------------------------------------------


def test_rec_11_a_legacy_attempt_refuses_recovery(ledger: RunLedger) -> None:
    """REC-11: an unfinished MVP Attempt refuses — drain with the old release."""
    ledger.open_attempt("#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    with pytest.raises(LegacyAttemptRefused):
        recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow(), base_branch="main"
        )

    assert tracker.calls == []
    assert ledger.get_attempt("#7/1").state == "open"


def test_rec_11_a_legacy_halt_leaves_the_ledger_untouched(
    ledger: RunLedger,
) -> None:
    """REC-11 through the poll loop: non-zero exit, nothing settled."""
    ledger.open_attempt("#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    assert ran == []
    assert tracker.calls == []
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(str(ledger.path))
    try:
        row = connection.execute(
            "SELECT reason FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None and str(row[0]) == "legacy-attempt"
    assert ledger.get_attempt("#7/1").state == "open"


# --- REC-18: three consecutive recovery write failures halt -------------------


def _failing_release_tracker(
    tracker: FakeStepTracker, failures_left: list[int]
) -> FakeStepTracker:
    """A tracker whose ``list_comments`` fails while ``failures_left`` runs.

    The failure lands before the release path: ``release_claim`` swallows
    tracker exceptions, so recovery-write failures must surface at the
    ``list_comments`` reads that precede it.
    """

    class _FailingListComments(FakeStepTracker):
        def list_comments(self, number: int, input_type: Any) -> Any:
            if failures_left:
                failures_left.pop()
                raise GitHubFailure(
                    "GET",
                    f"/repos/o/r/issues/{number}/comments",
                    502,
                    reason="server-error",
                    failure_class="github",
                )
            return super().list_comments(number, input_type)

    failing = _FailingUnassign(agent_login="coding-agent", agent_id=1001)
    failing.inputs.update(tracker.inputs)
    failing.labeled_events.update(tracker.labeled_events)
    failing.comments.extend(tracker.comments)
    failing.calls.extend(tracker.calls)
    return failing


def test_rec_18_three_consecutive_recovery_failures_halt(
    ledger: RunLedger,
) -> None:
    """REC-18: the failed recovery write is retried each poll; the third halts."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    claimed = sweep_and_claim(tracker, ledger, _claim_config())
    assert isinstance(claimed, ConfirmedClaim)
    # Back to an unconfirmed Claim: recovery must release it (unassign).
    ledger.set_attempt_state(claimed.attempt_id, "open")
    failing = _failing_release_tracker(tracker, [1, 1, 1, 1])
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=failing,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_sweeps=10,
    )

    assert exit_code == 1
    assert ran == []
    assert ledger.get_attempt(claimed.attempt_id).state == "open"
    assert ledger.has_unfinished_attempt()


def test_rec_18_a_success_resets_the_failure_count(ledger: RunLedger) -> None:
    """REC-18: two failed writes then a settled recovery — no halt, and the
    input is claimed as ``#N/2`` on the same run."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    claimed = sweep_and_claim(tracker, ledger, _claim_config())
    assert isinstance(claimed, ConfirmedClaim)
    ledger.set_attempt_state(claimed.attempt_id, "open")
    failing = _failing_release_tracker(tracker, [1, 1])
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=failing,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(failing, _stub_run(ledger, ran)),
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert ledger.get_attempt(claimed.attempt_id).state == "released"
    assert ran == [7]
    assert ledger.get_attempt("implement#7/2").state == "finished"


def test_recovery_failure_tracker_counts_to_three() -> None:
    """REC-18: the unit predicate halts exactly at three consecutive failures."""
    tracker = RecoveryFailureTracker()
    assert should_halt_after_failures(0) is False
    assert tracker.note_failure() is False
    assert tracker.note_failure() is False
    assert should_halt_after_failures(2) is False
    assert tracker.note_failure() is True
    assert should_halt_after_failures(3) is True
    tracker.note_success()
    assert tracker.consecutive_failures == 0


# --- REC-5 … REC-8: effect settlement ------------------------------------------


def test_settle_open_step_effects_with_no_intents_settles_nothing(
    ledger: RunLedger,
) -> None:
    """REC-5: an Attempt with no open intents settles trivially."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})

    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
    )

    assert settled == {"receipt": 0, "performed": 0, "not-performed": 0}


def test_recover_unconfirmed_attempt_releases_without_terminal_writes(
    ledger: RunLedger,
) -> None:
    """REC-10: the direct entry releases with no Terminal Outcome."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    recovered = recover_unconfirmed_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
    )

    assert recovered.released is True
    assert recovered.finished is False
    assert recovered.outcome is None
    assert ledger.get_attempt("implement#7/1").state == "released"


def test_run_ledger_refusal_names_run_ledger(tmp_path: Path) -> None:
    """A corrupted Effective Configuration cell refuses startup at run-ledger."""
    import sqlite3 as _sqlite3

    from coding_agent.worker import supervisor

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    seed = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        seed.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    finally:
        seed.close()
    connection = _sqlite3.connect(state_dir / "run-ledger.sqlite3")
    connection.execute(
        "UPDATE attempts SET effective_configuration = 'not-json'"
        " WHERE attempt_id = 'implement#7/1'"
    )
    connection.commit()
    connection.close()

    exit_code = supervisor.run_worker(
        config=tmp_path / "missing.yml",
        workflow="implement",
        state_dir=state_dir,
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    # The missing configuration refuses first (step 1, before run-ledger).
    assert exit_code == 1


def test_recovery_leaves_a_scripted_model_untouched(ledger: RunLedger) -> None:
    """Recovery never invokes the model: the scripted model records nothing."""
    from support.scripted_model import ScriptedModel

    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    model = ScriptedModel([])

    recover_unfinished_step_attempts(
        tracker=tracker, ledger=ledger, workflow=_workflow(), base_branch="main"
    )

    assert model.invocations == []
    assert model.bound_tool_names == []


def test_startup_recovery_reports_settled_attempts(
    ledger: RunLedger, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restart names the Attempts it settled on stderr."""
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    claimed = sweep_and_claim(tracker, ledger, _claim_config())
    assert isinstance(claimed, ConfirmedClaim)
    ran: list[int] = []

    exit_code = worker_run._poll_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_release_on_finish(tracker, _stub_run(ledger, ran)),
        sleep=lambda seconds: None,
        max_sweeps=2,
    )

    assert exit_code == 0
    assert f"run: recovery settled {claimed.attempt_id}" in capsys.readouterr().err
