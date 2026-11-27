"""The Jira Cloud tracker adapter contract (recorded-shape fixtures).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: candidates are open project issues carrying the
Selection Label; selection writes nothing), §5.1 (the ``labels`` changelog
entry is the authorisation event, falling back to the status-transition
actor where labels are automation-set), §6 (the Claim: assignment plus a
Claim Comment bearing an Attempt Marker with ``kind=claim``), §7 (Gate
re-reads), §14 (Effect Intent keys travel in Attempt Markers; lookups
filter by the Agent Identity and read the full paginated list) and §18
(the dedicated-only rule: no repository-owner identity, no mode knob),
plus [ADR
0038](../docs/adr/0038-the-issue-tracker-and-the-code-host-are-reached-only-through-their-ports.md)
(one pinned project, link-by-key delivery with no transition write).

Row ids: INP-1 … INP-14, INP-19, INP-20.

The adapter is exercised over mocked HTTP with recorded Jira shapes; no
network and no token are used. The fake in ``tests/support/`` stays
GitHub-shaped: this suite is the Jira side of the shared contract.
"""

from __future__ import annotations

import json as json_module
from typing import Any

import pytest

from coding_agent.identity.startup import StartupCheckFailed
from coding_agent.identity.token import TokenRejected
from coding_agent.inputs import jira as jira_adapter
from coding_agent.inputs.authorisation import label_authorised
from coding_agent.inputs.claim import claim_marker_for
from coding_agent.inputs.errors import (
    CredentialFailure,
    HostFailure,
    InputError,
    InputNotFound,
    PermissionRefusal,
    RateLimitExhausted,
)
from coding_agent.inputs.jira import (
    JiraTracker,
    format_jira_marker,
    marker_property_value,
)
from coding_agent.inputs.marker import AttemptMarker
from coding_agent.inputs.port import IssueTrackerPort
from coding_agent.jira.client import JiraClient
from coding_agent.runtime.effects.driver import _is_transient_perform_error

TEST_BASE_URL = "https://jira.test"
PROJECT = "PROJ"
AGENT_LOGIN = "Agent Display"
AGENT_ACCOUNT = "agent-acct-1"
AGENT_EMAIL = "agent@example.test"
SELECTION_LABEL = "agent:implement"


def _make_tracker(**overrides: Any) -> JiraTracker:
    """The Jira adapter over no network: failure stubs arrive per test."""
    sleeps: list[float] = []
    kwargs: dict[str, Any] = {
        "project": PROJECT,
        "client": JiraClient(
            "jira-token-secret", base_url=TEST_BASE_URL, email=AGENT_EMAIL
        ),
        "agent_login": AGENT_LOGIN,
        "account_id": AGENT_ACCOUNT,
        "sleep": sleeps.append,
        "now": lambda: 0.0,
    }
    kwargs.update(overrides)
    return JiraTracker(**kwargs)


def _user(
    account: str,
    display: str,
    account_type: str = "atlassian",
) -> dict[str, Any]:
    return {
        "accountId": account,
        "displayName": display,
        "accountType": account_type,
    }


def _status(category: str, name: str = "In Progress") -> dict[str, Any]:
    return {
        "name": name,
        "statusCategory": {"key": category, "name": name},
    }


def _issue_entry(
    number: int,
    *,
    labels: list[str] | None = None,
    assignee: str | None = None,
    status_category: str = "indeterminate",
    summary: str = "a title",
    description: Any = "a body",
    creator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "status": _status(status_category),
        "labels": labels if labels is not None else [SELECTION_LABEL],
        "assignee": (
            _user(assignee, f"user {assignee}") if assignee is not None else None
        ),
        "created": f"2026-10-{number:02d}T00:00:00.000+0000",
        "creator": creator if creator is not None else _user("human-1", "human"),
        "reporter": _user("human-1", "human"),
    }
    return {"id": str(10000 + number), "key": f"{PROJECT}-{number}", "fields": fields}


