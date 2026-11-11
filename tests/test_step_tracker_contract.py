"""The step-workflow tracker contract suite: fake and adapter behave the same.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: candidates, guards, filters and ordering — selection
writes nothing), §5.1 (the latest ``labeled`` event and its actor test),
§6 (the Claim: intent, assignment, Claim Comment, the read-back — the
lowest comment id wins), §7 (Gate re-reads and the base-branch tip) and
§14 (every external write is an Effect Intent settled once: the key
travels in the write's Attempt Marker; lookups filter by the Agent
Identity's numeric id and read the full paginated list; a write by another
account carrying our marker is not our receipt) and §18 (identity mode
detection from the numeric ids; no Selection Label application and no
input-body edit exist on the port).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.

The suite is parametrised over the fake step tracker in ``tests/support/``
and the GitHub adapter. The fake and the mocked-GitHub params run in the
default suite; the live param runs the same flows against the sandbox
repository and is opt-in through ``CODING_AGENT_LIVE_TESTS=1`` (plus
``GITHUB_TOKEN``) — never on token presence alone. Reads assert reported
facts; the Claim and Gate tests assert the write order and that selection
itself never writes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.inputs.claim import (
    CLAIM_COMMENT_ORDINAL,
    CLAIM_EXECUTION,
    CLAIM_STEP,
    claim_comment_body,
)
from coding_agent.inputs.github import GitHubStepTracker
from coding_agent.inputs.port import StepTrackerPort
from coding_agent.ledger.store import step_effect_key, step_effect_marker
from support.fake_step_tracker import FakeStepTracker

TEST_BASE_URL = "https://api.github.test"

SANDBOX_OWNER = "lbacik"
SANDBOX_REPO = "coding-agent-sandbox"

ISSUE_LABEL = "agent:implement"
PR_LABEL = "agent:review"

ISSUE_NUMBER = 30
PR_NUMBER = 31

AGENT_LOGIN = "coding-agent"
AGENT_ID = 1001
OWNER_ID = 999


@dataclass
class TrackerHarness:
    """One side of the contract suite: a tracker plus how to observe it."""

    tracker: StepTrackerPort
    issue_number: int
    pr_number: int
    is_live: bool


def _issue_payload(number: int, label: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": "a title",
        "body": "a body",
        "state": "open",
        "labels": [{"name": label}],
        "assignees": [],
        "created_at": "2026-10-01T00:00:00Z",
        "user": {"login": "human", "id": 7},
    }


def _pull_payload(number: int, marker: str) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "title": "a title",
        "body": f"implements {marker}",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "head": {
            "sha": "h" * 40,
            "ref": "feature",
            "repo": {"full_name": "octocat/sandbox"},
        },
        "base": {"ref": "main"},
        "user": {"login": "human", "id": 7},
        "labels": [{"name": PR_LABEL}],
        "assignees": [],
        "created_at": "2026-10-02T00:00:00Z",
    }


def _mocked_harness(requests_mock: Any) -> TrackerHarness:
    """The GitHub adapter over mocked HTTP: the same class the live side runs."""
    import json as json_module

    key = step_effect_key(
        "implement#30/1", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL
    )
    marker = claim_comment_body("implement#30/1", key)
    issue = _issue_payload(ISSUE_NUMBER, ISSUE_LABEL)
    pr_issue = _issue_payload(PR_NUMBER, PR_LABEL)
    pr_issue["pull_request"] = {"url": f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls/31"}
    pull = _pull_payload(PR_NUMBER, marker)

    requests_mock.get(f"{TEST_BASE_URL}/user", json={"login": AGENT_LOGIN, "id": AGENT_ID})
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox",
        json={"owner": {"id": OWNER_ID, "login": "octocat"}},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{ISSUE_NUMBER}", json=issue
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{PR_NUMBER}", json=pr_issue
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls/{PR_NUMBER}", json=pull
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues",
        json=[issue, pr_issue],
    )
    held_pulls: list[dict[str, Any]] = []
    next_pull_number = [101]
    foreign_pr_marker = step_effect_marker(
        "implement#9/9", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL,
        kind="open-pull-request",
    )
    held_pulls.append(
        {
            "number": 77,
            "html_url": f"{TEST_BASE_URL}/octocat/sandbox/pull/77",
            "title": "human PR",
            "head": {"ref": "feature-foreign"},
            "base": {"ref": "main"},
            "body": f"a human quoting us: {foreign_pr_marker}",
            "state": "open",
            "labels": [],
            "assignees": [],
            "created_at": "2026-10-02T00:00:00Z",
            "user": {"login": "human", "id": 424242},
            "draft": False,
        }
    )

    def _pulls_callback(request: Any, context: Any) -> str:
        context.status_code = 200
        heads = request.qs.get("head", [])
        if heads:
            wanted = heads[0].split(":", 1)[-1]
            selected = [
                entry
                for entry in held_pulls
                if entry.get("head", {}).get("ref") == wanted
            ]
        else:
            selected = [
                entry for entry in held_pulls if entry.get("state") == "open"
            ]
        return json_module.dumps(selected)

    def _post_pull_callback(request: Any, context: Any) -> str:
        posted = json_module.loads(request.text)
        entry = {
            "number": next_pull_number[0],
            "html_url": (
                f"https://github.test/octocat/sandbox/pull/{next_pull_number[0]}"
            ),
            "title": posted.get("title", ""),
            "head": {"ref": posted.get("head", "")},
            "base": {"ref": posted.get("base", "")},
            "body": posted.get("body", ""),
            "state": "open",
            "labels": [],
            "assignees": [],
            "created_at": "2026-10-03T00:00:00Z",
            "user": {"login": AGENT_LOGIN, "id": AGENT_ID},
            "draft": bool(posted.get("draft", False)),
        }
        next_pull_number[0] += 1
        held_pulls.append(entry)
        # A human copy carrying the same branch and marker: the lookup must
        # still return the Agent Identity's own pull request (§14).
        held_pulls.append(
            {
                **entry,
                "number": next_pull_number[0],
                "title": "human copy",
                "user": {"login": "human", "id": 424242},
            }
        )
        next_pull_number[0] += 1
        context.status_code = 201
        return json_module.dumps(entry)

    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        text=_pulls_callback,
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        text=_post_pull_callback,
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{ISSUE_NUMBER}/events",
        json=[
            {
                "event": "labeled",
                "label": {"name": ISSUE_LABEL},
                "actor": {"login": "some-bot", "id": 4321, "type": "Bot"},
                "created_at": "2026-09-01T00:00:00Z",
            },
            {
                "event": "unlabeled",
                "label": {"name": ISSUE_LABEL},
                "actor": {"login": "human", "id": 7, "type": "User"},
                "created_at": "2026-09-15T00:00:00Z",
            },
            {
                "event": "labeled",
                "label": {"name": ISSUE_LABEL},
                "actor": {"login": "human", "id": 7, "type": "User"},
                "created_at": "2026-10-01T00:00:00Z",
            },
        ],
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{PR_NUMBER}/events",
        json=[],
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{ISSUE_NUMBER}/assignees",
        json={"assignees": [{"login": AGENT_LOGIN}]},
    )
    requests_mock.delete(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{ISSUE_NUMBER}/assignees",
        json={},
    )
    held_comments: list[dict[str, Any]] = [
        {
            "id": 499,
            "body": f"a human quoting us: {marker}",
            "user": {"login": "human", "id": 424242},
        },
        {
            "id": 501,
            "body": marker,
            "user": {"login": AGENT_LOGIN, "id": AGENT_ID},
        },
    ]
    next_comment_id = [502]

    def _comments_callback(request: Any, context: Any) -> str:
        context.status_code = 200
        return json_module.dumps(held_comments)

    def _post_comment_callback(request: Any, context: Any) -> str:
        posted = json_module.loads(request.text)
        entry = {
            "id": next_comment_id[0],
            "body": posted.get("body", ""),
            "user": {"login": AGENT_LOGIN, "id": AGENT_ID},
        }
        next_comment_id[0] += 1
        held_comments.append(entry)
        context.status_code = 201
        return json_module.dumps(entry)

    def _delete_comment_callback(request: Any, context: Any) -> str:
        comment_id = int(request.url.split("/")[-1])
        for index, entry in enumerate(held_comments):
            if entry["id"] == comment_id:
                del held_comments[index]
                context.status_code = 204
                return ""
        context.status_code = 404
        return json_module.dumps({"message": "Not Found"})

    comments_url = f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{ISSUE_NUMBER}/comments"
    requests_mock.get(comments_url, text=_comments_callback)
    requests_mock.post(comments_url, text=_post_comment_callback)
    requests_mock.delete(
        re.compile(r"/issues/comments/\d+(\?.*)?$"),
        text=_delete_comment_callback,
    )

    held_reviews: list[dict[str, Any]] = [
        {
            "id": 599,
            "body": f"a human quoting us: {marker}",
            "user": {"login": "human", "id": 424242},
            "commit_id": "h" * 40,
        },
    ]
    next_review_id = [601]

    def _reviews_callback(request: Any, context: Any) -> str:
        context.status_code = 200
        return json_module.dumps(held_reviews)

    def _post_review_callback(request: Any, context: Any) -> str:
        posted = json_module.loads(request.text)
        entry = {
            "id": next_review_id[0],
            "body": posted.get("body", ""),
            "user": {"login": AGENT_LOGIN, "id": AGENT_ID},
            "commit_id": posted.get("commit_id"),
        }
        next_review_id[0] += 1
        held_reviews.append(entry)
        context.status_code = 200
        return json_module.dumps(entry)

    reviews_url = f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls/{PR_NUMBER}/reviews"
    requests_mock.get(reviews_url, text=_reviews_callback)
    requests_mock.post(reviews_url, text=_post_review_callback)
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/branches/main",
        json={"commit": {"sha": "t" * 40}},
    )
    return TrackerHarness(
        tracker=GitHubStepTracker(
            owner="octocat",
            repo="sandbox",
            client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
            agent_login=AGENT_LOGIN,
            agent_id=AGENT_ID,
        ),
        issue_number=ISSUE_NUMBER,
        pr_number=PR_NUMBER,
        is_live=False,
    )


def _fake_harness() -> TrackerHarness:
    """The in-process fake, seeded like the mocked side."""
    from coding_agent.inputs.port import LabeledEvent, TrackedInput

    key = step_effect_key(
        "implement#30/1", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL
    )
    marker = claim_comment_body("implement#30/1", key)
    fake = FakeStepTracker(
        owner="octocat",
        repo="sandbox",
        agent_login=AGENT_LOGIN,
        agent_id=AGENT_ID,
        owner_id=OWNER_ID,
    )
    fake.inputs[ISSUE_NUMBER] = TrackedInput(
        number=ISSUE_NUMBER,
        input_type="issue",
        title="a title",
        body="a body",
        is_open=True,
        labels=(ISSUE_LABEL,),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
    )
    fake.inputs[PR_NUMBER] = TrackedInput(
        number=PR_NUMBER,
        input_type="pull-request",
        title="a title",
        body=f"implements {marker}",
        is_open=True,
        labels=(PR_LABEL,),
        assignees=(),
        created_at="2026-10-02T00:00:00Z",
        author_login="human",
        author_id=7,
        draft=False,
        merged=False,
        head_sha="h" * 40,
        head_branch="feature",
        head_repo="octocat/sandbox",
        base_branch="main",
    )
    fake.labeled_events[(ISSUE_NUMBER, ISSUE_LABEL)] = LabeledEvent(
        label=ISSUE_LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )
    fake.base_tip = "t" * 40
    fake.seed_foreign_marker_comment(marker)
    own = fake.post_claim_comment(ISSUE_NUMBER, "issue", marker)
    assert own.comment_id == 2
    fake.seed_foreign_marker_review(marker)
    fake.publish_review(PR_NUMBER, commit_id="h" * 40, body=marker)
    fake.open_pull_requests.append(fake.inputs[PR_NUMBER])
    fake.calls.clear()
    return TrackerHarness(
        tracker=fake, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER, is_live=False
    )


def _live_opted_in() -> bool:
    return os.environ.get("CODING_AGENT_LIVE_TESTS") == "1"


def _live_harness() -> Iterator[TrackerHarness]:
    """The GitHub adapter against the sandbox repository (opt-in, live)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN is not set")
        raise AssertionError("unreachable")
    client = GitHubClient(token)
    user_response = client.get("/user")
    assert user_response.status_code == 200, user_response.text
    login = str(user_response.json()["login"])
    agent_id_raw = user_response.json()["id"]
    agent_id = int(agent_id_raw) if isinstance(agent_id_raw, int) else None
    opened = client.post(
        f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues",
        json={
            "title": "step tracker contract suite probe",
            "body": "Opened by the OP-14 contract suite; closed when its test ends.",
        },
    )
    assert opened.status_code == 201, opened.text
    number = int(opened.json()["number"])
    labelled = client.post(
        f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/labels",
        json={"labels": [ISSUE_LABEL]},
    )
    assert labelled.status_code == 200, labelled.text
    try:
        yield TrackerHarness(
            tracker=GitHubStepTracker(
                owner=SANDBOX_OWNER,
                repo=SANDBOX_REPO,
                client=client,
                agent_login=login,
                agent_id=agent_id,
            ),
            issue_number=number,
            pr_number=0,
            is_live=True,
        )
    finally:
        live_tracker = GitHubStepTracker(
            owner=SANDBOX_OWNER,
            repo=SANDBOX_REPO,
            client=client,
            agent_login=login,
            agent_id=agent_id,
        )
        for comment in live_tracker.list_comments(number, "issue"):
            if comment.author_login == login or comment.author_id == agent_id:
                try:
                    live_tracker.delete_comment(comment.comment_id)
                except Exception:
                    pass
        try:
            live_tracker.unassign_input(number, "issue")
        except Exception:
            pass
        client.delete(
            f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/labels/{ISSUE_LABEL}"
        )


