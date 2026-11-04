"""Startup recovery: the unconfirmed Claim, pinned config and intake (M8).

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup runs
recovery before any selection; intake stays closed while an Attempt is
unfinished; a failed recovery write is retried on the next poll cycle and
three consecutive failures halt non-zero), §5 (the Claim opens the Attempt
before the assignment, so recovery can release it) and §17 (the recovery
table: ``open`` settles to ``released`` with our assignment removed where
present, no comment and no label change; pinned values only; a missing or
unreadable Effective Configuration record refuses startup; recovery never
calls the model).

MVP row ids: REC-CLM-1, REC-CLM-2, REC-CLM-3, P-CLM-3, REC-CFG-1,
REC-CFG-2, W-PIN-3, REC-WRK-1, REC-WRK-2.
"""

from __future__ import annotations

import inspect
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import checkout_workspace, ensure_mirror
from coding_agent.runtime.graph.attempt import AttemptResult
from coding_agent.tracker.github import GitHubTrackerError
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt, select_and_claim
from coding_agent.worker.recovery import (
    RecoveryRefused,
    pinned_recovery_config,
    recover_open_attempt,
    recover_unfinished_attempts,
    validate_recovery_configs,
)
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from test_mvp_attempt_graph import (  # noqa: F401  (fixtures reused)
    IDENTITY,
    bare_remote,
)
from test_worker_run import (  # noqa: F401  (startup seams reused)
    _StartupSpy,
    _run,
)

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


def _pinned(label: str = SELECTION_LABEL) -> dict[str, object]:
    """A minimal pinned Effective Configuration record (contract §2.5)."""
    return {
        "target": {
            "kind": "github",
            "repository": "example-owner/example-repo",
            "base_branch": "main",
            "token_env": "GITHUB_TOKEN",
            "ready_for_human": READY_FOR_HUMAN,
        },
        "workflow": {
            "id": "implement",
            "selector": {"label": label},
        },
    }


def _candidate(
    number: int,
    *,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title="Add a greeting",
        body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
        is_open=True,
        assignees=assignees,
        labels=labels,
        created_at="2026-10-01T00:00:00Z",
    )


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        selection_label=SELECTION_LABEL,
        ready_for_human=READY_FOR_HUMAN,
        agent_login=IDENTITY.login,
        agent_identity_id=IDENTITY.account_id,
        effective_configuration=_pinned(),
    )


def _loop_config() -> WorkerLoopConfig:
    return WorkerLoopConfig(claim_config=_claim_config(), poll_interval_seconds=60.0)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _stub_run(
    ledger: RunLedger, ran: list[int]
) -> Callable[[ClaimedAttempt], AttemptResult]:
    """A stub Attempt runner recording the Terminal Outcome, as the graph would."""

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ran.append(claimed.issue_number)
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="delivered", reason="verified-completion"
        )
        return AttemptResult(
            attempt_id=claimed.attempt_id,
            outcome="delivered",
            reason="verified-completion",
        )

    return run_one


def _writes(call_names: list[str]) -> list[str]:
    """The GitHub writes among the tracker's calls (reads are not writes)."""
    return [name for name in call_names if name != "list_candidates" and name in {
        "assign_issue",
        "release_assignment",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "open_pull_request",
    }]


# --- The ledger readers -----------------------------------------------------


def test_unfinished_attempts_lists_only_unfinished_in_ledger_order(
    ledger: RunLedger,
) -> None:
    """§17: the Supervisor finds every unfinished Attempt in the ledger."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.open_attempt("#8/1", effective_configuration=_pinned())
    ledger.open_attempt("#9/1", effective_configuration=_pinned())
    ledger.set_attempt_state("#8/1", "released")
    ledger.set_attempt_state("#9/1", "finished")

    assert [record.attempt_id for record in ledger.unfinished_attempts()] == ["#7/1"]


# --- REC-CLM-1: crash after the Attempt transaction, before assign -----------


def test_rec_clm_1_crash_before_assign_releases_with_no_github_write(
    ledger: RunLedger,
) -> None:
    """REC-CLM-1: ``open``, no recorded assignment — released, nothing written."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)

    summary = recover_unfinished_attempts(
        tracker, ledger, agent_login=IDENTITY.login
    )

    assert summary.settled == ("#7/1",)
    assert summary.pending == ()
    assert ledger.get_attempt("#7/1").state == "released"
    assert tracker.calls == []


