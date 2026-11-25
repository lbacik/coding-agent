"""Local Runtime Operations for the step-workflow runtime: ``check-readiness``,
``run-validation`` and ``commit-and-push`` (plus revision binding of evidence).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.1 (the
catalog and its preconditions: ``requires``, the Gate before a write),
§10.2 (``check-readiness``: the Project Profile from either source in ADR 0030,
the Supported Toolchain Matrix, declared services, then ``bootstrap``;
``ready`` / ``not-ready`` / ``error``), §10.3 (``run-validation``: the pinned Validation Contract on the
current tree; ``passed`` / ``failed`` with Validation Evidence bound to the
tree's revision identity), §10.4 (``commit-and-push``: stage as ``git add
-A``, commit with explicit parents under the Agent Identity with the Attempt
trailer, fast-forward pushes only, compare-and-swap to a pull-request head
with ``stale-head``, the ``.github/workflows/`` authorship refusal),
§10.8 (``update-from-base``: merge the pinned Base Tip without committing;
``merged`` / ``conflicted`` / ``up-to-date``) and §13.4 (every
runtime-produced evidence record carries the tree identity: the commit when
clean, otherwise a digest over tracked plus non-ignored untracked files).

Slice S7a of ``docs/plan/step-workflow-implementation-plan.md``. Reuses
``profile/`` (parsing, readiness, toolchain, services), ``validate/``
(the Validation Contract harness, JUnit parsing, outcome classification)
and ``runtime/git.py`` (the mirror/workspace/push helpers, all
fast-forward only). Additive: until the S10 cutover the released Worker
keeps running the MVP runtime and nothing here is reachable from
``agent run``; the compiled Workflow graph calls these executors only when
a run injects an :class:`OperationRunner`.

Matrix row ids: OP-1 … OP-6, OP-13, OP-22 … OP-26, STEP-15.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from coding_agent.inputs.gates import GateResult
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.parser import InvalidProfile, MissingReadinessFacts
from coding_agent.profile.readiness import (
    ProfileSources,
    Ready,
    UnsupportedEnvironment,
    evaluate_readiness,
)
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber
from coding_agent.profile.substitution import render_command
from coding_agent.profile.toolchain import SupportedToolchainMatrix
from coding_agent.runtime.footprint import collect_dirty_digests
from coding_agent.runtime.git import (
    GitFailure,
    clear_merge_state,
    commit_tree_sha,
    create_commit_with_parents,
    diff_names_between,
    fetch_revision,
    head_revision,
    is_ancestor,
    merge_tip_no_commit,
    object_exists,
    push_branch,
    push_ref,
    read_blob_at_revision,
    read_file_at_revision,
    remote_ref_sha,
    tree_touches_workflows_path,
    unmerged_paths,
    update_branch_ref,
    write_staged_tree,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.validate.harness import CommandContext, run_validation_contract
from coding_agent.validate.results import RawCommandResult, classify
from coding_agent.validate.runner import CommandRunner
from coding_agent.workflow.schema import WorkflowStep

PROFILE_RELPATH = "docs/agents/project-profile.yml"
"""The Project Profile path, fixed by contract §10.2 (never configurable)."""

OPERATION_TIMEOUT_SECONDS = 600.0
"""Code-owned bound for one local-operation command run (§10, §11.5).

Operations are bounded by code-owned timeouts, never by ``limits``. The
runner adapters already carry this default; the name here documents the
guarantee where the operation builds its own context.
"""

CheckReadinessResultName = Literal["ready", "not-ready", "error"]
RunValidationResultName = Literal["passed", "failed"]
CommitPushResultName = Literal[
    "pushed", "no-changes", "stale-head", "requirement-unmet", "error"
]
UpdateFromBaseResultName = Literal["merged", "conflicted", "up-to-date", "error"]


CONFLICT_MARKER_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>")
"""Conflict-marker prefixes refused in a merge commit's conflicted paths (§10.8).

A line in a conflicted path beginning with exactly these seven characters
means the conflict is unresolved; ``commit-and-push`` ends ``error`` and
names the paths. No new result is added to the ``commit-and-push`` catalog.
"""


def has_conflict_marker(content: bytes) -> bool:
    """Whether staged content still carries an unresolved conflict marker (§10.8)."""
    text = content.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith(CONFLICT_MARKER_PREFIXES):
            return True
    return False


# --- Tree identity (§13.4) ----------------------------------------------------


def tree_identity(workspace: Path) -> str:
    """The identity of the tree the runtime produced evidence on (§13.4).

    The commit (``HEAD``) when the tree is clean, otherwise a hex digest
    over tracked plus non-ignored untracked files — the Execution Footprint
    set. The digest folds in ``HEAD`` (so two checkouts with the same dirty
    paths but different bases differ) and every dirty path's content
    digest, sorted by path. An unreadable tree state never reports clean:
    it yields a ``dirty:`` digest, so an unknown state cannot pass as the
    commit it might not be.

    Matrix row id: STEP-15.
    """
    try:
        head = head_revision(workspace)
    except GitFailure:
        head = "no-head"
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
    except OSError:
        completed = None
    if completed is None or completed.returncode != 0:
        marker = hashlib.sha256(f"unreadable:{head}".encode("utf-8")).hexdigest()
        return f"dirty:{marker}"
    if not completed.stdout.strip():
        return head
    digests = collect_dirty_digests(workspace)
    material = "\0".join(
        f"{path}\0{digests.get(path)}" for path in sorted(digests)
    )
    digest = hashlib.sha256(f"{head}\0{material}".encode("utf-8")).hexdigest()
    return f"dirty:{digest}"


# --- check-readiness (§10.2) --------------------------------------------------


@dataclass(frozen=True)
class CheckReadinessOutcome:
    """One ``check-readiness`` Step Execution's end (§10.2)."""

    result: CheckReadinessResultName
    facts: dict[str, object]
    detail: str
    profile: ProjectProfile | None = None


