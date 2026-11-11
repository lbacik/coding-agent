"""The ``StepTrackerPort`` Protocol the step-workflow runtime programs against.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: candidates are open inputs of the Workflow's type
carrying its Selection Label; selection writes nothing), §6 (the Claim is
an assignment plus a Claim Comment bearing an Attempt Marker with
``kind=claim``, read back before any Attempt exists), §7 (Gates re-read the
input; Drift and withdrawal end the Attempt), §14 (every external write is
an Effect Intent settled once: the key travels in the write's Attempt
Marker; lookups filter by the Agent Identity's numeric id; a write by
another account carrying our marker is not our receipt) and §18 (identity
mode detection from the Agent Identity's numeric id and the Target
Repository owner's id; the Worker never edits an input's title or body and
never applies a Selection Label — the port cannot express either).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.

There is deliberately no operation that applies the Selection Label and
none that edits an input's title or body — those two structural abstentions
(§18) hold because the port cannot express them, not because any caller
promises restraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

InputType = Literal["issue", "pull-request"]
"""The Workflow's one input type (§5.1)."""


@dataclass(frozen=True)
class TrackedInput:
    """One Workflow input as the tracker reports it (§5, §7).

    ``labels`` holds every label name; ``assignees`` holds every assignee
    login (any entry excludes at selection and at the Claim read-back).
    Pull-request-only facts are ``None`` for issues: ``draft``,
    ``merged``, ``head_sha`` (the Input Head once pinned), ``head_branch``
    and ``head_repo`` (the head repository's ``owner/name`` — a fork head
    is never eligible in the first release), and ``base_branch``.
    """

    number: int
    input_type: InputType
    title: str
    body: str
    is_open: bool
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    created_at: str
    author_login: str = ""
    author_id: int | None = None
    draft: bool = False
    merged: bool = False
    head_sha: str | None = None
    head_branch: str | None = None
    head_repo: str | None = None
    base_branch: str | None = None


@dataclass(frozen=True)
class LabeledEvent:
    """The latest ``labeled`` event for the Selection Label on one input (§5.1).

    The Claim read-back tests its actor under the detected identity mode;
    the actor, the time and the mode are recorded with the Attempt. ``None``
    where no such event exists: without an event there is no authorisation
    to test, so the Claim is refused.
    """

    label: str
    actor_login: str
    actor_id: int | None
    actor_type: str
    created_at: str


@dataclass(frozen=True)
class AgentFacts:
    """The Agent Identity as the tracker reports it (§18)."""

    login: str
    account_id: int


@dataclass(frozen=True)
class InputComment:
    """One issue or pull-request comment as the tracker reports it (§6, §14).

    ``marker_present`` records whether the body carries the Attempt Marker
    the caller looked for. The lookup rule reads in one direction only: a
    comment by another account that carries our marker is not our receipt.
    """

    comment_id: int
    body: str
    author_login: str
    author_id: int | None = None
    marker_present: bool = False


@dataclass(frozen=True)
class InputReview:
    """One pull-request review as the tracker reports it (§14, OP-14).

    Published through the review API as a COMMENT event against an Input
    Head. ``marker_present`` records whether the body carries the Attempt
    Marker the caller looked for; authorship decides the same way as for
    comments.
    """

    review_id: int
    body: str
    author_login: str
    author_id: int | None = None
    commit_id: str | None = None
    marker_present: bool = False


@dataclass(frozen=True)
class InputPullRequest:
    """One pull request as the step-workflow tracker reports it (§10.5, §14).

    ``marker_present`` records whether the body carries the Attempt Marker
    the caller looked for; the lookup rule reads in one direction only and
    filters by the Agent Identity, the same as comments and reviews.
    """

    number: int
    url: str
    title: str
    head_branch: str
    base_branch: str
    body: str
    state: str
    author_login: str
    author_id: int | None = None
    marker_present: bool = False
    draft: bool = False


