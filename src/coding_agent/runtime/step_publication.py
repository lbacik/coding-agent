"""Publishing Runtime Operations for the step-workflow runtime: ``open-pull-request``,
``publish-review`` and ``post-comment`` (plus the publication-body template).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.1 (the
closed catalog and its preconditions: ``requires`` for the revision being
published, the Gate before a write), §10.5 (``open-pull-request``: the fixed
runtime title template over the Target Issue title, the ``document`` body,
``draft`` per ``with.draft``, the ready/draft closing keyword,
``already-open``), §10.6 (``publish-review`` through the GitHub review API
as a COMMENT event against the current Input Head, ``stale-head`` on a moved
head), §10.7 (``post-comment``: one comment on the input), §14.1 (every
write has an Effect Intent keyed ``(Attempt, step, n, ordinal)`` with the
rendered payload and expected target state, written after the Gate
immediately before the write, and an Effect Receipt) and §14.4 (the
publication-body template: the ``document`` verbatim, the provenance
footer, the Attempt Marker; only the ``document`` is truncated; the
rendered body is recorded with its Effect Intent).

Slice S7b of ``docs/plan/step-workflow-implementation-plan.md``. Reuses the
renderers and the Attempt Marker in ``runtime/publication.py``, the
``ValidationRegistry``/``Gate``/``CommitPushGitOps`` seams in
``runtime/step_operations.py`` and the tracker port in ``inputs/port.py``.
Additive: until the S10 cutover the released Worker keeps running the MVP
runtime and nothing here is reachable from ``agent run``; the compiled
Workflow graph calls these executors only when a run injects a
:class:`PublishingOperationRunner`.

Matrix row ids: OP-7 … OP-12, OP-14 (with S6), OP-17 … OP-20, REC-4 (with S2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from coding_agent.github.errors import CredentialFailure
from coding_agent.inputs.port import (
    InputComment,
    InputPullRequest,
    InputReview,
    InputType,
    TrackedInput,
)
from coding_agent.ledger.store import RunLedger, step_effect_marker
from coding_agent.runtime.step_operations import (
    CommitPushGitOps,
    GateChecker,
    GateFailed,
    LocalOperationRunner,
    OperationExecution,
    ValidationRegistry,
    tree_identity,
)
from coding_agent.workflow.v2_schema import V2Step

PUBLISHING_OPERATIONS = frozenset({"open-pull-request", "publish-review", "post-comment"})
"""The S7b publishing operations this slice executes for real (§10.5–§10.7)."""

GITHUB_BODY_CHAR_LIMIT = 65_536
"""GitHub's body limit for pull requests, reviews and comments (§14.4, OP-19).