def _comment_entry(
    comment_id: int,
    body: Any,
    account: str,
    display: str,
    created: str = "2026-10-01T00:00:00.000+0000",
) -> dict[str, Any]:
    return {
        "id": str(comment_id),
        "body": body,
        "author": _user(account, display),
        "created": created,
        "updated": created,
    }


def _history(
    created: str,
    author: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"created": created, "author": author, "items": items}


def _labels_item(from_labels: str | None, to_labels: str | None) -> dict[str, Any]:
    return {
        "field": "labels",
        "fromString": from_labels,
        "toString": to_labels,
    }


def test_jira_serves_the_issue_tracker_port() -> None:
    """``IssueTrackerPort`` is ``InputPort`` plus ``delivery_reference`` (§5–§7, §14)."""
    tracker = _make_tracker()
    for operation in (
        "target_repo",
        "agent_login",
        "agent_id",
        "fetch_agent_identity",
        "fetch_repository_owner_id",
        "fetch_input",
        "list_issue_candidates",
        "list_pull_request_candidates",
        "read_labeled_event",
        "has_open_blocker",
        "assign_input",
        "unassign_input",
        "format_marker",
        "post_claim_comment",
        "list_comments",
        "find_comment_by_marker",
        "delete_comment",
        "post_comment",
        "remove_label",
        "add_labels",
        "delivery_reference",
    ):
        assert hasattr(tracker, operation), operation
    for operation in (
        "profile",
        "pull_requests",
        "fetch_base_tip",
        "list_open_pull_requests",
        "publish_review",
        "list_reviews",
        "find_review_by_marker",
        "open_pull_request",
        "find_pull_request",
    ):
        assert not hasattr(tracker, operation), operation
    assert tracker.target_repo == PROJECT
    assert tracker.agent_login == AGENT_LOGIN
    assert tracker.agent_id is None


def test_repository_owner_identity_is_not_implemented() -> None:
    """ADR 0038: no repository owner exists, so the dedicated rule needs no detection."""
    with pytest.raises(NotImplementedError):
        _make_tracker().fetch_repository_owner_id()


def test_format_marker_has_no_github_html() -> None:
    """The body line is human-readable, never the HTML comment fence (§14.1)."""
    marker = AttemptMarker(
        attempt="implement#42/1",
        kind="post-comment",
        step="process",
        execution_number=1,
        ordinal=0,
    )
    rendered = _make_tracker().format_marker(marker)
    assert rendered == format_jira_marker(marker)
    assert "<!--" not in rendered
    assert "coding-agent attempt=" not in rendered
    assert "attempt=implement#42/1" in rendered
    assert "kind=post-comment" in rendered
    assert "n=1" in rendered


def test_delivery_reference_returns_the_issue_key_and_ignores_draft(
    requests_mock: Any,
) -> None:
    """INP-14, ADR 0038: link-by-key is the whole delivery; no transition write."""
    _ = requests_mock
    tracker = _make_tracker()
    assert tracker.delivery_reference("42") == "PROJ-42"
    assert tracker.delivery_reference("42", draft=True) == "PROJ-42"
    assert tracker.delivery_reference("PROJ-42") == "PROJ-42"
    assert requests_mock.request_history == []


def test_list_issue_candidates_queries_the_pinned_project(
    requests_mock: Any,
) -> None:
    """§5.2: JQL pins the project, the label and the open category; selection writes nothing."""
    first = [_issue_entry(number) for number in range(1, 51)]
    second = [_issue_entry(51), _issue_entry(52)]

    def _search_callback(request: Any, context: Any) -> str:
        start = int(request.qs.get("startat", ["0"])[0])
        context.status_code = 200
        if start == 0:
            return json_module.dumps(
                {"startAt": 0, "maxResults": 50, "total": 52, "issues": first}
            )
        return json_module.dumps(
            {"startAt": 50, "maxResults": 50, "total": 52, "issues": second}
        )

    requests_mock.get(f"{TEST_BASE_URL}/rest/api/3/search", text=_search_callback)
    tracker = _make_tracker()
    candidates = tracker.list_issue_candidates(SELECTION_LABEL)
    assert [candidate.number for candidate in candidates] == list(range(1, 53))
    assert all(candidate.input_type == "issue" for candidate in candidates)
    assert all(candidate.is_open for candidate in candidates)
    search_request = requests_mock.request_history[0]
    jql = search_request.qs["jql"][0]
    assert "project = proj" in jql
    assert SELECTION_LABEL in jql
    assert "statuscategory != done" in jql
    assert all(entry.method == "GET" for entry in requests_mock.request_history)


