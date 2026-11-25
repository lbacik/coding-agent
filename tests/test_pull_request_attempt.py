"""A pull-request Attempt works on the pull request's head (§7.2, §9.10).

The workspace is checked out exactly at the pinned Input Head, on a local
branch named after the pull request's head branch: the first agent step sees
the pull request's tree, the change-versus-base reader shows the pull
request's commits and diff, and ``commit-and-push`` commits on top of the
Input Head and advances it with the existing compare-and-swap push. A remote
head that moved meanwhile still yields ``stale-head``; a pinned head the
mirror cannot serve fails before any model call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from coding_agent.identity.startup import Identity
from coding_agent.inputs.claim import ConfirmedClaim
from coding_agent.inputs.pinning import PinnedInput
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.worker import supervisor
from coding_agent.worker.attempt_run import checkout_pull_request, run_attempt
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from test_worker_run import _write_run_config, _write_run_matrix
from worker_fakes import WorkerFakes, make_worker_ports

IDENTITY = Identity(account_id=999, login="coding-agent")

LABEL = "agent:fix"
BODY = "## Acceptance criteria\n- [ ] fix the bug\n"
HEAD_BRANCH = "feature-pr-7"
BASE_BRANCH = "main"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _seed_remote(tmp_path: Path) -> dict[str, str]:
    """A local bare remote with a PR branch and a moved base branch.

    ``main`` carries the validation profile, then the ``feature`` branch
    adds the pull request's commit, then ``main`` moves on with its own
    commit. Returns the base commit (the merge base), the Input Head and
    the moved base tip.
    """
    from conftest import commit_validation_profile, init_origin_repo

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    base = commit_validation_profile(src)
    _git(src, "checkout", "-qb", HEAD_BRANCH)
    (src / "pr.txt").write_text("pull request change\n", encoding="utf-8")
    _git(src, "add", ".")
    head = _git(src, "commit", "-q", "-m", "the pull request")
    head = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", BASE_BRANCH)
    (src / "main-after.txt").write_text("base moved on\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base moves after the branch")
    tip = _git(src, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    return {"base": base, "head": head, "tip": tip, "remote": str(bare)}


def _pr_input(head: str) -> TrackedInput:
    return TrackedInput(
        number=7,
        input_type="pull-request",
        title="Fix the bug",
        body=BODY,
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
        head_sha=head,
        head_branch=HEAD_BRANCH,
        base_branch=BASE_BRANCH,
    )


def _pr_tracker(head: str, base_tip: str) -> FakeStepTracker:
    tracker = FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    tracker.inputs[7] = _pr_input(head)
    tracker.labeled_events[(7, LABEL)] = LabeledEvent(
        label=LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )
    tracker.base_tip = base_tip
    return tracker


_PR_CONFIG = """\
version: 4

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
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000

worker:
  poll-interval-seconds: 5

workflows:
  fix:
    model: sonnet
    input: {type: pull-request, label: agent:fix}
    limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
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


def _fakes(
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    remote: str,
) -> WorkerFakes:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return make_worker_ports(
        model=ScriptedModel([]), tracker=tracker, repository_url=remote
    )


def _seed_mirror(tmp_path: Path, remote: str) -> None:
    """Pre-create the Worker's local mirror, as an earlier run would leave it.

    The Claim's merge-base resolver runs ``git merge-base`` in this mirror
    before the Attempt run's ``ensure_mirror`` ever executes, so a volume
    that never ran anything has no mirror for a pull-request Claim to
    resolve against. Seeding it here mirrors steady-state production, where
    the mirror already exists from earlier runs.
    """
    mirror = (
        tmp_path / "state" / "mirrors" / "example-owner" / "example-repo.git"
    )
    mirror.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", remote, str(mirror)], check=True
    )


def _push_step() -> WorkflowStep:
    return WorkflowStep(
        id="push",
        agent=None,
        operation="commit-and-push",
        max_executions=1,
        routes={
            "pushed": RouteTarget(end="completed"),
            "stale-head": RouteTarget(end="failed"),
            "no-changes": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
        },
    )