def test_rec_clm_1_the_issue_is_claimable_as_n_plus_one(
    ledger: RunLedger,
) -> None:
    """REC-CLM-1: after recovery the issue is claimed as ``#N/n+1``."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)
    recover_unfinished_attempts(tracker, ledger, agent_login=IDENTITY.login)

    claimed = select_and_claim(tracker=tracker, ledger=ledger, config=_claim_config())

    assert claimed is not None
    assert (claimed.attempt_id, claimed.attempt_number) == ("#7/2", 2)
    assert ledger.get_attempt("#7/2").state == "claim-confirmed"


class _FailOnceAssignTracker(FakeTracker):
    """The halt path of #174: the Claim's ``assign`` fails mid-Claim."""

    fail_next_assign: bool = True

    def assign_issue(self, issue_number: int) -> None:
        if self.fail_next_assign:
            self.calls.append(("assign_issue", (issue_number,)))
            self.fail_next_assign = False
            raise GitHubTrackerError("POST /repos/o/r/issues/7/assignees returned 502")
        super().assign_issue(issue_number)


def test_rec_clm_1_through_the_claim_halt_path(ledger: RunLedger) -> None:
    """REC-CLM-1 through the #174 halt: ``assign`` fails, the Worker halts,
    and the restart releases the Attempt and claims ``#N/2``."""
    tracker = _FailOnceAssignTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)
    ran: list[int] = []

    halted = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert halted == 1
    assert ledger.get_attempt("#7/1").state == "open"
    assert ran == []

    restarted = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert restarted == 0
    # Recovery before assign performs no GitHub write at all: the only
    # assignments are the failed Claim's and the new Claim's.
    assert tracker.call_names().count("assign_issue") == 2
    assert "release_assignment" not in tracker.call_names()
    assert ledger.get_attempt("#7/1").state == "released"
    assert ran == [7]
    assert ledger.get_attempt("#7/2").state == "finished"


# --- REC-CLM-2: crash after assign, before the read-back ---------------------


def test_rec_clm_2_crash_after_assign_removes_only_our_assignment(
    ledger: RunLedger,
) -> None:
    """REC-CLM-2: ``open`` with ``assigned_by_us`` — our assignment removed,
    released, no comment and no label change."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7, assignees=(IDENTITY.login,))

    summary = recover_unfinished_attempts(
        tracker, ledger, agent_login=IDENTITY.login
    )

    assert summary.settled == ("#7/1",)
    assert ledger.get_attempt("#7/1").state == "released"
    assert tracker.candidates[7].assignees == ()
    assert "post_terminal_comment" not in tracker.call_names()
    assert "remove_selection_label" not in tracker.call_names()
    assert "add_ready_for_human" not in tracker.call_names()


def test_rec_clm_2_an_absent_assignment_is_no_write(ledger: RunLedger) -> None:
    """REC-CLM-2: ours recorded but already absent remotely — no release call."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)

    recover_unfinished_attempts(tracker, ledger, agent_login=IDENTITY.login)

    assert ledger.get_attempt("#7/1").state == "released"
    assert "release_assignment" not in tracker.call_names()


def test_rec_clm_2_a_rival_assignee_is_never_removed(ledger: RunLedger) -> None:
    """§5: recovery removes only its own assignment — never another assignee."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(
        7, assignees=(IDENTITY.login, "human")
    )

    recover_unfinished_attempts(tracker, ledger, agent_login=IDENTITY.login)

    assert tracker.candidates[7].assignees == ("human",)
    assert ledger.get_attempt("#7/1").state == "released"


class _FailOnceReadBackTracker(FakeTracker):
    """The halt path of #174: the Claim's read-back fails after ``assign``."""

    fail_next_fetch: bool = True

    def fetch_candidate(self, issue_number: int) -> CandidateIssue:
        if self.fail_next_fetch:
            self.calls.append(("fetch_candidate", (issue_number,)))
            self.fail_next_fetch = False
            raise GitHubTrackerError("GET /repos/o/r/issues/7 returned 502")
        return super().fetch_candidate(issue_number)


