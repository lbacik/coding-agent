"""One Worker per state volume, and SQLite never meets a full disk.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup step 2:
take the exclusive lock (``flock``) on the state volume; a second process
using the same state volume is refused) and §17 ("Storage": one SQLite file
on the state volume, accessed under the volume lock; "Liveness is the volume
lock").

MVP row ids: W-CFG-14, L3-ERR-13.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.identity.startup import Identity, StartupCheckResult
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker import supervisor
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from coding_agent.worker.volume import (
    MIN_FREE_BYTES,
    VolumeLockHeld,
    acquire_volume_lock,
    volume_free_bytes,
)
from support.fake_tracker import FakeTracker

IDENTITY = Identity(account_id=999, login="coding-agent")
SELECTION_LABEL = "ready-for-agent"
EFFECTIVE = {"repo": "example-owner/example-repo", "workflow": "implement"}
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"

_RUN_CONFIG = """\
version: 1

target:
  kind: github
  repository: example-owner/example-repo
  base_branch: main
  token_env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api_key_env: ANTHROPIC_API_KEY
    prices_usd_per_mtok: {input: 2.00, output: 10.00, cache_read: 0.20, cache_write: 2.50}

worker:
  poll_interval_seconds: 5

workflows:
  implement:
    model: sonnet
    selector:
      label: ready-for-agent
    steps:
      - id: implement
        skill: implement
"""


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(_RUN_CONFIG, encoding="utf-8")
    return path


def _write_matrix(tmp_path: Path) -> Path:
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


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        selection_label=SELECTION_LABEL,
        agent_login=IDENTITY.login,
        agent_identity_id=IDENTITY.account_id,
        effective_configuration=EFFECTIVE,
    )


# --- The exclusive volume lock (W-CFG-14) -----------------------------------


def test_a_second_holder_of_the_same_state_directory_is_refused(
    tmp_path: Path,
) -> None:
    """W-CFG-14, unit level: ``flock`` is exclusive; release hands it over."""
    first = acquire_volume_lock(tmp_path / "state")
    try:
        with pytest.raises(VolumeLockHeld):
            acquire_volume_lock(tmp_path / "state")
    finally:
        first.release()
    second = acquire_volume_lock(tmp_path / "state")
    second.release()


def test_the_lock_is_released_by_its_context_manager(tmp_path: Path) -> None:
    """The ``with`` form releases, so ``run_worker``'s early refusals unlock."""
    with acquire_volume_lock(tmp_path / "state"):
        with pytest.raises(VolumeLockHeld):
            acquire_volume_lock(tmp_path / "state")
    acquired = acquire_volume_lock(tmp_path / "state")
    acquired.release()


def test_w_cfg_14_a_second_run_on_the_same_state_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-CFG-14: while the volume is locked, ``run_worker`` exits non-zero
    before any GitHub call — no startup check, no tracker, no ledger file."""
    github_calls: list[str] = []
    tracker_built: list[str] = []

    def _github(*args: object, **kwargs: object) -> StartupCheckResult:
        github_calls.append("run_startup_checks")
        return StartupCheckResult(
            identity=IDENTITY,
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        )

    def _tracker_factory(**kwargs: object) -> FakeTracker:
        tracker_built.append("github_tracker")
        return FakeTracker(agent_login=IDENTITY.login)

    monkeypatch.setattr(supervisor, "run_startup_checks", _github)
    # Present but never used: the refusal lands at startup step 2, before
    # any GitHub call could read them.
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    state_dir = tmp_path / "state"
    held = acquire_volume_lock(state_dir)
    try:
        exit_code = supervisor.run_worker(
            config=_write_config(tmp_path),
            workflow="implement",
            state_dir=state_dir,
            skills_home=tmp_path,
            skill_bundle=supervisor.SkillBundleCheck(
                install_report=tmp_path / "install.json",
                list_report=tmp_path / "list.json",
                home=tmp_path,
                ignore_file=tmp_path / "ignore.txt",
            ),
            toolchain_matrix_path=_write_matrix(tmp_path),
            tracker_factory=_tracker_factory,
            sleep=lambda seconds: None,
            max_sweeps=1,
        )
    finally:
        held.release()

    assert exit_code == 1
    assert github_calls == []
    assert tracker_built == []
    assert not (state_dir / "run-ledger.sqlite3").exists()
    assert "locked" in capsys.readouterr().err


def test_the_volume_lock_is_held_for_the_whole_run_and_released_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is taken at startup step 2 and held until the Worker exits:
    a re-acquire attempted while the poll loop runs is refused, and one
    attempted after the return succeeds."""
    from coding_agent.skillbundle.verify import VerificationReport

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    passing = VerificationReport()
    passing.add("bundle", True, "verified")
    monkeypatch.setattr(
        supervisor, "verify_installed_skill_bundle", lambda check: passing
    )
    monkeypatch.setattr(
        supervisor,
        "run_startup_checks",
        lambda *args, **kwargs: StartupCheckResult(
            identity=IDENTITY,
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        ),
    )
    monkeypatch.setattr(supervisor, "build_chat_model", lambda pin, **kwargs: object())
    monkeypatch.setattr(
        supervisor,
        "assert_provider_capability",
        lambda pin, model, table, **kwargs: None,
    )

    state_dir = tmp_path / "state"
    held_during_run: list[bool] = []

    def _stub_loop(**kwargs: object) -> int:
        try:
            acquire_volume_lock(state_dir)
        except VolumeLockHeld:
            held_during_run.append(True)
        else:  # pragma: no cover - the lock must be held here
            held_during_run.append(False)
        return 0

    monkeypatch.setattr(supervisor, "run_worker_loop", _stub_loop)
    exit_code = supervisor.run_worker(
        config=_write_config(tmp_path),
        workflow="implement",
        state_dir=state_dir,
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        toolchain_matrix_path=_write_matrix(tmp_path),
        tracker_factory=lambda **kwargs: FakeTracker(agent_login=IDENTITY.login),
        sleep=lambda seconds: None,
    )

    assert exit_code == 0
    assert held_during_run == [True]
    released = acquire_volume_lock(state_dir)
    released.release()


