"""One Effect module per marker-looked-up write kind (§14, §14.2).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.5
(``open-pull-request``), §10.6 (``publish-review``), §10.7
(``post-comment``), §14.1 (the Effect Intent key travels in the write's
Attempt Marker; expected target state) and §15.3 (the terminal comment).

Each module owns its typed intent target — which serialises to and from
today's ``expected_target`` dict, so the stored format never changes — the
existing-effect lookup by Attempt Marker, the optional precondition, the
write itself and the remote-id extraction:

- ``post-comment`` and the terminal comment share the comment lookup and
  write; only their marker kind and target shape differ.
- The review precondition is the head compare-and-swap: the recorded Input
  Head must still equal the live head, so settlement refuses to publish
  against a moved head (ADR 0028) exactly like the forward path does.
- The pull-request lookup finds an open or human-closed own pull request by
  marker, so a human-closed own pull request is a receipt and is never
  re-created.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.runtime.effects.driver import EffectContext


@dataclass(frozen=True)
class CommentTarget:
    """The ``post-comment`` expected target (§10.7, §14.1)."""

    input_number: int
    input_type: InputType

    def to_dict(self) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§14.1)."""
        return {
            "operation": "post-comment",
            "input_number": self.input_number,
            "input_type": self.input_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CommentTarget:
        """Parse today's ``expected_target`` dict; never raises on old rows."""
        number = data.get("input_number")
        input_type = data.get("input_type")
        return cls(
            input_number=number if isinstance(number, int) else 0,
            input_type=input_type
            if input_type in ("issue", "pull-request")
            else "issue",
        )


@dataclass(frozen=True)
class TerminalCommentTarget:
    """The terminal-comment expected target (§15.3): no fields.

    Intent rows carry ``{"terminal-write": "terminal"}``; the input the
    comment lands on travels in the :class:`EffectContext`, never here.
    """

    def to_dict(self) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§15.3)."""
        return {"terminal-write": "terminal"}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TerminalCommentTarget:
        """Parse any terminal-shaped ``expected_target`` dict, ignoring it."""
        _ = data
        return cls()


@dataclass(frozen=True)
class ReviewTarget:
    """The ``publish-review`` expected target (§10.6, §14.1)."""

    input_number: int
    commit_id: str

    def to_dict(self) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§14.1)."""
        return {
            "operation": "publish-review",
            "input_number": self.input_number,
            "commit_id": self.commit_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ReviewTarget:
        """Parse today's ``expected_target`` dict; never raises on old rows."""
        number = data.get("input_number")
        commit = data.get("commit_id")
        return cls(
            input_number=number if isinstance(number, int) else 0,
            commit_id=commit if isinstance(commit, str) else "",
        )


@dataclass(frozen=True)
class PullRequestTarget:
    """The ``open-pull-request`` expected target (§10.5, §14.1)."""

    head_branch: str
    base_branch: str
    title: str
    draft: bool
    revision: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§14.1)."""
        return {
            "operation": "open-pull-request",
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "revision": self.revision,
            "draft": self.draft,
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PullRequestTarget:
        """Parse today's ``expected_target`` dict; never raises on old rows."""
        head = data.get("head_branch")
        base = data.get("base_branch")
        title = data.get("title")
        draft = data.get("draft")
        revision = data.get("revision")
        return cls(
            head_branch=head if isinstance(head, str) else "",
            base_branch=base if isinstance(base, str) else "main",
            title=title if isinstance(title, str) else "",
            draft=draft if isinstance(draft, bool) else False,
            revision=revision if isinstance(revision, str) else "",
        )


TargetT = TypeVar("TargetT")
"""One module's typed intent target."""


class EffectModule(Protocol[TargetT]):
    """One marker-looked-up write kind: target shape, lookup, write, receipt."""

    @property
    def operation(self) -> str:
        """The operation or terminal-write kind this module settles."""
        ...

    @property
    def marker_kind(self) -> str:
        """The Attempt Marker kind keying this module's writes (§14.1)."""
        ...

    def target_to_dict(self, target: TargetT) -> dict[str, object]:
        """Serialise the typed target to the stored ``expected_target``."""
        ...

    def target_from_dict(self, data: Mapping[str, object]) -> TargetT:
        """Parse the stored ``expected_target``; total over old rows."""
        ...

    def find(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: TargetT,
        marker: str,
    ) -> Any | None:
        """Find our write by Attempt Marker (§14); ``None`` where absent."""
        ...

    def check(
        self, tracker: StepTrackerPort, ctx: EffectContext, target: TargetT
    ) -> str | None:
        """The kind's precondition; the ``not-performed`` reason, or ``None``."""
        ...

    def perform(
        self, tracker: StepTrackerPort, ctx: EffectContext, target: TargetT, body: str
    ) -> Any:
        """Perform the write once, body already rendered; the tracker result."""
        ...

    def remote_id(self, result: Any) -> str | None:
        """The receipt's remote id: comment, review or pull-request number."""
        ...


def _comment_remote_id(result: Any) -> str | None:
    comment_id = getattr(result, "comment_id", None)
    return str(comment_id) if isinstance(comment_id, int) else None