def test_list_issue_candidates_reads_assignees_from_account_ids(
    requests_mock: Any,
) -> None:
    """§5.2: the assignee comes from the ``accountId`` field, never a JQL filter."""
    entry = _issue_entry(7, assignee="assignee-acct-9")
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/search",
        json={"startAt": 0, "maxResults": 50, "total": 1, "issues": [entry]},
    )
    candidates = _make_tracker().list_issue_candidates(SELECTION_LABEL)
    assert candidates[0].assignees == ("assignee-acct-9",)
    jql = requests_mock.request_history[0].qs["jql"][0]
    assert "assignee" not in jql.lower()


def test_list_issue_candidates_orders_oldest_first(
    requests_mock: Any,
) -> None:
    """§5.4 order holds even where the server order does not."""
    newer = _issue_entry(9)
    older = _issue_entry(3)
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/search",
        json={"startAt": 0, "maxResults": 50, "total": 2, "issues": [newer, older]},
    )
    candidates = _make_tracker().list_issue_candidates(SELECTION_LABEL)
    assert [candidate.number for candidate in candidates] == [3, 9]


def test_fetch_input_reports_guard_and_gate_facts(requests_mock: Any) -> None:
    """§5, §7: number, title, body, openness, labels, assignee, author."""
    entry = _issue_entry(
        42,
        labels=[SELECTION_LABEL, "extra"],
        assignee=AGENT_ACCOUNT,
        status_category="indeterminate",
    )
    requests_mock.get(f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42", json=entry)
    fetched = _make_tracker().fetch_input(42, "issue")
    assert fetched.number == 42
    assert fetched.title == "a title"
    assert fetched.body == "a body"
    assert fetched.is_open is True
    assert SELECTION_LABEL in fetched.labels
    assert fetched.assignees == (AGENT_ACCOUNT,)
    assert fetched.author_login == "human"
    assert fetched.author_id is None


def test_fetch_input_reads_done_status_as_closed(requests_mock: Any) -> None:
    """§7: the Done category withdraws the input."""
    entry = _issue_entry(42, status_category="done")
    requests_mock.get(f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42", json=entry)
    assert _make_tracker().fetch_input(42, "issue").is_open is False


def test_fetch_input_absent_is_neutral_not_found(requests_mock: Any) -> None:
    """A 404 read reports absence as neutral ``InputNotFound``."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        status_code=404,
        json={"errorMessages": ["Issue does not exist"]},
    )
    with pytest.raises(InputNotFound):
        _make_tracker().fetch_input(42, "issue")


def test_fetch_pull_request_input_is_not_served(requests_mock: Any) -> None:
    """The Jira tracker serves issues only: pull-request reads fail neutrally."""
    _ = requests_mock
    with pytest.raises(InputError) as exc_info:
        _make_tracker().fetch_input(42, "pull-request")
    assert exc_info.value.failure_class == "jira"
    assert requests_mock.request_history == []


def test_pull_request_candidates_are_always_empty(requests_mock: Any) -> None:
    """§5.2: no pull requests live on a Jira tracker; selection writes nothing."""
    assert _make_tracker().list_pull_request_candidates(SELECTION_LABEL) == []
    assert requests_mock.request_history == []


def test_read_labeled_event_latest_labels_add_wins(requests_mock: Any) -> None:
    """§5.1: the latest ``labels``-add entry decides; a later status move does not."""
    histories = [
        _history(
            "2026-09-01T00:00:00.000+0000",
            _user("bot-acct", "some-bot", "app"),
            [_labels_item(None, SELECTION_LABEL)],
        ),
        _history(
            "2026-10-01T00:00:00.000+0000",
            _user("human-1", "human"),
            [_labels_item("other", f"other, {SELECTION_LABEL}")],
        ),
        _history(
            "2026-10-02T00:00:00.000+0000",
            _user("human-2", "mover"),
            [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
        ),
    ]
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        json={"key": "PROJ-42", "changelog": {"histories": histories}},
    )
    event = _make_tracker().read_labeled_event(42, "issue", SELECTION_LABEL)
    assert event is not None
    assert event.label == SELECTION_LABEL
    assert event.actor_login == "human"
    assert event.actor_id is None
    assert event.actor_type == "User"
    assert event.created_at == "2026-10-01T00:00:00.000+0000"


def test_read_labeled_event_marks_automation_actors_as_bots(
    requests_mock: Any,
) -> None:
    """§5.1: an ``app`` account type reads as a ``Bot`` actor."""
    histories = [
        _history(
            "2026-10-01T00:00:00.000+0000",
            _user("bot-acct", "automation", "app"),
            [_labels_item(None, SELECTION_LABEL)],
        )
    ]
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        json={"key": "PROJ-42", "changelog": {"histories": histories}},
    )
    event = _make_tracker().read_labeled_event(42, "issue", SELECTION_LABEL)
    assert event is not None
    assert event.actor_type == "Bot"


def test_read_labeled_event_falls_back_to_the_status_actor(
    requests_mock: Any,
) -> None:
    """§5.1, ADR 0038: where labels are automation-set, the transition actor decides."""
    histories = [
        _history(
            "2026-10-01T00:00:00.000+0000",
            _user("human-2", "mover"),
            [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
        )
    ]
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        json={"key": "PROJ-42", "changelog": {"histories": histories}},
    )
    event = _make_tracker().read_labeled_event(42, "issue", SELECTION_LABEL)
    assert event is not None
    assert event.actor_login == "mover"


def test_read_labeled_event_absent_is_none(requests_mock: Any) -> None:
    """§5.1: with neither entry there is no authorisation, so the Claim is refused."""
    histories = [
        _history(
            "2026-10-01T00:00:00.000+0000",
            _user("human-2", "mover"),
            [{"field": "comment", "fromString": "", "toString": "a note"}],
        )
    ]
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        json={"key": "PROJ-42", "changelog": {"histories": histories}},
    )
    assert _make_tracker().read_labeled_event(42, "issue", SELECTION_LABEL) is None


def test_dedicated_rule_applies_to_jira_events() -> None:
    """§5.1, ADR 0038: the agent's own labelling and bots are refused; a human passes."""
    from coding_agent.inputs.port import LabeledEvent

    def _event(login: str, actor_type: str) -> LabeledEvent:
        return LabeledEvent(
            label=SELECTION_LABEL,
            actor_login=login,
            actor_id=None,
            actor_type=actor_type,
            created_at="2026-10-01T00:00:00.000+0000",
        )

    assert label_authorised(None, agent_login=AGENT_LOGIN, agent_id=None, identity_mode="dedicated") is False
    assert (
        label_authorised(
            _event(AGENT_LOGIN, "User"),
            agent_login=AGENT_LOGIN,
            agent_id=None,
            identity_mode="dedicated",
        )
        is False
    )
    assert (
        label_authorised(
            _event("some-bot", "Bot"),
            agent_login=AGENT_LOGIN,
            agent_id=None,
            identity_mode="dedicated",
        )
        is False
    )
    assert (
        label_authorised(
            _event("human", "User"),
            agent_login=AGENT_LOGIN,
            agent_id=None,
            identity_mode="dedicated",
        )
        is True
    )


def _blocker_issue(
    key: str, status_category: str, *, with_status: bool = True
) -> dict[str, Any]:
    fields: dict[str, Any] = {"summary": "blocker"}
    if with_status:
        fields["status"] = _status(status_category)
    return {"key": key, "fields": fields}


def _links_body(links: list[dict[str, Any]]) -> dict[str, Any]:
    entry = _issue_entry(42)
    assert isinstance(entry["fields"], dict)
    entry["fields"]["issuelinks"] = links
    return entry


def _blocked_by_link(linked: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "inwardIssue": linked,
    }


def test_has_open_blocker_true_only_for_open_blocked_side_links(
    requests_mock: Any,
) -> None:
    """§5.2: an ``is blocked by`` link to a non-Done issue blocks; the rest do not."""

    def _check(links: list[dict[str, Any]]) -> bool:
        requests_mock.reset_mock()
        requests_mock.get(
            f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42", json=_links_body(links)
        )
        return _make_tracker().has_open_blocker(42, "issue")

    assert _check([_blocked_by_link(_blocker_issue("PROJ-1", "indeterminate"))]) is True
    assert _check([_blocked_by_link(_blocker_issue("PROJ-1", "done"))]) is False
    assert (
        _check(
            [
                {
                    "type": {
                        "name": "Blocks",
                        "inward": "is blocked by",
                        "outward": "blocks",
                    },
                    "outwardIssue": _blocker_issue("PROJ-1", "indeterminate"),
                }
            ]
        )
        is False
    )
    assert _check([]) is False
    assert (
        _check([_blocked_by_link(_blocker_issue("PROJ-1", "done", with_status=False))])
        is True
    )


def test_claim_round_trip_through_the_comment_property(requests_mock: Any) -> None:
    """§6, §14: assign, then Claim Comment; found again by property, lowest id wins."""
    marker = claim_marker_for("implement#42/1")
    marker_text = format_jira_marker(marker)
    body = f"coding-agent claims implement#42/1 for the claim.\n\n{marker_text}\n"

    requests_mock.put(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/assignee",
        status_code=204,
        text="",
    )
    posted: list[dict[str, Any]] = []

    def _post_comment_callback(request: Any, context: Any) -> str:
        payload = json_module.loads(request.text)
        entry = _comment_entry(10003, payload["body"], AGENT_ACCOUNT, AGENT_LOGIN)
        entry["created"] = "2026-10-03T00:00:00.000+0000"
        entry["updated"] = "2026-10-03T00:00:00.000+0000"
        posted.append(entry)
        context.status_code = 201
        return json_module.dumps(entry)

    requests_mock.post(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        text=_post_comment_callback,
    )
    stored_properties: dict[str, dict[str, Any]] = {}

    def _put_property_callback(request: Any, context: Any) -> str:
        stored_properties[request.url] = json_module.loads(request.text)
        context.status_code = 200
        return json_module.dumps({})

    requests_mock.put(
        f"{TEST_BASE_URL}/rest/api/3/comment/10003/properties/attempt-marker",
        text=_put_property_callback,
    )
    foreign = _comment_entry(
        10001, f"a human quoting us: {marker_text}", "human-1", "human"
    )
    own_without_property = _comment_entry(
        10002, body, AGENT_ACCOUNT, AGENT_LOGIN, "2026-10-02T00:00:00.000+0000"
    )

    def _comments_callback(request: Any, context: Any) -> str:
        _ = request
        context.status_code = 200
        comments = [foreign, own_without_property, *posted]
        return json_module.dumps(
            {
                "startAt": 0,
                "maxResults": 50,
                "total": len(comments),
                "comments": comments,
            }
        )

    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        text=_comments_callback,
    )

    def _property_callback(request: Any, context: Any) -> str:
        context.status_code = 200
        if request.url.endswith("/10003/properties/attempt-marker"):
            return json_module.dumps(
                {"key": "attempt-marker", "value": marker_property_value(marker)}
            )
        context.status_code = 404
        return json_module.dumps({"errorMessages": ["missing"]})

    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/comment/10002/properties/attempt-marker",
        status_code=404,
        json={"errorMessages": ["missing"]},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/comment/10003/properties/attempt-marker",
        text=_property_callback,
    )

    tracker = _make_tracker()
    tracker.assign_input(42, "issue")
    assert requests_mock.request_history[-1].json() == {"accountId": AGENT_ACCOUNT}
    posted_comment = tracker.post_claim_comment(42, "issue", body, marker)
    assert posted_comment.comment_id == 10003
    assert posted_comment.author_login == AGENT_LOGIN
    assert (
        stored_properties[
            f"{TEST_BASE_URL}/rest/api/3/comment/10003/properties/attempt-marker"
        ]
        == marker_property_value(marker)
    )
    found = tracker.find_comment_by_marker(42, "issue", marker)
    assert found is not None
    assert found.comment_id == 10003
    property_gets = [
        entry
        for entry in requests_mock.request_history
        if entry.method == "GET" and "/properties/attempt-marker" in entry.url
    ]
    assert {entry.url.rsplit("/", 3)[-3] for entry in property_gets} == {
        "10002",
        "10003",
    }


