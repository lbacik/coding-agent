"""Publishing Runtime Operations: ``open-pull-request``, ``publish-review``
and ``post-comment`` (plus the publication-body template).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.1 (the
closed catalog and its preconditions: ``requires`` for the revision being
published, the Gate before a write), §10.5 (``open-pull-request``: the
fixed runtime title template over the Target Issue title, the ``document``
body, ``draft`` per ``with.draft``, the ready/draft closing keyword,
``already-open``), §10.6 (``publish-review`` through the GitHub review API
as a COMMENT event against the current Input Head, ``stale-head`` on a
moved head), §10.7 (``post-comment``: one comment on the input), §14.1
(every write has an Effect Intent keyed ``(Attempt, step, n, ordinal)``
with the rendered payload and expected target state, written after the Gate
immediately before the write, and an Effect Receipt) and §14.4 (the
publication-body template: the ``document`` verbatim, the provenance
footer, the Attempt Marker; only the ``document`` is truncated; the
rendered body is recorded with its Effect Intent).

Slice S7b of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: OP-7 … OP-12, OP-14 (with S6), OP-17 … OP-20, REC-4 (with S2).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.github.errors import CredentialFailure
from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.port import InputType, TrackedInput
from coding_agent.ledger.store import RunLedger, step_effect_marker
from coding_agent.profile.services import ServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix, load_toolchain_matrix
from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.runtime.step_operations import (
    CommitPushGitOps,
    GateChecker,
    GateFailed,
    LocalOperationRunner,
    ValidationRegistry,
)
from coding_agent.runtime.step_publication import (
    GITHUB_BODY_CHAR_LIMIT,
    PUBLISHING_OPERATIONS,
    DocumentResolutionError,
    PublishingOperationRunner,
    closing_keyword_line,
    provenance_footer,
    pull_request_title_for_issue,
    render_comment_body,
    render_open_pull_request_body,
    render_review_body,
    resolve_document,
    run_open_pull_request,
    run_post_comment,
    run_publish_review,
    truncate_document,
)
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.workflow.v2_schema import (
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.fake_step_tracker import FakeStepTracker

ATTEMPT = "implement#7/1"
WORKFLOW_ID = "implement"
CONFIG_DIGEST = "cfg-digest-1"


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
    """A ``CommitPushGitOps`` fake over a real repo (publishing reads HEAD)."""

    def __init__(self, remote: dict[str, str] | None = None) -> None:
        self.remote: dict[str, str] = dict(remote or {})

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
        raise GitFailure("FakeGitOps never pushes in publication tests")

    def push_ref(self, workspace: Path, local_sha: str, branch: str) -> None:
        raise GitFailure("FakeGitOps never pushes in publication tests")


def _seed_document(
    ledger: RunLedger,
    attempt_id: str,
    text: str,
    *,
    step_id: str = "process",
    result: str = "done",
    field: str = "document",
    seq: int = 1,
) -> str:
    """Plant one Step Execution carrying the publication ``document``."""
    if not ledger.has_attempt(attempt_id):
        ledger.open_attempt(
            attempt_id, effective_configuration={"workflow": WORKFLOW_ID}
        )
    ledger.open_step_execution(attempt_id, step_id, seq)
    ledger.end_step_execution(
        attempt_id, step_id, seq, result=result, payload={field: text}
    )
    return f"{step_id}.{seq}.{field}"


def _ok_gate() -> GateResult:
    return GateResult(status="ok", reason="")


def _drift_gate() -> GateResult:
    return GateResult(status="drift", reason="content-changed")


def _withdrawn_gate() -> GateResult:
    return GateResult(status="withdrawn", reason="selection-label-removed")


def _open_pr_kwargs(
    repo: Path,
    tracker: FakeStepTracker,
    ledger: RunLedger,
    text: str,
    *,
    gate: GateChecker | None = None,
    claim_active: bool = True,
    with_requires: tuple[tuple[str, str], ...] = (),
    registry: ValidationRegistry | None = None,
    draft: bool = False,
    step_id: str = "open-pr",
    seq: int = 1,
    with_body: str | None = "process.done.document",
) -> dict[str, object]:
    _seed_document(ledger, ATTEMPT, text)
    return {
        "workspace": repo,
        "attempt_id": ATTEMPT,
        "workflow_id": WORKFLOW_ID,
        "input_number": 7,
        "issue_title": "Add a greeting",
        "attempt_branch": "agent/implement/7/1-greeting",
        "base_branch": "main",
        "base_revision": "b" * 40,
        "config_digest": CONFIG_DIGEST,
        "with_body": with_body,
        "draft": draft,
        "with_requires": with_requires,
        "validation_registry": registry,
        "gate": gate,
        "claim_active": claim_active,
        "git": FakeGitOps(),
        "tracker": tracker,
        "ledger": ledger,
        "step_id": step_id,
        "seq": seq,
    }


# --- Template unit tests (OP-17, OP-18) ----------------------------------------


def test_title_is_the_issue_title_verbatim() -> None:
    """OP-7 (§10.5): the title is the fixed runtime template over the issue."""
    assert pull_request_title_for_issue("Add a greeting") == "Add a greeting"


def test_closing_keyword_ready_and_draft() -> None:
    """OP-17 (§10.5): ready closes, draft only names the issue."""
    assert closing_keyword_line(7, draft=False) == "Closes #7"
    assert closing_keyword_line(7, draft=True) == "Part of #7"


def test_open_pr_body_is_keyword_document_footer_marker() -> None:
    """OP-18 (§14.4): keyword, verbatim document, footer, marker — in order."""
    marker = step_effect_marker(ATTEMPT, "open-pr", 1, 0, kind="open-pull-request")
    body = render_open_pull_request_body(
        document="Implements the greeting.",
        artifact_ref="process.1.document",
        attempt_id=ATTEMPT,
        workflow_id=WORKFLOW_ID,
        step_id="open-pr",
        config_digest=CONFIG_DIGEST,
        issue_number=7,
        draft=False,
        base_revision="b" * 40,
        effect_marker=marker,
    )
    assert body.startswith("Closes #7\n\nImplements the greeting.\n\n")
    assert "Attempt: implement#7/1" in body
    assert "Workflow: implement" in body
    assert "Step: open-pr" in body
    assert f"Configuration: {CONFIG_DIGEST}" in body
    assert f"Base Revision: `{'b' * 40}`" in body
    assert body.endswith(marker)
    footer = provenance_footer(
        attempt_id=ATTEMPT,
        workflow_id=WORKFLOW_ID,
        step_id="open-pr",
        config_digest=CONFIG_DIGEST,
        revision_label="Base Revision",
        revision="b" * 40,
    )
    assert footer in body


def test_review_body_carries_the_reviewed_head() -> None:
    """OP-18 (§14.4): the review footer carries the reviewed Input Head."""
    marker = step_effect_marker(ATTEMPT, "publish", 1, 0, kind="publish-review")
    body = render_review_body(
        document="Looks good.",
        artifact_ref="review-step.1.document",
        attempt_id=ATTEMPT,
        workflow_id="review",
        step_id="publish",
        config_digest=CONFIG_DIGEST,
        input_head="h" * 40,
        effect_marker=marker,
    )
    assert body.startswith("Looks good.\n\n")
    assert f"Reviewed head: `{'h' * 40}`" in body
    assert body.endswith(marker)


def test_comment_body_carries_no_revision() -> None:
    """OP-18 (§14.4): the comment footer carries no revision."""
    marker = step_effect_marker(ATTEMPT, "explain", 1, 0, kind="post-comment")
    body = render_comment_body(
        document="Blocked on credentials.",
        artifact_ref="process.1.document",
        attempt_id=ATTEMPT,
        workflow_id=WORKFLOW_ID,
        step_id="explain",
        config_digest=CONFIG_DIGEST,
        effect_marker=marker,
    )
    assert body.startswith("Blocked on credentials.\n\n")
    assert "Revision" not in body
    assert "Reviewed head" not in body
    assert body.endswith(marker)


# --- OP-7: open-pull-request ----------------------------------------------------


def test_open_pr_opened_from_the_pushed_revision(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-7 (§10.5): opened from HEAD with the runtime title, draft flag, body."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    outcome = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Implements the greeting.")  # type: ignore[arg-type]
    )
    assert outcome.result == "opened"
    assert outcome.revision == head
    assert outcome.pull_number is not None
    assert outcome.pull_url is not None
    assert tracker.pull_requests[0].title == "Add a greeting"
    assert tracker.pull_requests[0].draft is False
    assert "Closes #7" in tracker.pull_requests[0].body
    assert "Implements the greeting." in tracker.pull_requests[0].body
    record = ledger.get_step_effect(ATTEMPT, "open-pr", 1, 0)
    assert record is not None
    assert record.status == "receipt"
    assert record.remote_id == str(outcome.pull_number)
    assert "Implements the greeting." in record.rendered_payload


def test_open_pr_draft_flag_and_keyword(tmp_path: Path, ledger: RunLedger) -> None:
    """OP-7, OP-17 (§10.5): ``with.draft`` opens a draft with ``Part of``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    outcome = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Draft work.", draft=True)  # type: ignore[arg-type]
    )
    assert outcome.result == "opened"
    assert tracker.pull_requests[0].draft is True
    assert "Part of #7" in tracker.pull_requests[0].body
    assert "Closes #7" not in tracker.pull_requests[0].body