@pytest.fixture(params=["fake", "mocked", "live"])
def harness(request: pytest.FixtureRequest, requests_mock: Any) -> Iterator[TrackerHarness]:
    """Every side of the contract suite."""
    if request.param == "fake":
        yield _fake_harness()
    elif request.param == "mocked":
        yield _mocked_harness(requests_mock)
    else:
        if not _live_opted_in():
            pytest.skip("live side runs only with CODING_AGENT_LIVE_TESTS=1")
        yield from _live_harness()


def _is_live(harness: TrackerHarness) -> bool:
    return harness.is_live


def test_fetch_issue_reports_facts(harness: TrackerHarness) -> None:
    """§5: the read reports number, title, body, openness and labels."""
    if _is_live(harness):
        pytest.skip("live probe assertions run in the issue flow test")
    fetched = harness.tracker.fetch_input(harness.issue_number, "issue")
    assert fetched.number == harness.issue_number
    assert fetched.title == "a title"
    assert fetched.body == "a body"
    assert fetched.is_open
    assert ISSUE_LABEL in fetched.labels


def test_fetch_pull_request_reports_head_facts(harness: TrackerHarness) -> None:
    """INP-1: the read pins head SHA, branches, draft and merged facts."""
    if _is_live(harness):
        pytest.skip("live side probes issues only")
    fetched = harness.tracker.fetch_input(harness.pr_number, "pull-request")
    assert fetched.head_sha == "h" * 40
    assert fetched.head_branch == "feature"
    assert fetched.base_branch == "main"
    assert fetched.draft is False
    assert fetched.merged is False


