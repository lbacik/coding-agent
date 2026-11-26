"""One effect driver plus one Effect module per marker-looked-up write kind.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14 (every
external write is an Effect Intent settled once: the key travels in the
write's Attempt Marker) and §14.2 (settlement on restart: look the write up
remotely by its key; a landed write records the receipt and is never written
twice; an absent write is performed once where the preconditions hold,
otherwise recorded ``not-performed``).

Matrix row ids: REC-4 … REC-9, REC-12.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from coding_agent.github.errors import GitHubFailure
from coding_agent.inputs.github import StepTrackerError
from coding_agent.inputs.port import InputComment, InputType, TrackedInput
from coding_agent.ledger.store import RunLedger, step_effect_marker
from coding_agent.runtime.effects.driver import (
    EffectOutcome,
    Failed,
    Landed,
    Performed,
    drive_effect,
)
from coding_agent.runtime.effects.modules import (
    CommentTarget,
    PullRequestTarget,
    ReviewTarget,
    TerminalCommentTarget,
)
from coding_agent.runtime.effects.registry import (
    OPEN_PULL_REQUEST,
    POST_COMMENT,
    PUBLISH_REVIEW,
    STEP_EFFECT_MODULES,
    STEP_EFFECT_OPERATIONS,
    TERMINAL_COMMENT,
    module_for_intent,
    module_for_operation,
)
from coding_agent.runtime.step_finalisation import (
    FINALISATION_EXECUTION,
    FINALISATION_STEP,
    TERMINAL_COMMENT_ORDINAL,
)
from coding_agent.runtime.step_operations import LOCAL_OPERATIONS
from coding_agent.runtime.step_recovery import (
    settle_open_step_effects,
    settle_step_effect,
)
from coding_agent.workflow.schema import OPERATIONS
from support.fake_step_tracker import FakeStepTracker

ATTEMPT = "effects#1/7"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    handle.open_attempt(ATTEMPT, effective_configuration={"workflow": "effects"})
    yield handle
    handle.close()


def test_effect_operations_cover_the_catalog_exactly() -> None:
    """Adding a write kind means one module and one registry entry (§10)."""
    assert LOCAL_OPERATIONS | STEP_EFFECT_OPERATIONS == set(OPERATIONS)
    assert set(STEP_EFFECT_MODULES) == STEP_EFFECT_OPERATIONS


def test_registry_resolves_step_operations() -> None:
    """Neither publication nor settlement carries a write-kind ladder (§14)."""
    assert module_for_operation("post-comment") is POST_COMMENT
    assert module_for_operation("publish-review") is PUBLISH_REVIEW
    assert module_for_operation("open-pull-request") is OPEN_PULL_REQUEST
    assert module_for_operation("terminal-comment") is TERMINAL_COMMENT
    assert module_for_operation("commit-and-push") is None
    assert module_for_operation("check-readiness") is None


def test_targets_round_trip_todays_format(ledger: RunLedger) -> None:
    """The stored ``expected_target`` format never changes (§14.1)."""
    assert CommentTarget(input_number=7, input_type="issue").to_dict() == {
        "operation": "post-comment",
        "input_number": 7,
        "input_type": "issue",
    }
    assert ReviewTarget(input_number=31, commit_id="h" * 40).to_dict() == {
        "operation": "publish-review",
        "input_number": 31,
        "commit_id": "h" * 40,
    }
    assert PullRequestTarget(
        head_branch="agent/x",
        base_branch="main",
        title="Add a greeting",
        draft=False,
        revision="r" * 40,
    ).to_dict() == {
        "operation": "open-pull-request",
        "base_branch": "main",
        "head_branch": "agent/x",
        "revision": "r" * 40,
        "draft": False,
        "title": "Add a greeting",
    }
    assert TerminalCommentTarget().to_dict() == {"terminal-write": "terminal"}
    ledger.record_step_effect_intent(
        ATTEMPT,
        "explain",
        1,
        0,
        rendered_payload="post-comment payload",
        expected_target={
            "operation": "post-comment",
            "input_number": 7,
            "input_type": "issue",
        },
    )
    row = ledger.get_step_effect(ATTEMPT, "explain", 1, 0)
    assert row is not None
    assert module_for_intent(row) is POST_COMMENT
    assert POST_COMMENT.target_from_dict(dict(row.expected_target or {})) == (
        CommentTarget(input_number=7, input_type="issue")
    )


def test_targets_parse_legacy_rows(ledger: RunLedger) -> None:
    """Settlement still settles intent rows the current code wrote (§14.2)."""
    ledger.record_step_effect_intent(
        ATTEMPT,
        "legacy-empty",
        1,
        0,
        rendered_payload="legacy payload",
        expected_target={"operation": ""},
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "legacy-terminal",
        1,
        0,
        rendered_payload="legacy payload",
        expected_target={"operation": "terminal"},
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        TERMINAL_COMMENT_ORDINAL,
        rendered_payload="terminal payload",
        expected_target={"terminal-write": "terminal"},
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "push",
        1,
        0,
        rendered_payload="push payload",
        expected_target={"operation": "commit-and-push"},
    )
    for step_id in ("legacy-empty", "legacy-terminal"):
        row = ledger.get_step_effect(ATTEMPT, step_id, 1, 0)
        assert row is not None
        assert module_for_intent(row) is TERMINAL_COMMENT
    terminal = ledger.get_step_effect(
        ATTEMPT, FINALISATION_STEP, FINALISATION_EXECUTION, TERMINAL_COMMENT_ORDINAL
    )
    assert terminal is not None
    assert module_for_intent(terminal) is TERMINAL_COMMENT
    push = ledger.get_step_effect(ATTEMPT, "push", 1, 0)
    assert push is not None
    assert module_for_intent(push) is None


def test_settle_publish_review_refuses_moved_head_without_gate(
    ledger: RunLedger,
) -> None:
    """ADR 0028: settlement re-checks the head compare-and-swap (§10.6, §14.2).

    The recorded Input Head no longer equals the live head, so the open
    intent settles ``not-performed`` with a ``stale-head`` reason and nothing
    is published — even where no Gate is supplied.
    """
    tracker = FakeStepTracker(agent_id=1001)
    tracker.inputs[31] = TrackedInput(
        number=31,
        input_type="pull-request",
        title="a title",
        body="a body",
        is_open=True,
        labels=(),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        head_sha="n" * 40,
        head_branch="feature",
        head_repo="octocat/sandbox",
        base_branch="main",
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "publish",
        1,
        0,
        rendered_payload=f"publish-review {ATTEMPT}: review {'h' * 40}\nbody",
        expected_target={
            "operation": "publish-review",
            "input_number": 31,
            "commit_id": "h" * 40,
        },
    )
    intent = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert intent is not None
    assert (
        settle_step_effect(
            tracker=tracker,
            ledger=ledger,
            intent=intent,
            number=31,
            input_type="pull-request",
        )
        == "not-performed"
    )
    assert tracker.reviews == []
    record = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert record is not None
    assert record.status == "not-performed"
    assert "stale-head" in record.rendered_payload


def test_settle_failure_leaves_the_intent_open(ledger: RunLedger) -> None:
    """A lookup failure propagates: the intent stays open (§14.2, §16.3)."""
    tracker = FakeStepTracker(agent_id=1001)
    tracker.fail_on = "find_review_by_marker"
    ledger.record_step_effect_intent(
        ATTEMPT,
        "publish",
        1,
        0,
        rendered_payload=f"publish-review {ATTEMPT}: review {'h' * 40}\nbody",
        expected_target={
            "operation": "publish-review",
            "input_number": 31,
            "commit_id": "h" * 40,
        },
    )
    intent = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert intent is not None
    with pytest.raises(RuntimeError, match="fake tracker failure"):
        settle_step_effect(
            tracker=tracker,
            ledger=ledger,
            intent=intent,
            number=31,
            input_type="pull-request",
        )
    record = ledger.get_step_effect(ATTEMPT, "publish", 1, 0)
    assert record is not None
    assert record.status == "intent"


def test_ledger_fixture_in_todays_format_settles(ledger: RunLedger) -> None:
    """Intent rows written by the current code settle after the change (§14.2).

    Each of post-comment, publish-review, open-pull-request, the terminal
    comment and a legacy ``""`` operation row: landed writes record the
    receipt and are never written twice, one absent comment is performed.
    """
    tracker = FakeStepTracker(agent_id=1001)
    number = 7
    input_type: InputType = "issue"

    comment_marker = step_effect_marker(ATTEMPT, "explain", 1, 0, kind="post-comment")
    ledger.record_step_effect_intent(
        ATTEMPT,
        "explain",
        1,
        0,
        rendered_payload=f"post-comment {ATTEMPT}: comment on issue 7\nfirst",
        expected_target={
            "operation": "post-comment",
            "input_number": number,
            "input_type": "issue",
        },
    )
    tracker.post_comment(number, input_type, f"already landed\n{comment_marker}")

    review_marker = step_effect_marker(ATTEMPT, "publish", 1, 0, kind="publish-review")
    ledger.record_step_effect_intent(
        ATTEMPT,
        "publish",
        1,
        0,
        rendered_payload=f"publish-review {ATTEMPT}: review {'h' * 40}\nsecond",
        expected_target={
            "operation": "publish-review",
            "input_number": 31,
            "commit_id": "h" * 40,
        },
    )
    tracker.publish_review(31, commit_id="h" * 40, body=f"landed\n{review_marker}")

    pr_marker = step_effect_marker(
        ATTEMPT, "open-pr", 1, 0, kind="open-pull-request"
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        "open-pr",
        1,
        0,
        rendered_payload=f"open-pull-request {ATTEMPT}: Title\nthird",
        expected_target={
            "operation": "open-pull-request",
            "base_branch": "main",
            "head_branch": "agent/effects/7/1",
            "revision": "r" * 40,
            "draft": False,
            "title": "Title",
        },
    )
    tracker.open_pull_request(
        title="Title",
        head_branch="agent/effects/7/1",
        base_branch="main",
        body=f"landed\n{pr_marker}",
        draft=False,
    )

    legacy_marker = step_effect_marker(ATTEMPT, "legacy", 1, 0, kind="terminal")
    ledger.record_step_effect_intent(
        ATTEMPT,
        "legacy",
        1,
        0,
        rendered_payload="legacy payload",
        expected_target={"operation": ""},
    )
    tracker.post_comment(number, input_type, f"legacy landed\n{legacy_marker}")

    ledger.record_step_effect_intent(
        ATTEMPT,
        "explain-late",
        1,
        0,
        rendered_payload=f"post-comment {ATTEMPT}: comment on issue 7\nlate",
        expected_target={
            "operation": "post-comment",
            "input_number": number,
            "input_type": "issue",
        },
    )

    settled = settle_open_step_effects(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        number=number,
        input_type=input_type,
    )
    assert settled == {"receipt": 4, "performed": 1, "not-performed": 0}
    assert len(tracker.pull_requests) == 1
    for step_id in ("explain", "publish", "open-pr", "legacy"):
        record = ledger.get_step_effect(ATTEMPT, step_id, 1, 0)
        assert record is not None
        assert record.status == "receipt", step_id
    late = ledger.get_step_effect(ATTEMPT, "explain-late", 1, 0)
    assert late is not None
    assert late.status == "receipt"

    terminal_marker = step_effect_marker(
        ATTEMPT,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        TERMINAL_COMMENT_ORDINAL,
        kind="terminal",
    )
    ledger.record_step_effect_intent(
        ATTEMPT,
        FINALISATION_STEP,
        FINALISATION_EXECUTION,
        TERMINAL_COMMENT_ORDINAL,
        rendered_payload="terminal body",
        expected_target={"terminal-write": "terminal"},
    )
    tracker.post_comment(number, input_type, f"terminal\n{terminal_marker}")
    terminal = ledger.get_step_effect(
        ATTEMPT, FINALISATION_STEP, FINALISATION_EXECUTION, TERMINAL_COMMENT_ORDINAL
    )
    assert terminal is not None
    assert (
        settle_step_effect(
            tracker=tracker,
            ledger=ledger,
            intent=terminal,
            number=number,
            input_type=input_type,
        )
        == "receipt"
    )


def test_credential_failure_propagates_from_the_driver(ledger: RunLedger) -> None:
    """A Credential Failure is never swallowed or recorded (§16.3, REC-17)."""
    from coding_agent.github.errors import CredentialFailure

    class _BadTracker(FakeStepTracker):
        def find_comment_by_marker(
            self, number: int, input_type: InputType, marker: str
        ) -> InputComment | None:
            raise CredentialFailure("GET", "/repos/o/s/issues/7/comments")

    tracker = _BadTracker(agent_id=1001)
    with pytest.raises(CredentialFailure):
        drive_effect(
            tracker=tracker,
            ledger=ledger,
            attempt_id=ATTEMPT,
            step_id="explain",
            seq=1,
            ordinal=0,
            number=7,
            input_type="issue",
            module=POST_COMMENT,
            target=CommentTarget(input_number=7, input_type="issue"),
            body="body",
            intent_payload="post-comment payload",
            record_intent=True,
        )


def _transient_error(status_code: int) -> StepTrackerError:
    """One tracker write failure marked transient (§14.4)."""
    return StepTrackerError(
        f"POST /repos/octocat/sandbox/issues/7/comments returned "
        f"{status_code}: boom",
        transient=True,
        method="POST",
        path="/repos/octocat/sandbox/issues/7/comments",
        status_code=status_code,
    )


class _ScriptedPostTracker(FakeStepTracker):
    """A tracker whose next comment posts fail as scripted, then post.

    A scripted failure raised after planting the comment simulates the
    §14.4 504: the write landed remotely even though the response was
    lost, so the re-lookup finds it and nothing is posted twice.
    """

    def __init__(
        self,
        script: list[BaseException | None],
        *,
        plant_before_fail: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._script = list(script)
        self._plant_before_fail = plant_before_fail

    def post_comment(
        self, number: int, input_type: InputType, body: str
    ) -> InputComment:
        if self._script:
            action = self._script.pop(0)
            if action is not None:
                if self._plant_before_fail:
                    super().post_comment(number, input_type, body)
                else:
                    self._record("post_comment", number, input_type, body)
                raise action
        return super().post_comment(number, input_type, body)


def _drive_post_comment(
    *,
    tracker: FakeStepTracker,
    ledger: RunLedger,
    body: str,
    sleep: Callable[[float], None],
) -> EffectOutcome:
    """Drive one ``post-comment`` effect through the scripted tracker."""
    return drive_effect(
        tracker=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        step_id="work",
        seq=1,
        ordinal=0,
        number=7,
        input_type="issue",
        module=POST_COMMENT,
        target=CommentTarget(input_number=7, input_type="issue"),
        body=body,
        intent_payload="post-comment payload",
        record_intent=True,
        sleep=sleep,
    )


def _post_comment_calls(tracker: FakeStepTracker) -> int:
    return sum(1 for name in tracker.call_names() if name == "post_comment")


def _find_calls(tracker: FakeStepTracker) -> int:
    return sum(1 for name in tracker.call_names() if name == "find_comment_by_marker")


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_transient_5xx_then_success_posts_once(
    ledger: RunLedger, status_code: int
) -> None:
    """§14.4: a 502, 503 or 504 then a 201 posts exactly one comment."""
    sleeps: list[float] = []
    marker = step_effect_marker(ATTEMPT, "work", 1, 0, kind="post-comment")
    tracker = _ScriptedPostTracker([_transient_error(status_code)], agent_id=1001)

    outcome = _drive_post_comment(
        tracker=tracker,
        ledger=ledger,
        body=f"hello\n{marker}\n",
        sleep=sleeps.append,
    )

    assert isinstance(outcome, Performed)
    assert [comment.body for comment in tracker.comments] == [f"hello\n{marker}\n"]
    assert _post_comment_calls(tracker) == 2
    assert _find_calls(tracker) == 2
    assert sleeps == [1.0]
    receipt = ledger.get_step_effect(ATTEMPT, "work", 1, 0)
    assert receipt is not None
    assert receipt.status == "receipt"


def test_transient_504_that_landed_posts_nothing_more(ledger: RunLedger) -> None:
    """§14.4: where the write landed despite the 504, the re-lookup finds
    it: the receipt is recorded and no second comment is posted."""
    sleeps: list[float] = []
    marker = step_effect_marker(ATTEMPT, "work", 1, 0, kind="post-comment")
    tracker = _ScriptedPostTracker(
        [_transient_error(504)], agent_id=1001, plant_before_fail=True
    )

    outcome = _drive_post_comment(
        tracker=tracker,
        ledger=ledger,
        body=f"hello\n{marker}\n",
        sleep=sleeps.append,
    )

    assert isinstance(outcome, Landed)
    assert [comment.body for comment in tracker.comments] == [f"hello\n{marker}\n"]
    assert _post_comment_calls(tracker) == 1
    assert sleeps == [1.0]
    receipt = ledger.get_step_effect(ATTEMPT, "work", 1, 0)
    assert receipt is not None
    assert receipt.status == "receipt"


def test_connection_error_and_timeout_retry_like_a_5xx(ledger: RunLedger) -> None:
    """§14.4: a connection error and a timeout are the same transient class."""
    sleeps: list[float] = []
    tracker = _ScriptedPostTracker(
        [
            requests.ConnectionError("connection reset"),
            requests.Timeout("timed out"),
        ],
        agent_id=1001,
    )

    outcome = _drive_post_comment(
        tracker=tracker, ledger=ledger, body="hello", sleep=sleeps.append
    )

    assert isinstance(outcome, Performed)
    assert [comment.body for comment in tracker.comments] == ["hello"]
    assert _post_comment_calls(tracker) == 3
    assert sleeps == [1.0, 2.0]


def test_transient_exhaustion_is_infrastructure_class(ledger: RunLedger) -> None:
    """§14.4: past the bound the write ends ``error`` as infra ``github``,
    within 3 writes and 5 minutes of total wait."""
    sleeps: list[float] = []
    tracker = _ScriptedPostTracker(
        [_transient_error(503), _transient_error(503), _transient_error(503)],
        agent_id=1001,
    )

    outcome = _drive_post_comment(
        tracker=tracker, ledger=ledger, body="hello", sleep=sleeps.append
    )

    assert isinstance(outcome, Failed)
    assert isinstance(outcome.error, GitHubFailure)
    assert outcome.error.failure_class == "github"
    assert outcome.error.reason == "infrastructure-failure"
    assert _post_comment_calls(tracker) == 3
    assert sleeps == [1.0, 2.0]
    assert sum(sleeps) <= 5 * 60.0
    assert tracker.comments == []
    intent = ledger.get_step_effect(ATTEMPT, "work", 1, 0)
    assert intent is not None
    assert intent.status == "intent"


def test_non_transient_failure_fails_at_once(ledger: RunLedger) -> None:
    """A non-transient write failure is not retried: one write, no wait."""
    sleeps: list[float] = []
    refusal = StepTrackerError(
        "POST /repos/octocat/sandbox/issues/7/comments returned 422: boom"
    )
    tracker = _ScriptedPostTracker([refusal], agent_id=1001)

    outcome = _drive_post_comment(
        tracker=tracker, ledger=ledger, body="hello", sleep=sleeps.append
    )

    assert isinstance(outcome, Failed)
    assert outcome.error is refusal
    assert _post_comment_calls(tracker) == 1
    assert sleeps == []
