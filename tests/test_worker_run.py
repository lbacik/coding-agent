"""The Worker: ``agent run``'s startup, poll loop and intake (contract §3).

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup in order;
a failure at any step exits non-zero with no Attempt opened and no GitHub
write; the poll loop sleeps only when a sweep claimed nothing and selects
again immediately after an Attempt; a Credential Failure halts at once,
three consecutive infrastructure-class failures halt only after the third
Attempt is finalized), §4 (``(created_at, number)`` order) and §5 (the
Worker's Claim opens the Attempt the graph then runs).

MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8, C-ERR-2,
P-SEL-5, P-CLM-1.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.github.errors import CredentialFailure, GitHubFailure
from coding_agent.identity.startup import StartupCheckFailed, StartupCheckResult
from coding_agent.identity.startup import run_startup_checks as real_run_startup_checks
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import ProviderCapabilityRefused
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.skillbundle.verify import VerificationReport
from coding_agent.tracker.github import GitHubTracker
from coding_agent.tracker.port import CandidateIssue, TrackedIssue
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt, select_and_claim
from coding_agent.worker import supervisor
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import REVIEW_SKILL_TEXT
from test_mvp_attempt_graph import (  # noqa: F401  (fixtures reused)
    IDENTITY,
    bare_remote,
    verified_flow_model,
    workspace,
)

SELECTION_LABEL = "ready-for-agent"
EFFECTIVE = {"repo": "example-owner/example-repo", "workflow": "implement"}
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


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


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def test_the_graph_runs_the_attempt_the_workers_claim_opened(
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    bare_remote: Path,
) -> None:
    """§5: the Claim opens ``#N/n``; the graph runs that Attempt, not a new one.

    The Claim's ledger row (Fingerprint, Base Revision, Agent Identity id,
    ``assigned_by_us``) is kept, and the Attempt is delivered.
    """
    (skills_dir / "code-review" / "SKILL.md").write_text(
        REVIEW_SKILL_TEXT, encoding="utf-8"
    )
    tracker = FakeTracker(agent_login=IDENTITY.login)
    tracker.candidates[7] = _candidate(7, "2026-10-01T00:00:00Z")
    claimed = select_and_claim(tracker=tracker, ledger=ledger, config=_claim_config())
    assert claimed is not None
    claimed_row = ledger.get_attempt(claimed.attempt_id)

    result = run_attempt(
        issue_number=claimed.issue_number,
        attempt_number=claimed.attempt_number,
        tracker=tracker,
        ledger=ledger,
        model=verified_flow_model(),
        workspace=workspace,
        effective_configuration=EFFECTIVE,
        skills_dir=skills_dir,
        remote_url=str(bare_remote),
        agent_identity=IDENTITY,
    )

    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    row = ledger.get_attempt(claimed.attempt_id)
    assert row.fingerprint == claimed_row.fingerprint
    assert row.base_revision == claimed_row.base_revision
    assert row.agent_identity_id == IDENTITY.account_id
    assert row.assigned_by_us is True
    assert tracker.call_names()[-1] == "release_assignment"


# --- The poll loop (§3), driven with a stub Attempt runner ---------------


def _loop_config(poll: float = 60.0) -> WorkerLoopConfig:
    return WorkerLoopConfig(claim_config=_claim_config(), poll_interval_seconds=poll)


def _recording_runner(
    ledger: RunLedger, reasons: dict[int, tuple[str, str]], ran: list[int]
) -> Callable[[ClaimedAttempt], AttemptResult]:
    """Records each claimed Attempt's scripted outcome, as the graph would."""

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        ran.append(claimed.issue_number)
        outcome, reason = reasons.get(
            claimed.issue_number, ("delivered", "verified-completion")
        )
        ledger.record_terminal_outcome(claimed.attempt_id, outcome=outcome, reason=reason)
        return AttemptResult(attempt_id=claimed.attempt_id, outcome=outcome, reason=reason)

    return run_one


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeTracker:
    tracker = FakeTracker(agent_login=IDENTITY.login)
    for number, created_at in numbers_and_dates:
        tracker.candidates[number] = _candidate(number, created_at)
    return tracker