def test_selection_lists_candidates_and_writes_nothing(
    harness: TrackerHarness, requests_mock: Any
) -> None:
    """§5.2: candidates list by label; selection writes nothing."""
    if _is_live(harness):
        pytest.skip("live probe assertions run in the issue flow test")
    issues = harness.tracker.list_issue_candidates(ISSUE_LABEL)
    assert [candidate.number for candidate in issues] == [harness.issue_number]
    pulls = harness.tracker.list_pull_request_candidates(PR_LABEL)
    assert [candidate.number for candidate in pulls] == [harness.pr_number]
    if isinstance(harness.tracker, FakeStepTracker):
        assert all(
            name
            in {
                "fetch_agent_identity",
                "fetch_repository_owner_id",
                "fetch_input",
                "list_issue_candidates",
                "list_pull_request_candidates",
                "list_open_pull_requests",
                "read_labeled_event",
                "has_open_blocker",
                "fetch_base_tip",
                "list_comments",
                "list_reviews",
                "find_comment_by_marker",
                "find_review_by_marker",
            }
            for name, _ in harness.tracker.calls
        )
    else:
        writes = [
            entry
            for entry in requests_mock.request_history
            if entry.method in ("POST", "PUT", "PATCH", "DELETE")
        ]
        assert writes == []


