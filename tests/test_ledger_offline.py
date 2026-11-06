"""Offline Read of the Run Ledger and the WAL checkpoint on orderly exit.

Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (one SQLite file
on the state volume, in WAL mode with ``synchronous=FULL``) and the
operator contract's Offline Read (adoption step 4, #245): a stopped Worker
— including one killed without a clean shutdown, whose ledger still holds
an uncheckpointed WAL — stays inspectable without starting the Worker, and
every orderly Worker exit checkpoints the WAL with ``TRUNCATE`` after its
last write.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.ledger.offline import open_offline_ledger
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.runtime.shutdown import request_shutdown, reset_shutdown
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.worker.volume import VOLUME_LOCK_FILE_NAME
from support.fake_tracker import FakeTracker

EFFECTIVE_CONFIGURATION = {"repo": "example-owner/example-repo", "workflow": "implement"}
SELECTION_LABEL = "ready-for-agent"
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


@pytest.fixture(autouse=True)
def _clean_shutdown_flag() -> Iterator[None]:
    reset_shutdown()
    try:
        yield
    finally:
        reset_shutdown()


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        selection_label=SELECTION_LABEL,
        agent_login="coding-agent",
        agent_identity_id=999,
        effective_configuration=EFFECTIVE_CONFIGURATION,
    )


def _candidate(number: int, created_at: str) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title=f"Add a greeting {number}",
        body=BODY,
        is_open=True,
        assignees=(),
        labels=(SELECTION_LABEL,),
        created_at=created_at,
    )


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
        ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
        ledger.record_terminal_outcome(
            "#7/1", outcome="delivered", reason="verified-completion"
        )
        wal = _wal_path(ledger_path)
        assert wal.is_file()
        assert wal.stat().st_size > 0

        before = _file_hashes(state_dir)
        assert (state_dir / VOLUME_LOCK_FILE_NAME).exists() is False

        os.chmod(state_dir, 0o555)
        try:
            with open_offline_ledger(ledger_path) as offline:
                assert offline.get_attempt("#7/1").attempt_id == "#7/1"
                outcome = offline.get_outcome("#7/1")
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
    ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
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
        assert offline.get_attempt("#7/1").attempt_id == "#7/1"

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
        ledger.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
        ledger.record_terminal_outcome(
            "#7/1", outcome="delivered", reason="verified-completion"
        )
        assert _wal_path(ledger_path).stat().st_size > 0

        ledger.checkpoint()

        _assert_wal_truncated(ledger_path)
        assert ledger.get_attempt("#7/1").attempt_id == "#7/1"
        outcome = ledger.get_outcome("#7/1")
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
        tracker = FakeTracker(agent_login="coding-agent")
        tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")

        def run_one(claimed: ClaimedAttempt) -> AttemptResult:
            tracker.remove_selection_label(claimed.issue_number)
            tracker.release_assignment(claimed.issue_number)
            ledger.record_terminal_outcome(
                claimed.attempt_id, outcome="delivered", reason="verified-completion"
            )
            return AttemptResult(
                attempt_id=claimed.attempt_id,
                outcome="delivered",
                reason="verified-completion",
            )

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=WorkerLoopConfig(
                claim_config=_claim_config(), poll_interval_seconds=60.0
            ),
            run_one_attempt=run_one,
            sleep=lambda seconds: None,
            max_attempts=1,
        )

        assert exit_code == 0
        _assert_wal_truncated(ledger.path)
        outcome = ledger.get_outcome("#31/1")
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
        tracker = FakeTracker(agent_login="coding-agent")
        tracker.candidates[31] = _candidate(31, "2026-10-02T00:00:00Z")
        tracker.candidates[32] = _candidate(32, "2026-10-01T00:00:00Z")
        ran: list[int] = []

        def run_one(claimed: ClaimedAttempt) -> AttemptResult:
            ran.append(claimed.issue_number)
            request_shutdown()
            tracker.remove_selection_label(claimed.issue_number)
            tracker.release_assignment(claimed.issue_number)
            ledger.record_terminal_outcome(
                claimed.attempt_id, outcome="failed", reason="interrupted"
            )
            return AttemptResult(
                attempt_id=claimed.attempt_id,
                outcome="failed",
                reason="interrupted",
            )

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=WorkerLoopConfig(
                claim_config=_claim_config(), poll_interval_seconds=60.0
            ),
            run_one_attempt=run_one,
            sleep=lambda seconds: None,
            max_attempts=2,
        )

        assert exit_code == 0
        assert ran == [32]
        _assert_wal_truncated(ledger.path)
        outcome = ledger.get_outcome("#32/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == ("failed", "interrupted")
    finally:
        ledger.close()