def run_check_readiness(
    *,
    workspace: Path,
    profile_text: str | None,
    matrix: SupportedToolchainMatrix,
    service_env: Mapping[str, str],
    prober: ServiceProber,
    runner: CommandRunner,
    evidence_dir: Path,
    profile_source: str = PROFILE_RELPATH,
    profile_origin: str = "target-repository",
    repository_profile_ignored: bool = False,
) -> CheckReadinessOutcome:
    """Run ``check-readiness`` on the current tree (§10.2, ADR 0030).

    Reads the Project Profile ``profile_text`` (already read at the start
    commit by the caller, or the instance-supplied text pinned at Claim),
    checks the toolchain against the Supported
    Toolchain Matrix and the declared services for reachability, then runs
    ``bootstrap``. ``not-ready`` names exactly what is missing or wrong;
    ``error`` means ``bootstrap`` failed. There is no model call on any
    path (matrix row OP-2): this function takes no model.

    ``profile_source`` stays the location label recorded in
    ``sources_searched`` (the repository relpath, or
    ``workflow-configuration`` where the deployment supplies the contract);
    ``profile_origin`` is the ``profile_source`` fact value
    (``target-repository`` or ``workflow-configuration``). Where the
    deployment supplies the contract and the repository still carries its own
    profile, ``repository_profile_ignored`` records that the repository file
    was ignored — never a refusal.
    """

    def _base_facts() -> dict[str, object]:
        facts: dict[str, object] = {"profile_source": profile_origin}
        if repository_profile_ignored:
            facts["repository_profile_ignored"] = True
        return facts

    if profile_text is None:
        missing = (
            f"{PROFILE_RELPATH}: no Project Profile at the Base Revision",
        )
        return CheckReadinessOutcome(
            result="not-ready",
            facts={**_base_facts(), "missing": list(missing)},
            detail=f"not-ready: {'; '.join(missing)}",
            profile=None,
        )
    resolution = evaluate_readiness(
        ProfileSources(
            profile_yaml_text=profile_text,
            sources_searched=(profile_source,),
        ),
        matrix,
        service_env,
        prober,
    )
    if isinstance(resolution, UnsupportedEnvironment):
        facts: dict[str, object] = {
            **_base_facts(),
            "unsupported": resolution.detail,
            "reason": resolution.reason,
        }
        return CheckReadinessOutcome(
            result="not-ready",
            facts=facts,
            detail=f"not-ready: {resolution.detail}",
            profile=None,
        )
    if isinstance(resolution, MissingReadinessFacts):
        return CheckReadinessOutcome(
            result="not-ready",
            facts={**_base_facts(), "missing": list(resolution.missing)},
            detail=f"not-ready: {'; '.join(resolution.missing)}",
            profile=None,
        )
    if isinstance(resolution, InvalidProfile):
        return CheckReadinessOutcome(
            result="not-ready",
            facts={**_base_facts(), "invalid": resolution.detail},
            detail=f"not-ready: {resolution.detail}",
            profile=None,
        )
    assert isinstance(resolution, Ready)
    profile = resolution.profile
    working_directory = workspace / profile.working_directory
    try:
        command = render_command(profile.bootstrap, evidence_dir=evidence_dir)
    except ValueError as exc:
        return CheckReadinessOutcome(
            result="error",
            facts={
                **_base_facts(),
                "bootstrap": f"unrenderable bootstrap template: {exc}",
            },
            detail=f"error: unrenderable bootstrap template: {exc}",
            profile=profile,
        )
    try:
        executed = runner.run(command, cwd=working_directory)
    except Exception as exc:
        return CheckReadinessOutcome(
            result="error",
            facts={
                **_base_facts(),
                "bootstrap": f"bootstrap raised {exc!r}",
            },
            detail=f"error: bootstrap raised {exc!r}",
            profile=profile,
        )
    toolchain_facts: dict[str, object] = {
        "runtime": profile.toolchain.runtime,
        "version": profile.toolchain.version,
        "package_manager": profile.toolchain.package_manager,
    }
    service_facts: list[object] = [
        {"name": service.name, "url_env": service.url_env}
        for service in profile.services
    ]
    if executed.exit_code != 0:
        return CheckReadinessOutcome(
            result="error",
            facts={
                **_base_facts(),
                "toolchain": toolchain_facts,
                "services": service_facts,
                "bootstrap": {
                    "command": executed.command,
                    "exit_code": executed.exit_code,
                    "timed_out": executed.timed_out,
                },
            },
            detail=f"error: bootstrap exited {executed.exit_code}",
            profile=profile,
        )
    return CheckReadinessOutcome(
        result="ready",
        facts={
            **_base_facts(),
            "toolchain": toolchain_facts,
            "services": service_facts,
            "bootstrap": {
                "command": executed.command,
                "exit_code": 0,
                "timed_out": executed.timed_out,
            },
        },
        detail="ready: bootstrap ran",
        profile=profile,
    )


def read_profile_at_commit(workspace: Path, commit: str) -> str | None:
    """Read the Project Profile as committed at ``commit`` (§10.2).

    The Validation Contract is the profile as it stands at the start
    commit; this is the read the operation runner pins.
    """
    return read_file_at_revision(workspace, commit, PROFILE_RELPATH)


# --- run-validation (§10.3) ---------------------------------------------------


@dataclass(frozen=True)
class ValidationCommandEvidence:
    """One Validation Contract command's evidence, bound to a tree (§13.4)."""

    name: str
    command: str
    exit_code: int
    executed: int | None
    failure_ids: frozenset[str]
    timed_out: bool = False


@dataclass(frozen=True)
class RunValidationOutcome:
    """One ``run-validation`` Step Execution's end (§10.3)."""

    result: RunValidationResultName
    tree_id: str
    evidence: tuple[ValidationCommandEvidence, ...]
    facts: dict[str, object]
    detail: str


def _evidence_of(result: RawCommandResult) -> ValidationCommandEvidence:
    executed: int | None = None
    failure_ids: frozenset[str] = frozenset()
    if result.junit is not None:
        executed = result.junit.executed
        if result.evidence_declared:
            failure_ids = result.junit.failure_ids
    return ValidationCommandEvidence(
        name=result.name,
        command=result.command,
        exit_code=result.exit_code,
        executed=executed,
        failure_ids=failure_ids,
        timed_out=result.timed_out,
    )