def test_latest_labeled_event_wins(harness: TrackerHarness) -> None:
    """INP-19: the Claim read-back tests the latest ``labeled`` event's actor."""
    if _is_live(harness):
        pytest.skip("live side reads the probe's own fresh event")
    event = harness.tracker.read_labeled_event(
        harness.issue_number, "issue", ISSUE_LABEL
    )
    assert event is not None
    assert event.actor_login == "human"
    assert event.actor_type == "User"


def test_identity_and_owner_ids_decide_the_mode(harness: TrackerHarness) -> None:
    """INP-19, INP-20: the numeric ids decide the identity mode."""
    if _is_live(harness):
        pytest.skip("live mode follows the sandbox ids")
    agent = harness.tracker.fetch_agent_identity()
    assert agent.login == AGENT_LOGIN
    assert agent.account_id == AGENT_ID
    assert harness.tracker.fetch_repository_owner_id() == OWNER_ID


def test_fetch_base_tip(harness: TrackerHarness) -> None:
    """§7: the base-branch tip resolves at Claim."""
    if _is_live(harness):
        pytest.skip("live side reads the sandbox tip in the issue flow test")
    assert harness.tracker.fetch_base_tip("main") == "t" * 40


def test_claim_writes_order_and_own_comment_found_by_key(
    harness: TrackerHarness,
) -> None:
    """INP-5, OP-14: assign, then Claim Comment; found again by intent key."""
    if _is_live(harness):
        pytest.skip("live side runs the full flow once in the issue flow test")
    attempt = "implement#30/9"
    key = step_effect_key(attempt, CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL)
    body = claim_comment_body(attempt, key)
    harness.tracker.assign_input(harness.issue_number, "issue")
    posted = harness.tracker.post_claim_comment(harness.issue_number, "issue", body)
    found = harness.tracker.find_comment_by_marker(
        harness.issue_number, "issue", attempt
    )
    assert found is not None
    assert found.comment_id == posted.comment_id
    if isinstance(harness.tracker, FakeStepTracker):
        names = harness.tracker.call_names()
        assert names.index("assign_input") < names.index("post_claim_comment")
    assert key in body


