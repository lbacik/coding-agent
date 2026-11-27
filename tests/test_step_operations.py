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
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.workflow_graph import run_workflow
from support.scripted_executor import ScriptedStepExecutor
from coding_agent.runtime.step_operations import (
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
from coding_agent.runtime.workspace_git import (
    AttemptWorkspaceGit,
    BareRepoRemote,
    GitRemote,
)
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.workflow.schema import AttemptCeiling, InputDecl, RouteTarget, WorkflowStep, Workflow

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
            "schema": 2,
            "toolchains": {
                "python": {
                    "version": "3.13.9",
                    "package_managers": {"uv": {"version": "0.6.1"}},
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


def _init_bare(bare: Path) -> Path:
    """Create a bare repository to serve as the test remote."""
    completed = subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return bare


def _workspace_git(repo: Path, bare: Path) -> AttemptWorkspaceGit:
    """One Attempt-bound workspace git over a real repo and a bare remote."""
    return AttemptWorkspaceGit(
        repo,
        "agent",
        "1+agent@users.noreply.github.com",
        BareRepoRemote(repo, bare),
    )


def _push_to_bare(repo: Path, bare: Path, sha: str, branch: str) -> None:
    """Serve ``sha`` at ``branch`` from the bare test remote."""
    completed = subprocess.run(
        ["git", "push", "-q", str(bare), f"{sha}:refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _foreign_sha(repo: Path, base: str) -> str:
    """A real commit sha that is not ``base`` (a foreign remote head)."""
    tree = _git("rev-parse", f"{base}^{{tree}}", cwd=repo)
    return _git("commit-tree", tree, "-m", "foreign", cwd=repo)


class _FailingPushRemote(GitRemote):
    """A narrow-port remote whose push always fails (OP-13 transport)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def remote_sha(self, branch: str) -> str | None:
        return None

    def fetch_revision(self, revision: str) -> None:
        return None

    def push_ref(self, local_sha: str, branch: str) -> None:
        raise self._exc


class _RaceRemote(GitRemote):
    """A narrow-port remote that moves between the read and the write (§10.4)."""

    def __init__(self, expected: str, race_to: str) -> None:
        self._current: str | None = expected
        self._race_to = race_to

    def remote_sha(self, branch: str) -> str | None:
        return self._current

    def fetch_revision(self, revision: str) -> None:
        return None

    def push_ref(self, local_sha: str, branch: str) -> None:
        self._current = self._race_to
        raise GitFailure("'git push' failed: non-fast-forward")


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
        evidence_dir, [QueuedRun("test_all.xml", 0, _junit({"test_ok": False}))]
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
        [QueuedRun("test_all.xml", 1, _junit({"test_ok": True, "test_new": False}))],
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
    head = _init_repo(repo)
    (repo / "work.txt").write_text("new\n", encoding="utf-8")
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    registry = ValidationRegistry()
    registry.record("some-earlier-tree", True)
    outcome = run_commit_and_push(
        attempt_id="implement#7/1",
        input_type="issue",
        branch="agent/implement/7/1-slug",
        expected_head=None,
        start_commit=head,
        with_requires=(("run-validation", "passed"),),
        validation_registry=registry,
        gate=None,
        git=git,
    )
    assert outcome.result == "requirement-unmet"
    assert git.remote_sha("agent/implement/7/1-slug") is None
    assert outcome.revision is None


# --- OP-5: commit-and-push on an issue branch ----------------------------------


def _start_or_head(git: AttemptWorkspaceGit, start_commit: str | None) -> str:
    """Default an omitted start commit to the current HEAD (test-only)."""
    if start_commit is not None:
        return start_commit
    return git.head_sha()


def _issue_push(
    git: AttemptWorkspaceGit,
    ledger: RunLedger | None = None,
    gate: GateChecker | None = None,
    with_requires: tuple[tuple[str, str], ...] = (),
    validation_registry: ValidationRegistry | None = None,
    branch: str = "agent/implement/7/1-greeting",
    start_commit: str | None = None,
) -> CommitPushOutcome:
    return run_commit_and_push(
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        start_commit=_start_or_head(git, start_commit),
        with_requires=with_requires,
        validation_registry=validation_registry,
        gate=gate,
        git=git,
        ledger=ledger,
        step_id="push",
        seq=1,
    )


def _pr_push(
    git: AttemptWorkspaceGit,
    expected_head: str | None,
    ledger: RunLedger | None = None,
    branch: str = "pr-head",
    start_commit: str | None = None,
) -> CommitPushOutcome:
    return run_commit_and_push(
        attempt_id="implement#7/1",
        input_type="pull-request",
        branch=branch,
        expected_head=expected_head,
        start_commit=_start_or_head(git, start_commit),
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
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    outcome = _issue_push(git, ledger)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert git.remote_sha("agent/implement/7/1-greeting") == outcome.revision
    message = _git("log", "-1", "--pretty=%B", outcome.revision, cwd=repo)
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
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, head, "agent/implement/7/1-greeting")
    outcome = _issue_push(git, ledger)
    assert outcome.result == "no-changes"
    assert outcome.revision == head
    assert git.remote_sha("agent/implement/7/1-greeting") == head
    assert ledger.get_step_effect("implement#7/1", "push", 1, 0) is None


def test_commit_and_push_issue_clean_without_attempt_commit_is_no_changes(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """§10.4: clean, HEAD == start, no remote branch → `no-changes`; no push."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    outcome = _issue_push(git, ledger, start_commit=head)
    assert outcome.result == "no-changes"
    assert outcome.revision == head
    assert git.remote_sha("agent/implement/7/1-greeting") is None
    assert ledger.get_step_effect("implement#7/1", "push", 1, 0) is None


def test_commit_and_push_issue_clean_past_start_pushes(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """§10.4: clean, HEAD past the start, no remote branch → `pushed`."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "an earlier Attempt commit", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)
    assert head != base
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    outcome = _issue_push(git, ledger, start_commit=base)
    assert outcome.result == "pushed"
    assert outcome.revision == head
    assert git.remote_sha("agent/implement/7/1-greeting") == head
    record = ledger.get_step_effect("implement#7/1", "push", 1, 0)
    assert record is not None
    assert record.status == "receipt"


def test_commit_and_push_issue_remote_moved_is_error(tmp_path: Path) -> None:
    """OP-5/§10.4: a moved remote branch is never overwritten (`error`)."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, _foreign_sha(repo, head), "agent/implement/7/1-greeting")
    outcome = _issue_push(git)
    assert outcome.result == "error"
    assert git.remote_sha("agent/implement/7/1-greeting") != head
    assert _git("rev-parse", "HEAD", cwd=repo) == head


def test_commit_and_push_pushes_fast_forward_only(tmp_path: Path) -> None:
    """OP-5: the live push path is fast-forward only (a divergent head fails)."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    bare = _init_bare(tmp_path / "remote.git")
    branch = "agent/implement/7/1-greeting"
    git = _workspace_git(repo, bare)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    first = run_commit_and_push(
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        start_commit=base,
        git=git,
    )
    assert first.result == "pushed"
    # Diverge the remote behind our back: a foreign commit on the branch.
    clone = tmp_path / "clone"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.email", "other@example.com", cwd=clone)
    _git("config", "user.name", "other", cwd=clone)
    _git("checkout", branch, cwd=clone)
    (clone / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-qm", "foreign", cwd=clone)
    _git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    second = run_commit_and_push(
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        start_commit=base,
        git=git,
    )
    assert second.result == "error"


# --- OP-6: commit-and-push to a pull-request head ------------------------------


def test_commit_and_push_pr_compare_and_swap_advances_head(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-6: remote == Input Head, so the CAS push lands and advances it."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, head, "pr-head")
    outcome = _pr_push(git, head, ledger)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert git.remote_sha("pr-head") == outcome.revision
    assert outcome.facts["input_head"] == outcome.revision
    record = ledger.get_step_effect("implement#7/1", "push", 1, 0)
    assert record is not None
    assert record.status == "receipt"


def test_commit_and_push_pr_moved_head_is_stale_head(tmp_path: Path) -> None:
    """OP-6: a moved remote head yields `stale-head`; nothing is forced."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, head, "pr-head")
    outcome = _pr_push(git, "a" * 40)
    assert outcome.result == "stale-head"
    assert git.remote_sha("pr-head") == head
    assert _git("rev-parse", "HEAD", cwd=repo) == head
    assert (repo / "fix.txt").read_text(encoding="utf-8") == "fix\n"


def test_commit_and_push_pr_race_is_stale_head(tmp_path: Path, ledger: RunLedger) -> None:
    """INP-3/§10.4: a foreign push between Gate and write yields `stale-head`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    git = AttemptWorkspaceGit(
        repo,
        "agent",
        "1+agent@users.noreply.github.com",
        _RaceRemote("a" * 40, "c" * 40),
    )
    outcome = _pr_push(git, "a" * 40, ledger)
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
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    outcome = _issue_push(git)
    assert outcome.result == "error"
    assert ".github/workflows/" in outcome.detail
    assert git.remote_sha("agent/implement/7/1-greeting") is None
    assert _git("rev-parse", "HEAD", cwd=repo) == head


def test_commit_and_push_permission_refusal_is_error(tmp_path: Path) -> None:
    """OP-13: a single refused permission ends `error`, routed by the Workflow."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    git = AttemptWorkspaceGit(
        repo,
        "agent",
        "1+agent@users.noreply.github.com",
        _FailingPushRemote(GitFailure("'git push' failed: permission denied")),
    )
    outcome = _issue_push(git)
    assert outcome.result == "error"


def test_commit_and_push_gate_drift_raises(tmp_path: Path) -> None:
    """§10.1: Drift at the Gate before the write is never routed (`GateFailed`)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)

    def drift() -> GateResult:
        return GateResult(status="drift", reason="content-changed")

    with pytest.raises(GateFailed) as excinfo:
        _issue_push(git, gate=drift)
    assert excinfo.value.status == "drift"
    assert git.remote_sha("agent/implement/7/1-greeting") is None


def test_commit_and_push_gate_withdrawal_raises(tmp_path: Path) -> None:
    """§10.1: withdrawal at the Gate before the write is never routed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)

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
        _issue_push(git, gate=withdrawn)
    assert excinfo.value.status == "withdrawn"
    assert git.remote_sha("agent/implement/7/1-greeting") is None


# --- Graph integration ---------------------------------------------------------


def _attempt_workflow() -> Workflow:
    preflight = WorkflowStep(
        id="preflight",
        operation="check-readiness",
        routes={
            "ready": RouteTarget(step="push"),
            "not-ready": RouteTarget(end="blocked"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="failed"),
        },
    )
    push = WorkflowStep(
        id="push",
        operation="commit-and-push",
        routes={
            "pushed": RouteTarget(end="completed"),
            "no-changes": RouteTarget(end="completed"),
            "stale-head": RouteTarget(end="failed"),
            "requirement-unmet": RouteTarget(end="failed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="failed"),
        },
    )
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:implement"),
        ceiling=AttemptCeiling(cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=60),
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
        step: WorkflowStep,
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
        step_executor=ProductionStepExecutor(operation_runner=runner),
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
        step_executor=ProductionStepExecutor(operation_runner=runner),
    )
    assert outcome.outcome == "drift"


def test_graph_scripted_adapter_answers_operations(ledger: RunLedger) -> None:
    """The scripted adapter answers operation steps with no effects."""
    workflow = _attempt_workflow()
    outcome = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        step_executor=ScriptedStepExecutor({"preflight": ["ready"], "push": ["pushed"]}),
    )
    assert outcome.outcome == "completed"
    assert list(ledger.list_step_effects("implement#7/1")) == []


def test_local_runner_advances_pr_head_on_own_push(tmp_path: Path) -> None:
    """§7.2: the Attempt's own CAS push advances the Input Head, not Drift."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, head, "pr-head")
    runner = LocalOperationRunner(
        start_commit=_git("rev-parse", "HEAD", cwd=repo),
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=QueueRunner(evidence_dir, []),
        evidence_dir=evidence_dir,
        attempt_branch="agent/review/9/1-slug",
        input_type="pull-request",
        head_branch="pr-head",
        expected_head=head,
        profile_text_override=GOOD_PROFILE,
        git=git,
    )
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    step = WorkflowStep(id="push", operation="commit-and-push", routes={})
    handle = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        execution = runner.run(step, 1, "review#9/1", handle)
    finally:
        handle.close()
    assert execution.result == "pushed"
    assert runner.expected_head == git.remote_sha("pr-head")


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
            QueuedRun("test_all.xml", 0, _junit({"test_ok": False})),
        ],
    )
    runner = LocalOperationRunner(
        start_commit=head,
        matrix=_matrix(),
        service_env={"DB_URL": "tcp://db.local:5432"},
        prober=FakeProber(reachable=True),
        runner=queue,
        evidence_dir=evidence_dir,
        attempt_branch="agent/implement/7/1-slug",
        input_type="issue",
        profile_text_override=None,
        git=_workspace_git(repo, _init_bare(tmp_path / "remote.git")),
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
            WorkflowStep(id="preflight", operation="check-readiness", routes={}), 1, "implement#7/1", handle
        )
        assert ready.result == "ready"
        validated = runner.run(
            WorkflowStep(id="validate", operation="run-validation", routes={}), 1, "implement#7/1", handle
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
        start_commit=head,
        matrix=_matrix(),
        service_env={},
        prober=FakeProber(),
        runner=QueueRunner(evidence_dir, []),
        evidence_dir=evidence_dir,
        attempt_branch="agent/implement/7/1-slug",
        input_type="issue",
        profile_text_override="schema: [unclosed\n",
        git=_workspace_git(repo, _init_bare(tmp_path / "remote.git")),
    )
    handle = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        execution = runner.run(
            WorkflowStep(id="validate", operation="run-validation", routes={}), 1, "implement#7/1", handle
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


# --- OP-21: commit-and-push inherits validation (§10.4) --------------------------


def test_inherit_result_binds_new_identity_to_recorded_result() -> None:
    """OP-21 (§10.4): inheriting copies the source result onto the target."""
    registry = ValidationRegistry()
    registry.record("dirty:abc", True)
    registry.inherit_result("dirty:abc", "commit-sha")
    assert registry.holds((("run-validation", "passed"),), "commit-sha") is True


def test_inherit_result_without_a_source_record_binds_nothing() -> None:
    """OP-21 (§10.4): no record on the source binds nothing on the target."""
    registry = ValidationRegistry()
    registry.inherit_result("dirty:missing", "commit-sha")
    assert registry.holds((("run-validation", "passed"),), "commit-sha") is False


def test_inherit_result_copies_failed_results() -> None:
    """OP-21 (§10.4): a `failed` validation is inherited the same way."""
    registry = ValidationRegistry()
    registry.record("dirty:abc", False)
    registry.inherit_result("dirty:abc", "commit-sha")
    assert registry.passed["commit-sha"] is False
    assert registry.holds((("run-validation", "passed"),), "commit-sha") is False


def test_inherit_result_later_record_overrides() -> None:
    """OP-21 (§10.4): a later validation on the commit overrides inheritance."""
    registry = ValidationRegistry()
    registry.record("dirty:abc", True)
    registry.inherit_result("dirty:abc", "commit-sha")
    registry.record("commit-sha", False)
    assert registry.holds((("run-validation", "passed"),), "commit-sha") is False
    registry.record("commit-sha", True)
    assert registry.holds((("run-validation", "passed"),), "commit-sha") is True


def test_commit_and_push_inherits_passed_validation_to_new_revision(
    tmp_path: Path,
) -> None:
    """OP-21 (§10.4): validating the dirty tree satisfies `requires` on the commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    pre_commit_tree = tree_identity(repo)
    assert pre_commit_tree.startswith("dirty:")
    registry = ValidationRegistry()
    registry.record(pre_commit_tree, True)
    git = _workspace_git(repo, _init_bare(tmp_path / "remote.git"))
    outcome = _issue_push(git, validation_registry=registry)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert outcome.facts["tree_id"] == pre_commit_tree
    assert outcome.facts["revision"] == outcome.revision
    assert registry.holds((("run-validation", "passed"),), outcome.revision) is True
    # The workspace is clean now: the new HEAD is the bound revision.
    assert tree_identity(repo) == outcome.revision


def test_commit_and_push_does_not_inherit_after_workspace_changed(
    tmp_path: Path,
) -> None:
    """OP-21 (§10.4): validation of an earlier tree never binds the new commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("first\n", encoding="utf-8")
    validated_tree = tree_identity(repo)
    registry = ValidationRegistry()
    registry.record(validated_tree, True)
    # The workspace moves on before the commit: a different dirty identity.
    (repo / "work.txt").write_text("second\n", encoding="utf-8")
    assert tree_identity(repo) != validated_tree
    git = _workspace_git(repo, _init_bare(tmp_path / "remote.git"))
    outcome = _issue_push(git, validation_registry=registry)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert registry.holds((("run-validation", "passed"),), outcome.revision) is False


def test_commit_and_push_binds_failed_results_too(tmp_path: Path) -> None:
    """OP-21 (§10.4): a `failed` validation is inherited like a `passed` one."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    pre_commit_tree = tree_identity(repo)
    registry = ValidationRegistry()
    registry.record(pre_commit_tree, False)
    git = _workspace_git(repo, _init_bare(tmp_path / "remote.git"))
    outcome = _issue_push(git, validation_registry=registry)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert registry.passed[outcome.revision] is False
    assert registry.holds((("run-validation", "passed"),), outcome.revision) is False


def test_commit_and_push_records_inheritance_even_when_push_fails(
    tmp_path: Path,
) -> None:
    """OP-21 (§10.4): the binding is a fact about content, not about the remote."""
    from coding_agent.runtime.git import GitFailure as _GitFailure

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    pre_commit_tree = tree_identity(repo)
    registry = ValidationRegistry()
    registry.record(pre_commit_tree, True)
    git = AttemptWorkspaceGit(
        repo,
        "agent",
        "1+agent@users.noreply.github.com",
        _FailingPushRemote(_GitFailure("'git push' failed: permission denied")),
    )
    outcome = _issue_push(git, validation_registry=registry)
    assert outcome.result == "error"
    assert outcome.revision is not None
    assert registry.holds((("run-validation", "passed"),), outcome.revision) is True


def test_commit_and_push_clean_workspace_binds_nothing_new(tmp_path: Path) -> None:
    """OP-21 (§10.4): a clean workspace creates no commit and changes nothing."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    registry = ValidationRegistry()
    registry.record(head, True)
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, head, "agent/implement/7/1-greeting")
    outcome = _issue_push(git, validation_registry=registry)
    assert outcome.result == "no-changes"
    assert outcome.revision == head
    assert registry.holds((("run-validation", "passed"),), head) is True


def test_commit_and_push_inherits_run_validation_end_to_end(tmp_path: Path) -> None:
    """OP-21 (§10.3/§10.4): a real `run-validation` end flows onto the commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(
        evidence_dir, [QueuedRun("test_all.xml", 0, _junit({"test_ok": False}))]
    )
    validated = run_run_validation(
        workspace=repo,
        profile=_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert validated.result == "passed"
    registry = ValidationRegistry()
    registry.record(validated.tree_id, validated.result == "passed")
    git = _workspace_git(repo, _init_bare(tmp_path / "remote.git"))
    outcome = _issue_push(git, validation_registry=registry)
    assert outcome.result == "pushed"
    assert outcome.revision is not None
    assert registry.holds((("run-validation", "passed"),), outcome.revision) is True


# --- OP-21 integration: the `implement` / `review` graph shapes -----------------


def test_implement_shape_open_pr_after_validated_commit(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-21 integration: validate → push → `open-pr` with `requires` opens."""
    from support.fake_step_tracker import FakeStepTracker

    from coding_agent.runtime.step_publication import run_open_pull_request

    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    runner = QueueRunner(
        evidence_dir, [QueuedRun("test_all.xml", 0, _junit({"test_ok": False}))]
    )
    validated = run_run_validation(
        workspace=repo,
        profile=_profile(),
        runner=runner,
        evidence_dir=evidence_dir,
    )
    assert validated.result == "passed"
    registry = ValidationRegistry()
    registry.record(validated.tree_id, validated.result == "passed")
    branch = "agent/implement/7/1-greeting"
    git = _workspace_git(repo, _init_bare(tmp_path / "remote.git"))
    push = run_commit_and_push(
        attempt_id="implement#7/1",
        input_type="issue",
        branch=branch,
        expected_head=None,
        start_commit=base,
        validation_registry=registry,
        git=git,
        ledger=ledger,
        step_id="push",
        seq=1,
    )
    assert push.result == "pushed"
    assert push.revision is not None
    if not ledger.has_attempt("implement#7/1"):
        ledger.open_attempt(
            "implement#7/1", effective_configuration={"workflow": "implement"}
        )
    ledger.open_step_execution("implement#7/1", "process", 1)
    ledger.end_step_execution(
        "implement#7/1",
        "process",
        1,
        result="done",
        payload={"document": "Implements the greeting."},
    )
    tracker = FakeStepTracker(agent_id=1001)
    pr = run_open_pull_request(
        attempt_id="implement#7/1",
        workflow_id="implement",
        input_number=7,
        issue_title="Add a greeting",
        attempt_branch=branch,
        base_branch="main",
        base_revision=base,
        config_digest="cfg-digest-1",
        with_body="process.done.document",
        draft=False,
        with_requires=(("run-validation", "passed"),),
        validation_registry=registry,
        git=git,
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        step_id="open-pr",
        seq=1,
    )
    assert pr.result == "opened"


def test_pr_shape_publish_review_after_validated_commit(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-21 integration: `commit-and-push` → `publish-review` with `requires`."""
    from support.fake_step_tracker import FakeStepTracker

    from coding_agent.inputs.port import TrackedInput
    from coding_agent.runtime.step_publication import run_publish_review

    repo = tmp_path / "repo"
    pr_base = _init_repo(repo)
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    pre_commit_tree = tree_identity(repo)
    registry = ValidationRegistry()
    registry.record(pre_commit_tree, True)
    bare = _init_bare(tmp_path / "remote.git")
    git = _workspace_git(repo, bare)
    _push_to_bare(repo, bare, pr_base, "pr-head")
    push = run_commit_and_push(
        attempt_id="review#9/1",
        input_type="pull-request",
        branch="pr-head",
        expected_head=pr_base,
        start_commit=pr_base,
        validation_registry=registry,
        git=git,
        ledger=ledger,
        step_id="push",
        seq=1,
    )
    assert push.result == "pushed"
    assert push.revision is not None
    tracker = FakeStepTracker(agent_id=1001)
    tracker.inputs[31] = TrackedInput(
        number=31,
        input_type="pull-request",
        title="a title",
        body="a body",
        is_open=True,
        labels=("agent:review",),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        head_sha=push.revision,
        head_branch="feature",
        head_repo="octocat/sandbox",
        base_branch="main",
    )
    if not ledger.has_attempt("review#9/1"):
        ledger.open_attempt(
            "review#9/1", effective_configuration={"workflow": "review"}
        )
    ledger.open_step_execution("review#9/1", "review", 1)
    ledger.end_step_execution(
        "review#9/1",
        "review",
        1,
        result="reviewed",
        payload={"body": "Looks good."},
    )
    review = run_publish_review(
        workspace=repo,
        attempt_id="review#9/1",
        workflow_id="review",
        input_number=31,
        expected_head=push.revision,
        config_digest="cfg-digest-1",
        with_body="review.reviewed.body",
        with_requires=(("run-validation", "passed"),),
        validation_registry=registry,
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        step_id="publish",
        seq=1,
    )
    assert review.result == "published"