class StepTrackerPort(Protocol):
    """The tracker operations the step-workflow runtime may call."""

    @property
    def target_repo(self) -> str:
        """The Target Repository, ``owner/name`` (§2.1)."""
        ...

    @property
    def agent_login(self) -> str:
        """The Agent Identity's login: the only login assigned or removed."""
        ...

    @property
    def agent_id(self) -> int | None:
        """The Agent Identity's numeric id; lookups filter by it where known."""
        ...

    def fetch_agent_identity(self) -> AgentFacts:
        """Resolve the Agent Identity: login and numeric id (§18)."""
        ...

    def fetch_repository_owner_id(self) -> int:
        """Resolve the Target Repository owner's numeric id (§18).

        Compared with the Agent Identity's id, this decides the identity
        mode — no YAML key selects it.
        """
        ...

    def fetch_input(self, number: int, input_type: InputType) -> TrackedInput:
        """Read one input with every guard and Gate fact (§5, §7)."""
        ...

    def list_issue_candidates(self, selection_label: str) -> list[TrackedInput]:
        """List the open issues carrying ``selection_label`` (§5.2).

        Read-only: selection writes nothing, including for an input
        carrying both the Selection Label and an end label.
        """
        ...

    def list_pull_request_candidates(
        self, selection_label: str
    ) -> list[TrackedInput]:
        """List the open pull requests carrying ``selection_label`` (§5.2).

        Read-only: selection writes nothing. Fork pull requests are
        reported with their foreign ``head_repo`` so the guard excludes
        them; they are never claimed.
        """
        ...

    def list_open_pull_requests(self) -> list[TrackedInput]:
        """List every open pull request with its body (§5.3).

        Read-only: the ``skip-open-agent-pr`` scan looks for the Agent
        Identity's open pull request bearing an issue's Attempt Marker.
        """
        ...

    def read_labeled_event(
        self, number: int, input_type: InputType, label: str
    ) -> LabeledEvent | None:
        """Read the latest ``labeled`` event for ``label`` on one input (§5.1).

        ``None`` where no such event exists: the Claim is refused, because
        there is no authorisation to test.
        """
        ...

    def has_open_blocker(self, number: int, input_type: InputType) -> bool:
        """Whether the input has an open native blocker (§5.2)."""
        ...

    def fetch_base_tip(self, base_branch: str) -> str:
        """Resolve the base-branch tip at Claim (§7.1, §7.2)."""
        ...

    def assign_input(self, number: int, input_type: InputType) -> None:
        """Assign the Agent Identity: the Claim's first write (§6, P-CLM-1).

        Adds no label and writes no comment.
        """
        ...

    def unassign_input(self, number: int, input_type: InputType) -> None:
        """Remove the Agent Identity's assignment, only the Worker's own (§6)."""
        ...

    def post_claim_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        """Post the Claim Comment bearing the Attempt Marker (§6.1)."""
        ...

    def list_comments(
        self, number: int, input_type: InputType
    ) -> list[InputComment]:
        """List the input's comments, oldest first (§6.1, §14)."""
        ...

    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: str
    ) -> InputComment | None:
        """Find our comment by Attempt Marker (§14).

        Scans the full list and returns the one whose body carries
        ``marker`` and whose author is the Agent Identity — filtered by the
        numeric id where it is known, otherwise by login. ``None`` where no
        such comment exists.
        """
        ...

    def delete_comment(self, comment_id: int) -> None:
        """Delete one of the Worker's own comments (§6.2, §16.2).

        Naturally idempotent: a comment already absent is the successful
        no-op, not a failure.
        """
        ...

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        """Post one comment on the input, body already rendered (§10.7)."""
        ...

    def publish_review(
        self, number: int, *, commit_id: str, body: str
    ) -> InputReview:
        """Publish a COMMENT-event review against ``commit_id`` (§10.6).

        The review API, never the pull-request body. Approve and
        request-changes are not in the catalog.
        """
        ...

    def list_reviews(self, number: int) -> list[InputReview]:
        """List the pull request's reviews, oldest first (§14)."""
        ...

    def find_review_by_marker(self, number: int, marker: str) -> InputReview | None:
        """Find our review by Attempt Marker (§14).

        The same authorship rule as comments: a review by another account
        carrying our marker is not our receipt.
        """
        ...

    def open_pull_request(
        self,
        *,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        draft: bool = False,
    ) -> InputPullRequest:
        """Open one pull request from ``head_branch`` (§10.5, §14).

        The title is the fixed runtime template over the Target Issue title
        and the body is the rendered publication body. ``draft`` opens a
        draft; the ready/draft closing keyword is already in the body.
        """
        ...

    def find_pull_request(
        self, head_branch: str, marker: str
    ) -> InputPullRequest | None:
        """Find our pull request by head branch and Attempt Marker (§14).

        Open preferred where both states match; a pull request by another
        account carrying our marker is not our receipt.
        """
        ...