def test_foreign_marker_comment_is_never_ours(harness: TrackerHarness) -> None:
    """§14: a comment by another account carrying our marker is not our receipt."""
    if _is_live(harness):
        pytest.skip("live side has no foreign marker comment")
    marker = "implement#30/1"
    if isinstance(harness.tracker, FakeStepTracker):
        found = harness.tracker.find_comment_by_marker(
            harness.issue_number, "issue", marker
        )
        assert found is not None
        assert found.author_login == AGENT_LOGIN
    else:
        found = harness.tracker.find_comment_by_marker(
            harness.issue_number, "issue", marker
        )
        assert found is not None
        assert found.comment_id == 501


def test_delete_claim_comment_is_idempotent(harness: TrackerHarness) -> None:
    """§6.2, §16.2: deleting the Claim Comment twice still succeeds."""
    if _is_live(harness):
        pytest.skip("live side deletes its comments once during teardown")
    found = harness.tracker.find_comment_by_marker(
        harness.issue_number, "issue", "implement#30/1"
    )
    assert found is not None
    harness.tracker.delete_comment(found.comment_id)
    harness.tracker.delete_comment(found.comment_id)
    assert (
        harness.tracker.find_comment_by_marker(
            harness.issue_number, "issue", "implement#30/1"
        )
        is None
    )