def test_open_pr_already_open_is_idempotent(tmp_path: Path, ledger: RunLedger) -> None:
    """OP-7 (§10.5): a second run finds the Attempt PR and opens nothing new."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    first = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Implements the greeting.")  # type: ignore[arg-type]
    )
    assert first.result == "opened"
    second = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Implements the greeting.")  # type: ignore[arg-type]
    )
    assert second.result == "already-open"
    assert second.pull_number == first.pull_number
    assert len(tracker.pull_requests) == 1
    record = ledger.get_step_effect(ATTEMPT, "open-pr", 1, 0)
    assert record is not None
    assert record.status == "receipt"


def test_open_pr_human_closed_is_reported_never_recreated(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-7, REC-9 (§14.2): a human-closed own PR is never re-created."""
    from dataclasses import replace

    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    first = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Implements the greeting.")  # type: ignore[arg-type]
    )
    assert first.result == "opened"
    tracker.pull_requests[0] = replace(tracker.pull_requests[0], state="closed")
    second = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Implements the greeting.")  # type: ignore[arg-type]
    )
    assert second.result == "already-open"
    assert second.pull_number == first.pull_number
    assert len(tracker.pull_requests) == 1


def test_open_pr_foreign_marker_pull_request_is_ignored(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-14 (§14): a human PR carrying our marker is not our receipt."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    marker = step_effect_marker(ATTEMPT, "open-pr", 1, 0, kind="open-pull-request")
    tracker.seed_foreign_marker_pull_request(
        "agent/implement/7/1-greeting", marker
    )
    outcome = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "Implements the greeting.")  # type: ignore[arg-type]
    )
    assert outcome.result == "opened"
    assert len(tracker.pull_requests) == 2