def test_find_comment_by_marker_ignores_foreign_account_property(
    requests_mock: Any,
) -> None:
    """§14: a property match on another account is never our receipt."""
    marker = claim_marker_for("implement#42/1")
    foreign = _comment_entry(10001, "a body", "human-1", "human")
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        json={"startAt": 0, "maxResults": 50, "total": 1, "comments": [foreign]},
    )
    assert _make_tracker().find_comment_by_marker(42, "issue", marker) is None
    assert not any(
        "/properties/" in entry.url for entry in requests_mock.request_history
    )


def test_delete_claim_comment_is_idempotent(requests_mock: Any) -> None:
    """§6.2, §16.2: deleting the Claim Comment twice still succeeds."""
    marker = claim_marker_for("implement#42/1")
    own = _comment_entry(10001, "a body", AGENT_ACCOUNT, AGENT_LOGIN)
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        json={"startAt": 0, "maxResults": 50, "total": 1, "comments": [own]},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/comment/10001/properties/attempt-marker",
        json={"key": "attempt-marker", "value": marker_property_value(marker)},
    )
    deletes = {"count": 0}

    def _delete_callback(request: Any, context: Any) -> str:
        _ = request
        deletes["count"] += 1
        if deletes["count"] == 1:
            context.status_code = 204
            return ""
        context.status_code = 404
        return json_module.dumps({"errorMessages": ["missing"]})

    requests_mock.delete(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment/10001",
        text=_delete_callback,
    )
    tracker = _make_tracker()
    found = tracker.find_comment_by_marker(42, "issue", marker)
    assert found is not None
    tracker.delete_comment(found.comment_id)
    tracker.delete_comment(found.comment_id)
    assert deletes["count"] == 2