def test_review_published_and_found_by_marker(
    harness: TrackerHarness, requests_mock: Any
) -> None:
    """OP-14: the review publishes through the review API and is found by key."""
    if _is_live(harness):
        pytest.skip("live side probes issues only")
    marker = step_effect_marker(
        "review#31/1", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL, kind="claim"
    )
    if isinstance(harness.tracker, FakeStepTracker):
        published = harness.tracker.publish_review(
            harness.pr_number, commit_id="n" * 40, body=marker
        )
        found = harness.tracker.find_review_by_marker(harness.pr_number, "review#31/1")
        assert found is not None
        assert found.review_id == published.review_id
    else:
        published = harness.tracker.publish_review(
            harness.pr_number, commit_id="h" * 40, body=marker
        )
        assert published.review_id == 601
        found = harness.tracker.find_review_by_marker(harness.pr_number, "review#31/1")
        assert found is not None
        assert found.review_id == 601
    if not isinstance(harness.tracker, FakeStepTracker):
        import json as json_module

        history = requests_mock.request_history
        review_posts = [
            entry
            for entry in history
            if entry.method == "POST" and entry.url.endswith("/reviews")
        ]
        assert review_posts, "no review posted"
        sent = json_module.loads(review_posts[-1].text)
        assert sent["event"] == "COMMENT"
        assert sent["commit_id"] == "h" * 40


def test_pull_request_opened_and_found_by_marker(
    harness: TrackerHarness, requests_mock: Any
) -> None:
    """OP-14: the pull request opens from the branch and is found by key."""
    if _is_live(harness):
        pytest.skip("live side probes issues only")
    marker = step_effect_marker(
        "implement#30/2", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL,
        kind="open-pull-request",
    )
    body = f"Closes #{harness.issue_number}\n\na document\n\n{marker}"
    if isinstance(harness.tracker, FakeStepTracker):
        opened = harness.tracker.open_pull_request(
            title="a title",
            head_branch="agent/implement/30/2-slug",
            base_branch="main",
            body=body,
            draft=False,
        )
        found = harness.tracker.find_pull_request(
            "agent/implement/30/2-slug", "implement#30/2"
        )
        assert found is not None
        assert found.number == opened.number
        assert found.draft is False
    else:
        opened = harness.tracker.open_pull_request(
            title="a title",
            head_branch="agent/implement/30/2-slug",
            base_branch="main",
            body=body,
            draft=False,
        )
        assert opened.number == 101
        found = harness.tracker.find_pull_request(
            "agent/implement/30/2-slug", "implement#30/2"
        )
        assert found is not None
        assert found.number == 101
    if not isinstance(harness.tracker, FakeStepTracker):
        import json as json_module

        history = requests_mock.request_history
        pull_posts = [
            entry
            for entry in history
            if entry.method == "POST" and entry.url.endswith("/pulls")
        ]
        assert pull_posts, "no pull request opened"
        sent = json_module.loads(pull_posts[-1].text)
        assert sent["title"] == "a title"
        assert sent["head"] == "agent/implement/30/2-slug"
        assert sent["base"] == "main"
        assert sent["body"] == body
        assert sent["draft"] is False