def run_run_validation(
    *,
    workspace: Path,
    profile: ProjectProfile,
    runner: CommandRunner,
    evidence_dir: Path,
    tree_id: str | None = None,
) -> RunValidationOutcome:
    """Run the pinned Validation Contract on the current tree (§10.3).

    Runs ``test_all`` and every check through the runtime-owned harness —
    commands from the pinned contract, credentials stripped by the runner,
    whole process group killed on timeout — and yields ``passed`` / ``failed``.
    Failed means a non-zero exit or any reported test failure. The returned
    evidence is bound to the tree's revision identity ``tree_id`` (§13.4):
    the commit when clean, otherwise the dirty-tree digest.
    """
    tree = tree_id if tree_id is not None else tree_identity(workspace)
    context = CommandContext(
        runner=runner,
        working_directory=workspace / profile.working_directory,
        evidence_dir=evidence_dir,
        workspace_root=workspace,
    )
    try:
        delivery = run_validation_contract(profile, context)
    except Exception as exc:
        return RunValidationOutcome(
            result="failed",
            tree_id=tree,
            evidence=(),
            facts={"tree_id": tree, "harness_error": f"{exc!r}"},
            detail=f"failed: validation harness raised {exc!r}",
        )
    evidence = tuple(_evidence_of(item) for item in delivery)
    failed = [item for item in delivery if classify(item) != "passed"]
    facts: dict[str, object] = {
        "tree_id": tree,
        "commands": [
            {
                "name": item.name,
                "command": item.command,
                "exit_code": item.exit_code,
                "executed": item.executed,
                "failure_ids": sorted(item.failure_ids),
                "timed_out": item.timed_out,
            }
            for item in evidence
        ],
    }
    if failed:
        names = ", ".join(item.name for item in failed)
        return RunValidationOutcome(
            result="failed",
            tree_id=tree,
            evidence=evidence,
            facts=facts,
            detail=f"failed: {names}",
        )
    return RunValidationOutcome(
        result="passed",
        tree_id=tree,
        evidence=evidence,
        facts=facts,
        detail="passed",
    )


@dataclass
class ValidationRegistry:
    """Which trees hold a ``passed`` validation (§10.1, ``requires``).

    Keyed by tree identity, so evidence from the base tree never satisfies
    ``requires`` for the revision being published: it is bound to a
    different tree. A commit created by ``commit-and-push`` from a dirty
    tree inherits the result recorded for that tree's dirty identity
    (§10.4, §13.4).
    """

    passed: dict[str, bool] = field(default_factory=dict)

    def record(self, tree_id: str, passed: bool) -> None:
        """Remember one ``run-validation`` end for its tree."""
        self.passed[tree_id] = passed

    def inherit_result(self, source_tree_id: str, new_tree_id: str) -> None:
        """Bind a new identity to the result recorded for another identity.

        A commit made from a dirty tree carries that tree's content, so the
        new commit inherits the dirty tree's validation result (§10.4).
        No record on the source binds nothing on the target; a later
        ``record`` on the target still overrides the inherited binding.
        """
        if source_tree_id == new_tree_id:
            return
        if source_tree_id not in self.passed:
            return
        self.passed[new_tree_id] = self.passed[source_tree_id]

    def holds(
        self,
        requires: tuple[tuple[str, str], ...],
        tree_id: str,
    ) -> bool:
        """Whether every ``requires`` entry holds for ``tree_id``."""
        for name, expected in requires:
            if (name, expected) == ("run-validation", "passed"):
                if not self.passed.get(tree_id, False):
                    return False
            else:  # pragma: no cover - the validator admits no other entry
                return False
        return True


# --- commit-and-push (§10.4) --------------------------------------------------


class GateFailed(Exception):
    """A Gate before a write found Drift or withdrawal (§7.3, §10.1).

    Never routed: the Attempt ends with the runtime-only outcome ``status``.
    """

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


GateChecker = Callable[[], GateResult]
"""Re-read the input and answer the Gate before a write (§7.3, §10.1)."""