# --- OP-8: publish-review -------------------------------------------------------


def _publish_kwargs(
    tracker: FakeStepTracker,
    ledger: RunLedger,
    text: str,
    *,
    expected_head: str | None = "h" * 40,
    input_number: int = 31,
    gate: GateChecker | None = None,
    claim_active: bool = True,
    with_requires: tuple[tuple[str, str], ...] = (),
    registry: ValidationRegistry | None = None,
    step_id: str = "publish",
    seq: int = 1,
    with_body: str | None = "process.done.document",
) -> dict[str, object]:
    _seed_document(ledger, ATTEMPT, text)
    tracker.inputs[input_number] = TrackedInput(
        number=input_number,
        input_type="pull-request",
        title="a title",
        body="a body",
        is_open=True,
        labels=("agent:review",),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        head_sha="h" * 40,
        head_branch="feature",
        head_repo="octocat/sandbox",
        base_branch="main",
    )
    return {
        "workspace": Path("/tmp"),
        "attempt_id": ATTEMPT,
        "workflow_id": "review",
        "input_number": input_number,
        "expected_head": expected_head,
        "config_digest": CONFIG_DIGEST,
        "with_body": with_body,
        "with_requires": with_requires,
        "validation_registry": registry,
        "gate": gate,
        "claim_active": claim_active,
        "tracker": tracker,
        "ledger": ledger,
        "step_id": step_id,
        "seq": seq,
    }