def test_post_comment_carries_adf_and_marker_property(requests_mock: Any) -> None:
    """§10.7, §14: the document posts as ADF; the property carries the marker."""
    marker = AttemptMarker(
        attempt="implement#42/1",
        kind="post-comment",
        step="publish",
        execution_number=1,
        ordinal=0,
    )
    body = f"a document\n\n{format_jira_marker(marker)}"
    entry = _comment_entry(10001, {"type": "doc", "version": 1, "content": []}, AGENT_ACCOUNT, AGENT_LOGIN)
    requests_mock.post(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        status_code=201,
        json=entry,
    )
    requests_mock.put(
        f"{TEST_BASE_URL}/rest/api/3/comment/10001/properties/attempt-marker",
        status_code=200,
        json={},
    )
    posted = _make_tracker().post_comment(42, "issue", body, marker)
    assert posted.comment_id == 10001
    assert posted.marker_present is True
    sent = requests_mock.request_history[0].json()
    assert sent["body"]["type"] == "doc"
    assert "a document" in json_module.dumps(sent["body"])
    assert requests_mock.request_history[1].json() == marker_property_value(marker)


def test_list_comments_returns_oldest_first(requests_mock: Any) -> None:
    """§6.1: the read reports every comment with author and timestamps."""
    newer = _comment_entry(
        10002, "second", "human-1", "human", "2026-10-02T00:00:00.000+0000"
    )
    older = _comment_entry(
        10001, "first", "human-1", "human", "2026-10-01T00:00:00.000+0000"
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        json={"startAt": 0, "maxResults": 50, "total": 2, "comments": [newer, older]},
    )
    comments = _make_tracker().list_comments(42, "issue")
    assert [comment.comment_id for comment in comments] == [10001, 10002]
    assert comments[0].body == "first"
    assert comments[0].created_at == "2026-10-01T00:00:00.000+0000"


