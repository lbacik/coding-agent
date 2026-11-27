"""A second vendor needs no Markdown-hidden HTML: the marker travels as a value.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14 (Effect
Intent receipts). The core passes the structured ``AttemptMarker`` to every
write and lookup; each adapter decides how to embed it and how to find it.
This suite proves the seam with a property-bag stub adapter that stores the
marker fields beside each record and round-trips writes and lookups through
the value — never rendering or parsing the GitHub HTML comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coding_agent.inputs.claim import claim_marker_for, terminal_marker_for
from coding_agent.inputs.github import format_attempt_marker
from coding_agent.inputs.marker import AttemptMarker
from coding_agent.inputs.port import (
    InputComment,
    InputPullRequest,
    InputReview,
    InputType,
)


@dataclass
class _PropertyBagTracker:
    """A second-vendor stub: markers live in a property, not in the body."""

    agent_login: str = "coding-agent"
    agent_id: int | None = 1001
    _comment_marks: dict[int, AttemptMarker] = field(default_factory=dict)
    _review_marks: dict[int, AttemptMarker] = field(default_factory=dict)
    _pr_marks: dict[int, AttemptMarker] = field(default_factory=dict)
    _comments: list[InputComment] = field(default_factory=list)
    _reviews: list[InputReview] = field(default_factory=list)
    _pulls: list[InputPullRequest] = field(default_factory=list)

    def format_marker(self, marker: AttemptMarker) -> str:
        """A visible, non-HTML embedding: no comment fence to hide behind."""
        return (
            f"[attempt-marker attempt={marker.attempt} kind={marker.kind} "
            f"step={marker.step or '-'} n={marker.execution_number} "
            f"ordinal={marker.ordinal}]"
        )

    def delivery_reference(self, ref: str, *, draft: bool = False) -> str:
        """A non-GitHub link line: core never formats vendor text itself."""
        return f"Refs issue {ref}" if draft else f"Fixes issue {ref}"

    def post_claim_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        _ = (number, input_type)
        return self.post_comment(number, input_type, body, marker)

    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: AttemptMarker
    ) -> InputComment | None:
        _ = (number, input_type)
        for comment in self._comments:
            if self._comment_marks.get(comment.comment_id) != marker:
                continue
            if comment.author_login != self.agent_login:
                continue
            return comment
        return None

    def post_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        _ = (number, input_type)
        comment = InputComment(
            comment_id=len(self._comments) + 1,
            body=body,
            author_login=self.agent_login,
            author_id=self.agent_id,
            marker_present=True,
        )
        self._comments.append(comment)
        self._comment_marks[comment.comment_id] = marker
        return comment

    def publish_review(
        self, number: int, *, commit_id: str, body: str, marker: AttemptMarker
    ) -> InputReview:
        _ = number
        review = InputReview(
            review_id=len(self._reviews) + 1,
            body=body,
            author_login=self.agent_login,
            author_id=self.agent_id,
            commit_id=commit_id,
            marker_present=True,
        )
        self._reviews.append(review)
        self._review_marks[review.review_id] = marker
        return review

    def find_review_by_marker(
        self, number: int, marker: AttemptMarker
    ) -> InputReview | None:
        _ = number
        for review in self._reviews:
            if self._review_marks.get(review.review_id) != marker:
                continue
            if review.author_login != self.agent_login:
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
        _ = title
        opened = InputPullRequest(
            number=len(self._pulls) + 1,
            url=f"https://example.test/pull/{len(self._pulls) + 1}",
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
        self._pulls.append(opened)
        self._pr_marks[opened.number] = marker
        return opened

    def find_pull_request(
        self, head_branch: str, marker: AttemptMarker
    ) -> InputPullRequest | None:
        for candidate in self._pulls:
            if candidate.head_branch != head_branch:
                continue
            if self._pr_marks.get(candidate.number) != marker:
                continue
            if candidate.author_login != self.agent_login:
                continue
            return candidate
        return None


def test_stub_rendering_uses_no_github_html() -> None:
    """The second vendor embeds the marker without the HTML comment fence."""
    marker = AttemptMarker(
        attempt="implement#7/1",
        kind="post-comment",
        step="process",
        execution_number=1,
        ordinal=0,
    )
    assert "<!--" not in _PropertyBagTracker().format_marker(marker)
    assert "coding-agent attempt=" not in _PropertyBagTracker().format_marker(marker)


def test_stub_comment_round_trip_through_the_value() -> None:
    """Post then find by the same ``AttemptMarker`` value, not by text."""
    tracker = _PropertyBagTracker()
    marker = AttemptMarker(
        attempt="implement#7/1",
        kind="post-comment",
        step="process",
        execution_number=1,
        ordinal=0,
    )
    posted = tracker.post_comment(7, "issue", "a document", marker)
    found = tracker.find_comment_by_marker(7, "issue", marker)
    assert found is not None
    assert found.comment_id == posted.comment_id
    assert found.body == "a document"
    assert (
        tracker.find_comment_by_marker(
            7,
            "issue",
            AttemptMarker(
                attempt="implement#7/1",
                kind="post-comment",
                step="process",
                execution_number=1,
                ordinal=1,
            ),
        )
        is None
    )


def test_stub_review_round_trip_through_the_value() -> None:
    """Reviews round-trip through the value with no marker text in the body."""
    tracker = _PropertyBagTracker()
    marker = AttemptMarker(
        attempt="implement#7/1",
        kind="publish-review",
        step="publish",
        execution_number=1,
        ordinal=0,
    )
    published = tracker.publish_review(
        31, commit_id="h" * 40, body="a document", marker=marker
    )
    found = tracker.find_review_by_marker(31, marker)
    assert found is not None
    assert found.review_id == published.review_id
    assert found.body == "a document"


def test_stub_pull_request_round_trip_through_the_value() -> None:
    """Pull requests round-trip through the value beside the link line."""
    tracker = _PropertyBagTracker()
    marker = AttemptMarker(
        attempt="implement#7/1",
        kind="open-pull-request",
        step="open-pr",
        execution_number=1,
        ordinal=0,
    )
    reference = tracker.delivery_reference("7", draft=False)
    assert "Closes" not in reference
    opened = tracker.open_pull_request(
        title="a title",
        head_branch="agent/branch",
        base_branch="main",
        body=f"{reference}\n\na document",
        draft=False,
        marker=marker,
    )
    assert opened.draft is False
    found = tracker.find_pull_request("agent/branch", marker)
    assert found is not None
    assert found.number == opened.number
    assert found.body == f"{reference}\n\na document"


def test_stub_claim_and_terminal_markers_are_ordinary_values() -> None:
    """Claim and terminal markers are ordinary values with the reserved shape."""
    tracker = _PropertyBagTracker()
    claim = claim_marker_for("implement#7/1")
    posted = tracker.post_claim_comment(7, "issue", "claiming", claim)
    assert tracker.find_comment_by_marker(7, "issue", claim) is not None
    assert posted.comment_id == 1
    terminal = terminal_marker_for("implement#7/1")
    assert terminal.step == ""
    assert terminal.execution_number == 0
    assert tracker.format_marker(terminal) != format_attempt_marker(terminal)
