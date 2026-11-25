"""The Claim's merge-base resolver prepares the mirror first (§7.2).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §7.2 (the Base
Revision is the merge base of the pinned Input Head and the pinned Base Tip).
The resolver brings the Worker's mirror up to date — cloning it where the
state volume is fresh, fetching where it already exists — under a short-lived
credential helper before running ``git merge-base``.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from coding_agent.identity.startup import Identity
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.worker import startup as startup_module
from coding_agent.worker import supervisor
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from test_worker_run import _write_run_config, _write_run_matrix
from worker_fakes import make_worker_ports

IDENTITY = Identity(account_id=999, login="coding-agent")

LABEL = "agent:fix"
BODY = "## Acceptance criteria\n- [ ] fix the bug\n"
HEAD_BRANCH = "feature-pr-7"
BASE_BRANCH = "main"
TOKEN = "github_pat_merge_base_testtoken"


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
    _git(src, "commit", "-q", "-m", "the pull request")
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


def _ready_for_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str
) -> Any:
    """A started Worker Ready pointed at ``remote`` (the caller closes it)."""
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fakes = make_worker_ports(
        model=ScriptedModel([]),
        tracker=FakeStepTracker(
            agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
        ),
        repository_url=remote,
    )
    started = startup_module.start(
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
    assert not isinstance(started, supervisor.StartupRefusal)
    return started


def test_claim_on_a_fresh_state_volume_resolves_the_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull-request Claim with no mirror yet claims without halting (§7.2).

    The state volume holds no mirror at all: the resolver clones it during
    the Claim, records the real merge base as the Base Revision, and the
    Worker exits 0 instead of halting with ``claim-interrupted``.
    """
    seeded = _seed_remote(tmp_path)
    tracker = _pr_tracker(seeded["head"], seeded["tip"])
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fakes = make_worker_ports(
        model=ScriptedModel([]), tracker=tracker, repository_url=seeded["remote"]
    )
    config = tmp_path / "workflow.yml"
    config.write_text(_PR_CONFIG, encoding="utf-8")
    mirror = (
        tmp_path / "state" / "mirrors" / "example-owner" / "example-repo.git"
    )
    assert not mirror.exists()
    seen: dict[str, str] = {}

    def _stub_workflow(**kwargs: Any) -> WorkflowResult:
        from coding_agent.runtime.graph.step_executor import ProductionStepExecutor

        executor: ProductionStepExecutor = kwargs["step_executor"]
        attempt_id: str = str(kwargs["attempt_id"])
        ledger: RunLedger = kwargs["ledger"]
        assert executor.agent_deps is not None
        seen["base_revision"] = executor.agent_deps.attempt.base_revision
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
    assert seen["attempt_id"] == "fix#7/1"
    assert seen["base_revision"] == seeded["base"]
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


def test_resolver_fetches_a_stale_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head pushed after the last fetch still resolves (§7.2).

    The mirror is cloned before the pull-request branch exists on the
    remote; the resolver fetches first, so the merge base resolves to the
    real base commit and the mirror then serves the head.
    """
    from conftest import commit_validation_profile, init_origin_repo

    src = tmp_path / "origin-src"
    init_origin_repo(src)
    base = commit_validation_profile(src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    _git(src, "checkout", "-qb", HEAD_BRANCH)
    (src / "pr.txt").write_text("pull request change\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "the pull request")
    head = _git(src, "rev-parse", "HEAD")
    _git(src, "push", "-q", str(bare), f"HEAD:refs/heads/{HEAD_BRANCH}")
    _git(src, "checkout", "-q", BASE_BRANCH)
    (src / "main-after.txt").write_text("base moved on\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base moves after the branch")
    tip = _git(src, "rev-parse", "HEAD")
    _git(src, "push", "-q", str(bare), f"HEAD:refs/heads/{BASE_BRANCH}")
    expected = _git(src, "merge-base", head, tip)
    assert expected == base

    started = _ready_for_remote(tmp_path, monkeypatch, str(bare))
    with started as ready:
        stale = ready.mirror_dir
        stale.parent.mkdir(parents=True, exist_ok=True)
        # A ``--mirror`` clone, the way ``ensure_mirror`` builds one: only
        # then does ``remote update --prune`` fetch new branches.
        subprocess.run(
            ["git", "clone", "-q", "--mirror", str(bare), str(stale)],
            check=True,
        )
        # Rewind the Worker's mirror to the stale state: drop the
        # pull-request branch and move the base branch back, as if both
        # were pushed after the last fetch.
        subprocess.run(
            ["git", "update-ref", "-d", f"refs/heads/{HEAD_BRANCH}"],
            cwd=stale,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{BASE_BRANCH}", base],
            cwd=stale,
            check=True,
        )
        # Dropping the refs is not enough: the objects stay until packed
        # away, so prune them to make the mirror genuinely stale.
        subprocess.run(
            ["git", "reflog", "expire", "--expire=now", "--all"],
            cwd=stale,
            capture_output=True,
        )
        subprocess.run(
            ["git", "gc", "--prune=now", "--quiet"], cwd=stale, check=True
        )
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", head],
                cwd=stale,
                capture_output=True,
            ).returncode
            != 0
        )
        resolve = supervisor._merge_base_resolver(ready)
        assert resolve(head, tip) == base
        fetched = _git(stale, "rev-parse", f"refs/heads/{BASE_BRANCH}")
        assert fetched == tip
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", head],
                cwd=stale,
                capture_output=True,
            ).returncode
            == 0
        )


def test_resolver_with_an_unreachable_remote_raises_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirror that cannot be built fails as ``GitFailure`` (§7.2, §18).

    The error is a redacted ``GitFailure`` — never a raw ``OSError`` such as
    ``FileNotFoundError`` — carrying no token, and the short-lived credential
    helper is removed.
    """
    bad_remote = str(tmp_path / "no-such-remote.git")
    created_helpers: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _recording_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, helper_path = real_mkstemp(*args, **kwargs)
        created_helpers.append(helper_path)
        return descriptor, helper_path

    monkeypatch.setattr(tempfile, "mkstemp", _recording_mkstemp)
    started = _ready_for_remote(tmp_path, monkeypatch, bad_remote)
    with started as ready:
        resolve = supervisor._merge_base_resolver(ready)
        with pytest.raises(GitFailure) as excinfo:
            resolve("a" * 40, "b" * 40)
    assert TOKEN not in str(excinfo.value)
    assert created_helpers != []
    for helper_path in created_helpers:
        assert not Path(helper_path).exists()


def test_resolver_docstring_describes_the_claim_interrupted_halt() -> None:
    """The docstring no longer promises a retry on the next sweep (§4.4)."""
    docstring = supervisor._merge_base_resolver.__doc__
    assert docstring is not None
    assert "claim-interrupted" in docstring
    assert "next sweep" not in docstring
