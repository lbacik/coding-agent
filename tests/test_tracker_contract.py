"""The tracker contract suite: the fake and the GitHub adapter behave the same.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph owns
every external write), §13 (the write sequence; bodies rendered once), §14
("Recording before publishing" and the terminal writes in order, C-OUT-1) and
§18 (the two structural abstentions: the Worker never edits the Target Issue's
body and never applies the Selection Label — the port cannot express them).

MVP row ids: C-TRK-1, C-OUT-1.

The suite is parametrised over the fake tracker in ``tests/support/`` and the
GitHub adapter. The fake and the mocked-GitHub params run in the default suite;
the live param runs the same flows against the sandbox repository
``lbacik/coding-agent-sandbox`` and is opt-in like the existing live tests
(it is skipped without ``GITHUB_TOKEN``). The fake and the mocked adapter
assert the C-OUT-1 order exactly; the live side asserts the end state the
ordered writes leave on GitHub.
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
        json={"number": 30, "title": "a title", "body": "a body"},
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
def harness(
    request: pytest.FixtureRequest, requests_mock: Any
) -> Iterator[TrackerHarness]:
    """One contract-suite side per param: fake, mocked GitHub, live GitHub."""
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
        yield _mocked_harness(requests_mock)
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


def test_c_out_1_terminal_writes_run_in_contract_order(
    harness: TrackerHarness, tmp_path: Path, requests_mock: Any
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
        assert _mocked_paths(requests_mock) == [
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
    harness: TrackerHarness, tmp_path: Path, requests_mock: Any
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
        assert ("POST", "/repos/octocat/sandbox/issues/30/labels") not in _mocked_paths(
            requests_mock
        )
    else:
        issue = _live_issue(harness)
        labels = {label["name"] for label in issue["labels"]}
        assert harness.ready_for_human not in labels


@pytest.mark.parametrize("assigned_by_us", [True, False, None])
def test_assignment_removed_only_when_recorded_as_assigned_by_us(
    harness: TrackerHarness,
    tmp_path: Path,
    requests_mock: Any,
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
        deletions = [
            path
            for method, path in _mocked_paths(requests_mock)
            if method == "DELETE" and path.endswith("/assignees")
        ]
        assert (deletions != []) is (assigned_by_us is True)
    else:
        issue = _live_issue(harness)
        assignees = {user["login"] for user in issue["assignees"]}
        assert (harness.agent_login in assignees) is (assigned_by_us is not True)


def test_repeated_write_sends_the_recorded_bytes(
    harness: TrackerHarness, tmp_path: Path, requests_mock: Any
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
        posted = [
            request.json()["body"]
            for request in requests_mock.request_history
            if request.method == "POST" and str(request.url).endswith("/comments")
        ]
        assert posted == [recorded, recorded]
    else:
        bodies = [str(comment["body"]) for comment in _live_comments(harness)]
        assert bodies.count(recorded) == 2