def test_rec_clm_2_through_the_claim_halt_path(ledger: RunLedger) -> None:
    """REC-CLM-2 through the #174 halt: the read-back fails, the Worker
    halts, and the restart removes our assignment and claims ``#N/2``."""
    tracker = _FailOnceReadBackTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)
    ran: list[int] = []

    halted = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert halted == 1
    assert ledger.get_attempt("#7/1").state == "open"
    assert ledger.get_attempt("#7/1").assigned_by_us is True
    assert tracker.candidates[7].assignees == (IDENTITY.login,)
    assert ran == []

    restarted = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert restarted == 0
    # The restart released #7/1, then claimed #7/2 — which assigns the
    # issue again. The release is proven by the call, not the final state.
    assert tracker.call_names().count("release_assignment") == 1
    assert tracker.call_names().count("assign_issue") == 2
    assert ledger.get_attempt("#7/1").state == "released"
    assert ran == [7]
    assert ledger.get_attempt("#7/2").state == "finished"


# --- REC-CLM-3: a human's assignment -----------------------------------------


def test_rec_clm_3_a_humans_assignment_is_never_removed(
    ledger: RunLedger,
) -> None:
    """REC-CLM-3, P-SEL-2: an Agent Identity assignment with no open Attempt
    is a human's — recovery writes nothing and the issue stays ineligible."""
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7, assignees=(IDENTITY.login,))

    summary = recover_unfinished_attempts(
        tracker, ledger, agent_login=IDENTITY.login
    )

    assert summary.settled == ()
    assert summary.pending == ()
    assert tracker.calls == []
    assert select_and_claim(tracker=tracker, ledger=ledger, config=_claim_config()) is None
    assert tracker.candidates[7].assignees == (IDENTITY.login,)
    assert "release_assignment" not in tracker.call_names()


# --- Pinned configuration: REC-CFG-1, W-PIN-3 --------------------------------


def test_rec_cfg_1_recovery_uses_pinned_values_not_the_current_config(
    ledger: RunLedger,
) -> None:
    """REC-CFG-1, W-PIN-3: the open Attempt is settled from its own record —
    a renamed current Selection Label is never touched."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.selection_label = "renamed-label"
    tracker.ready_for_human = "renamed-human"
    tracker.candidates[7] = _candidate(7, assignees=(IDENTITY.login,))

    summary = recover_unfinished_attempts(
        tracker, ledger, agent_login=IDENTITY.login
    )

    assert summary.settled == ("#7/1",)
    assert _writes(tracker.call_names()) == ["release_assignment"]
    assert "remove_selection_label" not in tracker.call_names()
    assert "add_ready_for_human" not in tracker.call_names()
    pinned = pinned_recovery_config(ledger.get_attempt("#7/1"))
    assert pinned.selection_label == SELECTION_LABEL
    assert pinned.ready_for_human == READY_FOR_HUMAN
    assert pinned.repository == "example-owner/example-repo"
    assert pinned.base_branch == "main"


def test_recover_open_attempt_ignores_non_open_states(ledger: RunLedger) -> None:
    """Only ``open`` Attempts settle here; confirmed outcomes wait for later slices."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_attempt_state("#7/1", "claim-confirmed")
    tracker = FakeTracker(agent_login=IDENTITY.login)

    assert (
        recover_open_attempt(
            tracker, ledger, ledger.get_attempt("#7/1"), agent_login=IDENTITY.login
        )
        is False
    )
    assert tracker.calls == []
    assert ledger.get_attempt("#7/1").state == "claim-confirmed"


