"""Local Runtime Operations: ``check-readiness``, ``run-validation`` and
``commit-and-push`` (plus revision binding of evidence).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.1 (the
catalog and its preconditions: ``requires``, the Gate before a write),
§10.2 (``check-readiness``), §10.3 (``run-validation``), §10.4
(``commit-and-push``: fast-forward only, compare-and-swap to a
pull-request head with ``stale-head``, the ``.github/workflows/`` write
scope) and §13.4 (evidence carries the tree identity: the commit when
clean, otherwise the dirty-tree digest).

Slice S7a of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: OP-1 … OP-6, OP-13, STEP-15.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.schema import Check, Evidence, ProjectProfile, Service, Toolchain
from coding_agent.profile.services import ServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix, load_toolchain_matrix
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.runtime.step_operations import (
    CommitPushGitOps,
    CommitPushOutcome,
    GateChecker,
    GateFailed,
    LocalOperationRunner,
    OperationExecution,
    OperationRunner,
    ValidationRegistry,
    commit_message_for_attempt,
    run_check_readiness,
    run_commit_and_push,
    run_run_validation,
    tree_identity,
)
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.workflow.v2_schema import V2Ceiling, V2Input, V2RouteTarget, V2Step, V2Workflow

GOOD_PROFILE = """schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "bootstrap-cmd"
  test_all: "run-test-all"
  test_targeted: "run-test-targeted {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services:
  - name: db
    url_env: DB_URL
"""


def _matrix() -> SupportedToolchainMatrix:
    return load_toolchain_matrix(
        {
            "schema": 1,
            "toolchains": {
                "python": {
                    "version": "3.13.9",
                    "package_manager": {"name": "uv", "version": "0.6.1"},
                },
            },
        }
    )


class FakeProber:
    """A ``ServiceProber`` fake with a per-host answer."""

    def __init__(self, reachable: bool = True) -> None:
        self._reachable = reachable
        self.probes: list[tuple[str, int]] = []

    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        self.probes.append((host, port))
        return self._reachable


@dataclass
class QueuedRun:
    """One scripted command run: the evidence file to plant and the exit."""

    filename: str
    exit_code: int
    junit: str | None = None
    stdout: str = ""
    timed_out: bool = False


class QueueRunner(CommandRunner):
    """A ``CommandRunner`` fake: pops one queued run per call (OP-2/OP-3).

    Planting the JUnit file mirrors what a real command would write to its
    declared evidence path; the harness reads it back itself.
    """

    def __init__(self, evidence_dir: Path, runs: list[QueuedRun]) -> None:
        self._evidence_dir = evidence_dir
        self._runs = list(runs)
        self.calls: list[str] = []

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        assert self._runs, f"no queued run left for {command!r}"
        item = self._runs.pop(0)
        self.calls.append(command)
        if item.junit is not None:
            target = self._evidence_dir / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.junit, encoding="utf-8")
        return ExecutedCommand(
            command=command,
            exit_code=item.exit_code,
            stdout=item.stdout,
            stderr="",
            timed_out=item.timed_out,
        )


class RaisingRunner(CommandRunner):
    """A ``CommandRunner`` fake whose command raises (OP-3 transport)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        self.calls.append(command)
        raise OSError("transport down")


def _junit(cases: dict[str, bool]) -> str:
    parts = [f'<testsuite name="suite" tests="{len(cases)}">']
    for name, failed in cases.items():
        if failed:
            parts.append(f'<testcase classname="pkg" name="{name}"><failure message="boom"/></testcase>')
        else:
            parts.append(f'<testcase classname="pkg" name="{name}"/>')
    parts.append("</testsuite>")
    return "".join(parts)