def _release_on_finish(
    tracker: FakeTracker, runner: Callable[[ClaimedAttempt], AttemptResult]
) -> Callable[[ClaimedAttempt], AttemptResult]:
    """The terminal writes a stub runner skips: label off, assignment off."""

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        result = runner(claimed)
        tracker.remove_selection_label(claimed.issue_number)
        tracker.release_assignment(claimed.issue_number)
        return result

    return run_one


def test_p_wrk_4_idle_the_worker_sleeps_for_the_poll_interval(ledger: RunLedger) -> None:
    """P-WRK-4: a sweep that claims nothing sleeps exactly the poll interval."""
    tracker = FakeTracker(agent_login=IDENTITY.login)
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
    assert sleeps == [42.0, 42.0]
    assert "assign_issue" not in tracker.call_names()


def test_p_wrk_4_after_an_attempt_the_worker_selects_again_immediately(
    ledger: RunLedger,
) -> None:
    """P-WRK-4 and P-SEL-5: two Attempts back to back, oldest first, no sleep."""
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
    assert ledger.get_attempt("#32/1").state == "finished"
    assert ledger.get_attempt("#31/1").state == "finished"


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
    assert ledger.get_attempt("#31/1").state == "finished"
    assert tracker.calls.count(("assign_issue", (32,))) == 0


