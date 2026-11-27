"""A fake ``InputPort``/``CodeHostPort`` for the S6 input and Claim tests.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: candidates, guards, filters, drafts,
``skip-open-agent-pr``, the ``labeled``-event authorisation — selection
writes nothing), §6 (the Claim: intent, assignment, Claim Comment, the
read-back, the lowest comment id winning), §7 (Gate re-reads, Drift and
withdrawal facts) and §14 (lookups by Effect Intent key, filtered by the
Agent Identity; a write by another account carrying our marker is never
ours).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.

The fake performs nothing remotely: every call is appended to ``calls``
and acknowledged. ``on_assign`` hooks a race before the Claim read-back.
``seed_comments`` plants a surrounding history around the Worker's own
comment, and foreign-authored marker carriers prove the lookup never
adopts them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from coding_agent.inputs.github import format_attempt_marker
from coding_agent.inputs.marker import AttemptMarker
from coding_agent.inputs.port import (
    AgentFacts,
    InputComment,
    InputPort,
    InputPullRequest,
    InputReview,
    InputType,
    LabeledEvent,
    TrackedInput,
)
from coding_agent.inputs.profile import GITHUB_PROFILE, CodeHostProfile

_FAKE_BASE_TIP = "b" * 40
"""The base-branch tip the fake reports at Claim (contract §7)."""


@dataclass
class FakeStepTracker:
    """In-process ``InputPort``/``CodeHostPort`` double; never selectable by configuration."""

    owner: str = "octocat"
    repo: str = "sandbox"
    agent_login: str = "coding-agent"
    agent_id: int | None = 1001
    owner_id: int = 999
    profile: CodeHostProfile = GITHUB_PROFILE
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    fail_on: str | None = None
    fail_with: BaseException | None = None
    inputs: dict[int, TrackedInput] = field(default_factory=dict)
    labeled_events: dict[tuple[int, str], LabeledEvent] = field(default_factory=dict)
    blockers_open: dict[int, bool] = field(default_factory=dict)
    base_tip: str = _FAKE_BASE_TIP
    comments: list[InputComment] = field(default_factory=list)
    reviews: list[InputReview] = field(default_factory=list)
    opened_pull_requests: list[InputPullRequest] = field(default_factory=list)
    _next_pull_number: int = field(default=101, repr=False)
    open_pull_requests: list[TrackedInput] = field(default_factory=list)
    _next_comment_id: int = field(default=1, repr=False)
    _next_review_id: int = field(default=101, repr=False)
    on_assign: Callable[[int], None] | None = None

    @property
    def target_repo(self) -> str:
        """The Target Repository, ``owner/name``."""
        return f"{self.owner}/{self.repo}"

    @property
    def pull_requests(self) -> InputPort:
        """The pull-request facet: pull-request Workflows claim through it."""
        return self

    def delivery_reference(self, ref: str, *, draft: bool = False) -> str:
        """The pull-request-to-issue link line for issue ``ref``."""
        return self.profile.delivery_reference(ref, draft=draft)

    def format_marker(self, marker: AttemptMarker) -> str:
        """Render an Attempt Marker the GitHub way (byte-identical)."""
        return format_attempt_marker(marker)

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self.fail_on == name:
            if self.fail_with is not None:
                raise self.fail_with
            raise RuntimeError(f"fake tracker failure in {name}")

    def call_names(self) -> list[str]:
        """Every operation performed through this fake, in order."""
        return [call[0] for call in self.calls]

    def fetch_agent_identity(self) -> AgentFacts:
        """Resolve the Agent Identity: login and numeric id."""
        self._record("fetch_agent_identity")
        assert self.agent_id is not None
        return AgentFacts(login=self.agent_login, account_id=self.agent_id)

    def fetch_repository_owner_id(self) -> int:
        """Resolve the Target Repository owner's numeric id."""
        self._record("fetch_repository_owner_id")
        return self.owner_id

    def _held(self, number: int, input_type: InputType) -> TrackedInput:
        held = self.inputs.get(number)
        if held is not None:
            return held
        return TrackedInput(
            number=number,
            input_type=input_type,
            title="Add a greeting",
            body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
            is_open=True,
            labels=(),
            assignees=(),
            created_at="2026-10-01T00:00:00Z",
        )

    def fetch_input(self, number: int, input_type: InputType) -> TrackedInput:
        """Read one input with every guard and Gate fact."""
        self._record("fetch_input", number, input_type)
        return self._held(number, input_type)

    def list_issue_candidates(self, selection_label: str) -> list[TrackedInput]:
        """List the open issues carrying ``selection_label``."""
        self._record("list_issue_candidates", selection_label)
        return sorted(
            (
                held
                for held in self.inputs.values()
                if held.input_type == "issue"
                and held.is_open
                and selection_label in held.labels
            ),
            key=lambda held: (held.created_at, held.number),
        )

    def list_pull_request_candidates(self, selection_label: str) -> list[TrackedInput]:
        """List the open pull requests carrying ``selection_label``."""
        self._record("list_pull_request_candidates", selection_label)
        return sorted(
            (
                held
                for held in self.inputs.values()
                if held.input_type == "pull-request"
                and held.is_open
                and selection_label in held.labels
            ),
            key=lambda held: (held.created_at, held.number),
        )

    def list_open_pull_requests(self) -> list[TrackedInput]:
        """List every open pull request with its body."""
        self._record("list_open_pull_requests")
        return list(self.open_pull_requests)

    def read_labeled_event(
        self, number: int, input_type: InputType, label: str
    ) -> LabeledEvent | None:
        """Read the latest ``labeled`` event for ``label``."""
        self._record("read_labeled_event", number, input_type, label)
        return self.labeled_events.get((number, label))

    def has_open_blocker(self, number: int, input_type: InputType) -> bool:
        """Whether the input has an open native blocker."""
        self._record("has_open_blocker", number, input_type)
        return self.blockers_open.get(number, False)

    def fetch_base_tip(self, base_branch: str) -> str:
        """Resolve the base-branch tip at Claim."""
        self._record("fetch_base_tip", base_branch)
        return self.base_tip

    def assign_input(self, number: int, input_type: InputType) -> None:
        """Assign the Agent Identity: the Claim's first write."""
        self._record("assign_input", number, input_type)
        held = self.inputs.get(number)
        if held is not None and self.agent_login not in held.assignees:
            self.inputs[number] = replace(
                held, assignees=(*held.assignees, self.agent_login)
            )
        if self.on_assign is not None:
            self.on_assign(number)

    def unassign_input(self, number: int, input_type: InputType) -> None:
        """Remove the Agent Identity's assignment, only the Worker's own."""
        self._record("unassign_input", number, input_type)
        held = self.inputs.get(number)
        if held is not None:
            self.inputs[number] = replace(
                held,
                assignees=tuple(
                    login for login in held.assignees if login != self.agent_login
                ),
            )

    def post_claim_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post the Claim Comment bearing the Attempt Marker."""
        self._record("post_claim_comment", number, input_type, body)
        comment = InputComment(
            comment_id=self._next_comment_id,
            body=body,
            author_login=self.agent_login,
            author_id=self.agent_id,
            marker_present=True,
        )
        self._next_comment_id += 1
        self.comments.append(comment)
        return comment

    def list_comments(self, number: int, input_type: InputType) -> list[InputComment]:
        """List the input's comments, oldest first."""
        self._record("list_comments", number, input_type)
        return list(self.comments)

    def _author_matches(self, login: str, account_id: int | None) -> bool:
        if self.agent_id is not None:
            return account_id == self.agent_id
        return login == self.agent_login

    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: AttemptMarker
    ) -> InputComment | None:
        """Find our comment by Attempt Marker."""
        rendered = self.format_marker(marker)
        self._record("find_comment_by_marker", number, input_type, rendered)
        for comment in self.comments:
            if rendered not in comment.body:
                continue
            if not self._author_matches(comment.author_login, comment.author_id):
                continue
            return comment
        return None

    def delete_comment(self, comment_id: int) -> None:
        """Delete one of the Worker's own comments; absent is success."""
        self._record("delete_comment", comment_id)
        self.comments = [
            comment for comment in self.comments if comment.comment_id != comment_id
        ]

    def post_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post one comment on the input, body already rendered."""
        _ = marker
        self._record("post_comment", number, input_type, body)
        comment = InputComment(
            comment_id=self._next_comment_id,
            body=body,
            author_login=self.agent_login,
            author_id=self.agent_id,
            marker_present=True,
        )
        self._next_comment_id += 1
        self.comments.append(comment)
        return comment

    def seed_comments(
        self, bodies: list[str], *, author_login: str = "human"
    ) -> None:
        """Plant a surrounding comment history, oldest first."""
        for body in bodies:
            self.comments.append(
                InputComment(
                    comment_id=self._next_comment_id,
                    body=body,
                    author_login=author_login,
                    author_id=None,
                    marker_present=False,
                )
            )
            self._next_comment_id += 1

    def seed_foreign_marker_comment(self, marker: str) -> InputComment:
        """Plant a human comment carrying our marker: never our receipt."""
        comment = InputComment(
            comment_id=self._next_comment_id,
            body=f"a human quoting us: {marker}",
            author_login="human",
            author_id=424242,
            marker_present=True,
        )
        self.comments.append(comment)
        self._next_comment_id += 1
        return comment

    def publish_review(
        self, number: int, *, commit_id: str, body: str, marker: AttemptMarker
    ) -> InputReview:
        """Publish a COMMENT-event review against ``commit_id``."""
        _ = marker
        self._record("publish_review", number, commit_id, body)
        review = InputReview(
            review_id=self._next_review_id,
            body=body,
            author_login=self.agent_login,
            author_id=self.agent_id,
            commit_id=commit_id,
            marker_present=True,
        )
        self._next_review_id += 1
        self.reviews.append(review)
        return review

    def list_reviews(self, number: int) -> list[InputReview]:
        """List the pull request's reviews, oldest first."""
        self._record("list_reviews", number)
        return list(self.reviews)

    def find_review_by_marker(
        self, number: int, marker: AttemptMarker
    ) -> InputReview | None:
        """Find our review by Attempt Marker."""
        rendered = self.format_marker(marker)
        self._record("find_review_by_marker", number, rendered)
        for review in self.reviews:
            if rendered not in review.body:
                continue
            if not self._author_matches(review.author_login, review.author_id):
                continue
            return review
        return None

    def open_pull_request(
        self,
        *,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        draft: bool = False,
        marker: AttemptMarker,
    ) -> InputPullRequest:
        """Open one pull request from ``head_branch``, body already rendered."""
        _ = marker
        self._record(
            "open_pull_request", title, head_branch, base_branch, body, draft
        )
        opened = InputPullRequest(
            number=self._next_pull_number,
            url=f"https://github.test/{self.owner}/{self.repo}/pull/{self._next_pull_number}",
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
            state="open",
            author_login=self.agent_login,
            author_id=self.agent_id,
            marker_present=True,
            draft=draft,
        )
        self._next_pull_number += 1
        self.opened_pull_requests.append(opened)
        return opened

    def find_pull_request(
        self, head_branch: str, marker: AttemptMarker
    ) -> InputPullRequest | None:
        """Find our pull request by head branch and Attempt Marker."""
        rendered = self.format_marker(marker)
        self._record("find_pull_request", head_branch, rendered)
        matching: list[InputPullRequest] = []
        for candidate in self.opened_pull_requests:
            if candidate.head_branch != head_branch:
                continue
            if rendered not in candidate.body:
                continue
            if not self._author_matches(
                candidate.author_login, candidate.author_id
            ):
                continue
            matching.append(candidate)
        for candidate in matching:
            if candidate.state == "open":
                return candidate
        return matching[0] if matching else None

    def remove_label(self, number: int, input_type: InputType, label: str) -> None:
        """Remove one label; absent is success (§15.3)."""
        self._record("remove_label", number, input_type, label)
        held = self.inputs.get(number)
        if held is not None:
            self.inputs[number] = replace(
                held,
                labels=tuple(name for name in held.labels if name != label),
            )

    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None:
        """Apply end labels; empty is a no-op (§15.3)."""
        self._record("add_labels", number, input_type, labels)
        if not labels:
            return
        held = self._held(number, input_type)
        current = list(held.labels)
        for name in labels:
            if name not in current:
                current.append(name)
        self.inputs[number] = replace(held, labels=tuple(current))

    def seed_foreign_marker_pull_request(
        self, head_branch: str, marker: str
    ) -> InputPullRequest:
        """Plant a human pull request carrying our marker: never our receipt."""
        opened = InputPullRequest(
            number=self._next_pull_number,
            url=f"https://github.test/{self.owner}/{self.repo}/pull/{self._next_pull_number}",
            title="human PR",
            head_branch=head_branch,
            base_branch="main",
            body=f"a human quoting us: {marker}",
            state="open",
            author_login="human",
            author_id=424242,
            marker_present=True,
            draft=False,
        )
        self.opened_pull_requests.append(opened)
        self._next_pull_number += 1
        return opened

    def seed_foreign_marker_review(self, marker: str) -> InputReview:
        """Plant a human review carrying our marker: never our receipt."""
        review = InputReview(
            review_id=self._next_review_id,
            body=f"a human quoting us: {marker}",
            author_login="human",
            author_id=424242,
            commit_id="deadbeef",
            marker_present=True,
        )
        self.reviews.append(review)
        self._next_review_id += 1
        return review
