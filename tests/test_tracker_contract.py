"""The tracker contract suite: the fake and the GitHub adapter behave the same.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph owns
every external write), §12 (the pull request opens only for a ready verdict),
§13 (the write sequence; bodies rendered once; pull-request identity is the
head branch plus the Attempt Marker in the body; the remote lookup filters by
the Agent Identity's id; the marker rule reads in one direction only),
§14 ("Recording before publishing" and the terminal writes in order, C-OUT-1)
and §18 (the two structural abstentions: the Worker never edits the Target
Issue's body and never applies the Selection Label — the port cannot express
them).

MVP row ids: C-TRK-1, C-OUT-1, P-DEL-1, P-DEL-10, L3-DEL-21.

The suite is parametrised over the fake tracker in ``tests/support/`` and the
GitHub adapter. The fake and the mocked-GitHub params run in the default suite;
the live param runs the same flows against the sandbox repository
``lbacik/coding-agent-sandbox`` and is opt-in like the existing live tests
(it is skipped without ``GITHUB_TOKEN``). The fake and the mocked adapter
assert the C-OUT-1 order exactly; the live side asserts the end state the
ordered writes leave on GitHub. The pull-request operations run on the fake
and mocked sides only: opening a live pull request needs a pushed branch, so
the live delivery shape is covered by the paid L4-1 run instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.ledger.store import RunLedger
from coding_agent.tracker.port import TrackerPort
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

TEST_BASE_URL = "https://api.github.test"

SANDBOX_OWNER = "lbacik"
SANDBOX_REPO = "coding-agent-sandbox"

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


@dataclass
class TrackerHarness:
    """One side of the contract suite: a tracker plus how to observe it."""

    tracker: TrackerPort
    issue_number: int
    selection_label: str
    ready_for_human: str
    agent_login: str
    is_live: bool
    owner: str = ""
    repo: str = ""
    raw_client: GitHubClient | None = None


def _github_tracker() -> Any:
    from coding_agent.tracker.github import GitHubTracker

    return GitHubTracker


def _mocked_harness(requests_mock: Any) -> TrackerHarness:
    """The GitHub adapter over mocked HTTP: the same class the live side runs."""
    GitHubTracker = _github_tracker()
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={
            "number": 30,
            "title": "a title",
            "body": "a body",
            "state": "open",
            "labels": [{"name": SELECTION_LABEL}],
        },
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/comments",
        json={"id": 101},
        status_code=201,
    )
    requests_mock.delete(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/labels/{SELECTION_LABEL}",
        json=[],
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/labels",
        json=[{"name": READY_FOR_HUMAN}],
    )
    requests_mock.delete(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/assignees",
        json={"number": 30},
    )
    return TrackerHarness(
        tracker=GitHubTracker(
            owner="octocat",
            repo="sandbox",
            client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
            agent_login="octocat",
            selection_label=SELECTION_LABEL,
            ready_for_human=READY_FOR_HUMAN,
        ),
        issue_number=30,
        selection_label=SELECTION_LABEL,
        ready_for_human=READY_FOR_HUMAN,
        agent_login="octocat",
        is_live=False,
    )


def _ensure_live_label(client: GitHubClient, owner: str, repo: str, name: str) -> None:
    """Create the lifecycle label on the sandbox where the probe needs it."""
    lookup = client.get(f"/repos/{owner}/{repo}/labels/{name}")
    if lookup.status_code == 404:
        created = client.post(
            f"/repos/{owner}/{repo}/labels",
            json={
                "name": name,
                "color": "ededed",
                "description": "Tracker contract suite probe label.",
            },
        )
        assert created.status_code in (201, 422), created.text


def _live_harness() -> Iterator[TrackerHarness]:
    """The GitHub adapter against the sandbox repository (opt-in, live)."""
    from coding_agent.tracker.github import GitHubTracker

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN is not set")
        raise AssertionError("unreachable")
    client = GitHubClient(token)
    user_response = client.get("/user")
    assert user_response.status_code == 200, user_response.text
    login = str(user_response.json()["login"])
    _ensure_live_label(client, SANDBOX_OWNER, SANDBOX_REPO, SELECTION_LABEL)
    _ensure_live_label(client, SANDBOX_OWNER, SANDBOX_REPO, READY_FOR_HUMAN)
    opened = client.post(
        f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues",
        json={
            "title": "tracker contract suite probe",
            "body": "Opened by the C-TRK-1 contract suite; closed when its test ends.",
        },
    )
    assert opened.status_code == 201, opened.text
    number = int(opened.json()["number"])
    labelled = client.post(
        f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/labels",
        json={"labels": [SELECTION_LABEL]},
    )
    assert labelled.status_code == 200, labelled.text
    try:
        yield TrackerHarness(
            tracker=GitHubTracker(
                owner=SANDBOX_OWNER,
                repo=SANDBOX_REPO,
                client=client,
                agent_login=login,
                selection_label=SELECTION_LABEL,
                ready_for_human=READY_FOR_HUMAN,
            ),
            issue_number=number,
            selection_label=SELECTION_LABEL,
            ready_for_human=READY_FOR_HUMAN,
            agent_login=login,
            is_live=True,
            owner=SANDBOX_OWNER,
            repo=SANDBOX_REPO,
            raw_client=client,
        )
    finally:
        try:
            client.post(
                f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/assignees",
                json={"assignees": [login]},
            )
        except Exception:
            pass
        try:
            client.delete(
                f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/assignees",
                json={"assignees": [login]},
            )
        except Exception:
            pass
        client.patch(
            f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}",
            json={"state": "closed"},
        )


@pytest.fixture(params=["fake", "github-mocked", "github-live"])
def harness(request: pytest.FixtureRequest) -> Iterator[TrackerHarness]:
    """One contract-suite side per param: fake, mocked GitHub, live GitHub.

    The HTTP mocker is fetched lazily so the live side never activates
    interception: its setup and assertions run against the real sandbox
    repository, skipped without ``GITHUB_TOKEN`` like the existing live tests.
    """
    kind = str(request.param)
    if kind == "fake":
        yield TrackerHarness(
            tracker=FakeTracker(),
            issue_number=7,
            selection_label=SELECTION_LABEL,
            ready_for_human=READY_FOR_HUMAN,
            agent_login="coding-agent",
            is_live=False,
        )
    elif kind == "github-mocked":
        mocked: Any = request.getfixturevalue("requests_mock")
        yield _mocked_harness(mocked)
    elif kind == "github-live":
        yield from _live_harness()
    else:  # pragma: no cover - the params above are exhaustive
        raise AssertionError(f"unknown tracker harness {kind!r}")


def _attempt_id(harness: TrackerHarness) -> str:
    return f"#{harness.issue_number}/1"


def _run_terminal_writes(
    harness: TrackerHarness,
    tmp_path: Path,
    *,
    outcome: str,
    reason: str,
    assigned_by_us: bool | None,
    pre_recorded_body: str | None = None,
) -> None:
    """Drive the ``terminal_writes`` node once over the harness tracker.

    Contract §14: the Terminal Outcome is recorded before the first tracker
    call, so the bytes the node sends are the recorded bytes.
    """
    from coding_agent.runtime.graph.attempt import terminal_writes
    from coding_agent.runtime.graph.context import AttemptContext

    import sqlite3

    attempt_id = _attempt_id(harness)
    ledger = RunLedger(tmp_path / f"contract-{harness.issue_number}.sqlite3")
    try:
        try:
            ledger.open_attempt(
                attempt_id,
                effective_configuration={
                    "repo": "example/test",
                    "issue": harness.issue_number,
                },
                assigned_by_us=assigned_by_us,
            )
        except sqlite3.IntegrityError:
            # A repeated write runs against the same ledger file, whose
            # attempt row the first run already opened (§14, §17).
            pass
        if pre_recorded_body is not None:
            from coding_agent.runtime.publication import terminal_writes_for_outcome

            ledger.record_terminal_outcome(
                attempt_id,
                outcome=outcome,
                reason=reason,
                terminal_comment_body=pre_recorded_body,
                writes=terminal_writes_for_outcome(outcome=outcome),
            )
        if harness.is_live:
            assert harness.raw_client is not None
            assigned = harness.raw_client.post(
                f"/repos/{harness.owner}/{harness.repo}"
                f"/issues/{harness.issue_number}/assignees",
                json={"assignees": [harness.agent_login]},
            )
            assert assigned.status_code in (200, 201), assigned.text
        context = AttemptContext(
            tracker=harness.tracker,
            ledger=ledger,
            model=ScriptedModel([]),
            workspace=tmp_path,
            effective_configuration={
                "repo": "example/test",
                "issue": harness.issue_number,
            },
            skills_dir=tmp_path,
        )
        terminal_writes(
            {
                "attempt_id": attempt_id,
                "issue_number": harness.issue_number,
                "attempt_number": 1,
                "issue_title": "Add a greeting",
                "outcome": outcome,
                "reason": reason,
            },
            {"configurable": context.to_configurable()},
        )
    finally:
        ledger.close()


def _fake_calls(harness: TrackerHarness) -> list[str]:
    assert isinstance(harness.tracker, FakeTracker)
    return harness.tracker.call_names()


def _mocked_sequence(requests_mock: Any) -> list[tuple[str, str]]:
    return [
        (str(request.method), str(request.url))
        for request in requests_mock.request_history
    ]


def _mocked_paths(requests_mock: Any) -> list[tuple[str, str]]:
    base = TEST_BASE_URL
    return [
        (method, url[len(base):] if url.startswith(base) else url)
        for method, url in _mocked_sequence(requests_mock)
    ]


def _live_issue(harness: TrackerHarness) -> dict[str, Any]:
    assert harness.raw_client is not None
    response = harness.raw_client.get(
        f"/repos/{harness.owner}/{harness.repo}/issues/{harness.issue_number}"
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _live_comments(harness: TrackerHarness) -> list[dict[str, Any]]:
    assert harness.raw_client is not None
    response = harness.raw_client.get(
        f"/repos/{harness.owner}/{harness.repo}"
        f"/issues/{harness.issue_number}/comments"
    )
    assert response.status_code == 200, response.text
    comments: list[dict[str, Any]] = response.json()
    return comments


def test_fetch_issue_returns_the_number_title_and_body(
    harness: TrackerHarness,
) -> None:
    """C-TRK-1: ``fetch_issue`` reads the Target Issue on every side."""
    issue = harness.tracker.fetch_issue(harness.issue_number)

    assert issue.number == harness.issue_number
    assert issue.title
    assert isinstance(issue.body, str)


def test_read_issue_state_reports_open_state_and_labels(
    harness: TrackerHarness,
) -> None:
    """§11: ``read_issue_state`` reports the open state and labels (P-CAN-1).

    Every Gate before the Delivery Intent re-reads these facts through the
    port to detect a human cancellation — a closed issue or a missing
    Selection Label. The read writes nothing: the fake records it as a read
    and the mocked adapter issues a single ``GET``.
    """
    state = harness.tracker.read_issue_state(harness.issue_number)

    assert state.number == harness.issue_number
    assert state.is_open is True
    assert harness.selection_label in state.labels


def test_c_out_1_terminal_writes_run_in_contract_order(
    harness: TrackerHarness, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """C-OUT-1: comment, Selection Label removal, ready-for-human, unassign.

    For an outcome without a pull request whose assignment the ledger records
    as ``assigned_by_us``, every side performs the four writes in §14 order.
    The fake and the mocked adapter assert the order exactly; the live side
    performs the same ordered calls and asserts the end state they leave.
    """
    _run_terminal_writes(
        harness,
        tmp_path,
        outcome="failed",
        reason="criteria-uncovered",
        assigned_by_us=True,
    )

    if isinstance(harness.tracker, FakeTracker):
        assert _fake_calls(harness) == [
            "post_terminal_comment",
            "remove_selection_label",
            "add_ready_for_human",
            "release_assignment",
        ]
    elif not harness.is_live:
        mocked: Any = request.getfixturevalue("requests_mock")
        assert _mocked_paths(mocked) == [
            ("POST", f"/repos/octocat/sandbox/issues/30/comments"),
            ("DELETE", f"/repos/octocat/sandbox/issues/30/labels/{SELECTION_LABEL}"),
            ("POST", f"/repos/octocat/sandbox/issues/30/labels"),
            ("DELETE", f"/repos/octocat/sandbox/issues/30/assignees"),
        ]
    else:
        issue = _live_issue(harness)
        labels = {label["name"] for label in issue["labels"]}
        assert harness.selection_label not in labels
        assert harness.ready_for_human in labels
        assert harness.agent_login not in {user["login"] for user in issue["assignees"]}
        comments = _live_comments(harness)
        assert comments, "the terminal comment was not posted"
        assert f"#{harness.issue_number}/1" in str(comments[-1]["body"])


def test_delivered_sends_no_ready_for_human(
    harness: TrackerHarness, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """C-OUT-1: ``delivered`` carries no ``ready-for-human`` on any side."""
    _run_terminal_writes(
        harness,
        tmp_path,
        outcome="delivered",
        reason="verified-completion",
        assigned_by_us=True,
    )

    if isinstance(harness.tracker, FakeTracker):
        assert _fake_calls(harness) == [
            "post_terminal_comment",
            "remove_selection_label",
            "release_assignment",
        ]
    elif not harness.is_live:
        mocked: Any = request.getfixturevalue("requests_mock")
        assert ("POST", "/repos/octocat/sandbox/issues/30/labels") not in _mocked_paths(
            mocked
        )
    else:
        issue = _live_issue(harness)
        labels = {label["name"] for label in issue["labels"]}
        assert harness.ready_for_human not in labels


@pytest.mark.parametrize("assigned_by_us", [True, False, None])
def test_assignment_removed_only_when_recorded_as_assigned_by_us(
    harness: TrackerHarness,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    assigned_by_us: bool | None,
) -> None:
    """§5: an assignment the ledger does not record as ours is never removed."""
    _run_terminal_writes(
        harness,
        tmp_path,
        outcome="failed",
        reason="criteria-uncovered",
        assigned_by_us=assigned_by_us,
    )

    if isinstance(harness.tracker, FakeTracker):
        assert ("release_assignment" in _fake_calls(harness)) is (assigned_by_us is True)
    elif not harness.is_live:
        mocked: Any = request.getfixturevalue("requests_mock")
        deletions = [
            path
            for method, path in _mocked_paths(mocked)
            if method == "DELETE" and path.endswith("/assignees")
        ]
        assert (deletions != []) is (assigned_by_us is True)
    else:
        issue = _live_issue(harness)
        assignees = {user["login"] for user in issue["assignees"]}
        assert (harness.agent_login in assignees) is (assigned_by_us is not True)


def test_repeated_write_sends_the_recorded_bytes(
    harness: TrackerHarness, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """§14: a write repeated after a failure sends the recorded bytes."""
    from coding_agent.runtime.publication import attempt_marker

    attempt_id = _attempt_id(harness)
    recorded = (
        f"{attempt_marker(attempt_id, kind='terminal')}\n\n"
        "Attempt #7/1: failed (criteria-uncovered)"
    )
    _run_terminal_writes(
        harness,
        tmp_path,
        outcome="failed",
        reason="criteria-uncovered",
        assigned_by_us=True,
        pre_recorded_body=recorded,
    )
    _run_terminal_writes(
        harness,
        tmp_path,
        outcome="failed",
        reason="criteria-uncovered",
        assigned_by_us=True,
        pre_recorded_body=recorded,
    )

    if isinstance(harness.tracker, FakeTracker):
        assert isinstance(harness.tracker, FakeTracker)
        bodies = [
            str(args[1])
            for name, args in harness.tracker.calls
            if name == "post_terminal_comment"
        ]
        assert bodies == [recorded, recorded]
    elif not harness.is_live:
        mocked: Any = request.getfixturevalue("requests_mock")
        posted = [
            entry.json()["body"]
            for entry in mocked.request_history
            if entry.method == "POST" and str(entry.url).endswith("/comments")
        ]
        assert posted == [recorded, recorded]
    else:
        bodies = [str(comment["body"]) for comment in _live_comments(harness)]
        assert bodies.count(recorded) == 2


def _delivery_marker(attempt_id: str) -> str:
    """The Attempt Marker a pull-request lookup searches for (§15)."""
    from coding_agent.runtime.publication import DELIVERY_COMMENT_KIND, attempt_marker

    return attempt_marker(attempt_id, kind=DELIVERY_COMMENT_KIND)


def _skip_live_pull_request(harness: TrackerHarness) -> None:
    """Live pull requests need a pushed branch; the paid L4-1 run covers them."""
    if harness.is_live:
        pytest.skip("the live pull-request shape is covered by the L4-1 run")


def _mocked_pull_requests(
    requests_mock: Any,
    items: list[dict[str, Any]],
    *,
    created_number: int = 12,
) -> None:
    """Serve the pull-request list and a non-draft creation on the mock."""
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        json=items,
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/pulls",
        json={
            "number": created_number,
            "html_url": f"{TEST_BASE_URL}/octocat/sandbox/pull/{created_number}",
            "title": "a title",
            "state": "open",
            "body": "recorded body",
            "head": {"ref": "agent/30/1-slug"},
            "base": {"ref": "main"},
            "user": {"id": 4242, "login": "octocat"},
            "draft": False,
        },
        status_code=201,
    )


def _mocked_pr_item(
    *,
    number: int,
    head: str = "agent/30/1-slug",
    body: str = "",
    state: str = "open",
    login: str = "octocat",
    user_id: int = 4242,
) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"{TEST_BASE_URL}/octocat/sandbox/pull/{number}",
        "title": "a title",
        "state": state,
        "body": body,
        "head": {"ref": head},
        "base": {"ref": "main"},
        "user": {"id": user_id, "login": login},
        "draft": False,
    }


def test_pull_request_opens_non_draft_and_is_found_by_marker(
    harness: TrackerHarness, request: pytest.FixtureRequest
) -> None:
    """P-DEL-1: the pull request opens non-draft and the lookup finds it (§13).

    The body the lookup matches is the recorded one the open sent; the head
    branch is the Attempt's branch.
    """
    _skip_live_pull_request(harness)
    marker = _delivery_marker(_attempt_id(harness))
    body = f"Closes #{harness.issue_number}\n{marker}\n"
    if isinstance(harness.tracker, FakeTracker):
        opened = harness.tracker.open_pull_request(
            title="a title",
            head_branch="agent/30/1-slug",
            base_branch="main",
            body=body,
        )
        assert opened.draft is False
        found = harness.tracker.find_pull_request("agent/30/1-slug", marker)
        assert found is not None
        assert found.number == opened.number
        assert found.marker_present is True
        assert all(pr.draft is False for pr in harness.tracker.pull_requests)
    else:
        mocked: Any = request.getfixturevalue("requests_mock")
        _mocked_pull_requests(mocked, [])
        opened = harness.tracker.open_pull_request(
            title="a title",
            head_branch="agent/30/1-slug",
            base_branch="main",
            body=body,
        )
        assert opened.draft is False
        posted = [
            entry.json()
            for entry in mocked.request_history
            if entry.method == "POST" and str(entry.url).endswith("/pulls")
        ]
        assert len(posted) == 1
        assert "draft" not in posted[0], "no code path creates a draft (P-DEL-10)"
        assert posted[0]["head"] == "agent/30/1-slug"
        assert posted[0]["base"] == "main"
        assert posted[0]["body"] == body


def test_unmarked_pull_request_from_the_same_branch_is_not_ours(
    harness: TrackerHarness, request: pytest.FixtureRequest
) -> None:
    """L3-DEL-21: the marker rule reads in one direction only (§13, §15, §18).

    A pull request from the same ``agent/`` branch whose body carries no
    Attempt Marker is never adopted by the remote lookup.
    """
    _skip_live_pull_request(harness)
    marker = _delivery_marker(_attempt_id(harness))
    if isinstance(harness.tracker, FakeTracker):
        from coding_agent.tracker.port import PullRequest

        harness.tracker.pull_requests.append(
            PullRequest(
                number=7,
                url="https://tracker.fake/pulls/7",
                title="a title",
                head_branch="agent/30/1-slug",
                base_branch="main",
                body="a human's pull request without our marker",
                state="open",
                author_login=harness.tracker.agent_login,
                author_id=harness.tracker.agent_id,
                marker_present=False,
                draft=False,
            )
        )
        assert harness.tracker.find_pull_request("agent/30/1-slug", marker) is None
    else:
        mocked: Any = request.getfixturevalue("requests_mock")
        _mocked_pull_requests(
            mocked,
            [_mocked_pr_item(number=7, body="a human's pull request")],
        )
        assert harness.tracker.find_pull_request("agent/30/1-slug", marker) is None


def test_pull_request_by_another_account_is_not_our_receipt(
    harness: TrackerHarness, request: pytest.FixtureRequest
) -> None:
    """§13, §18: the remote lookup filters by the Agent Identity's id.

    A pull request by another account is not our receipt even when its body
    carries our marker.
    """
    _skip_live_pull_request(harness)
    marker = _delivery_marker(_attempt_id(harness))
    if isinstance(harness.tracker, FakeTracker):
        from coding_agent.tracker.port import PullRequest

        harness.tracker.agent_id = 4242
        harness.tracker.pull_requests.append(
            PullRequest(
                number=8,
                url="https://tracker.fake/pulls/8",
                title="a title",
                head_branch="agent/30/1-slug",
                base_branch="main",
                body=f"borrowed bytes\n{marker}\n",
                state="open",
                author_login="mallory",
                author_id=9999,
                marker_present=True,
                draft=False,
            )
        )
        assert harness.tracker.find_pull_request("agent/30/1-slug", marker) is None
    else:
        from coding_agent.tracker.github import GitHubTracker

        mocked: Any = request.getfixturevalue("requests_mock")
        assert isinstance(harness.tracker, GitHubTracker)
        harness.tracker = GitHubTracker(
            owner=harness.tracker.owner,
            repo=harness.tracker.repo,
            client=harness.tracker.client,
            agent_login=harness.tracker.agent_login,
            selection_label=harness.selection_label,
            ready_for_human=harness.ready_for_human,
            agent_id=4242,
        )
        _mocked_pull_requests(
            mocked,
            [
                _mocked_pr_item(
                    number=8, body=f"borrowed bytes\n{marker}\n", login="mallory", user_id=9999
                )
            ],
        )
        assert harness.tracker.find_pull_request("agent/30/1-slug", marker) is None


def test_pull_request_lookup_prefers_the_open_pull_request(
    harness: TrackerHarness, request: pytest.FixtureRequest
) -> None:
    """§13: where both states match, the open pull request is the receipt."""
    _skip_live_pull_request(harness)
    marker = _delivery_marker(_attempt_id(harness))
    if isinstance(harness.tracker, FakeTracker):
        from coding_agent.tracker.port import PullRequest

        for number, pr_state in ((3, "closed"), (4, "open")):
            harness.tracker.pull_requests.append(
                PullRequest(
                    number=number,
                    url=f"https://tracker.fake/pulls/{number}",
                    title="a title",
                    head_branch="agent/30/1-slug",
                    base_branch="main",
                    body=f"Closes #{harness.issue_number}\n{marker}\n",
                    state=pr_state,
                    author_login=harness.tracker.agent_login,
                    author_id=harness.tracker.agent_id,
                    marker_present=True,
                    draft=False,
                )
            )
        found = harness.tracker.find_pull_request("agent/30/1-slug", marker)
        assert found is not None
        assert (found.number, found.state) == (4, "open")
    else:
        mocked: Any = request.getfixturevalue("requests_mock")
        _mocked_pull_requests(
            mocked,
            [
                _mocked_pr_item(number=3, body=f"Closes #30\n{marker}\n", state="closed"),
                _mocked_pr_item(number=4, body=f"Closes #30\n{marker}\n", state="open"),
            ],
        )
        found = harness.tracker.find_pull_request("agent/30/1-slug", marker)
        assert found is not None
        assert (found.number, found.state) == (4, "open")