def test_remove_absent_label_is_a_successful_noop(requests_mock: Any) -> None:
    """§15.3: removing an absent label succeeds and writes nothing."""
    entry = _issue_entry(42, labels=[SELECTION_LABEL])
    requests_mock.get(f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42", json=entry)
    tracker = _make_tracker()
    tracker.remove_label(42, "issue", "no-such-label")
    assert len(requests_mock.request_history) == 1
    fetched = tracker.fetch_input(42, "issue")
    assert SELECTION_LABEL in fetched.labels


def test_remove_present_label_sends_the_update(requests_mock: Any) -> None:
    """§15.3: removing a present label issues the idempotent update."""
    entry = _issue_entry(42, labels=[SELECTION_LABEL, "stale"])
    requests_mock.get(f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42", json=entry)
    requests_mock.put(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42", status_code=204, text=""
    )
    _make_tracker().remove_label(42, "issue", "stale")
    assert requests_mock.request_history[-1].json() == {
        "update": {"labels": [{"remove": "stale"}]}
    }


def test_add_labels_empty_is_a_noop(requests_mock: Any) -> None:
    """§15.3: empty end labels write nothing."""
    _make_tracker().add_labels(42, "issue", ())
    assert requests_mock.request_history == []


def test_unassign_removes_only_the_agent_identity(requests_mock: Any) -> None:
    """§6: unassign clears the agent's ``accountId`` slot."""
    requests_mock.put(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/assignee",
        status_code=204,
        text="",
    )
    _make_tracker().unassign_input(42, "issue")
    assert requests_mock.request_history[0].json() == {"accountId": None}


def test_confirmed_credential_failure_maps_to_neutral(requests_mock: Any) -> None:
    """A 401 confirmed by the identity probe raises neutral ``CredentialFailure``."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        status_code=401,
        json={"errorMessages": ["Unauthorized"]},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/myself",
        status_code=401,
        json={"errorMessages": ["Unauthorized"]},
    )
    with pytest.raises(CredentialFailure):
        _make_tracker().fetch_input(42, "issue")


def test_unconfirmed_401_is_infrastructure_class_jira(requests_mock: Any) -> None:
    """An unconfirmed 401 is infrastructure-class ``jira``, never a halt."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        status_code=401,
        json={"errorMessages": ["Unauthorized"]},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/myself",
        status_code=200,
        json=_user(AGENT_ACCOUNT, AGENT_LOGIN),
    )
    with pytest.raises(HostFailure) as exc_info:
        _make_tracker().fetch_input(42, "issue")
    assert exc_info.value.failure_class == "jira"
    assert not isinstance(exc_info.value, CredentialFailure)


def test_permission_refusal_is_caught_as_the_neutral_type(
    requests_mock: Any,
) -> None:
    """A permission-shaped 403 raises neutral ``PermissionRefusal``."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        status_code=403,
        json={"errorMessages": ["Forbidden"]},
    )
    with pytest.raises(PermissionRefusal):
        _make_tracker().fetch_input(42, "issue")
    with pytest.raises(InputError):
        _make_tracker().fetch_input(42, "issue")


def test_rate_limit_exhaustion_is_neutral_jira(requests_mock: Any) -> None:
    """An exhausted 429 raises neutral ``RateLimitExhausted`` (``jira``)."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        status_code=429,
        headers={"Retry-After": "600"},
        json={"errorMessages": ["slow down"]},
    )
    with pytest.raises(RateLimitExhausted) as exc_info:
        _make_tracker().fetch_input(42, "issue")
    assert exc_info.value.failure_class == "jira"
    with pytest.raises(HostFailure):
        _make_tracker().fetch_input(42, "issue")


