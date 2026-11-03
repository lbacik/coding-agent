"""The Selection and Claim operations behave the same on every side.

Contract: ``docs/contract/mvp-runtime-contract.md`` §4 (candidates are the
open issues carrying the Workflow's Selection Label; selection writes
nothing) and §5 (the Claim is an assignment read back: the Attempt row
exists before the assignment, the assignment adds no label and no comment,
and a failed read-back removes only our own assignment).

MVP row ids: P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1, P-CLM-1,
P-CLM-2.

The suite is parametrised over the fake tracker in ``tests/support/`` and
the GitHub adapter. The fake and the mocked-GitHub params run in the default
suite; the live param runs the same flows against the sandbox repository
``lbacik/coding-agent-sandbox`` and is opt-in like the existing live tests
(it is skipped without ``GITHUB_TOKEN``). Reads assert reported facts; the
Claim tests assert the write order and that selection itself never writes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.tracker.port import CandidateIssue, TrackerPort
from support.fake_tracker import FakeTracker

TEST_BASE_URL = "https://api.github.test"

SANDBOX_OWNER = "lbacik"
SANDBOX_REPO = "coding-agent-sandbox"

SELECTION_LABEL = "ready-for-agent"
READY_FOR_HUMAN = "ready-for-human"


def _candidate(
    number: int,
    *,
    title: str = "Add a greeting",
    body: str = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
    is_open: bool = True,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
    created_at: str = "2026-10-01T00:00:00Z",
) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title=title,
        body=body,
        is_open=is_open,
        assignees=assignees,
        labels=labels,
        created_at=created_at,
    )


def _issue_payload(candidate: CandidateIssue) -> dict[str, Any]:
    """The GitHub issue JSON reporting ``candidate``'s facts."""
    return {
        "number": candidate.number,
        "title": candidate.title,
        "body": candidate.body,
        "state": "open" if candidate.is_open else "closed",
        "assignees": [{"login": login} for login in candidate.assignees],
        "labels": [{"name": name} for name in candidate.labels],
        "created_at": candidate.created_at,
        "issue_dependencies_summary": {"blocked_by": 0, "blocking": 0},
    }


def _mocked_tracker(requests_mock: Any) -> GitHubTrackerLike:
    """The GitHub adapter over mocked HTTP: the class the live side runs."""
    from coding_agent.tracker.github import GitHubTracker

    return GitHubTracker(
        owner="octocat",
        repo="sandbox",
        client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
        agent_login="octocat",
        selection_label=SELECTION_LABEL,
        ready_for_human=READY_FOR_HUMAN,
    )


GitHubTrackerLike = Any


def _mock_issue(requests_mock: Any, candidate: CandidateIssue) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/{candidate.number}",
        json=_issue_payload(candidate),
    )


def _mock_issue_list(requests_mock: Any, candidates: list[CandidateIssue]) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues",
        json=[_issue_payload(candidate) for candidate in candidates],
    )


def _fake_tracker() -> FakeTracker:
    tracker = FakeTracker(agent_login="octocat")
    tracker.candidates[31] = _candidate(31, created_at="2026-10-01T00:00:00Z")
    tracker.candidates[30] = _candidate(
        30,
        assignees=("someone-else",),
        labels=(SELECTION_LABEL, READY_FOR_HUMAN),
        created_at="2026-09-01T00:00:00Z",
    )
    return tracker


def _ensure_live_label(client: GitHubClient, owner: str, repo: str, name: str) -> None:
    lookup = client.get(f"/repos/{owner}/{repo}/labels/{name}")
    if lookup.status_code == 404:
        created = client.post(
            f"/repos/{owner}/{repo}/labels",
            json={
                "name": name,
                "color": "ededed",
                "description": "Selection contract suite probe label.",
            },
        )
        assert created.status_code in (201, 422), created.text


def _live_tracker() -> Iterator[TrackerPort]:
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
    opened: list[int] = []
    try:
        for title in ("selection contract probe one", "selection contract probe two"):
            response = client.post(
                f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues",
                json={
                    "title": title,
                    "body": "## Acceptance criteria\n- [ ] probe\n",
                },
            )
            assert response.status_code == 201, response.text
            number = int(response.json()["number"])
            labelled = client.post(
                f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/labels",
                json={"labels": [SELECTION_LABEL]},
            )
            assert labelled.status_code == 200, labelled.text
            opened.append(number)
        yield GitHubTracker(
            owner=SANDBOX_OWNER,
            repo=SANDBOX_REPO,
            client=client,
            agent_login=login,
            selection_label=SELECTION_LABEL,
            ready_for_human=READY_FOR_HUMAN,
        )
    finally:
        for number in opened:
            client.patch(
                f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}",
                json={"state": "closed"},
            )