def test_foreign_marker_pull_request_is_never_ours(
    harness: TrackerHarness,
) -> None:
    """OP-14 (§14): a pull request by another account carrying our marker."""
    if _is_live(harness):
        pytest.skip("live side has no foreign marker pull request")
    marker = step_effect_marker(
        "implement#30/3", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL,
        kind="open-pull-request",
    )
    body = f"Closes #{harness.issue_number}\n\na document\n\n{marker}"
    if isinstance(harness.tracker, FakeStepTracker):
        harness.tracker.open_pull_request(
            title="a title",
            head_branch="agent/implement/30/3-slug",
            base_branch="main",
            body=body,
            draft=False,
        )
        harness.tracker.seed_foreign_marker_pull_request(
            "agent/implement/30/3-slug", marker
        )
        found = harness.tracker.find_pull_request(
            "agent/implement/30/3-slug", "implement#30/3"
        )
        assert found is not None
        assert found.author_login == AGENT_LOGIN
    else:
        harness.tracker.open_pull_request(
            title="a title",
            head_branch="agent/implement/30/3-slug",
            base_branch="main",
            body=body,
            draft=False,
        )
        # The mocked POST mirrors a human copy with the same branch and
        # marker; the lookup must still return the Agent Identity's own.
        found = harness.tracker.find_pull_request(
            "agent/implement/30/3-slug", "implement#30/3"
        )
        assert found is not None
        assert found.number == 101
        assert found.author_login == AGENT_LOGIN
        # The seeded human pull request on its own branch is never ours.
        assert (
            harness.tracker.find_pull_request(
                "feature-foreign", "implement#9/9"
            )
            is None
        )


def test_post_comment_published_and_found_by_marker(
    harness: TrackerHarness,
) -> None:
    """OP-14: the comment posts on the input and is found by key."""
    if _is_live(harness):
        pytest.skip("live side runs the full flow once in the issue flow test")
    attempt = "implement#30/10"
    key = step_effect_key(
        attempt, CLAIM_STEP, 2, CLAIM_COMMENT_ORDINAL
    )
    body = claim_comment_body(attempt, key)
    posted = harness.tracker.post_comment(harness.issue_number, "issue", body)
    found = harness.tracker.find_comment_by_marker(
        harness.issue_number, "issue", attempt
    )
    assert found is not None
    assert found.comment_id == posted.comment_id
    assert key in body


def test_live_issue_flow(harness: TrackerHarness) -> None:
    """OP-14 (live): the sandbox side runs fetch, label, assign, comment, tip."""
    if not _is_live(harness):
        pytest.skip("live only")
    tracker = harness.tracker
    fetched = tracker.fetch_input(harness.issue_number, "issue")
    assert fetched.is_open
    assert ISSUE_LABEL in fetched.labels
    event = tracker.read_labeled_event(harness.issue_number, "issue", ISSUE_LABEL)
    assert event is not None
    tip = tracker.fetch_base_tip("main")
    assert len(tip) == 40
    tracker.assign_input(harness.issue_number, "issue")
    marker = step_effect_marker(
        "implement#99/1", CLAIM_STEP, CLAIM_EXECUTION, CLAIM_COMMENT_ORDINAL, kind="claim"
    )
    posted = tracker.post_claim_comment(harness.issue_number, "issue", marker)
    try:
        found = tracker.find_comment_by_marker(
            harness.issue_number, "issue", "implement#99/1"
        )
        assert found is not None
        assert found.comment_id == posted.comment_id
    finally:
        tracker.delete_comment(posted.comment_id)
        tracker.unassign_input(harness.issue_number, "issue")