A rendered publication body never exceeds this: only the ``document`` is
truncated, with an explicit marker and a pointer to the Step Artifact, while
the closing keyword, the provenance footer and the Attempt Marker stay
intact.
"""

OPEN_PULL_REQUEST_KIND = "open-pull-request"
PUBLISH_REVIEW_KIND = "publish-review"
POST_COMMENT_KIND = "post-comment"

OpenPullRequestResultName = Literal["opened", "already-open", "requirement-unmet", "error"]
PublishReviewResultName = Literal["published", "stale-head", "requirement-unmet", "error"]
PostCommentResultName = Literal["posted", "requirement-unmet", "error"]


class PublicationTracker(Protocol):
    """The tracker surface the publishing operations need (all §14 lookups).

    ``StepTrackerPort`` implementers (the GitHub adapter, the in-process
    fake) satisfy this structurally: comments and reviews already exist on
    the port, and S7b adds pull-request open/lookup beside them.
    """

    def fetch_input(self, number: int, input_type: InputType) -> TrackedInput: ...
    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment: ...
    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: str
    ) -> InputComment | None: ...
    def publish_review(
        self, number: int, *, commit_id: str, body: str
    ) -> InputReview: ...
    def find_review_by_marker(self, number: int, marker: str) -> InputReview | None: ...
    def open_pull_request(
        self,
        *,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        draft: bool = False,
    ) -> InputPullRequest: ...
    def find_pull_request(
        self, head_branch: str, marker: str
    ) -> InputPullRequest | None: ...


class DocumentResolutionError(Exception):
    """A ``with.body`` reference resolved to no ``document`` text (§10.1).

    The operation ends ``error`` without attempting a write: a missing
    document is a malformed publication, never an empty one.
    """


def resolve_document(
    ledger: RunLedger, attempt_id: str, with_body: str | None
) -> tuple[str, str]:
    """Resolve a ``with.body`` reference to its ``document`` text (§10.1).

    ``with.body`` names ``<step>.<result>.<field>``; the text is the latest
    Step Execution of ``<step>`` whose Step Result is ``<result>``, read from
    its payload's ``<field>``, which must be a string. Returns the text and
    the Step Artifact reference (``<step>.<seq>.<field>``) the truncation
    pointer names (OP-19). Raises :class:`DocumentResolutionError` where the
    reference is malformed or names no string ``document``.
    """
    if with_body is None:
        raise DocumentResolutionError("no with.body names the publication document")
    parts = with_body.split(".")
    if len(parts) != 3 or not all(parts):
        raise DocumentResolutionError(
            f"with.body {with_body!r} is not a <step>.<result>.<field> reference"
        )
    step_id, result_name, field_name = parts
    latest_seq = -1
    latest_text: str | None = None
    for record in ledger.list_step_executions(attempt_id):
        if record.step_id != step_id or record.result != result_name:
            continue
        payload = record.payload or {}
        candidate = payload.get(field_name)
        if not isinstance(candidate, str):
            continue
        if record.seq >= latest_seq:
            latest_seq = record.seq
            latest_text = candidate
    if latest_text is None:
        raise DocumentResolutionError(
            f"with.body {with_body!r} names no string document in the Run Ledger"
        )
    return latest_text, f"{step_id}.{latest_seq}.{field_name}"


def pull_request_title_for_issue(issue_title: str) -> str:
    """The fixed runtime pull-request title template over the issue title (§10.5).

    Neither YAML nor the model sets the title (``with.title`` is refused at
    validation, CFG-35): the title is the Target Issue title verbatim.
    """
    return issue_title


def closing_keyword_line(issue_number: int, *, draft: bool) -> str:
    """The runtime-template closing keyword of a pull request (§10.5, OP-17).

    A ready pull request carries the closing ``Closes #<issue>``; a draft
    carries the non-closing ``Part of #<issue>``. There is no parameter for
    it, and the ``document`` is never rewritten for it (OP-20).
    """
    if draft:
        return f"Part of #{issue_number}"
    return f"Closes #{issue_number}"


def provenance_footer(
    *,
    attempt_id: str,
    workflow_id: str,
    step_id: str,
    config_digest: str,
    revision_label: str | None,
    revision: str | None,
) -> str:
    """The provenance footer appended after every publication ``document`` (§14.4).

    It carries the Attempt id, the Workflow id, the publishing step id, the
    Effective Configuration digest and the revision: the Base Revision for a
    pull request, the reviewed Input Head for a review, none for a comment.
    Validation evidence is never added: it belongs to the Workflow's
    ``document`` (OP-18).
    """
    lines = [
        "---",
        f"Attempt: {attempt_id}",
        f"Workflow: {workflow_id}",
        f"Step: {step_id}",
        f"Configuration: {config_digest}",
    ]
    if revision_label is not None and revision is not None:
        lines.append(f"{revision_label}: `{revision}`")
    return "\n".join(lines)


def truncate_document(
    document: str, *, artifact_ref: str, budget: int
) -> str:
    """Truncate only the ``document`` to fit ``budget`` characters (§14.4, OP-19).

    A document within budget is returned verbatim. Beyond it, the head is
    kept with an explicit truncation marker naming the full size and a
    pointer to the Step Artifact in the Attempt Archive — the closing
    keyword, footer and marker are never truncated because they live outside
    the ``document``.
    """
    if len(document) <= budget:
        return document
    if budget <= 0:
        marker = (
            f"... [truncated: {len(document)} characters total, "
            f"{len(document)} omitted; full document is Step Artifact "
            f"{artifact_ref} in the Attempt Archive]"
        )
        return marker
    omitted = len(document) - budget
    return (
        f"{document[:budget]}\n"
        f"... [truncated: {len(document)} characters total, {omitted} omitted; "
        f"full document is Step Artifact {artifact_ref} in the Attempt Archive]"
    )


def _fit_document(
    document: str,
    *,
    artifact_ref: str,
    assemble: Callable[[str], str],
    fixed: str,
) -> str:
    """Render the ``document`` part so ``fixed`` + it fits the limit (§14.4).

    The truncation marker itself takes space, so one pass can still exceed
    GitHub's limit; the budget shrinks by the overflow until the assembled
    body fits. The budget only ever shrinks, so the loop terminates.
    """
    budget = GITHUB_BODY_CHAR_LIMIT - len(fixed)
    rendered = truncate_document(document, artifact_ref=artifact_ref, budget=budget)
    body = assemble(rendered)
    while len(body) > GITHUB_BODY_CHAR_LIMIT and budget > 0:
        budget = max(0, budget - (len(body) - GITHUB_BODY_CHAR_LIMIT))
        rendered = truncate_document(
            document, artifact_ref=artifact_ref, budget=budget
        )
        body = assemble(rendered)
    return rendered


def render_open_pull_request_body(
    *,
    document: str,
    artifact_ref: str,
    attempt_id: str,
    workflow_id: str,
    step_id: str,
    config_digest: str,
    issue_number: int,
    draft: bool,
    base_revision: str,
    effect_marker: str,
) -> str:
    """Render a pull-request body from the fixed template (§10.5, §14.4, OP-18).

    The closing keyword, then the ``document`` verbatim (truncated only to
    fit GitHub's limit, OP-19), then the provenance footer carrying the Base
    Revision, then the Attempt Marker. The runtime adds no validation
    evidence and never rewrites model-authored text (OP-20).
    """
    keyword = closing_keyword_line(issue_number, draft=draft)
    footer = provenance_footer(
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        step_id=step_id,
        config_digest=config_digest,
        revision_label="Base Revision",
        revision=base_revision,
    )
    fixed = f"{keyword}\n\n\n{footer}\n{effect_marker}"
    rendered_document = _fit_document(
        document,
        artifact_ref=artifact_ref,
        assemble=lambda part: f"{keyword}\n\n{part}\n\n{footer}\n{effect_marker}",
        fixed=fixed,
    )
    return f"{keyword}\n\n{rendered_document}\n\n{footer}\n{effect_marker}"


def render_review_body(
    *,
    document: str,
    artifact_ref: str,
    attempt_id: str,
    workflow_id: str,
    step_id: str,
    config_digest: str,
    input_head: str,
    effect_marker: str,
) -> str:
    """Render a review body from the fixed template (§10.6, §14.4, OP-18).

    The ``document`` verbatim (truncated only to fit, OP-19), then the
    provenance footer carrying the reviewed Input Head, then the Attempt
    Marker.
    """
    footer = provenance_footer(
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        step_id=step_id,
        config_digest=config_digest,
        revision_label="Reviewed head",
        revision=input_head,
    )
    fixed = f"\n\n{footer}\n{effect_marker}"
    rendered_document = _fit_document(
        document,
        artifact_ref=artifact_ref,
        assemble=lambda part: f"{part}\n\n{footer}\n{effect_marker}",
        fixed=fixed,
    )
    return f"{rendered_document}\n\n{footer}\n{effect_marker}"


def render_comment_body(
    *,
    document: str,
    artifact_ref: str,
    attempt_id: str,
    workflow_id: str,
    step_id: str,
    config_digest: str,
    effect_marker: str,
) -> str:
    """Render a ``post-comment`` body from the fixed template (§10.7, §14.4, OP-18).

    The ``document`` verbatim (truncated only to fit, OP-19), then the
    provenance footer carrying no revision, then the Attempt Marker.
    """
    footer = provenance_footer(
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        step_id=step_id,
        config_digest=config_digest,
        revision_label=None,
        revision=None,
    )
    fixed = f"\n\n{footer}\n{effect_marker}"
    rendered_document = _fit_document(
        document,
        artifact_ref=artifact_ref,
        assemble=lambda part: f"{part}\n\n{footer}\n{effect_marker}",
        fixed=fixed,
    )
    return f"{rendered_document}\n\n{footer}\n{effect_marker}"


@dataclass(frozen=True)
class OpenPullRequestOutcome:
    """One ``open-pull-request`` Step Execution's end (§10.5)."""

    result: OpenPullRequestResultName
    revision: str | None
    pull_number: int | None
    pull_url: str | None
    facts: dict[str, object]
    detail: str


@dataclass(frozen=True)
class PublishReviewOutcome:
    """One ``publish-review`` Step Execution's end (§10.6)."""

    result: PublishReviewResultName
    head: str | None
    review_id: int | None
    facts: dict[str, object]
    detail: str


@dataclass(frozen=True)
class PostCommentOutcome:
    """One ``post-comment`` Step Execution's end (§10.7)."""

    result: PostCommentResultName
    comment_id: int | None
    facts: dict[str, object]
    detail: str


def _check_gate(
    gate: GateChecker | None, claim_active: bool
) -> None:
    """Enforce the pre-write precondition: an active Claim, no withdrawal or Drift.

    Wherever the operation is placed, it is not performed without an active
    Claim (OP-11, §10.1): a missing Claim raises :class:`GateFailed` like a
    withdrawn Gate. :class:`GateFailed` is never routed: the Attempt ends
    with the runtime-only outcome.
    """
    if not claim_active:
        raise GateFailed("withdrawn", "no active Claim for this Attempt")
    if gate is not None:
        verdict = gate()
        if verdict.status != "ok":
            raise GateFailed(verdict.status, verdict.reason or verdict.status)


def run_open_pull_request(
    *,
    workspace: Path,
    attempt_id: str,
    workflow_id: str,
    input_number: int,
    issue_title: str,
    attempt_branch: str,
    base_branch: str,
    base_revision: str,
    config_digest: str,
    with_body: str | None,
    draft: bool = False,
    with_requires: tuple[tuple[str, str], ...] = (),
    validation_registry: ValidationRegistry | None = None,
    tree_id: str | None = None,
    gate: GateChecker | None = None,
    claim_active: bool = True,
    git: CommitPushGitOps,
    tracker: PublicationTracker,
    ledger: RunLedger | None = None,
    step_id: str = "open-pull-request",
    seq: int = 1,
) -> OpenPullRequestOutcome:
    """Open a pull request from the pushed revision (§10.5).

    The title is the fixed runtime template over the Target Issue title;
    the body renders the named ``document`` with the ready/draft closing
    keyword and a provenance footer carrying the Base Revision. ``draft``
    defaults to ready. An Attempt pull request that already exists (open or
    human-closed, never re-created) yields ``already-open``. ``requires``
    is checked for the revision being published before any write; the Gate
    runs before the write and Drift or withdrawal raises :class:`GateFailed`.
    Every attempted write records its Effect Intent before the write and its
    Receipt — or its ``not-performed`` settlement — after it (§14.1).
    """
    tree = tree_id if tree_id is not None else tree_identity(workspace)
    registry = validation_registry if validation_registry is not None else ValidationRegistry()
    if with_requires and not registry.holds(with_requires, tree):
        return OpenPullRequestOutcome(
            result="requirement-unmet",
            revision=None,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree, "requires": [list(pair) for pair in with_requires]},
            detail="requirement-unmet: no passed run-validation is bound to this tree",
        )
    _check_gate(gate, claim_active)

    def _intent(payload: str, target: dict[str, object]) -> None:
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

    if ledger is None:
        return OpenPullRequestOutcome(
            result="error",
            revision=None,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree, "with_body": with_body},
            detail="error: no Run Ledger resolves with.body",
        )
    try:
        document, artifact_ref = resolve_document(ledger, attempt_id, with_body)
    except DocumentResolutionError as exc:
        return OpenPullRequestOutcome(
            result="error",
            revision=None,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree, "with_body": with_body},
            detail=f"error: {exc}",
        )
    try:
        clean = git.is_clean(workspace)
    except Exception as exc:
        return OpenPullRequestOutcome(
            result="error",
            revision=None,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree},
            detail=f"error: could not read the working tree: {exc!r}",
        )
    if not clean:
        return OpenPullRequestOutcome(
            result="error",
            revision=None,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree},
            detail="error: the working tree is dirty; the pull request needs a pushed revision",
        )
    try:
        revision = git.head_sha(workspace)
    except Exception as exc:
        return OpenPullRequestOutcome(
            result="error",
            revision=None,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree},
            detail=f"error: could not read HEAD: {exc!r}",
        )
    marker = step_effect_marker(
        attempt_id, step_id, seq, 0, kind=OPEN_PULL_REQUEST_KIND
    )
    title = pull_request_title_for_issue(issue_title)
    body = render_open_pull_request_body(
        document=document,
        artifact_ref=artifact_ref,
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        step_id=step_id,
        config_digest=config_digest,
        issue_number=input_number,
        draft=draft,
        base_revision=base_revision,
        effect_marker=marker,
    )
    target = {
        "operation": OPEN_PULL_REQUEST_KIND,
        "base_branch": base_branch,
        "head_branch": attempt_branch,
        "revision": revision,
        "draft": draft,
        "title": title,
    }
    _intent(f"open-pull-request {attempt_id}: {title} from {revision}\n{body}", target)
    try:
        existing = tracker.find_pull_request(attempt_branch, marker)
    except CredentialFailure:
        raise
    except Exception as exc:
        _not_performed(f"error: {exc}")
        return OpenPullRequestOutcome(
            result="error",
            revision=revision,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree, "branch": attempt_branch, "revision": revision},
            detail=f"error: could not look up the pull request: {exc}",
        )
    if existing is not None:
        _receipt(str(existing.number))
        return OpenPullRequestOutcome(
            result="already-open",
            revision=revision,
            pull_number=existing.number,
            pull_url=existing.url,
            facts={
                "tree_id": tree,
                "branch": attempt_branch,
                "revision": revision,
                "pull_number": existing.number,
                "pull_url": existing.url,
                "pull_state": existing.state,
            },
            detail=f"already-open: pull request #{existing.number} already carries this Attempt",
        )
    try:
        opened = tracker.open_pull_request(
            title=title,
            head_branch=attempt_branch,
            base_branch=base_branch,
            body=body,
            draft=draft,
        )
    except CredentialFailure:
        raise
    except Exception as exc:
        _not_performed(f"error: {exc}")
        return OpenPullRequestOutcome(
            result="error",
            revision=revision,
            pull_number=None,
            pull_url=None,
            facts={"tree_id": tree, "branch": attempt_branch, "revision": revision},
            detail=f"error: opening the pull request failed: {exc}",
        )
    _receipt(str(opened.number))
    return OpenPullRequestOutcome(
        result="opened",
        revision=revision,
        pull_number=opened.number,
        pull_url=opened.url,
        facts={
            "tree_id": tree,
            "branch": attempt_branch,
            "revision": revision,
            "pull_number": opened.number,
            "pull_url": opened.url,
            "draft": draft,
        },
        detail=f"opened: pull request #{opened.number} from {revision}",
    )


