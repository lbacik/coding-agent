"""Offline Read of the Run Ledger and the WAL checkpoint on orderly exit.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §17 (one SQLite
file on the state volume, in WAL mode with ``synchronous=FULL``; every
orderly Worker exit checkpoints the WAL with ``TRUNCATE`` after its last
write) and the operator contract's Offline Read (adoption step 4, #245): a
stopped Worker — including one killed without a clean shutdown, whose ledger
still holds an uncheckpointed WAL — stays inspectable without starting the
Worker, and every orderly Worker exit checkpoints the WAL with ``TRUNCATE``
after its last write.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.identity.startup import Identity
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.offline import open_offline_ledger
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.shutdown import request_shutdown, reset_shutdown
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.worker.volume import VOLUME_LOCK_FILE_NAME
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.fake_step_tracker import FakeStepTracker

IDENTITY = Identity(account_id=999, login="coding-agent")

LABEL = "agent:implement"
EFFECTIVE = {"workflow": "implement"}
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


@pytest.fixture(autouse=True)
def _clean_shutdown_flag() -> Iterator[None]:
    reset_shutdown()
    try:
        yield
    finally:
        reset_shutdown()


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
        agent=V2Agent(results=results),
        max_executions=1,
        routes=parsed,
    )


def _end(category: str) -> V2RouteTarget:
    return V2RouteTarget(end=category)


def _workflow() -> V2Workflow:
    """One agent step routed straight to ``delivered``: the loop-test Workflow."""
    return V2Workflow(
        id="implement",
        model="sonnet",
        input=V2Input(type="issue", label=LABEL),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        defaults_routes={"error": _end("failed"), "limit-exceeded": _end("unfinished")},
        steps={
            "work": _agent_step("work", {"done": {}}, {"done": _end("delivered")}),
        },
    )


def _loop_config(poll: float = 60.0) -> WorkerLoopConfig:
    return WorkerLoopConfig(
        claim_config=_claim_config(), workflow=_workflow(), poll_interval_seconds=poll
    )


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


def _file_hashes(directory: Path) -> dict[str, str]:
    """SHA-256 over every file in ``directory``, by name."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _wal_path(ledger_path: Path) -> Path:
    return Path(str(ledger_path) + "-wal")


def _assert_wal_truncated(ledger_path: Path) -> None:
    """The WAL holds no uncheckpointed frames: absent or zero bytes."""
    wal = _wal_path(ledger_path)
    assert not wal.is_file() or wal.stat().st_size == 0


def test_offline_read_sees_committed_rows_with_uncheckpointed_wal(
    tmp_path: Path,
) -> None:
    """A killed Worker leaves committed rows in the WAL; the reader shows them.

    The writer stays open (no checkpoint, no clean close — the kill shape),
    the source directory is read-only, and the offline reader still sees
    every committed row.
    """
    state_dir = tmp_path / "state"
    ledger_path = state_dir / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    try:
        ledger.open_attempt("implement#7/1", effective_configuration=EFFECTIVE)
        ledger.record_terminal_outcome(
            "implement#7/1", outcome="delivered", reason="verified-completion"
        )
        wal = _wal_path(ledger_path)
        assert wal.is_file()
        assert wal.stat().st_size > 0

        before = _file_hashes(state_dir)
        assert (state_dir / VOLUME_LOCK_FILE_NAME).exists() is False

        os.chmod(state_dir, 0o555)
        try:
            with open_offline_ledger(ledger_path) as offline:
                assert offline.get_attempt("implement#7/1").attempt_id == "implement#7/1"
                outcome = offline.get_outcome("implement#7/1")
                assert outcome is not None
                assert (outcome.outcome, outcome.reason) == (
                    "delivered",
                    "verified-completion",
                )
                assert offline.journal_mode() == "wal"
                assert offline.synchronous() == "2"
        finally:
            os.chmod(state_dir, 0o755)

        assert _file_hashes(state_dir) == before
        assert (state_dir / VOLUME_LOCK_FILE_NAME).exists() is False
    finally:
        ledger.close()