def _profile(
    *,
    bootstrap: str = "bootstrap-cmd",
    test_all: str = "run-test-all",
    checks: tuple[Check, ...] = (),
) -> ProjectProfile:
    return ProjectProfile(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap=bootstrap,
        test_all=test_all,
        test_targeted="run-test-targeted {path}",
        evidence=Evidence(
            format="junit-xml",
            test_all="{evidence_dir}/test_all.xml",
            test_targeted="{evidence_dir}/test_targeted.xml",
        ),
        checks=checks,
        services=(Service(name="db", url_env="DB_URL"),),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "agent@example.com", cwd=path)
    _git("config", "user.name", "agent", cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "initial", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


class FakeGitOps(CommitPushGitOps):
    """A ``CommitPushGitOps`` fake over a real repo with a fake remote."""

    def __init__(self, remote: dict[str, str] | None = None) -> None:
        self.remote: dict[str, str] = dict(remote or {})
        self.pushes: list[tuple[str, str]] = []
        self.fail_push: Exception | None = None
        self.race_to: str | None = None

    def is_clean(self, workspace: Path) -> bool:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0
        return not completed.stdout.strip()

    def touches_workflows(self, workspace: Path) -> bool:
        from coding_agent.runtime.git import tree_touches_workflows_path

        return tree_touches_workflows_path(workspace)

    def head_sha(self, workspace: Path) -> str:
        return _git("rev-parse", "HEAD", cwd=workspace)

    def stage_all(self, workspace: Path) -> None:
        _git("add", "-A", cwd=workspace)

    def commit(
        self, workspace: Path, message: str, author_name: str, author_email: str
    ) -> str:
        _git(
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            message,
            cwd=workspace,
        )
        return _git("rev-parse", "HEAD", cwd=workspace)

    def commit_touches_workflows(self, workspace: Path, revision: str) -> bool:
        from coding_agent.runtime.git import commit_touches_workflows_path

        return commit_touches_workflows_path(workspace, revision)

    def remote_sha(self, branch: str) -> str | None:
        return self.remote.get(branch)

    def push_issue_branch(self, workspace: Path, branch: str) -> None:
        self.pushes.append(("issue", branch))
        if self.fail_push is not None:
            raise self.fail_push
        if self.race_to is not None:
            self.remote[branch] = self.race_to
            raise GitFailure("'git push' failed: non-fast-forward")
        self.remote[branch] = self.head_sha(workspace)

    def push_ref(self, workspace: Path, local_sha: str, branch: str) -> None:
        self.pushes.append(("ref", branch))
        if self.fail_push is not None:
            raise self.fail_push
        if self.race_to is not None:
            self.remote[branch] = self.race_to
            raise GitFailure("'git push' failed: non-fast-forward")
        self.remote[branch] = local_sha


# --- STEP-15: revision binding of evidence -------------------------------------


def test_tree_identity_clean_tree_is_the_commit(tmp_path: Path) -> None:
    """STEP-15: evidence on a clean tree carries the commit (S7a, §13.4)."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    assert tree_identity(repo) == head


def test_tree_identity_dirty_tree_is_a_stable_content_digest(tmp_path: Path) -> None:
    """STEP-15: evidence on a dirty tree carries the dirty-tree digest."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
    dirty = tree_identity(repo)
    assert dirty != head
    assert dirty.startswith("dirty:")
    assert tree_identity(repo) == dirty
    (repo / "notes.txt").write_text("different\n", encoding="utf-8")
    assert tree_identity(repo) != dirty


def test_tree_identity_ignores_gitignored_paths(tmp_path: Path) -> None:
    """STEP-15: the digest covers tracked plus non-ignored untracked files."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "ignore logs", cwd=repo)
    committed = _git("rev-parse", "HEAD", cwd=repo)
    assert committed != head
    assert tree_identity(repo) == committed
    (repo / "debug.log").write_text("noise\n", encoding="utf-8")
    assert tree_identity(repo) == committed


# --- OP-1 … OP-3: check-readiness ---------------------------------------------


def test_check_readiness_ready_names_toolchain_and_services(tmp_path: Path) -> None:
    """OP-1: a valid profile and toolchain yields `ready`; bootstrap ran."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [QueuedRun("bootstrap", 0, None, stdout="ok")])
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=GOOD_PROFILE,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=True),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "ready"
    assert outcome.profile is not None
    assert outcome.facts["toolchain"] == {
        "runtime": "python",
        "version": "3.13",
        "package_manager": "uv",
    }
    assert outcome.facts["services"] == [{"name": "db", "url_env": "DB_URL"}]
    assert runner.calls == ["bootstrap-cmd"]