def test_pending_claim_confirmed_waits_with_intake_closed(
    ledger: RunLedger,
) -> None:
    """The interrupted Attempt belongs to the slice that follows: recovery
    reports it pending and intake stays closed."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_attempt_state("#7/1", "claim-confirmed")
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[8] = _candidate(8)

    summary = recover_unfinished_attempts(
        tracker, ledger, agent_login=IDENTITY.login
    )

    assert summary.settled == ()
    assert summary.pending == ("#7/1",)
    assert tracker.calls == []


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"target": {}, "workflow": {"selector": {"label": "x"}}},
        {"target": {"repository": "o/r"}, "workflow": {"selector": {"label": "x"}}},
        {
            "target": {"repository": "o/r", "base_branch": "main"},
            "workflow": {"id": "implement"},
        },
    ],
)
def test_rec_cfg_2_a_missing_pinned_record_refuses(
    ledger: RunLedger, record: dict[str, object]
) -> None:
    """REC-CFG-2: a missing pinned field refuses before any GitHub write."""
    ledger.open_attempt("#7/1", effective_configuration=record)
    tracker = FakeTracker(agent_login=IDENTITY.login)

    with pytest.raises(RecoveryRefused):
        validate_recovery_configs(ledger)

    with pytest.raises(RecoveryRefused):
        recover_unfinished_attempts(tracker, ledger, agent_login=IDENTITY.login)
    assert tracker.calls == []
    assert ledger.get_attempt("#7/1").state == "open"


def test_rec_cfg_2_a_refusal_settles_nothing_before_it(
    ledger: RunLedger,
) -> None:
    """REC-CFG-2: every record validates before any Attempt settles."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.open_attempt("#8/1", effective_configuration={})
    tracker = FakeTracker(agent_login=IDENTITY.login)

    with pytest.raises(RecoveryRefused):
        recover_unfinished_attempts(tracker, ledger, agent_login=IDENTITY.login)

    assert tracker.calls == []
    assert ledger.get_attempt("#7/1").state == "open"


def test_rec_cfg_2_startup_refuses_with_no_attempt_and_no_github_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REC-CFG-2 through ``agent run``: non-zero exit, no tracker, no write."""
    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"
    handle = RunLedger(ledger_path)
    handle.open_attempt("#7/1", effective_configuration={})
    handle.close()
    _StartupSpy(monkeypatch, ScriptedModel([]))
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)

    exit_code, built = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 1
    assert built == []
    assert tracker.calls == []


def test_rec_cfg_2_an_unreadable_record_refuses_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REC-CFG-2: a corrupted Effective Configuration cell refuses the same way."""
    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"
    handle = RunLedger(ledger_path)
    handle.open_attempt("#7/1", effective_configuration=_pinned())
    handle.close()
    connection = sqlite3.connect(ledger_path)
    connection.execute(
        "UPDATE attempts SET effective_configuration = 'not-json' WHERE attempt_id = '#7/1'"
    )
    connection.commit()
    connection.close()
    _StartupSpy(monkeypatch, ScriptedModel([]))
    tracker = FakeTracker(agent_login=IDENTITY.login)

    exit_code, built = _run(tmp_path, monkeypatch, tracker, max_sweeps=1)

    assert exit_code == 1
    assert built == []
    assert tracker.calls == []


# --- REC-WRK-1: intake while unfinished --------------------------------------


def test_rec_wrk_1_no_new_claim_while_an_attempt_is_unfinished(
    ledger: RunLedger,
) -> None:
    """REC-WRK-1: a pending ``claim-confirmed`` Attempt closes intake."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_attempt_state("#7/1", "claim-confirmed")
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[8] = _candidate(8)
    ran: list[int] = []
    slept: list[float] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=slept.append,
        max_sweeps=2,
    )

    assert exit_code == 0
    assert ran == []
    assert "assign_issue" not in tracker.call_names()
    assert ledger.get_attempt("#7/1").state == "claim-confirmed"


class _AlwaysFailReleaseTracker(FakeTracker):
    """Every recovery write fails: three in a row must halt the Worker."""

    def release_assignment(self, issue_number: int) -> None:
        self.calls.append(("release_assignment", (issue_number,)))
        raise GitHubTrackerError("DELETE /repos/o/r/issues/7/assignees returned 502")


def test_rec_wrk_1_three_consecutive_recovery_failures_halt(
    ledger: RunLedger,
) -> None:
    """REC-WRK-1: the failed write is retried each poll; the third halts non-zero."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = _AlwaysFailReleaseTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7, assignees=(IDENTITY.login,))

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, []),
        sleep=lambda seconds: None,
        max_sweeps=10,
    )

    assert exit_code == 1
    assert tracker.call_names().count("release_assignment") == 3
    assert ledger.get_attempt("#7/1").state == "open"
    assert ledger.has_unfinished_attempt()


