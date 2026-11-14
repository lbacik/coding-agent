"""A ``next issue N`` priority settles at the Claim boundary (issue #369).

Contract: operator contract v1.0.4 §8.1 (every command reaches a final
acknowledgement), §8.3 (eligibility is re-checked at the Claim boundary;
ineligible there resolves ``not_fulfilled``) and ADR-0026 (commands never
write lifecycle state: resolution rides the Claim-boundary and
finalization transactions).

The step-workflow Worker reads the pending priority before each sweep and
tries issue N first under the same §5.4 guards as normal selection. The
command then settles in the same write transaction as the Claim's
lifecycle write: ``completed`` where N's Claim confirms,
``not_fulfilled`` where N is absent, guarded or released by its
read-back. These tests assert the command acknowledgement and the
``intake.priority`` projection — not just which issue was claimed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    sweep_and_claim,
)
from coding_agent.inputs.port import InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import commands as operator_commands
from coding_agent.operator.commands import CommandRecord
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker

LABEL = "agent:implement"
REPO = "octocat/sandbox"


def _issue_input(
    *,
    number: int = 42,
    labels: tuple[str, ...] = (LABEL,),
    assignees: tuple[str, ...] = (),
    is_open: bool = True,
    created_at: str = "2026-10-01T00:00:00Z",
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=f"Add a greeting {number}",
        body="a body",
        is_open=is_open,
        labels=labels,
        assignees=assignees,
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _human_event(number: int, label: str = LABEL) -> LabeledEvent:
    return LabeledEvent(
        label=label,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )


def _bot_event(number: int, label: str = LABEL) -> LabeledEvent:
    return LabeledEvent(
        label=label,
        actor_login="some-bot",
        actor_id=4321,
        actor_type="Bot",
        created_at="2026-10-01T00:00:00Z",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> RunLedger:
    """A fresh Run Ledger per test."""
    return RunLedger(tmp_path / "ledger.db")


@pytest.fixture
def tracker() -> FakeStepTracker:
    """A dedicated-identity fake tracker with two labelled issues."""
    fake = FakeStepTracker()
    fake.inputs[40] = _issue_input(number=40, created_at="2026-09-01T00:00:00Z")
    fake.inputs[41] = _issue_input(number=41, created_at="2026-10-01T00:00:00Z")
    fake.labeled_events[(40, LABEL)] = _human_event(40)
    fake.labeled_events[(41, LABEL)] = _human_event(41)
    return fake


def _config(
    tracker: FakeStepTracker,
    *,
    workflow_id: str = "implement",
    input_type: InputType = "issue",
    label: str = LABEL,
) -> ClaimConfig:
    effective: Mapping[str, object] = {"workflow": workflow_id}
    return ClaimConfig(
        workflow_id=workflow_id,
        input_type=input_type,
        selection_label=label,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration=effective,
    )


def _commit(
    ledger: RunLedger,
    *,
    request_id: str,
    issue: int,
    kind: str = "next",
) -> CommandRecord:
    """Commit one operator command through its own write transaction."""
    connection = operator_commands.open_ledger_connection(
        ledger.path, timeout_seconds=2.0
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if kind == "next":
                record, _replayed, error = operator_commands.commit_next(
                    connection,
                    request_id=request_id,
                    expect_repo=REPO,
                    target_repository=REPO,
                    issue_number=issue,
                )
            elif kind == "stop":
                record, _replayed, error = operator_commands.commit_stop(
                    connection,
                    request_id=request_id,
                    expect_repo=REPO,
                    target_repository=REPO,
                )
            else:
                record, _replayed, error = operator_commands.commit_resume(
                    connection,
                    request_id=request_id,
                    expect_repo=REPO,
                    target_repository=REPO,
                )
            assert error is None
            assert record is not None
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    stored = _read_command(ledger, request_id)
    assert stored is not None
    return stored


def _read_command(ledger: RunLedger, request_id: str) -> CommandRecord | None:
    """The stored command for ``request_id``, read back fresh."""
    connection = operator_commands.open_ledger_connection(
        ledger.path, timeout_seconds=2.0
    )
    try:
        return operator_commands.get_command(connection, request_id)
    finally:
        connection.close()


def _resolved_events(ledger: RunLedger, request_id: str) -> list[dict[str, Any]]:
    """Every ``command_resolved`` event row for ``request_id``, oldest first."""
    connection = operator_commands.open_ledger_connection(
        ledger.path, timeout_seconds=2.0
    )
    try:
        rows = connection.execute(
            "SELECT type, kind, acknowledgement FROM operator_events"
            " WHERE type = 'command_resolved' AND request_id = ?"
            " ORDER BY id",
            (request_id,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {"type": str(row[0]), "kind": str(row[1]), "acknowledgement": str(row[2])}
        for row in rows
    ]


def _intake_priority(ledger: RunLedger) -> dict[str, Any] | None:
    """The ``status`` intake projection's priority, read back fresh."""
    connection = operator_commands.open_ledger_connection(
        ledger.path, timeout_seconds=2.0
    )
    try:
        return operator_commands.pending_next_priority(connection)
    finally:
        connection.close()