def run_publish_review(
    *,
    workspace: Path,
    attempt_id: str,
    workflow_id: str,
    input_number: int,
    expected_head: str | None,
    config_digest: str,
    with_body: str | None,
    with_requires: tuple[tuple[str, str], ...] = (),
    validation_registry: ValidationRegistry | None = None,
    tree_id: str | None = None,
    gate: GateChecker | None = None,
    claim_active: bool = True,
    tracker: PublicationTracker,
    ledger: RunLedger | None = None,
    step_id: str = "publish-review",
    seq: int = 1,
) -> PublishReviewOutcome:
    """Publish a review through the GitHub review API (§10.6).

    A COMMENT event against the current Input Head, with the named
    ``document`` as its body and a provenance footer carrying the reviewed
    head. A head that moved between the Gate and the write yields
    ``stale-head`` and the review is recorded ``not-performed``. ``requires``
    is checked for the revision being published before any write; the Gate
    runs before the write and Drift or withdrawal raises :class:`GateFailed`.
    Every attempted write records its Effect Intent before the write and its
    Receipt — or its ``not-performed`` settlement — after it (§14.1).
    """
    tree = tree_id if tree_id is not None else tree_identity(workspace)
    registry = validation_registry if validation_registry is not None else ValidationRegistry()
    if with_requires and not registry.holds(with_requires, tree):
        return PublishReviewOutcome(
            result="requirement-unmet",
            head=None,
            review_id=None,
            facts={"tree_id": tree, "requires": [list(pair) for pair in with_requires]},
            detail="requirement-unmet: no passed run-validation is bound to this tree",
        )
    _check_gate(gate, claim_active)

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

    if ledger is None:
        return PublishReviewOutcome(
            result="error",
            head=None,
            review_id=None,
            facts={"tree_id": tree, "with_body": with_body},
            detail="error: no Run Ledger resolves with.body",
        )
    try:
        document, artifact_ref = resolve_document(ledger, attempt_id, with_body)
    except DocumentResolutionError as exc:
        return PublishReviewOutcome(
            result="error",
            head=None,
            review_id=None,
            facts={"tree_id": tree, "with_body": with_body},
            detail=f"error: {exc}",
        )
    try:
        readback = tracker.fetch_input(input_number, "pull-request")
    except CredentialFailure:
        raise
    except Exception as exc:
        return PublishReviewOutcome(
            result="error",
            head=None,
            review_id=None,
            facts={"tree_id": tree, "input_number": input_number},
            detail=f"error: could not re-read the pull request: {exc}",
        )
    head = readback.head_sha
    if head is None:
        return PublishReviewOutcome(
            result="error",
            head=None,
            review_id=None,
            facts={"tree_id": tree, "input_number": input_number},
            detail="error: the pull request reports no head sha",
        )
    if expected_head is not None and head != expected_head:
        _not_performed("stale-head: the remote head moved")
        return PublishReviewOutcome(
            result="stale-head",
            head=head,
            review_id=None,
            facts={"tree_id": tree, "expected_head": expected_head, "head": head},
            detail="stale-head: the remote head differs from the Input Head",
        )
    marker = step_effect_marker(
        attempt_id, step_id, seq, 0, kind=PUBLISH_REVIEW_KIND
    )
    body = render_review_body(
        document=document,
        artifact_ref=artifact_ref,
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        step_id=step_id,
        config_digest=config_digest,
        input_head=head,
        effect_marker=marker,
    )
    target = {
        "operation": PUBLISH_REVIEW_KIND,
        "input_number": input_number,
        "commit_id": head,
    }
    if ledger is not None:
        ledger.record_step_effect_intent(
            attempt_id,
            step_id,
            seq,
            0,
            rendered_payload=f"publish-review {attempt_id}: review {head}\n{body}",
            expected_target=target,
        )
    try:
        existing = tracker.find_review_by_marker(input_number, marker)
    except CredentialFailure:
        raise
    except Exception as exc:
        _not_performed(f"error: {exc}")
        return PublishReviewOutcome(
            result="error",
            head=head,
            review_id=None,
            facts={"tree_id": tree, "head": head},
            detail=f"error: could not look up the review: {exc}",
        )
    if existing is not None:
        _receipt(str(existing.review_id))
        return PublishReviewOutcome(
            result="published",
            head=head,
            review_id=existing.review_id,
            facts={"tree_id": tree, "head": head, "review_id": existing.review_id},
            detail=f"published: review {existing.review_id} already carries this Attempt",
        )
    try:
        published = tracker.publish_review(input_number, commit_id=head, body=body)
    except CredentialFailure:
        raise
    except Exception as exc:
        try:
            reread = tracker.fetch_input(input_number, "pull-request")
        except Exception:
            reread = None
        reread_head = reread.head_sha if reread is not None else None
        if expected_head is not None and reread_head != expected_head:
            _not_performed("stale-head: the remote head moved")
            return PublishReviewOutcome(
                result="stale-head",
                head=reread_head,
                review_id=None,
                facts={
                    "tree_id": tree,
                    "expected_head": expected_head,
                    "head": reread_head,
                },
                detail="stale-head: the remote head differs from the Input Head",
            )
        _not_performed(f"error: {exc}")
        return PublishReviewOutcome(
            result="error",
            head=head,
            review_id=None,
            facts={"tree_id": tree, "head": head},
            detail=f"error: publishing the review failed: {exc}",
        )
    _receipt(str(published.review_id))
    return PublishReviewOutcome(
        result="published",
        head=head,
        review_id=published.review_id,
        facts={"tree_id": tree, "head": head, "review_id": published.review_id},
        detail=f"published: review {published.review_id} against {head}",
    )