def test_transient_read_retries_in_place_then_succeeds(requests_mock: Any) -> None:
    """§14.4: a 5xx on a safe read retries in place within the bound."""
    entry = _issue_entry(42)
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        [
            {"status_code": 500, "json": {"errorMessages": ["boom"]}},
            {"status_code": 200, "json": entry},
        ],
    )
    fetched = _make_tracker().fetch_input(42, "issue")
    assert fetched.number == 42
    assert len(requests_mock.request_history) == 2


def test_transient_comment_write_is_flagged_for_marker_lookup(
    requests_mock: Any,
) -> None:
    """§14.4: a 5xx comment write raises a transient neutral ``InputError``."""
    requests_mock.post(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42/comment",
        status_code=500,
        json={"errorMessages": ["boom"]},
    )
    marker = claim_marker_for("implement#42/1")
    with pytest.raises(InputError) as exc_info:
        _make_tracker().post_claim_comment(42, "issue", "a body", marker)
    assert exc_info.value.transient is True
    assert exc_info.value.failure_class == "jira"
    assert _is_transient_perform_error(exc_info.value) is True


def test_failures_never_copy_response_bodies(requests_mock: Any) -> None:
    """Secrets and bodies stay out of exception text (§18)."""
    secret_body = "token-secret-in-body must not leak"
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/issue/PROJ-42",
        status_code=403,
        json={"errorMessages": [secret_body]},
    )
    with pytest.raises(PermissionRefusal) as exc_info:
        _make_tracker().fetch_input(42, "issue")
    assert secret_body not in str(exc_info.value)
    assert "jira-token-secret" not in str(exc_info.value)