def test_priority_claimed_first_completes_command_and_clears_projection(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3: with 40 older, a priority for 41 claims 41 and completes."""
    _commit(ledger, request_id="req-old", issue=40)
    _commit(ledger, request_id="req-new", issue=41)
    pending = ledger.pending_next_command()
    assert pending is not None
    assert pending.request_id == "req-new"

    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 41
    assert claimed.attempt_id == "implement#41/1"
    assert ledger.get_attempt("implement#41/1").state == "claim-confirmed"
    stored = _read_command(ledger, "req-new")
    assert stored is not None
    assert stored.acknowledgement == "completed"
    assert stored.detail == operator_commands.NEXT_COMPLETED_DETAIL
    assert _intake_priority(ledger) is None
    resolved = _resolved_events(ledger, "req-new")
    assert len(resolved) == 1
    assert resolved[0]["acknowledgement"] == "completed"
    # The superseded older priority is untouched by this sweep.
    older = _read_command(ledger, "req-old")
    assert older is not None
    assert older.acknowledgement == "superseded"
    assert len(_resolved_events(ledger, "req-old")) == 1


def test_priority_absent_resolves_and_claims_normal(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3: a priority for a non-candidate resolves; the sweep claims oldest."""
    _commit(ledger, request_id="req-absent", issue=99)
    pending = ledger.pending_next_command()
    assert pending is not None

    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 40
    stored = _read_command(ledger, "req-absent")
    assert stored is not None
    assert stored.acknowledgement == "not_fulfilled"
    assert "#99" in stored.detail
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-absent")) == 1


def test_priority_guarded_resolves_with_reason_and_claims_normal(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3: a guarded priority resolves with the guard reason; order proceeds."""
    tracker.labeled_events[(41, LABEL)] = _bot_event(41)
    _commit(ledger, request_id="req-guarded", issue=41)
    pending = ledger.pending_next_command()
    assert pending is not None

    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 40
    stored = _read_command(ledger, "req-guarded")
    assert stored is not None
    assert stored.acknowledgement == "not_fulfilled"
    assert "#41" in stored.detail
    assert "label-authorisation" in stored.detail
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-guarded")) == 1


def test_priority_readback_race_releases_resolves_and_claims_normal(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3: a lost Claim race releases and resolves; the sweep claims on."""
    tracker.inputs[7] = _issue_input(number=7, created_at="2026-08-01T00:00:00Z")
    tracker.labeled_events[(7, LABEL)] = _human_event(7)

    def _intrude(number: int) -> None:
        if number != 41:
            return
        held = tracker.inputs[number]
        tracker.inputs[number] = TrackedInput(
            number=held.number,
            input_type=held.input_type,
            title=held.title,
            body=held.body,
            is_open=held.is_open,
            labels=held.labels,
            assignees=(*held.assignees, "intruder"),
            created_at=held.created_at,
            author_login=held.author_login,
            author_id=held.author_id,
        )

    tracker.on_assign = _intrude
    _commit(ledger, request_id="req-race", issue=41)
    pending = ledger.pending_next_command()
    assert pending is not None

    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 7
    assert ledger.get_attempt("implement#41/1").state == "released"
    stored = _read_command(ledger, "req-race")
    assert stored is not None
    assert stored.acknowledgement == "not_fulfilled"
    assert "#41" in stored.detail
    assert "assigned" in stored.detail
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-race")) == 1


def test_priority_invalid_issue_resolves_and_claims_normal(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3: a malformed priority resolves ``not_fulfilled``; order proceeds."""
    _commit(ledger, request_id="req-invalid", issue=0)
    pending = ledger.pending_next_command()
    assert pending is not None

    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 40
    stored = _read_command(ledger, "req-invalid")
    assert stored is not None
    assert stored.acknowledgement == "not_fulfilled"
    assert "invalid issue" in stored.detail
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-invalid")) == 1


def test_intake_closed_keeps_priority_pending_then_resume_settles(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3: closed Intake opens no Claim; the priority stays ``accepted``."""
    _commit(ledger, request_id="req-stop", issue=0, kind="stop")
    _commit(ledger, request_id="req-closed", issue=41)
    pending = ledger.pending_next_command()
    assert pending is not None

    assert sweep_and_claim(tracker, ledger, _config(tracker), priority=pending) is None

    assert not ledger.has_unfinished_attempt()
    stored = _read_command(ledger, "req-closed")
    assert stored is not None
    assert stored.acknowledgement == "accepted"
    assert _intake_priority(ledger) == {"request_id": "req-closed", "issue": 41}
    assert _resolved_events(ledger, "req-closed") == []

    _commit(ledger, request_id="req-resume", issue=0, kind="resume")
    pending = ledger.pending_next_command()
    assert pending is not None
    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 41
    stored = _read_command(ledger, "req-closed")
    assert stored is not None
    assert stored.acknowledgement == "completed"
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-closed")) == 1


def test_finalize_without_confirm_resolves_priority(
    ledger: RunLedger,
) -> None:
    """§8.1: an Attempt that never confirmed resolves its priority on finalize."""
    _commit(ledger, request_id="req-doomed", issue=44)
    ledger.open_attempt(
        "implement#44/1", effective_configuration={"workflow": "implement"}
    )

    ledger.set_attempt_state("implement#44/1", "finished")

    stored = _read_command(ledger, "req-doomed")
    assert stored is not None
    assert stored.acknowledgement == "not_fulfilled"
    assert "finalized without a confirmed claim" in stored.detail
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-doomed")) == 1


def test_finalize_after_confirm_leaves_newer_priority_pending(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.1: finalizing never resolves a priority committed after the Claim."""
    _commit(ledger, request_id="req-first", issue=44)
    tracker.inputs[44] = _issue_input(number=44, created_at="2026-11-01T00:00:00Z")
    tracker.labeled_events[(44, LABEL)] = _human_event(44)
    pending = ledger.pending_next_command()
    assert pending is not None
    claimed = sweep_and_claim(tracker, ledger, _config(tracker), priority=pending)
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.input_number == 44
    first = _read_command(ledger, "req-first")
    assert first is not None
    assert first.acknowledgement == "completed"

    _commit(ledger, request_id="req-second", issue=44)
    ledger.set_attempt_state(claimed.attempt_id, "finished")

    second = _read_command(ledger, "req-second")
    assert second is not None
    assert second.acknowledgement == "accepted"
    assert _resolved_events(ledger, "req-second") == []
    assert _intake_priority(ledger) == {"request_id": "req-second", "issue": 44}


def test_attempt_issue_number_accepts_step_and_legacy_ids() -> None:
    """The finalization fallback parses step-workflow Attempt ids."""
    assert operator_commands._attempt_issue_number("implement#44/1") == 44
    assert operator_commands._attempt_issue_number("#44/1") == 44
    assert operator_commands._attempt_issue_number("implement#44") is None
    assert operator_commands._attempt_issue_number("#0/1") is None

    from coding_agent.operator import listener as operator_listener

    assert operator_listener._attempt_issue_number("implement#44/1") == 44
    assert operator_listener._attempt_issue_number("#44/1") == 44


def _loop_workflow() -> V2Workflow:
    """One agent step routed straight to ``delivered``: the loop-test Workflow."""
    return V2Workflow(
        id="implement",
        model="sonnet",
        input=V2Input(type="issue", label=LABEL),
        ceiling=V2Ceiling(
            cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="work",
        defaults_routes={"error": _end("failed"), "limit-exceeded": _end("unfinished")},
        steps={
            "work": V2Step(
                id="work",
                agent=V2Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": _end("delivered")},
            ),
        },
    )


def _end(category: str) -> V2RouteTarget:
    return V2RouteTarget(end=category)


def test_worker_loop_tries_priority_first(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """§8.3 end to end: the run loop reads the pending priority each sweep."""
    from coding_agent.runtime.graph.workflow_graph import WorkflowResult
    from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop

    _commit(ledger, request_id="req-loop", issue=41)
    ran: list[int] = []

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="delivered", reason="verified-completion"
        )
        tracker.remove_label(claimed.input_number, "issue", LABEL)
        tracker.unassign_input(claimed.input_number, "issue")
        return WorkflowResult(
            attempt_id=claimed.attempt_id,
            outcome="delivered",
            reason="verified-completion",
        )

    workflow = _loop_workflow()
    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=WorkerLoopConfig(
            claim_config=ClaimConfig(
                workflow_id="implement",
                input_type="issue",
                selection_label=LABEL,
                base_branch="main",
                agent_login=tracker.agent_login,
                agent_id=tracker.agent_id,
                effective_configuration=full_effective_configuration(workflow),
            ),
            workflow=workflow,
            poll_interval_seconds=60.0,
        ),
        run_one_attempt=run_one,
        sleep=lambda _seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert ran == [41]
    stored = _read_command(ledger, "req-loop")
    assert stored is not None
    assert stored.acknowledgement == "completed"
    assert _intake_priority(ledger) is None
    assert len(_resolved_events(ledger, "req-loop")) == 1