def test_p_wrk_1_a_credential_failure_in_the_graph_writes_no_comment(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """P-WRK-1 through the real graph: the outcome is recorded, no comment
    is written, and the Worker halts non-zero."""

    class RevokedTracker(FakeTracker):
        def fetch_issue(self, issue_number: int) -> TrackedIssue:
            raise CredentialFailure("GET", f"/repos/o/r/issues/{issue_number}")

    tracker = RevokedTracker(agent_login=IDENTITY.login)
    tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        return run_attempt(
            issue_number=claimed.issue_number,
            attempt_number=claimed.attempt_number,
            tracker=tracker,
            ledger=ledger,
            model=ScriptedModel([]),
            workspace=workspace,
            effective_configuration=EFFECTIVE,
            skills_dir=skills_dir,
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=5,
    )

    assert exit_code == 1
    outcome = ledger.get_outcome("#31/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("failed", "credential-failure")
    assert "post_terminal_comment" not in tracker.call_names()


def test_p_wrk_1_a_revoked_credential_during_selection_halts(ledger: RunLedger) -> None:
    """A credential revoked while the Worker idles halts it non-zero (§3)."""

    class RevokedTracker(FakeTracker):
        def list_candidates(self, selection_label: str) -> list[CandidateIssue]:
            raise CredentialFailure("GET", "/repos/o/r/issues")

    exit_code = run_worker_loop(
        tracker=RevokedTracker(agent_login=IDENTITY.login),
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(ledger, {}, []),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1


def _github_tracker() -> GitHubTracker:
    return GitHubTracker(
        owner="o",
        repo="r",
        client=GitHubClient("github_pat_testtoken", base_url="https://api.github.test"),
        agent_login=IDENTITY.login,
        sleep=lambda seconds: None,
    )


def test_p_wrk_1_a_token_revoked_while_idle_halts_through_the_github_adapter(
    ledger: RunLedger, requests_mock: Any
) -> None:
    """P-WRK-1 over the real adapter, as the first live run (#154) saw it:
    the sweep's 401, confirmed by the probe, halts the Worker non-zero, and
    nothing is written."""
    requests_mock.get(
        "https://api.github.test/repos/o/r/issues",
        status_code=401,
        json={"message": "Bad credentials"},
    )
    requests_mock.get(
        "https://api.github.test/user",
        status_code=401,
        json={"message": "Bad credentials"},
    )
    ran: list[int] = []

    exit_code = run_worker_loop(
        tracker=_github_tracker(),
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(ledger, {}, ran),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    assert ran == []
    assert {request.method for request in requests_mock.request_history} == {"GET"}
    assert requests_mock.call_count == 2


def test_c_err_2_an_unconfirmed_401_while_idle_retries_at_the_next_poll(
    ledger: RunLedger, requests_mock: Any
) -> None:
    """C-ERR-2 over the real adapter: the sweep 401s but the ``GET /user``
    probe succeeds, so the token still authenticates. The failed sweep is
    infrastructure-class ``github``: the Worker sleeps and selects again
    instead of halting."""
    requests_mock.get(
        "https://api.github.test/repos/o/r/issues",
        [
            {"status_code": 401, "json": {"message": "Bad credentials"}},
            {"status_code": 200, "json": []},
        ],
    )
    requests_mock.get(
        "https://api.github.test/user",
        status_code=200,
        json={"id": 1, "login": "octocat"},
    )
    ran: list[int] = []
    slept: list[float] = []

    exit_code = run_worker_loop(
        tracker=_github_tracker(),
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(ledger, {}, ran),
        sleep=slept.append,
        max_sweeps=2,
    )

    assert exit_code == 0
    assert ran == []
    assert slept == [60.0]


def test_c_err_2_an_unconfirmed_401_in_the_graph_writes_no_comment_and_continues(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    """C-ERR-2 through the real graph: an unconfirmed 401 (here standing in
    for the identity-probe verdict) records ``failed`` /
    ``infrastructure-failure`` and the Worker continues with exit zero,
    never halting as it would on a Credential Failure."""
    class Unconfirmed401Tracker(FakeTracker):
        def fetch_issue(self, issue_number: int) -> TrackedIssue:
            raise GitHubFailure(
                "GET",
                f"/repos/o/r/issues/{issue_number}",
                401,
                reason="infrastructure-failure",
                failure_class="github",
            )

    tracker = Unconfirmed401Tracker(agent_login=IDENTITY.login)
    tracker.candidates[31] = _candidate(31, "2026-10-01T00:00:00Z")

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        return run_attempt(
            issue_number=claimed.issue_number,
            attempt_number=claimed.attempt_number,
            tracker=tracker,
            ledger=ledger,
            model=ScriptedModel([]),
            workspace=workspace,
            effective_configuration=EFFECTIVE,
            skills_dir=skills_dir,
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=1,
    )

    assert exit_code == 0
    outcome = ledger.get_outcome("#31/1")
    assert outcome is not None
    assert outcome.outcome == "failed"
    assert outcome.reason.startswith("infrastructure-failure")


def test_a_github_server_error_during_selection_is_retried_next_sweep(
    ledger: RunLedger, requests_mock: Any
) -> None:
    """§3: a failed sweep is not a crash; the Worker sleeps and selects again."""
    requests_mock.get(
        "https://api.github.test/repos/o/r/issues",
        [{"status_code": 502, "json": {}}, {"status_code": 200, "json": []}],
    )
    slept: list[float] = []

    exit_code = run_worker_loop(
        tracker=_github_tracker(),
        ledger=ledger,
        loop_config=_loop_config(poll=60.0),
        run_one_attempt=_recording_runner(ledger, {}, []),
        sleep=slept.append,
        max_sweeps=2,
    )

    assert exit_code == 0
    assert requests_mock.call_count == 2
    assert slept[0] == 60.0


def test_a_claim_interrupted_after_the_attempt_opened_halts_rather_than_idling(
    ledger: RunLedger, requests_mock: Any
) -> None:
    """§3, §5: a failure mid-Claim leaves an open Attempt, which closes intake.
    Retrying the sweep would idle forever with nothing said, so the Worker
    halts non-zero and leaves the Attempt to startup recovery (§17)."""
    base = "https://api.github.test/repos/o/r"
    requests_mock.get(
        f"{base}/issues",
        json=[
            {
                "number": 7,
                "title": "Add a greeting",
                "body": BODY,
                "state": "open",
                "assignees": [],
                "labels": [{"name": SELECTION_LABEL}],
                "created_at": "2026-10-01T00:00:00Z",
            }
        ],
    )
    requests_mock.get(f"{base}/issues/7", json={"number": 7, "body": BODY})
    requests_mock.get(f"{base}/issues/7/dependencies/blocked_by", json=[])
    requests_mock.get(f"{base}/pulls", json=[])
    requests_mock.get(f"{base}/branches/main", json={"commit": {"sha": "a" * 40}})
    requests_mock.post(f"{base}/issues/7/assignees", status_code=502, json={})

    exit_code = run_worker_loop(
        tracker=_github_tracker(),
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=_recording_runner(ledger, {}, []),
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    assert ledger.has_unfinished_attempt()
    assert [r.method for r in requests_mock.request_history].count("POST") == 1


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
    assert ledger.get_attempt("#33/1").state == "finished"
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


# --- ``run_worker``: startup in order, then the real graph (§3) ----------

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

    def _provider(self, pin: object, model: object, table: object) -> None:
        self.order.append("provider")
        if self.provider_error is not None:
            raise self.provider_error


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeTracker,
    *,
    config: Path | None = None,
    remote: Path | None = None,
    skills_home: Path | None = None,
    max_attempts: int | None = None,
    max_sweeps: int | None = None,
) -> tuple[int, list[dict[str, object]]]:
    built: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeTracker:
        built.append(kwargs)
        return tracker

    if remote is not None:
        monkeypatch.setattr(supervisor, "remote_url", lambda owner, repo, token: str(remote))
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


def test_agent_run_takes_two_issues_in_order_to_recorded_terminal_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path,
    bare_remote: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A scripted end-to-end run over the fake tracker (issue #153).

    Two eligible issues are claimed in ``(created_at, number)`` order, one
    at a time, each through the real Attempt graph to ``delivered``.
    """
    (skills_dir / "code-review" / "SKILL.md").write_text(
        REVIEW_SKILL_TEXT, encoding="utf-8"
    )
    model = ScriptedModel(
        verified_flow_model()._outcomes + verified_flow_model()._outcomes
    )
    spy = _StartupSpy(monkeypatch, model)
    tracker = _tracker_with((41, "2026-10-02T00:00:00Z"), (40, "2026-10-01T00:00:00Z"))

    exit_code, built = _run(
        tmp_path, monkeypatch, tracker, remote=bare_remote, max_attempts=2
    )

    assert exit_code == 0
    assert spy.order == ["skill-bundle", "github", "provider"]
    assert [call for call in tracker.calls if call[0] == "assign_issue"] == [
        ("assign_issue", (40,)),
        ("assign_issue", (41,)),
    ]
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        for attempt_id in ("#40/1", "#41/1"):
            outcome = ledger.get_outcome(attempt_id)
            assert outcome is not None
            assert (outcome.outcome, outcome.reason) == (
                "delivered",
                "verified-completion",
            )
            assert ledger.get_attempt(attempt_id).state == "finished"
    finally:
        ledger.close()
    assert len(tracker.pull_requests) == 2
    out = capsys.readouterr().out
    assert "run: delivered (verified-completion); attempt=#40/1" in out
    assert "run: delivered (verified-completion); attempt=#41/1" in out
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
    assert not (tmp_path / "state" / "run-ledger.sqlite3").exists()
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
    assert not (tmp_path / "state" / "run-ledger.sqlite3").exists()


def test_an_invalid_configuration_refuses_before_any_other_startup_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3 step 1 (W-CFG-13): the YAML is validated before anything else."""
    spy = _StartupSpy(monkeypatch, ScriptedModel([]))
    config = _write_run_config(tmp_path, _RUN_CONFIG.replace("skill: implement", "skill: tdd"))

    exit_code, built = _run(
        tmp_path, monkeypatch, FakeTracker(), config=config, max_sweeps=1
    )

    assert exit_code == 1
    assert spy.order == []
    assert built == []


def test_a_setup_failure_outside_the_graph_is_recorded_finalized_and_counted(
    ledger: RunLedger,
) -> None:
    """§3: setup is an infrastructure class. An ``OSError`` preparing the
    Attempt (its evidence directory, its transcript) is recorded, finalized
    and counted toward the halt-after-three; intake never stays closed."""
    tracker = _tracker_with(
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-02T00:00:00Z"),
        (33, "2026-10-03T00:00:00Z"),
    )

    def run_one(claimed: ClaimedAttempt) -> AttemptResult:
        tracker.remove_selection_label(claimed.issue_number)
        raise OSError(28, "No space left on device")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=_loop_config(),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
    )

    assert exit_code == 1
    outcome = ledger.get_outcome("#31/1")
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