def test_offline_read_creates_no_wal_or_shm_in_the_source(
    tmp_path: Path,
) -> None:
    """The source directory gains no file: the read takes no lock either.

    A ledger closed after an explicit checkpoint holds no WAL; reading it
    back offline must not create a ``-wal`` or ``-shm`` file in the source,
    and the volume lock file must not appear.
    """
    state_dir = tmp_path / "state"
    ledger_path = state_dir / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    ledger.open_attempt("implement#7/1", effective_configuration=EFFECTIVE)
    ledger.checkpoint()
    ledger.close()
    wal = _wal_path(ledger_path)
    if wal.is_file():
        assert wal.stat().st_size == 0
        wal.unlink()
    shm = Path(str(ledger_path) + "-shm")
    if shm.is_file():
        shm.unlink()
    before = sorted(path.name for path in state_dir.iterdir())

    with open_offline_ledger(ledger_path) as offline:
        assert offline.get_attempt("implement#7/1").attempt_id == "implement#7/1"

    assert sorted(path.name for path in state_dir.iterdir()) == before
    assert not _wal_path(ledger_path).exists()
    assert not Path(str(ledger_path) + "-shm").exists()
    assert (state_dir / VOLUME_LOCK_FILE_NAME).exists() is False


def test_offline_read_missing_ledger_raises(tmp_path: Path) -> None:
    """No ledger file means no Offline Read, instead of an empty one."""
    with pytest.raises(FileNotFoundError):
        with open_offline_ledger(tmp_path / "run-ledger.sqlite3"):
            pass  # pragma: no cover - the open itself raises


def test_checkpoint_truncates_the_wal_and_keeps_every_row(
    tmp_path: Path,
) -> None:
    """``checkpoint`` replays committed frames and truncates the WAL."""
    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    try:
        ledger.open_attempt("implement#7/1", effective_configuration=EFFECTIVE)
        ledger.record_terminal_outcome(
            "implement#7/1", outcome="delivered", reason="verified-completion"
        )
        assert _wal_path(ledger_path).stat().st_size > 0

        ledger.checkpoint()

        _assert_wal_truncated(ledger_path)
        assert ledger.get_attempt("implement#7/1").attempt_id == "implement#7/1"
        outcome = ledger.get_outcome("implement#7/1")
        assert outcome is not None
        assert outcome.outcome == "delivered"
        assert ledger.journal_mode() == "wal"
        assert ledger.synchronous() == "2"
    finally:
        ledger.close()


def test_orderly_exit_checkpoints_the_wal_with_truncate(tmp_path: Path) -> None:
    """After a normal exit the WAL is checkpointed; rows stay readable."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

        def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
            tracker.remove_label(claimed.input_number, "issue", LABEL)
            tracker.unassign_input(claimed.input_number, "issue")
            ledger.record_terminal_outcome(
                claimed.attempt_id, outcome="delivered", reason="verified-completion"
            )
            return WorkflowResult(
                attempt_id=claimed.attempt_id,
                outcome="delivered",
                reason="verified-completion",
            )

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(),
            run_one_attempt=run_one,
            sleep=lambda seconds: None,
            max_attempts=1,
            max_sweeps=5,
        )

        assert exit_code == 0
        _assert_wal_truncated(ledger.path)
        outcome = ledger.get_outcome("implement#31/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == (
            "delivered",
            "verified-completion",
        )
    finally:
        ledger.close()


def test_sigterm_shutdown_checkpoints_the_wal_with_truncate(tmp_path: Path) -> None:
    """A SIGTERM shutdown checkpoints too: intake stops, the WAL truncates."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = _tracker_with(
            (31, "2026-10-02T00:00:00Z"), (32, "2026-10-01T00:00:00Z")
        )
        ran: list[int] = []

        def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
            ran.append(claimed.input_number)
            request_shutdown()
            tracker.remove_label(claimed.input_number, "issue", LABEL)
            tracker.unassign_input(claimed.input_number, "issue")
            ledger.record_terminal_outcome(
                claimed.attempt_id, outcome="failed", reason="interrupted"
            )
            return WorkflowResult(
                attempt_id=claimed.attempt_id,
                outcome="failed",
                reason="interrupted",
            )

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(),
            run_one_attempt=run_one,
            sleep=lambda seconds: None,
            max_attempts=2,
            max_sweeps=5,
        )

        assert exit_code == 0
        assert ran == [32]
        _assert_wal_truncated(ledger.path)
        outcome = ledger.get_outcome("implement#32/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == ("failed", "interrupted")
    finally:
        ledger.close()