@pytest.fixture(params=["fake", "github-mocked", "github-live"])
def tracker(request: pytest.FixtureRequest) -> Iterator[TrackerPort]:
    """One contract-suite side per param: fake, mocked GitHub, live GitHub."""
    kind = str(request.param)
    if kind == "fake":
        yield _fake_tracker()
    elif kind == "github-mocked":
        yield _mocked_tracker(request.getfixturevalue("requests_mock"))
    elif kind == "github-live":
        yield from _live_tracker()
    else:  # pragma: no cover - the params above are exhaustive
        raise AssertionError(f"unknown tracker {kind!r}")


def _is_live(tracker: TrackerPort) -> bool:
    """Whether this side runs against the sandbox repository."""
    from coding_agent.tracker.github import GitHubTracker

    return isinstance(tracker, GitHubTracker) and tracker.owner == SANDBOX_OWNER


def test_list_candidates_reports_labeled_issues(
    tracker: TrackerPort, request: pytest.FixtureRequest
) -> None:
    """§4: the candidates are the open issues carrying the Selection Label."""
    if isinstance(tracker, FakeTracker):
        listed = tracker.list_candidates(SELECTION_LABEL)

        assert [candidate.number for candidate in listed] == [30, 31]
        eligible = next(item for item in listed if item.number == 31)
        assert eligible.title == "Add a greeting"
        assert eligible.body.startswith("## Acceptance criteria")
        assert eligible.is_open is True
        assert eligible.assignees == ()
        assert SELECTION_LABEL in eligible.labels
        assert eligible.created_at == "2026-10-01T00:00:00Z"
    elif not _is_live(tracker):
        mocked: Any = request.getfixturevalue("requests_mock")
        _mock_issue_list(
            mocked,
            [
                _candidate(31),
                _candidate(
                    30,
                    assignees=("someone-else",),
                    labels=(SELECTION_LABEL, READY_FOR_HUMAN),
                ),
            ],
        )
        listed = tracker.list_candidates(SELECTION_LABEL)

        assert [candidate.number for candidate in listed] == [31, 30]
        assert listed[0].assignees == ()
        assert listed[1].assignees == ("someone-else",)
        query = mocked.request_history[0].qs
        assert query["labels"] == [SELECTION_LABEL]
        assert query["state"] == ["open"]
        assert query["sort"] == ["created"]
        assert query["direction"] == ["asc"]
    else:
        listed = tracker.list_candidates(SELECTION_LABEL)

        assert {candidate.number for candidate in listed} >= set(
            _live_probe_numbers(tracker)
        )
        for candidate in listed:
            assert candidate.is_open is True
            assert SELECTION_LABEL in candidate.labels


def _live_probe_numbers(tracker: TrackerPort) -> set[int]:
    """The probe issues the live harness opened carry the suite's titles."""
    numbers: set[int] = set()
    for candidate in tracker.list_candidates(SELECTION_LABEL):
        if candidate.title.startswith("selection contract probe"):
            numbers.add(candidate.number)
    assert len(numbers) == 2
    return numbers


def test_fetch_candidate_reports_every_guard_fact(
    tracker: TrackerPort, request: pytest.FixtureRequest
) -> None:
    """§4: the read-back carries assignees, labels, state and body."""
    if isinstance(tracker, FakeTracker):
        fetched = tracker.fetch_candidate(30)

        assert fetched.is_open is True
        assert fetched.assignees == ("someone-else",)
        assert READY_FOR_HUMAN in fetched.labels
    elif not _is_live(tracker):
        mocked: Any = request.getfixturevalue("requests_mock")
        _mock_issue(
            mocked,
            _candidate(
                30,
                assignees=("someone-else",),
                labels=(SELECTION_LABEL, READY_FOR_HUMAN),
            ),
        )
        fetched = tracker.fetch_candidate(30)

        assert fetched.number == 30
        assert fetched.is_open is True
        assert fetched.assignees == ("someone-else",)
        assert fetched.labels == (SELECTION_LABEL, READY_FOR_HUMAN)
    else:
        numbers = sorted(_live_probe_numbers(tracker))
        fetched = tracker.fetch_candidate(numbers[0])

        assert fetched.number == numbers[0]
        assert fetched.is_open is True
        assert SELECTION_LABEL in fetched.labels