def test_check_readiness_no_profile_is_not_ready_without_a_command(tmp_path: Path) -> None:
    """OP-2: no profile is `not-ready` with a naming fact; no model call."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [])
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=None,
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "not-ready"
    assert "project-profile.yml" in str(outcome.facts.get("missing"))
    assert runner.calls == []


def test_check_readiness_invalid_profile_is_not_ready(tmp_path: Path) -> None:
    """OP-2: an invalid profile is `not-ready`; nothing is executed."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [])
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text="schema: [unclosed\n",
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "not-ready"
    assert "invalid" in outcome.facts
    assert runner.calls == []


def test_check_readiness_toolchain_outside_matrix_is_not_ready(tmp_path: Path) -> None:
    """OP-2: a toolchain outside the matrix is `not-ready`, never `error`."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [])
    profile = GOOD_PROFILE.replace('python: "3.13"', 'python: "9.99"')
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=profile,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=True),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "not-ready"
    assert "unsupported" in outcome.facts
    assert runner.calls == []


def test_check_readiness_unknown_schema_is_not_ready(tmp_path: Path) -> None:
    """OP-2: an unknown `schema` version is `not-ready`."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [])
    profile = GOOD_PROFILE.replace("schema: 2", "schema: 99")
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=profile,
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "not-ready"
    assert "unsupported" in outcome.facts
    assert runner.calls == []


