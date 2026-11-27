"""A Worker on an independent state volume trusts the terminal comment (§6.1).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §6.1 (a Claim
Comment stops being in force when its Attempt's terminal comment is
published) read across state volumes: where the Worker holds no local record
of the old Claim, a trusted terminal comment on the Workflow Input ends it.
Trust requires the Claim's exact Attempt id, the configured Agent Identity's
numeric author id and the canonical terminal marker; a forged author, a
malformed marker or a mismatched Attempt never ends the Claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    attempt_claim,
    has_trusted_terminal_comment,
    sweep_and_claim,
    terminal_marker_for,
    trusted_terminal_attempt_ids,
)
from coding_agent.inputs.port import InputComment, InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger, step_effect_marker
from coding_agent.runtime.step_finalisation import (
    FinalisationTarget,
    WorkflowEnded,
    finish_attempt,
    render_terminal_comment,
)
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.fake_step_tracker import FakeStepTracker
from support.effective_config import full_effective_configuration

LABEL = "agent:implement"
AGENT_ID = 1001


def _issue(
    *,
    number: int = 42,
    labels: tuple[str, ...] = (LABEL,),
    assignees: tuple[str, ...] = (),
    is_open: bool = True,
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=is_open,
        labels=labels,
        assignees=assignees,
        created_at="2026-10-01T00:00:00Z",
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


def _config(tracker: FakeStepTracker) -> ClaimConfig:
    effective: Mapping[str, object] = {"workflow": "implement"}
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration=effective,
    )


def _claim_body(attempt: str) -> str:
    marker = step_effect_marker(attempt, "", 0, 1, kind="claim")
    return f"coding-agent claims {attempt}.\n\n{marker}\n"


@pytest.fixture
def tracker() -> FakeStepTracker:
    fake = FakeStepTracker(agent_login="coding-agent", agent_id=AGENT_ID)
    fake.inputs[42] = _issue()
    fake.labeled_events[(42, LABEL)] = _human_event()
    return fake


@pytest.fixture
def ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "ledger.db")


def _seed_claim(tracker: FakeStepTracker, attempt: str) -> InputComment:
    comment = InputComment(
        comment_id=tracker._next_comment_id,
        body=_claim_body(attempt),
        author_login="coding-agent",
        author_id=AGENT_ID,
    )
    tracker._next_comment_id += 1
    tracker.comments.append(comment)
    return comment


def _seed_trusted_terminal(tracker: FakeStepTracker, attempt: str) -> InputComment:
    body = render_terminal_comment(attempt_id=attempt, outcome="completed")
    return tracker.post_comment(42, "issue", body)


def test_trusted_terminal_matches_exact_attempt_and_author(
    tracker: FakeStepTracker,
) -> None:
    """The trusted check needs the exact id, the numeric author and the marker."""
    _seed_claim(tracker, "implement#42/1")
    _seed_trusted_terminal(tracker, "implement#42/1")
    assert has_trusted_terminal_comment(
        tracker.comments, "implement#42/1", AGENT_ID
    ) is True
    assert has_trusted_terminal_comment(
        tracker.comments, "implement#42/2", AGENT_ID
    ) is False


def test_forged_author_terminal_is_ignored(tracker: FakeStepTracker) -> None:
    """A terminal comment by another account never ends the Claim."""
    _seed_claim(tracker, "implement#42/1")
    body = render_terminal_comment(attempt_id="implement#42/1", outcome="completed")
    tracker.comments.append(
        InputComment(
            comment_id=tracker._next_comment_id,
            body=body,
            author_login="human",
            author_id=424242,
        )
    )
    tracker._next_comment_id += 1
    assert has_trusted_terminal_comment(
        tracker.comments, "implement#42/1", AGENT_ID
    ) is False
    assert trusted_terminal_attempt_ids(tracker.comments, AGENT_ID) == set()


def test_malformed_terminal_marker_is_ignored(tracker: FakeStepTracker) -> None:
    """A terminal comment without the canonical marker never ends the Claim."""
    _seed_claim(tracker, "implement#42/1")
    tracker.comments.append(
        InputComment(
            comment_id=tracker._next_comment_id,
            body="coding-agent implement#42/1: completed\nkind=terminal but no marker",
            author_login="coding-agent",
            author_id=AGENT_ID,
        )
    )
    tracker._next_comment_id += 1
    assert has_trusted_terminal_comment(
        tracker.comments, "implement#42/1", AGENT_ID
    ) is False


def test_mismatched_attempt_terminal_is_ignored(tracker: FakeStepTracker) -> None:
    """A terminal comment for another Attempt never ends this Claim."""
    _seed_claim(tracker, "implement#42/1")
    _seed_trusted_terminal(tracker, "implement#42/2")
    assert has_trusted_terminal_comment(
        tracker.comments, "implement#42/1", AGENT_ID
    ) is False
    assert has_trusted_terminal_comment(
        tracker.comments, "implement#42/2", AGENT_ID
    ) is True


def test_missing_numeric_identity_trusts_nothing(
    tracker: FakeStepTracker,
) -> None:
    """Without the numeric author id no terminal comment is trusted."""
    _seed_claim(tracker, "implement#42/1")
    _seed_trusted_terminal(tracker, "implement#42/1")
    assert has_trusted_terminal_comment(tracker.comments, "implement#42/1", None) is False
    assert trusted_terminal_attempt_ids(tracker.comments, None) == set()


def test_independent_volume_claims_past_trusted_terminal(
    tracker: FakeStepTracker, ledger: RunLedger, tmp_path: Path
) -> None:
    """No local record + trusted terminal: the old Claim is not in force."""
    _seed_claim(tracker, "implement#42/1")
    _seed_trusted_terminal(tracker, "implement#42/1")
    fresh = RunLedger(tmp_path / "fresh.db")
    try:
        claimed = sweep_and_claim(tracker, tracker, fresh, _config(tracker))
    finally:
        fresh.close()
    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.attempt_id == "implement#42/2"


def test_independent_volume_blocked_by_active_claim(
    tracker: FakeStepTracker, tmp_path: Path
) -> None:
    """No terminal: the old Claim still participates in arbitration."""
    _seed_claim(tracker, "implement#42/1")
    fresh = RunLedger(tmp_path / "fresh.db")
    try:
        assert sweep_and_claim(tracker, tracker, fresh, _config(tracker)) is None
    finally:
        fresh.close()


def test_independent_volume_blocked_by_forged_terminal(
    tracker: FakeStepTracker, tmp_path: Path
) -> None:
    """A forged terminal never releases the old Claim."""
    _seed_claim(tracker, "implement#42/1")
    body = render_terminal_comment(attempt_id="implement#42/1", outcome="completed")
    tracker.comments.append(
        InputComment(
            comment_id=tracker._next_comment_id,
            body=body,
            author_login="human",
            author_id=424242,
        )
    )
    tracker._next_comment_id += 1
    fresh = RunLedger(tmp_path / "fresh.db")
    try:
        assert sweep_and_claim(tracker, tracker, fresh, _config(tracker)) is None
    finally:
        fresh.close()


def test_guards_still_apply_past_trusted_terminal(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """Trusted terminal clears the Claim guard, never the assignment guard."""
    _seed_claim(tracker, "implement#42/1")
    _seed_trusted_terminal(tracker, "implement#42/1")
    tracker.inputs[42] = _issue(assignees=("human",))
    claimed = attempt_claim(
        tracker, ledger, _config(tracker), tracker.inputs[42], identity_mode="dedicated"
    )
    assert isinstance(claimed, ReleasedClaim)
    assert claimed.reason == "assigned"


def test_terminal_marker_is_canonical() -> None:
    """The trusted marker is the finalisation position with kind=terminal."""
    assert terminal_marker_for("implement#42/1") == (
        "<!-- coding-agent attempt=implement#42/1 kind=terminal step=- n=0 ordinal=0 -->"
    )


def _workflow() -> Workflow:
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20,
            effective_tokens=1000,
            tool_calls=10,
            wall_clock_minutes=5,
        ),
        start="work",
        outcomes={},
        defaults_routes={"error": RouteTarget(end="failed")},
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": RouteTarget(end="completed")},
            ),
        },
    )


def _loop_config(tracker: FakeStepTracker, workflow: Workflow) -> WorkerLoopConfig:
    return WorkerLoopConfig(
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
        poll_interval_seconds=0.0,
    )


def test_agent_run_two_volumes_finished_permits_new_claim(tmp_path: Path) -> None:
    """Through ``agent run``: finished Claim ends, a new volume claims next n."""
    from coding_agent.runtime.graph.workflow_graph import WorkflowResult

    workflow = _workflow()
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=AGENT_ID)
    tracker.inputs[42] = _issue()
    tracker.labeled_events[(42, LABEL)] = _human_event()

    first_path = tmp_path / "vol-a" / "run-ledger.sqlite3"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_ledger = RunLedger(first_path)

    def _finish_one(
        ledger: RunLedger,
    ) -> object:
        def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
            finish_attempt(
                inputs=tracker, code_host=tracker,
                ledger=ledger,
                attempt_id=claimed.attempt_id,
                target=FinalisationTarget(
                    number=claimed.input_number,
                    input_type="issue",
                    selection_label=LABEL,
                ),
                decision=WorkflowEnded(category="completed", reason="done"),
                end_labels_for={},
            )
            return WorkflowResult(
                attempt_id=claimed.attempt_id, outcome="completed", reason="done"
            )

        return run_one

    try:
        exit_code = run_worker_loop(
            inputs=tracker, code_host=tracker,
            ledger=first_ledger,
            loop_config=_loop_config(tracker, workflow),
            run_one_attempt=_finish_one(first_ledger),  # type: ignore[arg-type]
            sleep=lambda _: None,
            max_attempts=1,
        )
    finally:
        pass
    assert exit_code == 0
    assert first_ledger.get_attempt("implement#42/1").state == "finished"
    assert any(
        "kind=terminal" in comment.body and comment.author_id == AGENT_ID
        for comment in tracker.comments
    )
    # The finished Attempt removed the Selection Label; a human re-labels
    # the input to request the next Attempt.
    held = tracker.inputs[42]
    tracker.inputs[42] = TrackedInput(
        number=held.number,
        input_type=held.input_type,
        title=held.title,
        body=held.body,
        is_open=held.is_open,
        labels=(LABEL,),
        assignees=(),
        created_at=held.created_at,
        author_login=held.author_login,
        author_id=held.author_id,
    )

    second_path = tmp_path / "vol-b" / "run-ledger.sqlite3"
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_ledger = RunLedger(second_path)
    try:
        second_claimed = sweep_and_claim(
            tracker, tracker, second_ledger, _loop_config(tracker, workflow).claim_config
        )
    finally:
        first_ledger.close()
        second_ledger.close()
    assert isinstance(second_claimed, ConfirmedClaim)
    assert second_claimed.attempt_id == "implement#42/2"


def test_agent_run_two_volumes_active_claim_still_arbitrates(
    tmp_path: Path, input_type: InputType = "issue"
) -> None:
    """Through the Claim path: an active Claim on volume A blocks volume B."""
    _ = input_type
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=AGENT_ID)
    tracker.inputs[42] = _issue()
    tracker.labeled_events[(42, LABEL)] = _human_event()
    first_ledger = RunLedger(tmp_path / "a.db")
    second_ledger = RunLedger(tmp_path / "b.db")
    try:
        first = sweep_and_claim(tracker, tracker, first_ledger, _config(tracker))
        assert isinstance(first, ConfirmedClaim)
        assert first.attempt_id == "implement#42/1"
        assert sweep_and_claim(tracker, tracker, second_ledger, _config(tracker)) is None
    finally:
        first_ledger.close()
        second_ledger.close()