def test_fetch_agent_identity_reports_the_display_name(requests_mock: Any) -> None:
    """§18: the probe resolves the agent; the numeric field stays unset."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/myself",
        json={**_user(AGENT_ACCOUNT, AGENT_LOGIN), "emailAddress": AGENT_EMAIL},
    )
    identity = _make_tracker().fetch_agent_identity()
    assert identity.login == AGENT_LOGIN
    assert identity.account_id == 0


def test_production_factory_hides_the_concrete_client_type() -> None:
    """Core startup passes the token; only the adapter names ``JiraClient``."""
    tracker = jira_adapter.make_issue_tracker(
        project=PROJECT,
        base_url=TEST_BASE_URL,
        email=AGENT_EMAIL,
        token="jira-token-secret",
        agent_login=AGENT_LOGIN,
        account_id=AGENT_ACCOUNT,
    )
    assert tracker.target_repo == PROJECT
    assert tracker.agent_login == AGENT_LOGIN
    assert tracker.agent_id is None


def test_startup_checks_resolve_the_account_and_project(
    requests_mock: Any,
) -> None:
    """ADR 0038: one identity probe plus one project read-permission check."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/myself",
        json={**_user(AGENT_ACCOUNT, AGENT_LOGIN), "emailAddress": AGENT_EMAIL},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/project/PROJ",
        json={"key": "PROJ", "name": "Project"},
    )
    result = jira_adapter.run_startup_checks(
        base_url=TEST_BASE_URL,
        email=AGENT_EMAIL,
        token="jira-token-secret",
        project=PROJECT,
    )
    assert result.account_id == AGENT_ACCOUNT
    assert result.display_name == AGENT_LOGIN
    assert result.project_key == PROJECT


def test_startup_checks_refuse_a_bad_credential(requests_mock: Any) -> None:
    """A 401 identity probe is a refused credential, never a green light."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/myself",
        status_code=401,
        json={"errorMessages": ["Unauthorized"]},
    )
    with pytest.raises(TokenRejected):
        jira_adapter.run_startup_checks(
            base_url=TEST_BASE_URL,
            email=AGENT_EMAIL,
            token="jira-token-secret",
            project=PROJECT,
        )


def test_startup_checks_refuse_an_unreadable_project(requests_mock: Any) -> None:
    """A project the credential cannot read fails startup without a write."""
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/myself",
        json={**_user(AGENT_ACCOUNT, AGENT_LOGIN), "emailAddress": AGENT_EMAIL},
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/rest/api/3/project/PROJ",
        status_code=403,
        json={"errorMessages": ["Forbidden"]},
    )
    with pytest.raises(StartupCheckFailed):
        jira_adapter.run_startup_checks(
            base_url=TEST_BASE_URL,
            email=AGENT_EMAIL,
            token="jira-token-secret",
            project=PROJECT,
        )
    assert all(entry.method == "GET" for entry in requests_mock.request_history)