def test_pull_request_attempt_checks_out_the_head_and_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Attempt opens the pinned head and pushes on top of it (§7.2)."""
    seeded = _seed_remote(tmp_path)
    _seed_mirror(tmp_path, seeded["remote"])
    tracker = _pr_tracker(seeded["head"], seeded["tip"])
    fakes = _fakes(monkeypatch, tracker, seeded["remote"])
    config = tmp_path / "workflow.yml"
    config.write_text(_PR_CONFIG, encoding="utf-8")
    seen: dict[str, Any] = {}

    def _stub_workflow(**kwargs: Any) -> WorkflowResult:
        from coding_agent.runtime.graph.step_executor import ProductionStepExecutor

        executor: ProductionStepExecutor = kwargs["step_executor"]
        attempt_id: str = str(kwargs["attempt_id"])
        ledger: RunLedger = kwargs["ledger"]
        assert executor.agent_deps is not None
        deps = executor.agent_deps
        workspace = deps.workspace
        # The first agent step sees the pinned Input Head on the head branch,
        # not the base-branch tip.
        assert _git(workspace, "rev-parse", "HEAD") == seeded["head"]
        assert _git(workspace, "symbolic-ref", "--short", "HEAD") == HEAD_BRANCH
        assert deps.attempt.current_revision == seeded["head"]
        assert deps.attempt.base_revision == seeded["base"]
        assert deps.attempt_branch == HEAD_BRANCH
        # The change-versus-base view is exactly the pull request: its file
        # is present, the base branch's later commit is not.
        assert (workspace / "pr.txt").is_file()
        assert not (workspace / "main-after.txt").exists()
        commits = _git(
            workspace, "log", f"{seeded['base']}..HEAD", "--oneline"
        )
        assert "the pull request" in commits
        assert "base moves after the branch" not in commits
        # Commit on top of the Input Head and push to the head branch.
        (workspace / "fix.txt").write_text("fix\n", encoding="utf-8")
        execution = executor.operation_runner.run(
            _push_step(), 1, attempt_id, ledger
        )
        assert execution.result == "pushed"
        revision = str(execution.payload["revision"])
        assert _git(workspace, "rev-parse", f"{revision}^") == seeded["head"]
        seen["revision"] = revision
        seen["attempt_id"] = attempt_id
        ledger.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
            ended=True,
        )

    exit_code = supervisor.run_worker(
        config=config,
        workflow="fix",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_attempts=1,
        workflow_runner=_stub_workflow,
    )

    assert exit_code == 0
    revision = seen["revision"]
    # The remote head advanced to the pushed commit, whose parent is the
    # pinned Input Head.
    assert _git(tmp_path, "ls-remote", seeded["remote"], f"refs/heads/{HEAD_BRANCH}").split()[0] == revision
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        outcome = ledger.get_outcome(seen["attempt_id"])
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == (
            "completed",
            "verified-completion",
        )
    finally:
        ledger.close()


def test_pull_request_push_reports_stale_head_when_the_remote_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head that moved between pin and push still ends ``stale-head``."""
    seeded = _seed_remote(tmp_path)
    _seed_mirror(tmp_path, seeded["remote"])
    tracker = _pr_tracker(seeded["head"], seeded["tip"])
    fakes = _fakes(monkeypatch, tracker, seeded["remote"])
    config = tmp_path / "workflow.yml"
    config.write_text(_PR_CONFIG, encoding="utf-8")
    seen: dict[str, Any] = {}

    def _stub_workflow(**kwargs: Any) -> WorkflowResult:
        from coding_agent.runtime.graph.step_executor import ProductionStepExecutor

        executor: ProductionStepExecutor = kwargs["step_executor"]
        attempt_id: str = str(kwargs["attempt_id"])
        ledger: RunLedger = kwargs["ledger"]
        assert executor.agent_deps is not None
        workspace = executor.agent_deps.workspace
        assert _git(workspace, "rev-parse", "HEAD") == seeded["head"]
        # Someone else advances the head branch after the Claim pinned it.
        rival = tmp_path / "rival"
        _git(tmp_path, "clone", "-q", seeded["remote"], str(rival))
        _git(rival, "checkout", "-q", HEAD_BRANCH)
        (rival / "rival.txt").write_text("rival\n", encoding="utf-8")
        _git(rival, "add", ".")
        _git(rival, "commit", "-q", "-m", "rival")
        _git(rival, "push", "-q", "origin", f"HEAD:refs/heads/{HEAD_BRANCH}")
        (workspace / "fix.txt").write_text("fix\n", encoding="utf-8")
        execution = executor.operation_runner.run(
            _push_step(), 1, attempt_id, ledger
        )
        assert execution.result == "stale-head"
        seen["attempt_id"] = attempt_id
        ledger.record_terminal_outcome(
            attempt_id, outcome="failed", reason="stale-head"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="failed",
            reason="stale-head",
            ending_step="work",
            step_result="stale-head",
            workflow_end="failed",
            ended=True,
        )

    exit_code = supervisor.run_worker(
        config=config,
        workflow="fix",
        state_dir=tmp_path / "state",
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_attempts=1,
        workflow_runner=_stub_workflow,
    )

    assert exit_code == 0
    assert seen["attempt_id"] == "fix#7/1"
    # The rival commit — not our fix — is still the remote head.
    remote_head = _git(
        tmp_path, "ls-remote", seeded["remote"], f"refs/heads/{HEAD_BRANCH}"
    ).split()[0]
    assert remote_head != seeded["head"]