def run_post_comment(
    *,
    workspace: Path,
    attempt_id: str,
    workflow_id: str,
    input_number: int,
    input_type: InputType,
    config_digest: str,
    with_body: str | None,
    with_requires: tuple[tuple[str, str], ...] = (),
    validation_registry: ValidationRegistry | None = None,
    tree_id: str | None = None,
    gate: GateChecker | None = None,
    claim_active: bool = True,
    tracker: PublicationTracker,
    ledger: RunLedger | None = None,
    step_id: str = "post-comment",
    seq: int = 1,
) -> PostCommentOutcome:
    """Post one comment on the input issue or pull request (§10.7).

    The named ``document`` renders with a provenance footer carrying no
    revision. There is no facts-mode comment. ``requires`` is checked for
    the revision being published before any write; the Gate runs before the
    write and Drift or withdrawal raises :class:`GateFailed`. Every
    attempted write records its Effect Intent before the write and its
    Receipt after it (§14.1); an already-posted comment is found by key and
    never written twice.
    """
    tree = tree_id if tree_id is not None else tree_identity(workspace)
    registry = validation_registry if validation_registry is not None else ValidationRegistry()
    if with_requires and not registry.holds(with_requires, tree):
        return PostCommentOutcome(
            result="requirement-unmet",
            comment_id=None,
            facts={"tree_id": tree, "requires": [list(pair) for pair in with_requires]},
            detail="requirement-unmet: no passed run-validation is bound to this tree",
        )
    _check_gate(gate, claim_active)

    def _receipt(remote_id: str | None) -> None:
        if ledger is not None:
            ledger.record_step_effect_receipt(
                attempt_id, step_id, seq, 0, remote_id=remote_id
            )

    if ledger is None:
        return PostCommentOutcome(
            result="error",
            comment_id=None,
            facts={"tree_id": tree, "with_body": with_body},
            detail="error: no Run Ledger resolves with.body",
        )
    try:
        document, artifact_ref = resolve_document(ledger, attempt_id, with_body)
    except DocumentResolutionError as exc:
        return PostCommentOutcome(
            result="error",
            comment_id=None,
            facts={"tree_id": tree, "with_body": with_body},
            detail=f"error: {exc}",
        )
    marker = step_effect_marker(
        attempt_id, step_id, seq, 0, kind=POST_COMMENT_KIND
    )
    body = render_comment_body(
        document=document,
        artifact_ref=artifact_ref,
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        step_id=step_id,
        config_digest=config_digest,
        effect_marker=marker,
    )
    target: dict[str, object] = {
        "operation": POST_COMMENT_KIND,
        "input_number": input_number,
        "input_type": input_type,
    }
    if ledger is not None:
        ledger.record_step_effect_intent(
            attempt_id,
            step_id,
            seq,
            0,
            rendered_payload=f"post-comment {attempt_id}: comment on {input_type} {input_number}\n{body}",
            expected_target=target,
        )
    try:
        existing = tracker.find_comment_by_marker(input_number, input_type, marker)
    except CredentialFailure:
        raise
    except Exception as exc:
        if ledger is not None:
            ledger.record_step_effect_not_performed(
                attempt_id, step_id, seq, 0, reason=f"error: {exc}"
            )
        return PostCommentOutcome(
            result="error",
            comment_id=None,
            facts={"tree_id": tree, "input_number": input_number},
            detail=f"error: could not look up the comment: {exc}",
        )
    if existing is not None:
        _receipt(str(existing.comment_id))
        return PostCommentOutcome(
            result="posted",
            comment_id=existing.comment_id,
            facts={
                "tree_id": tree,
                "input_number": input_number,
                "comment_id": existing.comment_id,
            },
            detail=f"posted: comment {existing.comment_id} already carries this Attempt",
        )
    try:
        posted = tracker.post_comment(input_number, input_type, body)
    except CredentialFailure:
        raise
    except Exception as exc:
        if ledger is not None:
            ledger.record_step_effect_not_performed(
                attempt_id, step_id, seq, 0, reason=f"error: {exc}"
            )
        return PostCommentOutcome(
            result="error",
            comment_id=None,
            facts={"tree_id": tree, "input_number": input_number},
            detail=f"error: posting the comment failed: {exc}",
        )
    _receipt(str(posted.comment_id))
    return PostCommentOutcome(
        result="posted",
        comment_id=posted.comment_id,
        facts={
            "tree_id": tree,
            "input_number": input_number,
            "comment_id": posted.comment_id,
        },
        detail=f"posted: comment {posted.comment_id} on {input_type} {input_number}",
    )