def test_publish_review_published_against_the_input_head(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-8 (§10.6): a COMMENT review against the current Input Head."""
    del tmp_path
    tracker = FakeStepTracker(agent_id=1001)
    outcome = run_publish_review(
        **_publish_kwargs(tracker, ledger, "Looks good.")  # type: ignore[arg-type]
    )
    assert outcome.result == "published"
    assert outcome.head == "h" * 40
    assert outcome.review_id is not None
    assert tracker.reviews[0].commit_id == "h" * 40
    assert "Looks good." in tracker.reviews[0].body
    assert f"Reviewed head: `{'h' * 40}`" in tracker.reviews[0].body
    record = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert record is not None
    assert record.status == "receipt"
    assert record.remote_id == str(outcome.review_id)


def test_publish_review_stale_head_before_any_write(ledger: RunLedger) -> None:
    """OP-8 (§10.6): a moved head yields ``stale-head`` with no write."""
    tracker = FakeStepTracker(agent_id=1001)
    outcome = run_publish_review(
        **_publish_kwargs(  # type: ignore[arg-type]
            tracker, ledger, "Looks good.", expected_head="e" * 40
        )
    )
    assert outcome.result == "stale-head"
    assert outcome.head == "h" * 40
    assert tracker.reviews == []
    record = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert record is not None
    assert record.status == "not-performed"


def test_publish_review_idempotent_by_marker(ledger: RunLedger) -> None:
    """OP-8 (§14): a second run finds the Attempt review and writes nothing new."""
    tracker = FakeStepTracker(agent_id=1001)
    first = run_publish_review(
        **_publish_kwargs(tracker, ledger, "Looks good.")  # type: ignore[arg-type]
    )
    assert first.result == "published"
    second = run_publish_review(
        **_publish_kwargs(tracker, ledger, "Looks good.")  # type: ignore[arg-type]
    )
    assert second.result == "published"
    assert second.review_id == first.review_id
    assert len(tracker.reviews) == 1


class _RaceTracker(FakeStepTracker):
    """A head that moves between the Gate read and the write (OP-12)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._reads = 0

    def fetch_input(self, number: int, input_type: InputType) -> TrackedInput:
        self._reads += 1
        head = "h" * 40 if self._reads == 1 else "n" * 40
        return TrackedInput(
            number=number,
            input_type=input_type,
            title="a title",
            body="a body",
            is_open=True,
            labels=(),
            assignees=(),
            created_at="2026-10-01T00:00:00Z",
            head_sha=head,
            head_branch="feature",
            head_repo="octocat/sandbox",
            base_branch="main",
        )

    def publish_review(self, number: int, *, commit_id: str, body: str):  # type: ignore[no-untyped-def]
        from coding_agent.inputs.port import InputReview

        raise RuntimeError("push raced: remote head moved")


def test_publish_review_head_moved_at_write_time_is_stale_head(
    ledger: RunLedger,
) -> None:
    """OP-12 (§10.6): the write-time race settles ``not-performed``."""
    tracker = _RaceTracker(agent_id=1001)
    outcome = run_publish_review(
        **_publish_kwargs(  # type: ignore[arg-type]
            tracker, ledger, "Looks good.", expected_head="h" * 40
        )
    )
    assert outcome.result == "stale-head"
    assert outcome.head == "n" * 40
    record = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert record is not None
    assert record.status == "not-performed"


# --- OP-9: post-comment -----------------------------------------------------------


def _comment_kwargs(
    tracker: FakeStepTracker,
    ledger: RunLedger,
    text: str,
    *,
    input_type: InputType = "issue",
    input_number: int = 7,
    gate: GateChecker | None = None,
    claim_active: bool = True,
    with_requires: tuple[tuple[str, str], ...] = (),
    registry: ValidationRegistry | None = None,
    step_id: str = "explain",
    seq: int = 1,
    with_body: str | None = "process.done.document",
) -> dict[str, object]:
    _seed_document(ledger, ATTEMPT, text)
    return {
        "workspace": Path("/tmp"),
        "attempt_id": ATTEMPT,
        "workflow_id": WORKFLOW_ID,
        "input_number": input_number,
        "input_type": input_type,
        "config_digest": CONFIG_DIGEST,
        "with_body": with_body,
        "with_requires": with_requires,
        "validation_registry": registry,
        "gate": gate,
        "claim_active": claim_active,
        "tracker": tracker,
        "ledger": ledger,
        "step_id": step_id,
        "seq": seq,
    }


@pytest.mark.parametrize("input_type", ["issue", "pull-request"])
def test_post_comment_posts_one_comment(
    ledger: RunLedger, input_type: InputType
) -> None:
    """OP-9 (§10.7): one comment on the input, body from the ``document``."""
    tracker = FakeStepTracker(agent_id=1001)
    outcome = run_post_comment(
        **_comment_kwargs(  # type: ignore[arg-type]
            tracker, ledger, "Blocked on credentials.", input_type=input_type
        )
    )
    assert outcome.result == "posted"
    assert outcome.comment_id is not None
    assert len(tracker.comments) == 1
    assert "Blocked on credentials." in tracker.comments[0].body
    record = ledger.get_step_effect(ATTEMPT, "explain", 1, 0)
    assert record is not None
    assert record.status == "receipt"
    assert record.remote_id == str(outcome.comment_id)


def test_post_comment_idempotent_by_marker(ledger: RunLedger) -> None:
    """OP-9 (§14): a second run finds the Attempt comment and writes nothing."""
    tracker = FakeStepTracker(agent_id=1001)
    first = run_post_comment(
        **_comment_kwargs(tracker, ledger, "Blocked.")  # type: ignore[arg-type]
    )
    second = run_post_comment(
        **_comment_kwargs(tracker, ledger, "Blocked.")  # type: ignore[arg-type]
    )
    assert second.result == "posted"
    assert second.comment_id == first.comment_id
    assert len(tracker.comments) == 1


# --- OP-10: requires --------------------------------------------------------------


def test_requires_unmet_before_any_write_all_ops(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-10 (§10.1): evidence from another tree never satisfies ``requires``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    registry = ValidationRegistry()
    registry.record("some-earlier-tree", True)
    requires = (("run-validation", "passed"),)

    pr_outcome = run_open_pull_request(
        **_open_pr_kwargs(  # type: ignore[arg-type]
            repo,
            tracker,
            ledger,
            "doc",
            with_requires=requires,
            registry=registry,
            step_id="open-pr",
            seq=1,
        )
    )
    assert pr_outcome.result == "requirement-unmet"

    review_outcome = run_publish_review(
        **_publish_kwargs(  # type: ignore[arg-type]
            tracker,
            ledger,
            "doc",
            with_requires=requires,
            registry=registry,
            step_id="publish",
            seq=1,
        )
    )
    assert review_outcome.result == "requirement-unmet"

    comment_outcome = run_post_comment(
        **_comment_kwargs(  # type: ignore[arg-type]
            tracker,
            ledger,
            "doc",
            with_requires=requires,
            registry=registry,
            step_id="explain",
            seq=1,
        )
    )
    assert comment_outcome.result == "requirement-unmet"

    assert tracker.pull_requests == []
    assert tracker.reviews == []
    assert tracker.comments == []
    assert ledger.get_step_effect(ATTEMPT, "open-pr", 1, 0) is None
    assert ledger.get_step_effect(ATTEMPT, "publish", 1, 0) is None
    assert ledger.get_step_effect(ATTEMPT, "explain", 1, 0) is None


# --- OP-11: Gate / Claim ------------------------------------------------------------


def test_gate_and_claim_hold_for_every_publishing_operation(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-11 (§10.1): Drift, withdrawal and no Claim raise before any write."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    for gate_fn, status in (
        (_drift_gate, "drift"),
        (_withdrawn_gate, "withdrawn"),
    ):
        tracker = FakeStepTracker(agent_id=1001)
        with pytest.raises(GateFailed) as excinfo:
            run_open_pull_request(
                **_open_pr_kwargs(repo, tracker, ledger, "doc", gate=gate_fn)  # type: ignore[arg-type]
            )
        assert excinfo.value.status == status
        with pytest.raises(GateFailed):
            run_publish_review(
                **_publish_kwargs(tracker, ledger, "doc", gate=gate_fn)  # type: ignore[arg-type]
            )
        with pytest.raises(GateFailed):
            run_post_comment(
                **_comment_kwargs(tracker, ledger, "doc", gate=gate_fn)  # type: ignore[arg-type]
            )
        assert tracker.pull_requests == []
        assert tracker.reviews == []
        assert tracker.comments == []
    tracker = FakeStepTracker(agent_id=1001)
    with pytest.raises(GateFailed) as excinfo:
        run_open_pull_request(
            **_open_pr_kwargs(repo, tracker, ledger, "doc", claim_active=False)  # type: ignore[arg-type]
        )
    assert excinfo.value.status == "withdrawn"
    with pytest.raises(GateFailed):
        run_publish_review(
            **_publish_kwargs(tracker, ledger, "doc", claim_active=False)  # type: ignore[arg-type]
        )
    with pytest.raises(GateFailed):
        run_post_comment(
            **_comment_kwargs(tracker, ledger, "doc", claim_active=False)  # type: ignore[arg-type]
        )


def test_credential_failure_propagates(tmp_path: Path, ledger: RunLedger) -> None:
    """REC-17 (§16.3): a Credential Failure is never routed or swallowed."""

    class _BadTracker(FakeStepTracker):
        def find_pull_request(self, head_branch: str, marker: str):  # type: ignore[no-untyped-def]
            raise CredentialFailure("GET", "/repos/o/s/pulls")

    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = _BadTracker(agent_id=1001)
    with pytest.raises(CredentialFailure):
        run_open_pull_request(
            **_open_pr_kwargs(repo, tracker, ledger, "doc")  # type: ignore[arg-type]
        )


# --- Malformed documents ------------------------------------------------------------


def test_missing_document_is_error_without_a_write(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """§10.1: a ``with.body`` naming no string document ends ``error``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    if not ledger.has_attempt(ATTEMPT):
        ledger.open_attempt(
            attempt_id=ATTEMPT, effective_configuration={"workflow": WORKFLOW_ID}
        )
    outcome = run_open_pull_request(
        **_open_pr_kwargs(  # type: ignore[arg-type]
            repo, tracker, ledger, "unused", with_body="process.done.missing"
        )
    )
    assert outcome.result == "error"
    assert tracker.pull_requests == []

    with pytest.raises(DocumentResolutionError):
        resolve_document(ledger, ATTEMPT, None)
    with pytest.raises(DocumentResolutionError):
        resolve_document(ledger, ATTEMPT, "not-a-reference")


# --- OP-19, OP-20: truncation and verbatim documents ----------------------------------


def test_huge_document_truncates_only_the_document() -> None:
    """OP-19 (§14.4): the keyword, footer and marker survive truncation."""
    document = "x" * 70_000
    marker = step_effect_marker(ATTEMPT, "open-pr", 1, 0, kind="open-pull-request")
    footer = provenance_footer(
        attempt_id=ATTEMPT,
        workflow_id=WORKFLOW_ID,
        step_id="open-pr",
        config_digest=CONFIG_DIGEST,
        revision_label="Base Revision",
        revision="b" * 40,
    )
    body = render_open_pull_request_body(
        document=document,
        artifact_ref="process.1.document",
        attempt_id=ATTEMPT,
        workflow_id=WORKFLOW_ID,
        step_id="open-pr",
        config_digest=CONFIG_DIGEST,
        issue_number=7,
        draft=False,
        base_revision="b" * 40,
        effect_marker=marker,
    )
    assert len(body) <= GITHUB_BODY_CHAR_LIMIT
    assert body.startswith("Closes #7\n\n")
    assert footer in body
    assert body.endswith(marker)
    assert "truncated" in body
    assert "process.1.document" in body
    again = render_open_pull_request_body(
        document=document,
        artifact_ref="process.1.document",
        attempt_id=ATTEMPT,
        workflow_id=WORKFLOW_ID,
        step_id="open-pr",
        config_digest=CONFIG_DIGEST,
        issue_number=7,
        draft=False,
        base_revision="b" * 40,
        effect_marker=marker,
    )
    assert again == body


def test_truncate_document_keeps_head_and_names_the_artifact() -> None:
    """OP-19 (§14.4): the head is kept with an explicit marker and pointer."""
    rendered = truncate_document(
        "y" * 100, artifact_ref="process.1.document", budget=10
    )
    assert rendered.startswith("y" * 10)
    assert "100 characters total" in rendered
    assert "90 omitted" in rendered
    assert "process.1.document" in rendered
    assert truncate_document("short", artifact_ref="r", budget=10) == "short"


def test_huge_document_end_to_end_records_rendered_body_with_intent(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-19, REC-4 (§14): the recorded intent carries the truncated body."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    outcome = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "z" * 70_000)  # type: ignore[arg-type]
    )
    assert outcome.result == "opened"
    stored = tracker.pull_requests[0].body
    assert len(stored) <= GITHUB_BODY_CHAR_LIMIT
    assert "Closes #7" in stored
    record = ledger.get_step_effect(ATTEMPT, "open-pr", 1, 0)
    assert record is not None
    assert stored in record.rendered_payload


def test_document_containing_a_closing_keyword_publishes_verbatim(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-20 (§14.4): the runtime never rewrites model-authored text."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    text = "Fixes the bug.\n\nCloses #12"
    outcome = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, text)  # type: ignore[arg-type]
    )
    assert outcome.result == "opened"
    stored = tracker.pull_requests[0].body
    assert "Closes #12" in stored
    assert stored.startswith("Closes #7\n\nFixes the bug.\n\nCloses #12")


# --- REC-4: intent and receipt ----------------------------------------------------------


def test_every_write_records_intent_beforehand_and_receipt_after(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """REC-4 (§14.1): the intent carries payload and target; the receipt lands."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    pr_outcome = run_open_pull_request(
        **_open_pr_kwargs(repo, tracker, ledger, "doc")  # type: ignore[arg-type]
    )
    review_outcome = run_publish_review(
        **_publish_kwargs(tracker, ledger, "doc")  # type: ignore[arg-type]
    )
    comment_outcome = run_post_comment(
        **_comment_kwargs(tracker, ledger, "doc")  # type: ignore[arg-type]
    )
    pr_record = ledger.get_step_effect(ATTEMPT, "open-pr", 1, 0)
    assert pr_record is not None
    assert pr_record.status == "receipt"
    assert pr_record.remote_id == str(pr_outcome.pull_number)
    assert "open-pull-request" in pr_record.rendered_payload

    review_record = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert review_record is not None
    assert review_record.status == "receipt"
    assert review_record.remote_id == str(review_outcome.review_id)

    comment_record = ledger.get_step_effect(ATTEMPT, "explain", 1, 0)
    assert comment_record is not None
    assert comment_record.status == "receipt"
    assert comment_record.remote_id == str(comment_outcome.comment_id)


# --- Graph integration ---------------------------------------------------------------------


class _FakeProber(ServiceProber):
    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        return True


class _QueueRunner(CommandRunner):
    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        raise AssertionError("no command should run in publication graph tests")


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


def _ceiling() -> V2Ceiling:
    return V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=60)


def _publishing_runner(
    tmp_path: Path,
    repo: Path,
    tracker: FakeStepTracker,
    *,
    input_type: str = "issue",
    expected_head: str | None = None,
) -> PublishingOperationRunner:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    local = LocalOperationRunner(
        workspace=repo,
        start_commit=_git("rev-parse", "HEAD", cwd=repo),
        matrix=_matrix(),
        service_env={},
        prober=_FakeProber(),
        runner=_QueueRunner(),
        evidence_dir=evidence_dir,
        author_name="agent",
        author_email="1+agent@users.noreply.github.com",
        attempt_branch="agent/implement/7/1-greeting",
        input_type=input_type,
        head_branch="feature" if input_type == "pull-request" else None,
        expected_head=expected_head,
        input_number=7,
        profile_text_override=None,
        git=FakeGitOps(),
    )
    return PublishingOperationRunner(
        local=local,
        tracker=tracker,
        workflow_id=WORKFLOW_ID,
        config_digest=CONFIG_DIGEST,
        issue_title="Add a greeting",
        base_branch="main",
        base_revision="b" * 40,
    )


def test_graph_runs_open_pr_for_real(tmp_path: Path, ledger: RunLedger) -> None:
    """S7b wiring: the graph consults the runner for publishing operations."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = FakeStepTracker(agent_id=1001)
    attempt_id = "implement#7/2"
    ledger.open_attempt(
        attempt_id, effective_configuration={"workflow": WORKFLOW_ID}
    )
    ledger.open_step_execution(attempt_id, "process", 1)
    ledger.end_step_execution(
        attempt_id, "process", 1, result="done", payload={"document": "Does it."}
    )
    step = V2Step(
        id="open-pr",
        operation="open-pull-request",
        with_body="process.done.document",
        routes={
            "opened": V2RouteTarget(end="completed"),
            "already-open": V2RouteTarget(end="completed"),
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="failed"),
        },
    )
    workflow = V2Workflow(
        id=WORKFLOW_ID,
        model="sonnet",
        input=V2Input(type="issue", label="agent:implement"),
        ceiling=_ceiling(),
        start="open-pr",
        steps={"open-pr": step},
    )
    assert "open-pull-request" in PUBLISHING_OPERATIONS
    runner = _publishing_runner(tmp_path, repo, tracker)
    outcome = run_workflow(
        workflow=workflow,
        attempt_id=attempt_id,
        ledger=ledger,
        operation_runner=runner,
    )
    assert outcome.outcome == "completed"
    assert len(tracker.pull_requests) == 1
    executions = ledger.list_step_executions(attempt_id)
    assert [(item.step_id, item.result) for item in executions] == [
        ("process", "done"),
        ("open-pr", "opened"),
    ]


def test_graph_runs_publish_review_and_stale_head_routes_failed(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OP-12 trace (§10.6): ``publish#1=stale-head`` routes to ``failed``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracker = _RaceTracker(agent_id=1001)
    attempt_id = "review#31/1"
    ledger.open_attempt(attempt_id, effective_configuration={"workflow": "review"})
    ledger.open_step_execution(attempt_id, "review-step", 1)
    ledger.end_step_execution(
        attempt_id,
        "review-step",
        1,
        result="reviewed",
        payload={"document": "Needs work."},
    )
    step = V2Step(
        id="publish",
        operation="publish-review",
        with_body="review-step.reviewed.document",
        routes={
            "published": V2RouteTarget(end="completed"),
            "stale-head": V2RouteTarget(end="failed"),
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="failed"),
        },
    )
    workflow = V2Workflow(
        id="review",
        model="sonnet",
        input=V2Input(type="pull-request", label="agent:review"),
        ceiling=_ceiling(),
        start="publish",
        steps={"publish": step},
    )
    runner = _publishing_runner(
        tmp_path, repo, tracker, input_type="pull-request", expected_head="h" * 40
    )
    outcome = run_workflow(
        workflow=workflow,
        attempt_id=attempt_id,
        ledger=ledger,
        operation_runner=runner,
    )
    assert outcome.outcome == "failed"
    executions = ledger.list_step_executions(attempt_id)
    assert ("publish", "stale-head") in [
        (item.step_id, item.result) for item in executions
    ]