def test_check_readiness_unreachable_service_is_not_ready(tmp_path: Path) -> None:
    """OP-2: an unreachable declared service is `not-ready`."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [])
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=GOOD_PROFILE,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=False),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "not-ready"
    assert "unsupported" in outcome.facts
    assert runner.calls == []


def test_check_readiness_bootstrap_failure_is_error(tmp_path: Path) -> None:
    """OP-3: a failing `bootstrap` yields `error` (never `not-ready`)."""
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [QueuedRun("bootstrap", 1, None, stdout="boom")])
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=GOOD_PROFILE,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=True),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "error"
    assert "bootstrap" in str(outcome.detail)


def test_check_readiness_runner_failure_is_error(tmp_path: Path) -> None:
    """OP-3: a transport failure running `bootstrap` yields `error`."""
    evidence_dir = tmp_path / "evidence"
    runner = RaisingRunner()
    outcome = run_check_readiness(
        workspace=tmp_path / "workspace",
        profile_text=GOOD_PROFILE,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=True),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "error"


# --- OP-4: run-validation ------------------------------------------------------


def test_run_validation_passed_bound_to_clean_tree(tmp_path: Path) -> None:
    """OP-4: a green tree yields `passed`, evidence bound to the commit."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(
        evidence_dir, [QueuedRun("test_all.xml", 0, _junit({"pkg::test_ok": False}))]
    )
    outcome = run_run_validation(
        workspace=repo,
        profile=_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "passed"
    assert outcome.tree_id == head
    assert outcome.facts["tree_id"] == head
    assert outcome.evidence[0].executed == 1
    assert outcome.evidence[0].failure_ids == frozenset()


def test_run_validation_failed_bound_to_dirty_tree(tmp_path: Path) -> None:
    """OP-4: a red tree yields `failed`, evidence bound to the digest."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "README.md").write_text("broken\n", encoding="utf-8")
    tree = tree_identity(repo)
    assert tree != head
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(
        evidence_dir,
        [QueuedRun("test_all.xml", 1, _junit({"pkg::test_ok": True, "pkg::test_new": False}))],
    )
    outcome = run_run_validation(
        workspace=repo,
        profile=_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "failed"
    assert outcome.tree_id == tree
    assert outcome.evidence[0].exit_code == 1
    assert outcome.evidence[0].executed == 2
    assert outcome.evidence[0].failure_ids == frozenset({"pkg::test_ok"})


def test_run_validation_missing_evidence_is_failed(tmp_path: Path) -> None:
    """OP-4: declared evidence that names no test is `failed`, not `passed`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(evidence_dir, [QueuedRun("test_all.xml", 0, None)])
    outcome = run_run_validation(
        workspace=repo,
        profile=_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert outcome.result == "failed"


# --- requires (§10.1) ----------------------------------------------------------


def test_requires_base_tree_evidence_never_satisfies() -> None:
    """OP-10 (§10.1): `requires` holds only for the revision being published."""
    registry = ValidationRegistry()
    registry.record("base-tree", True)
    assert registry.holds((("run-validation", "passed"),), "base-tree") is True
    assert registry.holds((("run-validation", "passed"),), "current-tree") is False
    assert registry.holds((), "anything") is True


def test_commit_and_push_requirement_unmet_before_any_write(tmp_path: Path) -> None:
    """`requirement-unmet`: validation passed only on an earlier tree."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("new\n", encoding="utf-8")
    git = FakeGitOps()
    registry = ValidationRegistry()
    registry.record("some-earlier-tree", True)
    outcome = run_commit_and_push(
        workspace=repo,
        attempt_id="implement#7/1",
        input_type="issue",
        branch="agent/implement/7/1-slug",
        expected_head=None,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        with_requires=(("run-validation", "passed"),),
        validation_registry=registry,
        gate=None,
        git=git,
    )
    assert outcome.result == "requirement-unmet"
    assert git.pushes == []
    assert outcome.revision is None


# --- OP-5: commit-and-push on an issue branch ----------------------------------


def _issue_push(
    repo: Path,
    git: FakeGitOps,
    ledger: RunLedger | None = None,
    gate: GateChecker | None = None,
    with_requires: tuple[tuple[str, str], ...] = (),
    validation_registry: ValidationRegistry | None = None,
    branch: str = "agent/implement/7/1-greeting",
) -> CommitPushOutcome:
    return run_commit_and_push(
        workspace=repo,
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        with_requires=with_requires,
        validation_registry=validation_registry,
        gate=gate,
        git=git,
        ledger=ledger,
        step_id="push",
        seq=1,
    )


def _pr_push(
    repo: Path,
    git: FakeGitOps,
    expected_head: str | None,
    ledger: RunLedger | None = None,
    branch: str = "pr-head",
) -> CommitPushOutcome:
    return run_commit_and_push(
        workspace=repo,
        attempt_id="implement#7/1",
        input_type="pull-request",
        branch=branch,
        expected_head=expected_head,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        with_requires=(),
        validation_registry=ValidationRegistry(),
        gate=None,
        git=git,
        ledger=ledger,
        step_id="push",
        seq=1,
    )


def test_commit_and_push_issue_with_changes(tmp_path: Path, ledger: RunLedger) -> None:
    """OP-5: changes commit under the Attempt trailer and push fast-forward."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    git = FakeGitOps()
    outcome = _issue_push(repo, git, ledger)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert git.remote["agent/implement/7/1-greeting"] == outcome.revision
    assert git.pushes == [("issue", "agent/implement/7/1-greeting")]
    message = _git("log", "-1", "--pretty=%B", cwd=repo)
    assert "Attempt: implement#7/1" in message
    assert commit_message_for_attempt("implement#7/1").startswith("implement#7/1")
    record = ledger.get_step_effect("implement#7/1", "push", 1, 0)
    assert record is not None
    assert record.status == "receipt"
    assert record.remote_id == outcome.revision


def test_commit_and_push_issue_no_changes(tmp_path: Path, ledger: RunLedger) -> None:
    """OP-5: a clean, up-to-date branch yields `no-changes`; no push."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    git = FakeGitOps(remote={"agent/implement/7/1-greeting": head})
    outcome = _issue_push(repo, git, ledger)
    assert outcome.result == "no-changes"
    assert outcome.revision == head
    assert git.pushes == []
    assert ledger.get_step_effect("implement#7/1", "push", 1, 0) is None


def test_commit_and_push_issue_remote_moved_is_error(tmp_path: Path) -> None:
    """OP-5/§10.4: a moved remote branch is never overwritten (`error`)."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    git = FakeGitOps(remote={"agent/implement/7/1-greeting": "f" * 40})
    outcome = _issue_push(repo, git)
    assert outcome.result == "error"
    assert git.pushes == []
    assert _git("rev-parse", "HEAD", cwd=repo) == head


def test_commit_and_push_pushes_fast_forward_only(tmp_path: Path) -> None:
    """OP-5: the live push path is fast-forward only (a divergent head fails)."""
    from coding_agent.runtime.step_operations import LiveCommitPushGitOps

    repo = tmp_path / "repo"
    _init_repo(repo)
    bare = tmp_path / "remote.git"
    completed = subprocess.run(
        ["git", "init", "--bare", str(bare)], capture_output=True, text=True
    )
    assert completed.returncode == 0
    branch = "agent/implement/7/1-greeting"
    git = LiveCommitPushGitOps(str(bare))
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    first = run_commit_and_push(
        workspace=repo,
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        git=git,
    )
    assert first.result == "pushed"
    # Diverge the remote behind our back: a foreign commit on the branch.
    clone = tmp_path / "clone"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.email", "other@example.com", cwd=clone)
    _git("config", "user.name", "other", cwd=clone)
    _git("checkout", "-qb", branch, cwd=clone)
    (clone / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-qm", "foreign", cwd=clone)
    _git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    second = run_commit_and_push(
        workspace=repo,
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        git=git,
    )
    assert second.result == "error"


# --- OP-6: commit-and-push to a pull-request head ------------------------------


def test_commit_and_push_pr_compare_and_swap_advances_head(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-6: remote == Input Head, so the CAS push lands and advances it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    git = FakeGitOps(remote={"pr-head": "a" * 40})
    outcome = _pr_push(repo, git, "a" * 40, ledger)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert git.remote["pr-head"] == outcome.revision
    assert outcome.facts["input_head"] == outcome.revision
    record = ledger.get_step_effect("implement#7/1", "push", 1, 0)
    assert record is not None
    assert record.status == "receipt"


def test_commit_and_push_pr_moved_head_is_stale_head(tmp_path: Path) -> None:
    """OP-6: a moved remote head yields `stale-head`; nothing is forced."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    git = FakeGitOps(remote={"pr-head": "b" * 40})
    outcome = _pr_push(repo, git, "a" * 40)
    assert outcome.result == "stale-head"
    assert git.pushes == []
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    assert (repo / "fix.txt").read_text(encoding="utf-8") == "fix\n"


def test_commit_and_push_pr_race_is_stale_head(tmp_path: Path, ledger: RunLedger) -> None:
    """INP-3/§10.4: a foreign push between Gate and write yields `stale-head`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    git = FakeGitOps(remote={"pr-head": "a" * 40})
    git.race_to = "c" * 40
    outcome = _pr_push(repo, git, "a" * 40, ledger)
    assert outcome.result == "stale-head"
    record = ledger.get_step_effect("implement#7/1", "push", 1, 0)
    assert record is not None
    assert record.status == "not-performed"


# --- OP-13: refusals ------------------------------------------------------------


def test_commit_and_push_workflows_path_is_error(tmp_path: Path) -> None:
    """OP-13: a push touching `.github/workflows/` is refused (`error`)."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    git = FakeGitOps()
    outcome = _issue_push(repo, git)
    assert outcome.result == "error"
    assert ".github/workflows/" in outcome.detail
    assert git.pushes == []
    assert _git("rev-parse", "HEAD", cwd=repo) == head


def test_commit_and_push_permission_refusal_is_error(tmp_path: Path) -> None:
    """OP-13: a single refused permission ends `error`, routed by the Workflow."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    git = FakeGitOps()
    git.fail_push = GitFailure("'git push' failed: permission denied")
    outcome = _issue_push(repo, git)
    assert outcome.result == "error"


def test_commit_and_push_gate_drift_raises(tmp_path: Path) -> None:
    """§10.1: Drift at the Gate before the write is never routed (`GateFailed`)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    git = FakeGitOps()

    def drift() -> GateResult:
        return GateResult(status="drift", reason="content-changed")

    with pytest.raises(GateFailed) as excinfo:
        _issue_push(repo, git, gate=drift)
    assert excinfo.value.status == "drift"
    assert git.pushes == []


def test_commit_and_push_gate_withdrawal_raises(tmp_path: Path) -> None:
    """§10.1: withdrawal at the Gate before the write is never routed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    git = FakeGitOps()

    def withdrawn() -> GateResult:
        current = TrackedInput(
            number=7,
            input_type="issue",
            title="t",
            body="b",
            is_open=True,
            labels=(),
            assignees=(),
            created_at="2026-10-01T00:00:00Z",
        )
        assert current.labels == ()
        return GateResult(status="withdrawn", reason="selection-label-removed")

    with pytest.raises(GateFailed) as excinfo:
        _issue_push(repo, git, gate=withdrawn)
    assert excinfo.value.status == "withdrawn"
    assert git.pushes == []


# --- Graph integration ---------------------------------------------------------


def _attempt_workflow() -> V2Workflow:
    preflight = V2Step(
        id="preflight",
        operation="check-readiness",
        routes={
            "ready": V2RouteTarget(step="push"),
            "not-ready": V2RouteTarget(end="blocked"),
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="failed"),
        },
    )
    push = V2Step(
        id="push",
        operation="commit-and-push",
        routes={
            "pushed": V2RouteTarget(end="completed"),
            "no-changes": V2RouteTarget(end="completed"),
            "stale-head": V2RouteTarget(end="failed"),
            "requirement-unmet": V2RouteTarget(end="failed"),
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="failed"),
        },
    )
    return V2Workflow(
        id="implement",
        model="sonnet",
        input=V2Input(type="issue", label="agent:implement"),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=60),
        start="preflight",
        steps={"preflight": preflight, "push": push},
    )


@dataclass
class ScriptedRunner(OperationRunner):
    """An :class:`OperationRunner` fake answering from a script."""

    results: dict[str, str]
    gate_status: str | None = None
    calls: list[str] = field(default_factory=list)

    def run(
        self,
        step: V2Step,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> OperationExecution:
        self.calls.append(step.id)
        if self.gate_status is not None and step.operation == "commit-and-push":
            raise GateFailed(self.gate_status, "content-changed")
        result = self.results[step.id]
        return OperationExecution(
            result=result,
            payload={"result": result},
            facts={"scripted": True},
        )


def test_graph_runs_local_operations_for_real(ledger: RunLedger) -> None:
    """S7a wiring: the graph consults the runner for local operations."""
    workflow = _attempt_workflow()
    runner = ScriptedRunner(results={"preflight": "ready", "push": "pushed"})
    outcome = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        operation_runner=runner,
    )
    assert outcome.outcome == "completed"
    assert runner.calls == ["preflight", "push"]
    executions = ledger.list_step_executions("implement#7/1")
    assert [(item.step_id, item.result) for item in executions] == [
        ("preflight", "ready"),
        ("push", "pushed"),
    ]


def test_graph_gate_drift_ends_drift(ledger: RunLedger) -> None:
    """S7a wiring: a Gate failure at a write ends `drift`, never routed."""
    workflow = _attempt_workflow()
    runner = ScriptedRunner(results={"preflight": "ready"}, gate_status="drift")
    outcome = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        operation_runner=runner,
    )
    assert outcome.outcome == "drift"