class CommentEffect:
    """The ``post-comment`` write kind (§10.7, §14)."""

    operation = "post-comment"
    marker_kind = "post-comment"

    def target_to_dict(self, target: CommentTarget) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§14.1)."""
        return target.to_dict()

    def target_from_dict(self, data: Mapping[str, object]) -> CommentTarget:
        """Parse today's ``expected_target`` dict."""
        return CommentTarget.from_dict(data)

    def find(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: CommentTarget,
        marker: str,
    ) -> Any | None:
        """Find our comment by Attempt Marker (§14)."""
        _ = target
        return tracker.find_comment_by_marker(ctx.number, ctx.input_type, marker)

    def check(
        self, tracker: StepTrackerPort, ctx: EffectContext, target: CommentTarget
    ) -> str | None:
        """A comment has no precondition: it posts on any input state."""
        _ = (tracker, ctx, target)
        return None

    def perform(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: CommentTarget,
        body: str,
    ) -> Any:
        """Post one comment on the input, body already rendered (§10.7)."""
        _ = target
        return tracker.post_comment(ctx.number, ctx.input_type, body)

    def remote_id(self, result: Any) -> str | None:
        """The comment id the receipt carries (§14.1)."""
        return _comment_remote_id(result)


class TerminalCommentEffect:
    """The terminal comment write kind (§15.3).

    The same marker lookup and write as a comment; only the marker kind and
    the target shape differ.
    """

    operation = "terminal-comment"
    marker_kind = "terminal"

    def target_to_dict(self, target: TerminalCommentTarget) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§15.3)."""
        return target.to_dict()

    def target_from_dict(self, data: Mapping[str, object]) -> TerminalCommentTarget:
        """Parse any terminal-shaped ``expected_target`` dict."""
        return TerminalCommentTarget.from_dict(data)

    def find(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: TerminalCommentTarget,
        marker: str,
    ) -> Any | None:
        """Find our terminal comment by Attempt Marker (§14, §15.3)."""
        _ = target
        return tracker.find_comment_by_marker(ctx.number, ctx.input_type, marker)

    def check(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: TerminalCommentTarget,
    ) -> str | None:
        """The terminal comment posts even on a closed or merged input."""
        _ = (tracker, ctx, target)
        return None

    def perform(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: TerminalCommentTarget,
        body: str,
    ) -> Any:
        """Post the terminal comment, body already rendered (§15.3)."""
        _ = target
        return tracker.post_comment(ctx.number, ctx.input_type, body)

    def remote_id(self, result: Any) -> str | None:
        """The comment id the receipt carries (§14.1)."""
        return _comment_remote_id(result)


class ReviewEffect:
    """The ``publish-review`` write kind (§10.6, §14)."""

    operation = "publish-review"
    marker_kind = "publish-review"

    def target_to_dict(self, target: ReviewTarget) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§14.1)."""
        return target.to_dict()

    def target_from_dict(self, data: Mapping[str, object]) -> ReviewTarget:
        """Parse today's ``expected_target`` dict."""
        return ReviewTarget.from_dict(data)

    def find(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: ReviewTarget,
        marker: str,
    ) -> Any | None:
        """Find our review by Attempt Marker (§14)."""
        _ = target
        return tracker.find_review_by_marker(ctx.number, marker)

    def check(
        self, tracker: StepTrackerPort, ctx: EffectContext, target: ReviewTarget
    ) -> str | None:
        """The head compare-and-swap: refuse to publish on a moved head (§10.6).

        The recorded Input Head must still equal the live head; settlement
        re-checks this exactly like the forward path (ADR 0028), even where
        no Gate is supplied.
        """
        if not target.commit_id:
            return "error: no commit id in the recorded intent"
        live = tracker.fetch_input(ctx.number, "pull-request")
        head = live.head_sha
        if head is None:
            return "error: the pull request reports no head sha"
        if head != target.commit_id:
            return "stale-head: the remote head moved"
        return None

    def perform(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: ReviewTarget,
        body: str,
    ) -> Any:
        """Publish a COMMENT-event review against the recorded head (§10.6)."""
        return tracker.publish_review(ctx.number, commit_id=target.commit_id, body=body)

    def remote_id(self, result: Any) -> str | None:
        """The review id the receipt carries (§14.1)."""
        review_id = getattr(result, "review_id", None)
        return str(review_id) if isinstance(review_id, int) else None


class PullRequestEffect:
    """The ``open-pull-request`` write kind (§10.5, §14)."""

    operation = "open-pull-request"
    marker_kind = "open-pull-request"

    def target_to_dict(self, target: PullRequestTarget) -> dict[str, object]:
        """Serialise to today's ``expected_target`` dict (§14.1)."""
        return target.to_dict()

    def target_from_dict(self, data: Mapping[str, object]) -> PullRequestTarget:
        """Parse today's ``expected_target`` dict."""
        return PullRequestTarget.from_dict(data)

    def find(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: PullRequestTarget,
        marker: str,
    ) -> Any | None:
        """Find our pull request by head branch and Attempt Marker (§14).

        Open preferred where both states match; a human-closed own pull
        request is found too, so it is a receipt and is never re-created.
        """
        _ = ctx
        return tracker.find_pull_request(target.head_branch, marker)

    def check(
        self, tracker: StepTrackerPort, ctx: EffectContext, target: PullRequestTarget
    ) -> str | None:
        """No precondition beyond the lookup: a closed own PR is a receipt."""
        _ = (tracker, ctx, target)
        return None

    def perform(
        self,
        tracker: StepTrackerPort,
        ctx: EffectContext,
        target: PullRequestTarget,
        body: str,
    ) -> Any:
        """Open one pull request from the recorded branch (§10.5)."""
        _ = ctx
        return tracker.open_pull_request(
            title=target.title,
            head_branch=target.head_branch,
            base_branch=target.base_branch,
            body=body,
            draft=target.draft,
        )

    def remote_id(self, result: Any) -> str | None:
        """The pull-request number the receipt carries (§14.1)."""
        number = getattr(result, "number", None)
        return str(number) if isinstance(number, int) else None