class CommitPushGitOps(Protocol):
    """The git surface ``run_commit_and_push`` needs, injectable for tests.

    Commits are built with explicit parents (``commit-and-push`` and
    ``update-from-base``, §10.4, §10.8): merge state left by ``shell`` is
    cleared and never becomes a parent.
    """

    def is_clean(self, workspace: Path) -> bool: ...
    def touches_workflows(self, workspace: Path) -> bool: ...
    def head_sha(self, workspace: Path) -> str: ...
    def stage_all(self, workspace: Path) -> None: ...
    def clear_merge_state(self, workspace: Path) -> None: ...
    def write_tree(self, workspace: Path) -> str: ...
    def create_commit(
        self,
        workspace: Path,
        tree: str,
        parents: list[str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str: ...
    def move_branch(self, workspace: Path, branch: str, sha: str) -> None: ...
    def advance_head(self, workspace: Path, sha: str) -> None: ...
    def commit_tree(self, workspace: Path, revision: str) -> str: ...
    def diff_names(self, workspace: Path, old: str, new: str) -> list[str]: ...
    def read_blob(self, workspace: Path, revision: str, path: str) -> bytes | None: ...
    def read_staged_blob(self, workspace: Path, path: str) -> bytes | None: ...
    def object_exists(self, workspace: Path, revision: str) -> bool: ...
    def fetch_revision(self, workspace: Path, revision: str) -> None: ...
    def is_ancestor(self, workspace: Path, ancestor: str, descendant: str) -> bool: ...
    def merge_tip(
        self,
        workspace: Path,
        tip: str,
        *,
        author_name: str,
        author_email: str,
    ) -> int: ...
    def unmerged_paths(self, workspace: Path) -> list[str]: ...
    def remote_sha(self, branch: str) -> str | None: ...
    def push_issue_branch(self, workspace: Path, branch: str) -> None: ...
    def push_ref(self, workspace: Path, local_sha: str, branch: str) -> None: ...


class LiveCommitPushGitOps:
    """The production :class:`CommitPushGitOps`: ``runtime/git.py`` helpers.

    Every push is fast-forward only — ``push_branch`` and ``push_ref`` never
    pass ``--force``, ``--force-with-lease`` or a delete flag — and the
    credential rides on a helper, never in the remote URL (§10.4, §18).
    The Attempt's helper and redaction registry arrive here from the
    supervisor; every remote-touching call forwards them.
    """

    def __init__(
        self,
        remote_url: str,
        helper: GitCredentialHelper | None = None,
        redact: Redactable = None,
    ) -> None:
        self._remote_url = remote_url
        self._helper = helper
        self._redact = redact

    def is_clean(self, workspace: Path) -> bool:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return False
        return not completed.stdout.strip()

    def touches_workflows(self, workspace: Path) -> bool:
        return tree_touches_workflows_path(workspace)

    def head_sha(self, workspace: Path) -> str:
        return head_revision(workspace)

    def stage_all(self, workspace: Path) -> None:
        completed = subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise GitFailure(f"'git add -A' failed: {(completed.stderr or completed.stdout).strip()}")

    def clear_merge_state(self, workspace: Path) -> None:
        clear_merge_state(workspace)

    def write_tree(self, workspace: Path) -> str:
        return write_staged_tree(workspace)

    def create_commit(
        self,
        workspace: Path,
        tree: str,
        parents: list[str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        revision = create_commit_with_parents(
            workspace,
            tree,
            parents,
            message,
            author_name=author_name,
            author_email=author_email,
        )
        return revision

    def move_branch(self, workspace: Path, branch: str, sha: str) -> None:
        update_branch_ref(workspace, branch, sha)

    def advance_head(self, workspace: Path, sha: str) -> None:
        from coding_agent.runtime.git import restore_detached_head as _restore
        from coding_agent.runtime.git import head_branch as _head_branch

        if _head_branch(workspace) is None:
            _restore(workspace, sha=sha)
            return
        completed = subprocess.run(
            ["git", "update-ref", "HEAD", sha],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise GitFailure(
                f"'git update-ref HEAD {sha}' failed: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )

    def commit_tree(self, workspace: Path, revision: str) -> str:
        return commit_tree_sha(workspace, revision)

    def diff_names(self, workspace: Path, old: str, new: str) -> list[str]:
        return diff_names_between(workspace, old, new)

    def read_blob(self, workspace: Path, revision: str, path: str) -> bytes | None:
        return read_blob_at_revision(workspace, revision, path)

    def read_staged_blob(self, workspace: Path, path: str) -> bytes | None:
        result = subprocess.run(
            ["git", "show", f":{path}"],
            cwd=workspace,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def object_exists(self, workspace: Path, revision: str) -> bool:
        return object_exists(workspace, revision)

    def fetch_revision(self, workspace: Path, revision: str) -> None:
        fetch_revision(
            workspace,
            self._remote_url,
            revision,
            redact=self._redact,
            helper=self._helper,
        )

    def is_ancestor(self, workspace: Path, ancestor: str, descendant: str) -> bool:
        from coding_agent.runtime.git import is_ancestor as _is_ancestor

        return _is_ancestor(workspace, ancestor, descendant)

    def merge_tip(
        self,
        workspace: Path,
        tip: str,
        *,
        author_name: str,
        author_email: str,
    ) -> int:
        return merge_tip_no_commit(
            workspace, tip, author_name=author_name, author_email=author_email
        )

    def unmerged_paths(self, workspace: Path) -> list[str]:
        return unmerged_paths(workspace)

    def remote_sha(self, branch: str) -> str | None:
        return remote_ref_sha(
            self._remote_url, branch, redact=self._redact, helper=self._helper
        )

    def push_issue_branch(self, workspace: Path, branch: str) -> None:
        from coding_agent.runtime.git import attempt_branch_sha as _branch_sha

        local_sha = _branch_sha(workspace, branch)
        if local_sha is None:
            local_sha = head_revision(workspace)
        push_ref(
            workspace,
            self._remote_url,
            local_sha,
            branch,
            redact=self._redact,
            helper=self._helper,
        )

    def push_ref(self, workspace: Path, local_sha: str, branch: str) -> None:
        push_ref(
            workspace,
            self._remote_url,
            local_sha,
            branch,
            redact=self._redact,
            helper=self._helper,
        )


@dataclass(frozen=True)
class CommitPushOutcome:
    """One ``commit-and-push`` Step Execution's end (§10.4)."""

    result: CommitPushResultName
    revision: str | None
    facts: dict[str, object]
    detail: str


WORKFLOWS_PATH_PREFIX = ".github/workflows/"
WORKFLOWS_PATH_ROOT = ".github/workflows"


def _is_workflows_path(path: str) -> bool:
    """Whether ``path`` is the workflows directory or a file under it (§10.4)."""
    normalized = path.strip()
    return normalized == WORKFLOWS_PATH_ROOT or normalized.startswith(WORKFLOWS_PATH_PREFIX)


def _unresolved_conflict_paths(
    git: CommitPushGitOps, workspace: Path, conflicted_paths: tuple[str, ...] | list[str]
) -> list[str]:
    """Conflicted paths whose staged content still carries markers (§10.8)."""
    marked: list[str] = []
    for path in conflicted_paths:
        content = git.read_staged_blob(workspace, path)
        if content is not None and has_conflict_marker(content):
            marked.append(path)
    return marked


def _workflows_violation_paths(
    git: CommitPushGitOps, workspace: Path, revision: str, parents: list[str]
) -> list[str]:
    """Paths under ``.github/workflows/`` not taken verbatim from a parent (§10.4).

    A path changed against every parent whose content in the new commit is
    not byte-identical to its content in at least one parent is refused. For
    a single-parent commit this is exactly the old rule: any workflows
    change is a violation. A base change brought in by the merge matches
    the Base Tip parent verbatim and is allowed; a hand edit — including a
    hand-resolved conflict in a workflows file — matches no parent.
    """
    changed: set[str] = set()
    for parent in parents:
        changed.update(git.diff_names(workspace, parent, revision))
    violating: list[str] = []
    for path in sorted(changed):
        if not _is_workflows_path(path):
            continue
        try:
            new_blob = git.read_blob(workspace, revision, path)
        except Exception:
            new_blob = None
        allowed = False
        for parent in parents:
            try:
                parent_blob = git.read_blob(workspace, parent, path)
            except Exception:
                continue
            if parent_blob == new_blob:
                allowed = True
                break
        if not allowed:
            violating.append(path)
    return violating


def commit_message_for_attempt(attempt_id: str, summary: str | None = None) -> str:
    """The commit message for a step-workflow snapshot (§14.4).

    Carries the Attempt id as a trailer, so the Worker's own commit is told
    apart from a human's by the same rule that reads every Attempt Marker.
    """
    first_line = summary or f"{attempt_id} workspace snapshot"
    return f"{first_line}\n\nAttempt: {attempt_id}"


def run_commit_and_push(
    *,
    workspace: Path,
    attempt_id: str,
    input_type: str,
    branch: str,
    expected_head: str | None,
    author_name: str,
    author_email: str,
    with_requires: tuple[tuple[str, str], ...] = (),
    validation_registry: ValidationRegistry | None = None,
    tree_id: str | None = None,
    gate: GateChecker | None = None,
    git: CommitPushGitOps,
    ledger: RunLedger | None = None,
    step_id: str = "commit-and-push",
    seq: int = 1,
    commit_summary: str | None = None,
) -> CommitPushOutcome:
    """Persist the workspace as a commit and push it (§10.4).

    Stages the workspace as ``git add -A`` would, commits with the Agent
    Identity as author and committer and the Attempt trailer, then pushes:
    an issue Attempt branch fast-forward only, or a pull-request head branch
    as a compare-and-swap against the current Input Head. A landed push to a
    pull-request head advances the Input Head; a moved remote head yields
    ``stale-head`` and nothing is ever force-pushed. A clean workspace with
    nothing to push yields ``no-changes``.

    Every commit is built with explicit parents (§10.4, ADR 0034): without a
    pending second parent (recorded by ``update-from-base``) the only parent
    is ``HEAD``; with one, the first parent is ``HEAD`` and the second is
    the pinned Base Tip. A ``MERGE_HEAD`` or other merge state left by
    ``shell`` is ignored and cleared, never a parent. The pending parent is
    consumed by the first ``commit-and-push`` that creates a commit; it
    survives ``stale-head`` and ``error`` ends that create none.

    ``with_requires`` is checked before the write: without a ``passed``
    validation bound to the revision being published the result is
    ``requirement-unmet`` and no write is attempted. The Gate runs before
    the write: Drift or withdrawal raises :class:`GateFailed` (a
    runtime-only ending, never routed). A refused permission or a push
    touching ``.github/workflows/`` ends ``error``. Every attempted push
    records its Effect Intent before the write and its Receipt — or its
    ``not-performed`` settlement — after it (§14.1).

    A commit created from a dirty tree inherits the validation result
    recorded for that tree's pre-commit identity, so ``requires`` checked
    against the new revision sees the validation of the content the commit
    was made from. The facts carry the link: ``tree_id`` is the pre-commit
    identity, ``revision`` the new commit.
    """
    tree = tree_id if tree_id is not None else tree_identity(workspace)
    registry = validation_registry if validation_registry is not None else ValidationRegistry()
    if with_requires and not registry.holds(with_requires, tree):
        return CommitPushOutcome(
            result="requirement-unmet",
            revision=None,
            facts={"tree_id": tree, "requires": [list(pair) for pair in with_requires]},
            detail="requirement-unmet: no passed run-validation is bound to this tree",
        )
    if gate is not None:
        verdict = gate()
        if verdict.status != "ok":
            raise GateFailed(verdict.status, verdict.reason or verdict.status)

    def _intent(payload: str, target: Mapping[str, object]) -> None:
        if ledger is not None:
            ledger.record_step_effect_intent(
                attempt_id,
                step_id,
                seq,
                0,
                rendered_payload=payload,
                expected_target=target,
            )

    def _receipt(remote_id: str | None) -> None:
        if ledger is not None:
            ledger.record_step_effect_receipt(
                attempt_id, step_id, seq, 0, remote_id=remote_id
            )

    def _not_performed(reason: str) -> None:
        if ledger is not None:
            ledger.record_step_effect_not_performed(
                attempt_id, step_id, seq, 0, reason=reason
            )

    if input_type not in ("issue", "pull-request"):
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree, "input_type": input_type},
            detail=f"error: unknown input type {input_type!r}",
        )

    # Merge state left by `shell` is ignored and cleared before anything
    # else: it never becomes a commit parent (§10.4).
    try:
        git.clear_merge_state(workspace)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree},
            detail=f"error: could not clear git merge state: {exc!r}",
        )

    pending = None
    if ledger is not None and input_type == "pull-request":
        try:
            pending = ledger.get_merge_pending(attempt_id)
        except Exception as exc:
            return CommitPushOutcome(
                result="error",
                revision=None,
                facts={"tree_id": tree},
                detail=f"error: could not read the pending merge parent: {exc!r}",
            )

    try:
        clean = git.is_clean(workspace)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree},
            detail=f"error: could not read the working tree: {exc!r}",
        )

    if clean:
        try:
            current = git.head_sha(workspace)
        except Exception as exc:
            return CommitPushOutcome(
                result="error",
                revision=None,
                facts={"tree_id": tree},
                detail=f"error: could not read HEAD: {exc!r}",
            )
        try:
            remote = git.remote_sha(branch)
        except Exception as exc:
            return CommitPushOutcome(
                result="error",
                revision=None,
                facts={"tree_id": tree, "branch": branch, "revision": current},
                detail=f"error: could not read the remote head: {exc!r}",
            )
        if input_type == "issue":
            if remote == current:
                return CommitPushOutcome(
                    result="no-changes",
                    revision=current,
                    facts={"tree_id": tree, "branch": branch, "revision": current},
                    detail="no-changes: the workspace is clean and the branch is up to date",
                )
            if remote is not None:
                return CommitPushOutcome(
                    result="error",
                    revision=current,
                    facts={"tree_id": tree, "branch": branch, "revision": current},
                    detail="error: the remote branch moved; the push is not performed",
                )
            _intent(
                f"commit-and-push {attempt_id}: push {current} to {branch}",
                {"operation": "commit-and-push", "branch": branch, "revision": current},
            )
            try:
                git.push_issue_branch(workspace, branch)
            except Exception as exc:
                _not_performed(f"error: {exc}")
                return CommitPushOutcome(
                    result="error",
                    revision=current,
                    facts={"tree_id": tree, "branch": branch, "revision": current},
                    detail=f"error: push failed: {exc}",
                )
            _receipt(current)
            return CommitPushOutcome(
                result="pushed",
                revision=current,
                facts={"tree_id": tree, "branch": branch, "revision": current},
                detail=f"pushed: {current} to {branch}",
            )
        wanted = expected_head
        if wanted is not None and remote != wanted:
            _not_performed("stale-head: the remote head moved")
            return CommitPushOutcome(
                result="stale-head",
                revision=current,
                facts={"tree_id": tree, "branch": branch, "expected_head": wanted},
                detail="stale-head: the remote head differs from the Input Head",
            )
        if remote == current:
            return CommitPushOutcome(
                result="no-changes",
                revision=current,
                facts={"tree_id": tree, "branch": branch, "revision": current},
                detail="no-changes: the workspace is clean and the head is up to date",
            )
        _intent(
            f"commit-and-push {attempt_id}: push {current} to {branch}",
            {
                "operation": "commit-and-push",
                "branch": branch,
                "revision": current,
                "expected_head": wanted or "",
            },
        )
        try:
            git.push_ref(workspace, current, branch)
        except Exception as exc:
            try:
                moved = git.remote_sha(branch)
            except Exception:
                moved = None
            if wanted is not None and moved != wanted:
                _not_performed("stale-head: the remote head moved")
                return CommitPushOutcome(
                    result="stale-head",
                    revision=current,
                    facts={"tree_id": tree, "branch": branch, "expected_head": wanted},
                    detail="stale-head: the remote head differs from the Input Head",
                )
            _not_performed(f"error: {exc}")
            return CommitPushOutcome(
                result="error",
                revision=current,
                facts={"tree_id": tree, "branch": branch, "revision": current},
                detail=f"error: push failed: {exc}",
            )
        _receipt(current)
        return CommitPushOutcome(
            result="pushed",
            revision=current,
            facts={
                "tree_id": tree,
                "branch": branch,
                "revision": current,
                "input_head": current,
            },
            detail=f"pushed: {current} to {branch}; the Input Head advances",
        )

    # Without a pending second parent the pre-commit working-tree check
    # stands as before. With one, the base merge legitimately brings base
    # content into the tree, so only the post-commit authorship check
    # governs (§10.4).
    if pending is None:
        try:
            if git.touches_workflows(workspace):
                return CommitPushOutcome(
                    result="error",
                    revision=None,
                    facts={"tree_id": tree, "branch": branch},
                    detail="error: the workspace touches .github/workflows/; the push is refused",
                )
        except Exception as exc:
            return CommitPushOutcome(
                result="error",
                revision=None,
                facts={"tree_id": tree, "branch": branch},
                detail=f"error: could not inspect the working tree: {exc!r}",
            )
    try:
        head_before = git.head_sha(workspace)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree, "branch": branch},
            detail=f"error: could not read HEAD: {exc!r}",
        )
    try:
        remote_before = git.remote_sha(branch)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree, "branch": branch},
            detail=f"error: could not read the remote head: {exc!r}",
        )
    if input_type == "issue":
        if remote_before is not None and remote_before != head_before:
            return CommitPushOutcome(
                result="error",
                revision=head_before,
                facts={"tree_id": tree, "branch": branch, "revision": head_before},
                detail="error: the remote branch moved; the push is not performed",
            )
    else:
        wanted = expected_head
        if wanted is not None and remote_before != wanted:
            return CommitPushOutcome(
                result="stale-head",
                revision=head_before,
                facts={"tree_id": tree, "branch": branch, "expected_head": wanted},
                detail="stale-head: the remote head differs from the Input Head",
            )
    try:
        git.stage_all(workspace)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree, "branch": branch},
            detail=f"error: commit failed: {exc}",
        )
    if pending is not None:
        try:
            marked = _unresolved_conflict_paths(git, workspace, pending.conflicted_paths)
        except Exception as exc:
            return CommitPushOutcome(
                result="error",
                revision=None,
                facts={"tree_id": tree, "branch": branch},
                detail=f"error: could not inspect the staged merge: {exc!r}",
            )
        if marked:
            return CommitPushOutcome(
                result="error",
                revision=None,
                facts={"tree_id": tree, "branch": branch, "conflicted_paths": marked},
                detail=(
                    "error: the merge still carries conflict markers in "
                    f"{', '.join(marked)}; resolve them before committing"
                ),
            )
    parents = [head_before] if pending is None else [head_before, pending.base_tip]
    try:
        staged_tree = git.write_tree(workspace)
        revision = git.create_commit(
            workspace,
            staged_tree,
            parents,
            commit_message_for_attempt(attempt_id, commit_summary),
            author_name,
            author_email,
        )
        git.move_branch(workspace, branch, revision)
        git.advance_head(workspace, revision)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=None,
            facts={"tree_id": tree, "branch": branch},
            detail=f"error: commit failed: {exc}",
        )
    if pending is not None and ledger is not None:
        # The first commit created consumes the pending second parent,
        # whether or not the push then lands (§10.8).
        try:
            ledger.clear_merge_pending(attempt_id)
        except Exception:
            pass
    # The commit carries exactly the pre-commit tree's content, so it inherits
    # that tree's validation result (§10.4). A fact about content, recorded
    # once the commit exists whether or not the push then lands.
    registry.inherit_result(tree, revision)
    if pending is not None:
        try:
            new_tree = git.commit_tree(workspace, revision)
            changed = git.diff_names(workspace, pending.auto_tree, new_tree)
        except Exception as exc:
            return CommitPushOutcome(
                result="error",
                revision=revision,
                facts={"tree_id": tree, "branch": branch, "revision": revision},
                detail=f"error: could not inspect the commit: {exc!r}",
            )
        allowed = set(pending.conflicted_paths)
        extra = [path for path in changed if path not in allowed]
        if extra:
            return CommitPushOutcome(
                result="error",
                revision=revision,
                facts={"tree_id": tree, "branch": branch, "revision": revision, "paths": extra},
                detail=(
                    "error: the merge commit differs from the automatic merge "
                    f"outside the conflicted paths: {', '.join(extra)}"
                ),
            )
    try:
        violating = _workflows_violation_paths(git, workspace, revision, parents)
    except Exception as exc:
        return CommitPushOutcome(
            result="error",
            revision=revision,
            facts={"tree_id": tree, "branch": branch, "revision": revision},
            detail=f"error: could not inspect the commit: {exc!r}",
        )
    if violating:
        return CommitPushOutcome(
            result="error",
            revision=revision,
            facts={"tree_id": tree, "branch": branch, "revision": revision, "paths": violating},
            detail=(
                "error: the commit touches .github/workflows/ with content "
                f"not taken from a parent: {', '.join(violating)}; the push is refused"
            ),
        )
    if input_type == "issue":
        _intent(
            f"commit-and-push {attempt_id}: push {revision} to {branch}",
            {"operation": "commit-and-push", "branch": branch, "revision": revision},
        )
        try:
            git.push_issue_branch(workspace, branch)
        except Exception as exc:
            try:
                moved = git.remote_sha(branch)
            except Exception:
                moved = None
            if moved is not None and moved != head_before:
                return CommitPushOutcome(
                    result="error",
                    revision=revision,
                    facts={"tree_id": tree, "branch": branch, "revision": revision},
                    detail="error: the remote branch moved; the push is not performed",
                )
            _not_performed(f"error: {exc}")
            return CommitPushOutcome(
                result="error",
                revision=revision,
                facts={"tree_id": tree, "branch": branch, "revision": revision},
                detail=f"error: push failed: {exc}",
            )
        _receipt(revision)
        return CommitPushOutcome(
            result="pushed",
            revision=revision,
            facts={"tree_id": tree, "branch": branch, "revision": revision},
            detail=f"pushed: {revision} to {branch}",
        )
    wanted = expected_head
    _intent(
        f"commit-and-push {attempt_id}: push {revision} to {branch}",
        {
            "operation": "commit-and-push",
            "branch": branch,
            "revision": revision,
            "expected_head": wanted or "",
        },
    )
    try:
        git.push_ref(workspace, revision, branch)
    except Exception as exc:
        try:
            moved = git.remote_sha(branch)
        except Exception:
            moved = None
        if wanted is not None and moved != wanted:
            _not_performed("stale-head: the remote head moved")
            return CommitPushOutcome(
                result="stale-head",
                revision=revision,
                facts={"tree_id": tree, "branch": branch, "expected_head": wanted},
                detail="stale-head: the remote head differs from the Input Head",
            )
        _not_performed(f"error: {exc}")
        return CommitPushOutcome(
            result="error",
            revision=revision,
            facts={"tree_id": tree, "branch": branch, "revision": revision},
            detail=f"error: push failed: {exc}",
        )
    _receipt(revision)
    return CommitPushOutcome(
        result="pushed",
        revision=revision,
        facts={
            "tree_id": tree,
            "branch": branch,
            "revision": revision,
            "input_head": revision,
        },
        detail=f"pushed: {revision} to {branch}; the Input Head advances",
    )