def test_graph_without_runner_keeps_scripted_operations(ledger: RunLedger) -> None:
    """S3 preserved: runs without a runner still answer from the script."""
    workflow = _attempt_workflow()
    outcome = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        script={"preflight": ["ready"], "push": ["pushed"]},
    )
    assert outcome.outcome == "completed"


def test_local_runner_advances_pr_head_on_own_push(tmp_path: Path) -> None:
    """§7.2: the Attempt's own CAS push advances the Input Head, not Drift."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    git = FakeGitOps(remote={"pr-head": "a" * 40})
    runner = LocalOperationRunner(
        workspace=repo,
        start_commit=_git("rev-parse", "HEAD", cwd=repo),
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=QueueRunner(evidence_dir, []),
        evidence_dir=evidence_dir,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        attempt_branch="agent/review/9/1-slug",
        input_type="pull-request",
        head_branch="pr-head",
        expected_head="a" * 40,
        profile_text_override=GOOD_PROFILE,
        git=git,
    )
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    step = V2Step(id="push", operation="commit-and-push", routes={})
    handle = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        execution = runner.run(step, 1, "review#9/1", handle)
    finally:
        handle.close()
    assert execution.result == "pushed"
    assert runner.expected_head == git.remote["pr-head"]


def test_local_runner_check_readiness_then_run_validation(tmp_path: Path) -> None:
    """S7a pinning: the profile read once serves both local operations."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    queue = QueueRunner(
        evidence_dir,
        [
            QueuedRun("bootstrap", 0, None),
            QueuedRun("test_all.xml", 0, _junit({"pkg::test_ok": False})),
        ],
    )
    runner = LocalOperationRunner(
        workspace=repo,
        start_commit=head,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=True),
        runner=queue,
        evidence_dir=evidence_dir,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        attempt_branch="agent/implement/7/1-slug",
        input_type="issue",
        profile_text_override=None,
        git=FakeGitOps(),
    )
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "project-profile.yml").write_text(
        GOOD_PROFILE, encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "add profile", cwd=repo)
    runner.start_commit = _git("rev-parse", "HEAD", cwd=repo)
    handle = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        ready = runner.run(
            V2Step(id="preflight", operation="check-readiness", routes={}), 1, "implement#7/1", handle
        )
        assert ready.result == "ready"
        validated = runner.run(
            V2Step(id="validate", operation="run-validation", routes={}), 1, "implement#7/1", handle
        )
        assert validated.result == "passed"
        assert validated.facts["tree_id"] == tree_identity(repo)
    finally:
        handle.close()


def test_local_runner_run_validation_without_profile_fails(tmp_path: Path) -> None:
    """S7a: no pinned Validation Contract means the validation cannot run."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    runner = LocalOperationRunner(
        workspace=repo,
        start_commit=head,
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=QueueRunner(evidence_dir, []),
        evidence_dir=evidence_dir,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        attempt_branch="agent/implement/7/1-slug",
        input_type="issue",
        profile_text_override="schema: [unclosed\n",
        git=FakeGitOps(),
    )
    handle = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        execution = runner.run(
            V2Step(id="validate", operation="run-validation", routes={}), 1, "implement#7/1", handle
        )
    finally:
        handle.close()
    assert execution.result == "failed"


def test_commit_message_carries_attempt_trailer() -> None:
    """§14.4: commits carry the Attempt id as a trailer."""
    message = commit_message_for_attempt("review#42/2")
    assert "Attempt: review#42/2" in message


def test_validation_registry_defaults_to_unheld() -> None:
    """§10.1: `requires` on an unknown tree does not hold."""
    registry = ValidationRegistry()
    assert registry.holds((("run-validation", "passed"),), "unknown-tree") is False