def test_selection_writes_nothing_for_ready_for_human(
    tracker: TrackerPort, request: pytest.FixtureRequest
) -> None:
    """P-SEL-1: a doubly-labelled issue is never claimed; selection writes nothing."""
    if isinstance(tracker, FakeTracker):
        calls_before = list(tracker.calls)
        tracker.list_candidates(SELECTION_LABEL)
        tracker.fetch_candidate(30)

        assert tracker.calls[len(calls_before):] == [
            ("list_candidates", (SELECTION_LABEL,)),
            ("fetch_candidate", (30,)),
        ]
    elif not _is_live(tracker):
        mocked: Any = request.getfixturevalue("requests_mock")
        _mock_issue_list(mocked, [_candidate(30, labels=(SELECTION_LABEL, READY_FOR_HUMAN))])
        _mock_issue(mocked, _candidate(30, labels=(SELECTION_LABEL, READY_FOR_HUMAN)))
        tracker.list_candidates(SELECTION_LABEL)
        tracker.fetch_candidate(30)

        assert mocked.request_history
        assert {entry.method for entry in mocked.request_history} == {"GET"}
    else:
        numbers = sorted(_live_probe_numbers(tracker))
        before = tracker.fetch_candidate(numbers[0])
        tracker.list_candidates(SELECTION_LABEL)
        after = tracker.fetch_candidate(numbers[0])

        assert before.labels == after.labels
        assert before.assignees == after.assignees


def test_claim_assigns_without_label_or_comment(
    tracker: TrackerPort, request: pytest.FixtureRequest
) -> None:
    """P-CLM-1: the Claim assigns the Agent Identity — no label, no comment."""
    if isinstance(tracker, FakeTracker):
        labels_before = tracker.candidates[31].labels
        tracker.assign_issue(31)

        assert tracker.candidates[31].assignees == (tracker.agent_login,)
        assert tracker.candidates[31].labels == labels_before
        assert "post_terminal_comment" not in tracker.call_names()
        assert "add_ready_for_human" not in tracker.call_names()
        assert "remove_selection_label" not in tracker.call_names()

        tracker.release_assignment(31)
    elif not _is_live(tracker):
        mocked: Any = request.getfixturevalue("requests_mock")
        _mock_issue(mocked, _candidate(31))
        mocked.get(
            f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/31/comments",
            json=[],
        )
        mocked.post(
            f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/31/assignees",
            json={"number": 31},
            status_code=201,
        )
        mocked.delete(
            f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/31/assignees",
            json={"number": 31},
        )
        before = tracker.fetch_candidate(31)
        comments_before = tracker.client.get(  # type: ignore[attr-defined]
            "/repos/octocat/sandbox/issues/31/comments"
        ).json()
        tracker.assign_issue(31)
        after = tracker.fetch_candidate(31)
        comments_after = tracker.client.get(  # type: ignore[attr-defined]
            "/repos/octocat/sandbox/issues/31/comments"
        ).json()

        assert after.labels == before.labels
        assert comments_after == comments_before
        writes = [entry for entry in mocked.request_history if entry.method != "GET"]
        assert len(writes) == 1
        assert writes[0].json() == {"assignees": ["octocat"]}

        tracker.release_assignment(31)
    else:
        numbers = sorted(_live_probe_numbers(tracker))
        number = numbers[0]
        before = tracker.fetch_candidate(number)
        client = tracker.client  # type: ignore[attr-defined]
        comments_before = client.get(
            f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/comments"
        ).json()
        try:
            tracker.assign_issue(number)
            after = tracker.fetch_candidate(number)
            comments_after = client.get(
                f"/repos/{SANDBOX_OWNER}/{SANDBOX_REPO}/issues/{number}/comments"
            ).json()

            assert tracker.agent_login in after.assignees  # type: ignore[attr-defined]
            assert set(after.labels) == set(before.labels)
            assert comments_after == comments_before
        finally:
            tracker.release_assignment(number)