# --- update-from-base (§10.8) ----------------------------------------------------


@dataclass(frozen=True)
class UpdateFromBaseOutcome:
    """One ``update-from-base`` Step Execution's end (§10.8, ADR 0034)."""

    result: UpdateFromBaseResultName
    base_tip: str | None
    facts: dict[str, object]
    detail: str


def run_update_from_base(
    *,
    workspace: Path,
    attempt_id: str,
    input_type: str,
    base_tip: str | None,
    author_name: str,
    author_email: str,
    git: CommitPushGitOps,
    ledger: RunLedger | None = None,
    step_id: str = "update-from-base",
    seq: int = 1,
) -> UpdateFromBaseOutcome:
    """Merge the pinned Base Tip into the workspace without committing (§10.8).

    ``pull-request`` Workflows only. Merges the pinned **Base Tip** — never
    the base branch's current tip — with git's default merge strategy, no
    rerere and no strategy options. Ends ``up-to-date`` where the Base Tip
    is already an ancestor of ``HEAD`` (nothing changes), ``merged`` where
    the merge applied cleanly, or ``conflicted`` where it stopped on
    conflicts (the merged content stays in the working tree with standard
    conflict markers, and the facts list the conflicted paths).

    After ``merged`` or ``conflicted`` the runtime clears git's own merge
    state (``MERGE_HEAD``, unmerged index entries) — the parent is kept in
    the Run Ledger instead — and records the automatic merge result's tree
    plus the Base Tip as the pending second parent, so the strict
    merge-commit rule of ``commit-and-push`` and recovery can see them. A
    second call while a parent is pending ends ``error`` ("merge already
    pending"). Performs no external write: no Effect Intent, no push.
    """
    if input_type != "pull-request":
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=None,
            facts={"input_type": input_type},
            detail=f"error: update-from-base is available only in pull-request Workflows (this one is {input_type!r})",
        )
    if not base_tip:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=None,
            facts={},
            detail="error: the Attempt pins no Base Tip to merge",
        )
    if ledger is not None:
        try:
            existing = ledger.get_merge_pending(attempt_id)
        except Exception as exc:
            return UpdateFromBaseOutcome(
                result="error",
                base_tip=base_tip,
                facts={"base_tip": base_tip},
                detail=f"error: could not read the pending merge parent: {exc!r}",
            )
        if existing is not None:
            return UpdateFromBaseOutcome(
                result="error",
                base_tip=base_tip,
                facts={"base_tip": base_tip},
                detail="error: merge already pending; commit-and-push must consume it first",
            )
    try:
        head = git.head_sha(workspace)
    except Exception as exc:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=base_tip,
            facts={"base_tip": base_tip},
            detail=f"error: could not read HEAD: {exc!r}",
        )
    try:
        present = git.object_exists(workspace, base_tip)
    except Exception as exc:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=base_tip,
            facts={"base_tip": base_tip},
            detail=f"error: could not look up the Base Tip: {exc!r}",
        )
    if not present:
        try:
            git.fetch_revision(workspace, base_tip)
        except Exception as exc:
            return UpdateFromBaseOutcome(
                result="error",
                base_tip=base_tip,
                facts={"base_tip": base_tip},
                detail=f"error: could not fetch the Base Tip: {exc}",
            )
        try:
            present = git.object_exists(workspace, base_tip)
        except Exception as exc:
            return UpdateFromBaseOutcome(
                result="error",
                base_tip=base_tip,
                facts={"base_tip": base_tip},
                detail=f"error: could not look up the Base Tip: {exc!r}",
            )
        if not present:
            return UpdateFromBaseOutcome(
                result="error",
                base_tip=base_tip,
                facts={"base_tip": base_tip},
                detail="error: the Base Tip is not available in the workspace",
            )
    try:
        if git.is_ancestor(workspace, base_tip, head):
            return UpdateFromBaseOutcome(
                result="up-to-date",
                base_tip=base_tip,
                facts={"base_tip": base_tip, "head": head},
                detail="up-to-date: the Base Tip is already an ancestor of HEAD",
            )
    except Exception as exc:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=base_tip,
            facts={"base_tip": base_tip},
            detail=f"error: could not compare the Base Tip with HEAD: {exc!r}",
        )
    try:
        git.merge_tip(
            workspace, base_tip, author_name=author_name, author_email=author_email
        )
    except Exception as exc:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=base_tip,
            facts={"base_tip": base_tip},
            detail=f"error: the merge failed: {exc}",
        )
    try:
        conflicted = git.unmerged_paths(workspace)
    except Exception as exc:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=base_tip,
            facts={"base_tip": base_tip},
            detail=f"error: could not read the merge outcome: {exc!r}",
        )
    try:
        git.stage_all(workspace)
        auto_tree = git.write_tree(workspace)
        git.clear_merge_state(workspace)
    except Exception as exc:
        return UpdateFromBaseOutcome(
            result="error",
            base_tip=base_tip,
            facts={"base_tip": base_tip},
            detail=f"error: could not record the automatic merge result: {exc!r}",
        )
    if ledger is not None:
        try:
            ledger.record_merge_pending(
                attempt_id,
                base_tip=base_tip,
                auto_tree=auto_tree,
                conflicted_paths=conflicted,
            )
        except Exception as exc:
            return UpdateFromBaseOutcome(
                result="error",
                base_tip=base_tip,
                facts={"base_tip": base_tip},
                detail=f"error: could not record the pending merge parent: {exc!r}",
            )
    if conflicted:
        return UpdateFromBaseOutcome(
            result="conflicted",
            base_tip=base_tip,
            facts={"base_tip": base_tip, "conflicted_paths": list(conflicted)},
            detail=(
                "conflicted: the merge stopped on conflicts in "
                f"{', '.join(conflicted)}"
            ),
        )
    return UpdateFromBaseOutcome(
        result="merged",
        base_tip=base_tip,
        facts={"base_tip": base_tip},
        detail="merged: the Base Tip merged cleanly; the merge is uncommitted",
    )