# --- Graph integration --------------------------------------------------------


@dataclass
class PublishingOperationRunner:
    """The production S7b :class:`OperationRunner` (§10.1, §10.5–§10.7, §14).

    Delegates the S7a local operations to a :class:`LocalOperationRunner`
    (sharing its Validation Registry, Gate, workspace and pull-request
    compare-and-swap head) and runs the publishing operations for real:
    resolving the named ``document`` from the Run Ledger, rendering the
    fixed publication body around it, and writing through the tracker with
    an Effect Intent and Receipt per write.
    """

    local: LocalOperationRunner
    tracker: PublicationTracker
    workflow_id: str
    config_digest: str
    issue_title: str = ""
    base_branch: str = "main"
    base_revision: str = ""
    claim_active: bool = True

    def run(
        self,
        step: V2Step,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> OperationExecution:
        """Run one ``operation`` step (§10.1–§10.7)."""
        assert step.operation is not None, "PublishingOperationRunner needs an operation step"
        if step.operation in ("check-readiness", "run-validation", "commit-and-push"):
            return self.local.run(step, seq, attempt_id, ledger)
        assert step.operation in PUBLISHING_OPERATIONS, (
            f"PublishingOperationRunner cannot run {step.operation!r}"
        )
        return self._run_publishing(
            step, seq, attempt_id, ledger, with_body=step.with_body
        )

    def _run_publishing(
        self,
        step: V2Step,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
        *,
        with_body: str | None,
    ) -> OperationExecution:
        """Dispatch one publishing ``operation`` step to its S7b executor."""
        assert step.operation is not None
        workspace = self.local.workspace
        registry = self.local.validation_registry
        gate = self.local.gate
        input_type = self.local.input_type
        if step.operation == "open-pull-request":
            outcome = run_open_pull_request(
                workspace=workspace,
                attempt_id=attempt_id,
                workflow_id=self.workflow_id,
                input_number=self.local.input_number,
                issue_title=self.issue_title,
                attempt_branch=self.local.attempt_branch,
                base_branch=self.base_branch,
                base_revision=self.base_revision,
                config_digest=self.config_digest,
                with_body=with_body,
                draft=bool(step.with_draft),
                with_requires=step.with_requires,
                validation_registry=registry,
                gate=gate,
                claim_active=self.claim_active,
                git=self.local._git_ops(),
                tracker=self.tracker,
                ledger=ledger,
                step_id=step.id,
                seq=seq,
            )
            payload: dict[str, object] = {"result": outcome.result}
            if outcome.revision is not None:
                payload["revision"] = outcome.revision
            if outcome.pull_number is not None:
                payload["pull_number"] = outcome.pull_number
            if outcome.pull_url is not None:
                payload["pull_url"] = outcome.pull_url
            return OperationExecution(
                result=outcome.result,
                payload=payload,
                facts={**outcome.facts, "detail": outcome.detail},
            )
        if step.operation == "publish-review":
            review_outcome = run_publish_review(
                workspace=workspace,
                attempt_id=attempt_id,
                workflow_id=self.workflow_id,
                input_number=self.local.input_number,
                expected_head=self.local.expected_head,
                config_digest=self.config_digest,
                with_body=with_body,
                with_requires=step.with_requires,
                validation_registry=registry,
                gate=gate,
                claim_active=self.claim_active,
                tracker=self.tracker,
                ledger=ledger,
                step_id=step.id,
                seq=seq,
            )
            review_payload: dict[str, object] = {"result": review_outcome.result}
            if review_outcome.head is not None:
                review_payload["head"] = review_outcome.head
            if review_outcome.review_id is not None:
                review_payload["review_id"] = review_outcome.review_id
            return OperationExecution(
                result=review_outcome.result,
                payload=review_payload,
                facts={**review_outcome.facts, "detail": review_outcome.detail},
            )
        assert step.operation == "post-comment"
        comment_outcome = run_post_comment(
            workspace=workspace,
            attempt_id=attempt_id,
            workflow_id=self.workflow_id,
            input_number=self.local.input_number,
            input_type=cast(InputType, input_type),
            config_digest=self.config_digest,
            with_body=with_body,
            with_requires=step.with_requires,
            validation_registry=registry,
            gate=gate,
            claim_active=self.claim_active,
            tracker=self.tracker,
            ledger=ledger,
            step_id=step.id,
            seq=seq,
        )
        comment_payload: dict[str, object] = {"result": comment_outcome.result}
        if comment_outcome.comment_id is not None:
            comment_payload["comment_id"] = comment_outcome.comment_id
        return OperationExecution(
            result=comment_outcome.result,
            payload=comment_payload,
            facts={**comment_outcome.facts, "detail": comment_outcome.detail},
        )


def publishing_operations() -> frozenset[str]:
    """The S7b publishing operations, for the graph's runner consult (§10.5–§10.7)."""
    return PUBLISHING_OPERATIONS