def test_pull_request_with_an_unavailable_head_fails_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned head the mirror cannot serve fails without model work."""
    from conftest import commit_validation_profile, init_origin_repo

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    commit_validation_profile(src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    tracker = FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )
    config = _write_run_config(tmp_path)

    from coding_agent.worker import startup as startup_module

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fakes = make_worker_ports(
        model=ScriptedModel([]), tracker=tracker, repository_url=str(bare)
    )
    started = startup_module.start(
        config=config,
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
    assert not isinstance(started, supervisor.StartupRefusal)

    with started as ready:
        pin = PinnedInput(
            workflow_id="implement",
            input_type="pull-request",
            input_number=7,
            title="Fix the bug",
            body=BODY,
            fingerprint="f" * 64,
            base_revision="b" * 40,
            base_tip="b" * 40,
            input_head="d" * 40,
            head_branch=HEAD_BRANCH,
            base_branch=BASE_BRANCH,
        )
        claimed = ConfirmedClaim(
            attempt_id="implement#7/1",
            workflow_id="implement",
            input_number=7,
            n=1,
            pin=pin,
        )
        calls: list[str] = []

        def _stub_workflow(**kwargs: Any) -> WorkflowResult:
            calls.append(str(kwargs["attempt_id"]))
            raise AssertionError("no model work may run on a missing head")

        with pytest.raises(GitFailure, match="cannot fetch Input Head"):
            run_attempt(ready, claimed, workflow_runner=_stub_workflow)
        # The Workflow runner — the only caller that could reach the model —
        # never ran, so no model call was made.
        assert calls == []


def test_checkout_pull_request_opens_the_head_branch(
    tmp_path: Path,
) -> None:
    """The helper checks out the pinned head on the named branch."""
    seeded = _seed_remote(tmp_path)
    workspace = tmp_path / "work"
    _git(tmp_path, "clone", "-q", seeded["remote"], str(workspace))

    returned = checkout_pull_request(
        workspace_path=workspace,
        canonical_remote=seeded["remote"],
        input_head=seeded["head"],
        head_branch=HEAD_BRANCH,
    )

    assert returned == seeded["head"]
    assert _git(workspace, "rev-parse", "HEAD") == seeded["head"]
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == HEAD_BRANCH
    assert (workspace / "pr.txt").is_file()
    assert not (workspace / "main-after.txt").exists()


def test_checkout_pull_request_refuses_a_missing_head(
    tmp_path: Path,
) -> None:
    """A head the remote cannot serve fails before any checkout."""
    seeded = _seed_remote(tmp_path)
    workspace = tmp_path / "work"
    _git(tmp_path, "clone", "-q", seeded["remote"], str(workspace))
    before = _git(workspace, "rev-parse", "HEAD")

    with pytest.raises(GitFailure, match="cannot fetch Input Head"):
        checkout_pull_request(
            workspace_path=workspace,
            canonical_remote=seeded["remote"],
            input_head="d" * 40,
            head_branch=HEAD_BRANCH,
        )

    assert _git(workspace, "rev-parse", "HEAD") == before