# --- Graph integration --------------------------------------------------------


@dataclass(frozen=True)
class OperationExecution:
    """One local operation's end, for the graph node to commit (§13.2)."""

    result: str
    payload: dict[str, object]
    facts: dict[str, object]


class OperationRunner(Protocol):
    """Run one ``operation`` step's local executor (S7a seam for the graph)."""

    def run(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> OperationExecution:
        """Answer one Step Execution; raise :class:`GateFailed` on Drift."""
        ...


LOCAL_OPERATIONS = frozenset(
    {"check-readiness", "run-validation", "commit-and-push", "update-from-base"}
)
"""The local operations this slice executes for real (§10.2–§10.4, §10.8)."""


@dataclass
class LocalOperationRunner:
    """The production S7a :class:`OperationRunner` (§10.1–§10.4, §10.8, §13.4).

    Pins the Validation Contract from the start commit on first use — an
    Attempt that edits the profile changes the contract for later Attempts,
    never its own — records every ``passed`` validation by tree identity for
    ``requires``, and carries the pull-request compare-and-swap head, which
    an own landed push advances (§7.2), plus the pinned Base Tip, which
    ``update-from-base`` merges (§10.8).
    """

    workspace: Path
    start_commit: str
    matrix: SupportedToolchainMatrix
    service_env: Mapping[str, str]
    prober: ServiceProber
    runner: CommandRunner
    evidence_dir: Path
    author_name: str
    author_email: str
    attempt_branch: str
    input_type: str
    head_branch: str | None = None
    expected_head: str | None = None
    base_tip: str | None = None
    input_number: int = 0
    gate: GateChecker | None = None
    git: CommitPushGitOps | None = None
    remote_url: str | None = None
    helper: GitCredentialHelper | None = None
    redact: Redactable = None
    profile_text_override: str | None = None
    _profile: ProjectProfile | None = field(default=None, init=False, repr=False)
    _profile_loaded: bool = field(default=False, init=False, repr=False)
    validation_registry: ValidationRegistry = field(default_factory=ValidationRegistry)

    def _pinned_profile(self) -> ProjectProfile | None:
        if self._profile_loaded:
            return self._profile
        self._profile_loaded = True
        text = self.profile_text_override
        if text is None:
            try:
                text = read_profile_at_commit(self.workspace, self.start_commit)
            except GitFailure:
                text = None
            sources = (f"{self.start_commit}:{PROFILE_RELPATH}",)
        else:
            sources = ("workflow-configuration",)
        if text is None:
            return None
        resolution = evaluate_readiness(
            ProfileSources(
                profile_yaml_text=text,
                sources_searched=sources,
            ),
            self.matrix,
            self.service_env,
            self.prober,
        )
        if isinstance(resolution, Ready):
            self._profile = resolution.profile
            return self._profile
        return None

    def _git_ops(self) -> CommitPushGitOps:
        if self.git is not None:
            return self.git
        if self.remote_url is None:
            raise ValueError("LocalOperationRunner needs `git` or `remote_url`")
        return LiveCommitPushGitOps(
            self.remote_url, helper=self.helper, redact=self.redact
        )

    def run(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> OperationExecution:
        """Run one local ``operation`` step (§10.1–§10.4)."""
        assert step.operation in LOCAL_OPERATIONS, (
            f"LocalOperationRunner cannot run {step.operation!r}"
        )
        if step.operation == "check-readiness":
            text = self.profile_text_override
            if text is None:
                try:
                    text = read_profile_at_commit(self.workspace, self.start_commit)
                except GitFailure:
                    text = None
                outcome = run_check_readiness(
                    workspace=self.workspace,
                    profile_text=text,
                    matrix=self.matrix,
                    service_env=self.service_env,
                    prober=self.prober,
                    runner=self.runner,
                    evidence_dir=self.evidence_dir,
                )
            else:
                try:
                    ignored = (
                        read_profile_at_commit(self.workspace, self.start_commit)
                        is not None
                    )
                except GitFailure:
                    ignored = False
                outcome = run_check_readiness(
                    workspace=self.workspace,
                    profile_text=text,
                    matrix=self.matrix,
                    service_env=self.service_env,
                    prober=self.prober,
                    runner=self.runner,
                    evidence_dir=self.evidence_dir,
                    profile_source="workflow-configuration",
                    profile_origin="workflow-configuration",
                    repository_profile_ignored=ignored,
                )
            if outcome.profile is not None:
                self._profile = outcome.profile
                self._profile_loaded = True
            return OperationExecution(
                result=outcome.result,
                payload={"result": outcome.result, "detail": outcome.detail},
                facts={**outcome.facts, "detail": outcome.detail},
            )
        if step.operation == "run-validation":
            profile = self._pinned_profile()
            if profile is None:
                return OperationExecution(
                    result="failed",
                    payload={"result": "failed"},
                    facts={"error": "no pinned Validation Contract for this Attempt"},
                )
            validation = run_run_validation(
                workspace=self.workspace,
                profile=profile,
                runner=self.runner,
                evidence_dir=self.evidence_dir,
            )
            self.validation_registry.record(
                validation.tree_id, validation.result == "passed"
            )
            return OperationExecution(
                result=validation.result,
                payload={"result": validation.result, "tree_id": validation.tree_id},
                facts={**validation.facts, "detail": validation.detail},
            )
        if step.operation == "update-from-base":
            outcome_base = run_update_from_base(
                workspace=self.workspace,
                attempt_id=attempt_id,
                input_type=self.input_type,
                base_tip=self.base_tip,
                author_name=self.author_name,
                author_email=self.author_email,
                git=self._git_ops(),
                ledger=ledger,
                step_id=step.id,
                seq=seq,
            )
            payload_base: dict[str, object] = {"result": outcome_base.result}
            if outcome_base.base_tip is not None:
                payload_base["base_tip"] = outcome_base.base_tip
            return OperationExecution(
                result=outcome_base.result,
                payload=payload_base,
                facts={**outcome_base.facts, "detail": outcome_base.detail},
            )
        assert step.operation == "commit-and-push"
        branch = self.head_branch if self.input_type == "pull-request" else self.attempt_branch
        outcome_push = run_commit_and_push(
            workspace=self.workspace,
            attempt_id=attempt_id,
            input_type=self.input_type,
            branch=branch or self.attempt_branch,
            expected_head=self.expected_head,
            author_name=self.author_name,
            author_email=self.author_email,
            with_requires=step.with_requires,
            validation_registry=self.validation_registry,
            gate=self.gate,
            git=self._git_ops(),
            ledger=ledger,
            step_id=step.id,
            seq=seq,
        )
        if outcome_push.result == "pushed" and self.input_type == "pull-request":
            if outcome_push.revision is not None:
                self.expected_head = outcome_push.revision
        payload: dict[str, object] = {"result": outcome_push.result}
        if outcome_push.revision is not None:
            payload["revision"] = outcome_push.revision
        return OperationExecution(
            result=outcome_push.result,
            payload=payload,
            facts={**outcome_push.facts, "detail": outcome_push.detail},
        )