# --- The volume free-space check (L3-ERR-13) ---------------------------------


def test_volume_free_bytes_reports_the_tmp_volume(tmp_path: Path) -> None:
    """The seam reads the real filesystem: a non-negative byte count."""
    free = volume_free_bytes(tmp_path)
    assert isinstance(free, int)
    assert free >= 0
    assert MIN_FREE_BYTES > 0


def test_l3_err_13_an_attempt_does_not_start_on_a_full_volume(
    tmp_path: Path,
) -> None:
    """L3-ERR-13: below the threshold the sweep claims nothing — no tracker
    call, no ledger row, and nothing written for the Attempt."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = FakeTracker(agent_login=IDENTITY.login)
        tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")
        sleeps: list[float] = []

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=WorkerLoopConfig(
                claim_config=_claim_config(), poll_interval_seconds=60.0
            ),
            run_one_attempt=lambda claimed: (_ for _ in ()).throw(
                AssertionError("no Attempt may start on a full volume")
            ),
            sleep=sleeps.append,
            max_sweeps=3,
            free_bytes=lambda path: 0,
        )

        assert exit_code == 0
        assert tracker.calls == []
        assert not ledger.has_unfinished_attempt()
        assert ledger.records("#31/1") == ()
        assert sum(sleeps) == pytest.approx(2 * 60.0)
    finally:
        ledger.close()


def test_l3_err_13_a_stat_failure_is_treated_as_a_full_volume(
    tmp_path: Path,
) -> None:
    """Where free space cannot be read, the Attempt does not start either:
    SQLite never meets a disk the Worker could not measure."""

    def _unmeasurable(path: Path) -> int:
        raise OSError(5, "Input/output error")

    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = FakeTracker(agent_login=IDENTITY.login)
        tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=WorkerLoopConfig(
                claim_config=_claim_config(), poll_interval_seconds=60.0
            ),
            run_one_attempt=lambda claimed: (_ for _ in ()).throw(
                AssertionError("no Attempt may start on an unmeasurable volume")
            ),
            sleep=lambda seconds: None,
            max_sweeps=1,
            free_bytes=_unmeasurable,
        )

        assert exit_code == 0
        assert tracker.calls == []
        assert not ledger.has_unfinished_attempt()
    finally:
        ledger.close()


def test_l3_err_13_the_worker_claims_again_once_space_returns(
    tmp_path: Path,
) -> None:
    """One full sweep sleeps without claiming; the next sweep, with space
    back, claims the candidate and runs it."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = FakeTracker(agent_login=IDENTITY.login)
        tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")
        answers: list[int] = [0, MIN_FREE_BYTES + 1]

        def _space(path: Path) -> int:
            return answers.pop(0)

        ran: list[int] = []
        sleeps: list[float] = []

        def _run_one(claimed: ClaimedAttempt) -> AttemptResult:
            ran.append(claimed.issue_number)
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
            run_one_attempt=_run_one,
            sleep=sleeps.append,
            max_attempts=1,
            free_bytes=_space,
        )

        assert exit_code == 0
        assert ran == [31]
        assert sum(sleeps) == pytest.approx(60.0)
        assert ledger.get_attempt("#31/1").state == "finished"
    finally:
        ledger.close()


def test_free_space_is_checked_before_each_claim_not_just_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate sits on every sweep: space vanishing mid-run stops the next
    Attempt, and the warning names the volume."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = FakeTracker(agent_login=IDENTITY.login)
        tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")
        tracker.candidates[32] = _candidate(32, "2026-10-02T00:00:00Z")
        answers: list[int] = [MIN_FREE_BYTES + 1, 0]

        def _space(path: Path) -> int:
            return answers.pop(0) if answers else 0

        ran: list[int] = []

        def _run_one(claimed: ClaimedAttempt) -> AttemptResult:
            ran.append(claimed.issue_number)
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
            run_one_attempt=_run_one,
            sleep=lambda seconds: None,
            max_sweeps=3,
            free_bytes=_space,
        )

        assert exit_code == 0
        assert ran == [31]
        assert "free space" in capsys.readouterr().err
    finally:
        ledger.close()