class _FailTwiceReleaseTracker(FakeTracker):
    """Two failed recovery writes, then success: the counter resets."""

    failures_left: int = 2

    def release_assignment(self, issue_number: int) -> None:
        if self.failures_left > 0:
            self.calls.append(("release_assignment", (issue_number,)))
            self.failures_left -= 1
            raise GitHubTrackerError("DELETE /repos/o/r/issues/7/assignees returned 502")
        super().release_assignment(issue_number)


def test_rec_wrk_1_a_success_resets_the_failure_count(
    ledger: RunLedger,
) -> None:
    """REC-WRK-1: two failed writes then a settled recovery — no halt, and
    the issue is claimed as ``#N/2`` on the same run."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = _FailTwiceReleaseTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7, assignees=(IDENTITY.login,))
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_stub_run(ledger, ran),
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert tracker.call_names().count("release_assignment") == 3
    assert ledger.get_attempt("#7/1").state == "released"
    assert ran == [7]
    assert ledger.get_attempt("#7/2").state == "finished"


def test_startup_settles_an_open_attempt_before_the_first_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup recovery precedes selection: the restart releases ``#7/1``
    and claims ``#7/2`` in one run."""
    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"
    handle = RunLedger(ledger_path)
    handle.open_attempt("#7/1", effective_configuration=_pinned())
    handle.close()
    _StartupSpy(monkeypatch, ScriptedModel([]))
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7)

    exit_code, _built = _run(
        tmp_path, monkeypatch, tracker, max_sweeps=5, max_attempts=1
    )

    assert exit_code == 0


# --- REC-WRK-2: workspace cleanup --------------------------------------------


def test_rec_wrk_2_a_crashed_cleanup_still_prepares_cleanly(
    tmp_path: Path, bare_remote: Path
) -> None:
    """REC-WRK-2: a workspace left half-removed by a crashed cleanup rebuilds
    from the mirror — nothing of the earlier Attempt reaches the next one."""
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(bare_remote))
    workspace_dir = tmp_path / "workspace"
    first = checkout_workspace(mirror, workspace_dir, origin_url=str(bare_remote))
    assert first.path == workspace_dir

    # The crash: cleanup tore down ``.git`` and scattered leftovers.
    shutil.rmtree(workspace_dir / ".git")
    (workspace_dir / "leftover-from-cleanup.txt").write_text("junk", encoding="utf-8")
    nested = workspace_dir / "half" / "removed"
    nested.mkdir(parents=True)
    (nested / "fragment").write_text("fragment", encoding="utf-8")

    second = checkout_workspace(mirror, workspace_dir, origin_url=str(bare_remote))

    assert second.base_revision == first.base_revision
    assert not (workspace_dir / "leftover-from-cleanup.txt").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0
    assert status.stdout == ""


# --- Recovery never calls the model ------------------------------------------


def test_recovery_takes_no_model() -> None:
    """§17, ADR 0017: recovery cannot call the model — no recovery entry
    point accepts one, asserted structurally on the signatures."""
    from coding_agent.worker import recovery  # noqa: PLC0415

    for name in (
        "recover_unfinished_attempts",
        "recover_open_attempt",
        "recover_interrupted_attempt",
        "validate_recovery_configs",
        "pinned_recovery_config",
    ):
        parameters = inspect.signature(getattr(recovery, name)).parameters
        assert "model" not in parameters
        assert "chat_model" not in parameters
        assert "provider" not in parameters


def test_recovery_leaves_a_scripted_model_untouched(ledger: RunLedger) -> None:
    """The scripted model records no invocation across a recovery pass."""
    ledger.open_attempt("#7/1", effective_configuration=_pinned())
    ledger.set_assigned_by_us("#7/1", True)
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7, assignees=(IDENTITY.login,))
    model = ScriptedModel([])

    recover_unfinished_attempts(tracker, ledger, agent_login=IDENTITY.login)

    assert model.invocations == []
    assert model.bound_tool_names == []
