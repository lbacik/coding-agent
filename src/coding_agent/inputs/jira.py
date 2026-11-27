"""The Jira Cloud adapter of ``IssueTrackerPort``: issues, Claim and Gates.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: candidates are the open issues of the pinned project
carrying its Selection Label; selection writes nothing), §5.1 (the
``labels`` changelog entry for the Selection Label, falling back to the
relevant status-transition actor where labels are automation-set), §6 (the
Claim: the Agent Identity's assignment plus a Claim Comment bearing an
Attempt Marker with ``kind=claim``, read back before any Attempt exists),
§7 (Gate re-reads), §14 (Effect Intent keys travel in Attempt Markers;
lookups filter by the Agent Identity's account and read the full paginated
list — a write by another account carrying our marker is not our receipt)
and §18 (the dedicated-only authorisation rule: a Jira tracker has no
repository-owner identity, so only the dedicated rule applies), plus [ADR
0038](../../../docs/adr/0038-the-issue-tracker-and-the-code-host-are-reached-only-through-their-ports.md)
(one pinned project per configuration, link-by-key delivery with no
transition write, separate tracker/host credentials).

Row ids: INP-1 … INP-14, INP-19, INP-20.

Every request goes through ``request_with_retry`` (§14.4): a 401 confirmed
by the identity probe raises ``CredentialFailure``, an unconfirmed 401 is
infrastructure-class ``jira``, a permission-shaped 403 raises
``PermissionRefusal``, rate limits are retried in place, a transient failure
(a 5xx, a connection error or a timeout) on a safe request is retried in
place, and every other status comes back here to be read by the operation
itself. A write that fails with a 5xx raises a transient-flagged neutral
error, so the effect driver retries it by Attempt-Marker lookup. The
response body is never copied into an exception: only the method, the path
and the status travel outward.

The Attempt Marker travels in two places: the human-readable
``[attempt-marker ...]`` line stays in the comment body for debuggability
(core renders bodies through ``format_marker`` unconditionally, and the
Claim read-back parses it back), while the authoritative copy lives in a
Jira comment property (``attempt-marker``) as JSON. ``find_comment_by_marker``
matches the property value, never a body substring.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from coding_agent.inputs.errors import InputNotFound
from coding_agent.inputs.errors import StepTrackerError as StepTrackerError
from coding_agent.inputs.marker import AttemptMarker
from coding_agent.inputs.numbering import parse_attempt_marker
from coding_agent.inputs.port import (
    AgentFacts,
    InputComment,
    InputType,
    LabeledEvent,
    TrackedInput,
)
from coding_agent.jira.client import JiraClient
from coding_agent.jira.errors import request_with_retry

_PAGE_SIZE = 50
"""One search or comment page (reads paginate)."""

MARKER_PROPERTY_KEY = "attempt-marker"
"""The comment-property key carrying the authoritative Attempt Marker."""

_CANDIDATE_FIELDS = "summary,description,status,labels,assignee,created,creator,reporter"
"""The minimal issue fields a candidate or input read needs."""

_STATUS_DONE = "done"
"""The status-category key that closes an issue (``statusCategory.key``)."""


# ``StepTrackerError`` is canonical in ``coding_agent.inputs.errors`` and
# re-exported here for one release, so ``from coding_agent.inputs.jira
# import StepTrackerError`` keeps resolving to the same class object.


def _write_error(method: str, path: str, response: requests.Response) -> StepTrackerError:
    """Build the write failure for one non-2xx response (§14.4).

    A 5xx marks the error transient: the write may have landed, so the
    effect driver re-looks it up by Attempt Marker before repeating it.
    Any other status is terminal for the write. The response body is never
    copied: only the method, the path and the status travel outward.
    """
    return StepTrackerError(
        f"{method} {path} returned {response.status_code}",
        transient=500 <= response.status_code <= 599,
        method=method,
        path=path,
        status_code=response.status_code,
        failure_class="jira",
    )


def _read_error(method: str, path: str, response: requests.Response) -> StepTrackerError:
    """Build the read failure for one unexpected non-2xx response (§14.4)."""
    return StepTrackerError(
        f"{method} {path} returned {response.status_code}",
        method=method,
        path=path,
        status_code=response.status_code,
        failure_class="jira",
    )


def format_jira_marker(marker: AttemptMarker) -> str:
    """Render an Attempt Marker to its short human-readable line (§14.1).

    The single marker construction site for this adapter:
    ``[attempt-marker attempt=… kind=… step=… n=… ordinal=…]`` with the
    empty-step placeholder convention (``step=-``). Kept free of the
    GitHub HTML comment fence on purpose: the property carries the
    authoritative value and this line is only for debuggability. The
    ``attempt=<workflow>#<N>/<n>`` fragment stays parseable, so the Claim
    read-back keeps working on the body text.
    """
    return (
        f"[attempt-marker attempt={marker.attempt} kind={marker.kind} "
        f"step={marker.step or '-'} n={marker.execution_number} "
        f"ordinal={marker.ordinal}]"
    )


def marker_property_value(marker: AttemptMarker) -> dict[str, object]:
    """The authoritative Attempt Marker value stored in the comment property."""
    return {
        "attempt": marker.attempt,
        "kind": marker.kind,
        "step": marker.step,
        "execution_number": marker.execution_number,
        "ordinal": marker.ordinal,
    }


def _marker_matches(value: Any, marker: AttemptMarker) -> bool:
    """Whether a comment-property value is exactly ``marker``."""
    return isinstance(value, dict) and dict(value) == marker_property_value(marker)


def jira_issue_tracker(
    *,
    project: str,
    base_url: str,
    email: str | None,
    token: str,
    agent_login: str,
    account_id: str,
) -> JiraTracker:
    """Build the production Jira tracker, owning its transport (§5, §14, §18).

    The worker startup port owns the transport: callers pass only the
    credential, the site and the pinned project coordinates, never a
    client. ``token`` arrives from the tracker's configured ``token-env``
    secret; ``account_id`` is the agent's opaque Jira ``accountId``.
    """
    return JiraTracker(
        project=project,
        client=JiraClient(token, base_url=base_url, email=email),
        agent_login=agent_login,
        account_id=account_id,
    )


def make_issue_tracker(
    *,
    project: str,
    base_url: str,
    email: str | None,
    token: str,
    agent_login: str,
    account_id: str,
) -> JiraTracker:
    """Build the production Jira tracker for the Agent Identity (§5, §14, §18).

    The adapter owns the concrete client: core startup and the port
    factory pass the token, never the client type. No configuration can
    select a fake: production always builds this one.
    """
    return jira_issue_tracker(
        project=project,
        base_url=base_url,
        email=email,
        token=token,
        agent_login=agent_login,
        account_id=account_id,
    )


def _quote_jql_string(value: str) -> str:
    """Quote one JQL string literal (backslashes and single quotes escaped)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _adf_body(text: str) -> dict[str, Any]:
    """Render comment text as a Jira document (Atlassian Document Format).

    Blank lines split paragraphs; single newlines become hard breaks, so
    :func:`_adf_to_text` inverts this exactly.
    """
    paragraphs: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        children: list[dict[str, Any]] = []
        for index, line in enumerate(block.split("\n")):
            if index:
                children.append({"type": "hardBreak"})
            children.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": children})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _adf_inline_to_text(node: Any) -> str:
    """Extract the text of one ADF inline node, recursing into containers."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_inline_to_text(entry) for entry in node)
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            text = attrs.get("text")
            if isinstance(text, str) and text:
                return text
        return ""
    content = node.get("content")
    inner = _adf_inline_to_text(content) if content is not None else ""
    if node_type == "listItem":
        return inner + "\n"
    return inner


def _adf_to_text(body: Any) -> str:
    """Read a Jira description or comment body as plain text.

    Plain strings (as recorded fixtures use) pass through; ADF documents
    reduce to their text with paragraphs joined by blank lines. Anything
    else reads as empty.
    """
    if isinstance(body, str):
        return body
    if body is None:
        return ""
    if not isinstance(body, dict):
        return ""
    blocks: list[str] = []
    for block in body.get("content") or []:
        if isinstance(block, dict):
            blocks.append(_adf_inline_to_text(block))
    return "\n\n".join(text for text in blocks if text != "").strip()


def _as_str(value: Any) -> str:
    """Read an optional JSON string field, defaulting to empty."""
    return value if isinstance(value, str) else ""


def _status_category_key(status: Any) -> str:
    """The issue status's category key (``new``/``indeterminate``/``done``)."""
    if not isinstance(status, dict):
        return ""
    category = status.get("statusCategory")
    if not isinstance(category, dict):
        return ""
    key = category.get("key")
    return key.lower() if isinstance(key, str) else ""


def _label_set(value: Any) -> set[str]:
    """Split a Jira changelog ``fromString``/``toString`` into a label set."""
    if not isinstance(value, str) or not value.strip():
        return set()
    return {entry.strip() for entry in value.split(",") if entry.strip()}


def _account_id(user: Any) -> str:
    """The opaque Jira ``accountId`` of a user object, or empty."""
    if not isinstance(user, dict):
        return ""
    account = user.get("accountId")
    return account if isinstance(account, str) else ""


def _display_name(user: Any, fallback: str = "") -> str:
    """The display name of a Jira user object, falling back as told."""
    if not isinstance(user, dict):
        return fallback
    name = user.get("displayName")
    if isinstance(name, str) and name:
        return name
    return fallback


@dataclass(frozen=True)
class JiraStartupResult:
    """What the Jira startup checks resolve (§18, ADR 0038).

    The identity probe resolves the agent's opaque ``accountId`` and
    display name; the project check proves read permission on the one
    pinned project. There is no push-permission or rate-headroom read:
    the tracker holds no repository and Jira sends no GitHub-style
    rate headers.
    """

    account_id: str
    display_name: str
    project_key: str


@dataclass(frozen=True)
class JiraTracker:
    """An ``IssueTrackerPort`` over the Jira Cloud REST API.

    ``project`` is the one pinned project key (``PROJ``); ``client``
    carries the Agent Identity's credential; ``agent_login`` is the
    agent's display name (or email); ``account_id`` is the agent's opaque
    Jira ``accountId``, which every receipt lookup filters by. This object
    serves the ``inputs`` port only: pull-request, review, base-tip and
    host-profile operations stay GitHub-only, so a Jira tracker is always
    paired with a separate code host.
    """

    project: str
    client: JiraClient
    agent_login: str
    account_id: str
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    _comment_issues: dict[int, str] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def target_repo(self) -> str:
        """The pinned project key: a tracker holds no repository (§2.1)."""
        return self.project

    @property
    def agent_id(self) -> int | None:
        """No numeric id: Jira identity is the opaque ``accountId`` string."""
        return None

    def delivery_reference(self, ref: str, *, draft: bool = False) -> str:
        """The pull-request-to-issue link line for issue ``ref`` (§10.5).

        Link-by-key only: the Jira issue key (``PROJ-42``), whether or not
        the pull request is a draft. Delivery never transitions tracker
        state (ADR 0038); ``draft`` is accepted and ignored. Pure: no HTTP.
        """
        _ = draft
        if ref.startswith(f"{self.project}-"):
            return ref
        return f"{self.project}-{ref}"

    def format_marker(self, marker: AttemptMarker) -> str:
        """Render an Attempt Marker to its short human-readable line (§14.1)."""
        return format_jira_marker(marker)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One §14.4-classified call; statuses outside 401/403/429 are returned."""
        return request_with_retry(
            self.client,
            method,
            path,
            sleep=self.sleep,
            now=self.now,
            raise_other=False,
            **kwargs,
        )

    def _issue_path(self, number: int) -> str:
        return f"/rest/api/3/issue/{self.project}-{number}"

    def _remember_comment_issue(self, comment_id: int, key: str) -> None:
        """Record which issue a comment lives on for ``delete_comment``."""
        self._comment_issues[comment_id] = key

    def fetch_agent_identity(self) -> AgentFacts:
        """Resolve the Agent Identity: display name; no numeric id (§18).

        Jira's ``accountId`` is an opaque string, so the port's numeric
        field carries ``0`` and must never be compared: authorisation uses
        the dedicated-only rule (ADR 0038) and marker lookups filter by
        ``accountId``.
        """
        response = self._request("GET", "/rest/api/3/myself")
        if response.status_code != 200:
            raise _read_error("GET", "/rest/api/3/myself", response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                "GET /rest/api/3/myself returned an unexpected body",
                method="GET",
                path="/rest/api/3/myself",
                status_code=200,
                failure_class="jira",
            )
        login = _display_name(
            payload, _as_str(payload.get("emailAddress")) or self.agent_login
        )
        return AgentFacts(login=login or self.agent_login, account_id=0)

    def fetch_repository_owner_id(self) -> int:
        """Unimplemented: a Jira tracker has no repository owner (ADR 0038).

        Callers apply the dedicated-only rule without detection instead of
        calling this.
        """
        raise NotImplementedError(
            "the Jira tracker has no repository-owner identity: "
            "apply the dedicated-only rule (ADR 0038)"
        )

    @staticmethod
    def _input_from_issue_payload(
        number: int, payload: dict[str, Any]
    ) -> TrackedInput:
        """Report one issue's guard and Gate facts from its JSON."""
        fields = payload.get("fields")
        fields_map = fields if isinstance(fields, dict) else {}
        status = fields_map.get("status")
        labels = fields_map.get("labels") or []
        assignee = fields_map.get("assignee")
        creator = fields_map.get("creator")
        reporter = fields_map.get("reporter")
        author_source = (
            creator
            if isinstance(creator, dict)
            else (reporter if isinstance(reporter, dict) else None)
        )
        assignee_id = _account_id(assignee)
        return TrackedInput(
            number=number,
            input_type="issue",
            title=_as_str(fields_map.get("summary")),
            body=_adf_to_text(fields_map.get("description")),
            is_open=_status_category_key(status) != _STATUS_DONE,
            labels=tuple(
                entry for entry in labels if isinstance(entry, str) and entry
            ),
            assignees=(assignee_id,) if assignee_id else (),
            created_at=_as_str(fields_map.get("created")),
            author_login=_display_name(author_source),
            author_id=None,
        )

    def fetch_input(self, number: int, input_type: InputType) -> TrackedInput:
        """Read one issue with every guard and Gate fact (§5, §7)."""
        if input_type != "issue":
            raise StepTrackerError(
                f"the Jira tracker serves issues only, not {input_type}",
                method="GET",
                path=self._issue_path(number),
                failure_class="jira",
            )
        if number < 1:
            raise StepTrackerError(
                f"invalid Jira issue number: {number}",
                method="GET",
                path=self._issue_path(number),
                failure_class="jira",
            )
        response = self._request(
            "GET", self._issue_path(number), params={"fields": _CANDIDATE_FIELDS}
        )
        if response.status_code == 404:
            raise InputNotFound("GET", self._issue_path(number))
        if response.status_code != 200:
            raise _read_error("GET", self._issue_path(number), response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned an unexpected body",
                method="GET",
                path=self._issue_path(number),
                status_code=200,
                failure_class="jira",
            )
        return self._input_from_issue_payload(number, payload)

    def _search_issues(self, jql: str, fields: str) -> list[dict[str, Any]]:
        """One paginated JQL sweep, oldest first (§5.2)."""
        found: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self._request(
                "GET",
                "/rest/api/3/search",
                params={
                    "jql": jql,
                    "fields": fields,
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                },
            )
            if response.status_code != 200:
                raise _read_error("GET", "/rest/api/3/search", response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise StepTrackerError(
                    "GET /rest/api/3/search returned an unexpected body",
                    method="GET",
                    path="/rest/api/3/search",
                    status_code=200,
                    failure_class="jira",
                )
            batch = payload.get("issues") or []
            if not isinstance(batch, list):
                raise StepTrackerError(
                    "GET /rest/api/3/search returned an unexpected body",
                    method="GET",
                    path="/rest/api/3/search",
                    status_code=200,
                    failure_class="jira",
                )
            for entry in batch:
                if isinstance(entry, dict):
                    found.append(entry)
            total = payload.get("total")
            start_at += len(batch)
            if len(batch) < _PAGE_SIZE:
                break
            if isinstance(total, int) and start_at >= total:
                break
        return found

    @staticmethod
    def _issue_number(key: Any) -> int | None:
        """The integer part of an issue key (``PROJ-42`` → ``42``)."""
        if not isinstance(key, str) or "-" not in key:
            return None
        try:
            return int(key.rsplit("-", 1)[-1])
        except ValueError:
            return None

    def list_issue_candidates(self, selection_label: str) -> list[TrackedInput]:
        """List the open project issues carrying ``selection_label`` (§5.2).

        Searched by JQL within the pinned project; the assignee is read
        per issue from its ``accountId`` field, never as a JQL filter.
        Read-only: selection writes nothing.
        """
        jql = (
            f"project = {self.project} AND labels = "
            f"{_quote_jql_string(selection_label)} "
            f"AND statusCategory != Done ORDER BY created ASC"
        )
        candidates: list[TrackedInput] = []
        for entry in self._search_issues(jql, _CANDIDATE_FIELDS):
            number = self._issue_number(entry.get("key"))
            if number is None:
                continue
            candidates.append(self._input_from_issue_payload(number, entry))
        candidates.sort(key=lambda candidate: (candidate.created_at, candidate.number))
        return candidates

    def list_pull_request_candidates(self, selection_label: str) -> list[TrackedInput]:
        """No pull requests live on a Jira tracker: always empty (§5.2).

        Read-only: selection writes nothing.
        """
        _ = selection_label
        return []

    def read_labeled_event(
        self, number: int, input_type: InputType, label: str
    ) -> LabeledEvent | None:
        """Read the latest ``labels``-add event for ``label`` (§5.1).

        Scans the issue changelog oldest-to-newest for entries adding the
        Selection Label; the latest wins. Falls back to the latest
        status-transition actor only where no labels entry exists (labels
        set by automation). ``None`` where neither exists: without an
        event there is no authorisation to test, so the Claim is refused.
        """
        _ = input_type
        response = self._request(
            "GET",
            self._issue_path(number),
            params={"fields": "labels", "expand": "changelog"},
        )
        if response.status_code == 404:
            raise InputNotFound("GET", self._issue_path(number))
        if response.status_code != 200:
            raise _read_error("GET", self._issue_path(number), response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned an unexpected body",
                method="GET",
                path=self._issue_path(number),
                status_code=200,
                failure_class="jira",
            )
        changelog = payload.get("changelog")
        histories = (
            changelog.get("histories")
            if isinstance(changelog, dict)
            else None
        )
        if not isinstance(histories, list):
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned no changelog",
                method="GET",
                path=self._issue_path(number),
                status_code=200,
                failure_class="jira",
            )
        latest_labels: tuple[str, dict[str, Any]] | None = None
        latest_status: tuple[str, dict[str, Any]] | None = None
        for history in histories:
            if not isinstance(history, dict):
                continue
            created = history.get("created")
            created_at = created if isinstance(created, str) else ""
            author = history.get("author")
            author_map = author if isinstance(author, dict) else {}
            items = history.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("field") == "labels":
                    added = _label_set(item.get("toString")) - _label_set(
                        item.get("fromString")
                    )
                    if label in added and (
                        latest_labels is None or created_at >= latest_labels[0]
                    ):
                        latest_labels = (created_at, author_map)
                elif item.get("field") == "status" and (
                    latest_status is None or created_at >= latest_status[0]
                ):
                    latest_status = (created_at, author_map)
        chosen = latest_labels if latest_labels is not None else latest_status
        if chosen is None:
            return None
        created_at, author_map = chosen
        account_type = author_map.get("accountType")
        return LabeledEvent(
            label=label,
            actor_login=_display_name(author_map, _account_id(author_map)),
            actor_id=None,
            actor_type=(
                "Bot" if isinstance(account_type, str) and account_type == "app" else "User"
            ),
            created_at=created_at,
        )

    @staticmethod
    def _linked_issue_open(linked: Any) -> bool:
        """Whether a linked issue counts as open: not Done, unknown counts open."""
        if not isinstance(linked, dict):
            return True
        fields = linked.get("fields")
        if not isinstance(fields, dict):
            return True
        return _status_category_key(fields.get("status")) != _STATUS_DONE

    def has_open_blocker(self, number: int, input_type: InputType) -> bool:
        """Whether the issue has an open native blocker (§5.2).

        True only where an ``is blocked by`` link (either direction, with
        this issue on the blocked side) points at a linked issue whose
        status category is not Done. Closed blockers do not block.
        """
        _ = input_type
        response = self._request(
            "GET",
            self._issue_path(number),
            params={"fields": "issuelinks,status"},
        )
        if response.status_code == 404:
            raise InputNotFound("GET", self._issue_path(number))
        if response.status_code != 200:
            raise _read_error("GET", self._issue_path(number), response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned an unexpected body",
                method="GET",
                path=self._issue_path(number),
                status_code=200,
                failure_class="jira",
            )
        fields = payload.get("fields")
        fields_map = fields if isinstance(fields, dict) else {}
        links = fields_map.get("issuelinks") or []
        if not isinstance(links, list):
            return False
        for link in links:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type")
            type_map = link_type if isinstance(link_type, dict) else {}
            inward_issue = link.get("inwardIssue")
            outward_issue = link.get("outwardIssue")
            blocked: Any = None
            if isinstance(inward_issue, dict) and type_map.get("inward") == "is blocked by":
                blocked = inward_issue
            elif (
                isinstance(outward_issue, dict)
                and type_map.get("outward") == "is blocked by"
            ):
                blocked = outward_issue
            if blocked is not None and self._linked_issue_open(blocked):
                return True
        return False

    def assign_input(self, number: int, input_type: InputType) -> None:
        """Assign the Agent Identity: the Claim's first write (§6)."""
        _ = input_type
        path = f"{self._issue_path(number)}/assignee"
        response = self._request(
            "PUT", path, json={"accountId": self.account_id}
        )
        if response.status_code not in (200, 204):
            raise _write_error("PUT", path, response)

    def unassign_input(self, number: int, input_type: InputType) -> None:
        """Remove the Agent Identity's assignment, only the Worker's own (§6)."""
        _ = input_type
        path = f"{self._issue_path(number)}/assignee"
        response = self._request("PUT", path, json={"accountId": None})
        if response.status_code not in (200, 204):
            raise _write_error("PUT", path, response)

    def _comment_from_payload(
        self, key: str, payload: dict[str, Any]
    ) -> tuple[InputComment, str]:
        """Read one listed comment with its author's ``accountId``.

        Returns the port comment plus the opaque author account id the
        marker lookup filters by.
        """
        raw_id = payload.get("id")
        try:
            if isinstance(raw_id, int):
                comment_id = raw_id
            elif isinstance(raw_id, str):
                comment_id = int(raw_id)
            else:
                raise TypeError(f"unexpected comment id: {raw_id!r}")
        except (TypeError, ValueError):
            raise StepTrackerError(
                f"comment on {key} returned an unexpected id",
                method="GET",
                path=f"/rest/api/3/issue/{key}/comment",
                status_code=200,
                failure_class="jira",
            ) from None
        author = payload.get("author")
        author_map = author if isinstance(author, dict) else {}
        text = _adf_to_text(payload.get("body"))
        created = payload.get("created")
        updated = payload.get("updated")
        comment = InputComment(
            comment_id=comment_id,
            body=text,
            author_login=_display_name(author_map),
            author_id=None,
            marker_present=parse_attempt_marker(text) is not None,
            created_at=created if isinstance(created, str) else "",
            updated_at=updated if isinstance(updated, str) else None,
        )
        self._remember_comment_issue(comment_id, key)
        return comment, _account_id(author_map)

    def _list_raw_comments(
        self, number: int, input_type: InputType
    ) -> list[tuple[InputComment, str]]:
        """List the issue's comments, oldest first, with author account ids."""
        _ = input_type
        key = f"{self.project}-{number}"
        found: list[tuple[InputComment, str]] = []
        start_at = 0
        while True:
            response = self._request(
                "GET",
                f"/rest/api/3/issue/{key}/comment",
                params={"startAt": start_at, "maxResults": _PAGE_SIZE},
            )
            if response.status_code == 404:
                raise InputNotFound("GET", f"/rest/api/3/issue/{key}/comment")
            if response.status_code != 200:
                raise _read_error(
                    "GET", f"/rest/api/3/issue/{key}/comment", response
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise StepTrackerError(
                    f"GET /rest/api/3/issue/{key}/comment returned "
                    "an unexpected body",
                    method="GET",
                    path=f"/rest/api/3/issue/{key}/comment",
                    status_code=200,
                    failure_class="jira",
                )
            batch = payload.get("comments") or []
            if not isinstance(batch, list):
                raise StepTrackerError(
                    f"GET /rest/api/3/issue/{key}/comment returned "
                    "an unexpected body",
                    method="GET",
                    path=f"/rest/api/3/issue/{key}/comment",
                    status_code=200,
                    failure_class="jira",
                )
            for entry in batch:
                if isinstance(entry, dict):
                    found.append(self._comment_from_payload(key, entry))
            total = payload.get("total")
            start_at += len(batch)
            if len(batch) < _PAGE_SIZE:
                break
            if isinstance(total, int) and start_at >= total:
                break
        found.sort(key=lambda pair: (pair[0].created_at, pair[0].comment_id))
        return found

    def list_comments(self, number: int, input_type: InputType) -> list[InputComment]:
        """List the issue's comments, oldest first (§6.1, §14)."""
        return [
            comment for comment, _ in self._list_raw_comments(number, input_type)
        ]

    def _read_marker_property(self, comment_id: int) -> dict[str, Any] | None:
        """Read one comment's authoritative Attempt Marker property.

        ``None`` where the property is absent; anything else unexpected
        raises.
        """
        path = f"/rest/api/3/comment/{comment_id}/properties/{MARKER_PROPERTY_KEY}"
        response = self._request("GET", path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _read_error("GET", path, response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"GET {path} returned an unexpected body",
                method="GET",
                path=path,
                status_code=200,
                failure_class="jira",
            )
        value = payload.get("value")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise StepTrackerError(
                f"GET {path} returned an unexpected body",
                method="GET",
                path=path,
                status_code=200,
                failure_class="jira",
            )
        return value

    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: AttemptMarker
    ) -> InputComment | None:
        """Find our comment by Attempt Marker (§14).

        Filters by the Agent Identity's ``accountId`` first, then matches
        the comment property value. The property is authoritative: a body
        carrying the marker text from another account is never ours.
        """
        for comment, author_account_id in self._list_raw_comments(number, input_type):
            if author_account_id != self.account_id:
                continue
            value = self._read_marker_property(comment.comment_id)
            if value is None:
                continue
            if _marker_matches(value, marker):
                return comment
        return None

    def delete_comment(self, comment_id: int) -> None:
        """Delete one of the Worker's own comments (§6.2, §16.2).

        Naturally idempotent: a comment already absent is the successful
        no-op, not a failure.
        """
        key = self._comment_issues.get(comment_id)
        if key is None:
            raise StepTrackerError(
                f"cannot delete comment {comment_id}: "
                "its issue was never listed or posted here",
                method="DELETE",
                path=f"/rest/api/3/issue/{self.project}-?/comment/{comment_id}",
                failure_class="jira",
            )
        path = f"/rest/api/3/issue/{key}/comment/{comment_id}"
        response = self._request("DELETE", path)
        if response.status_code == 404:
            return
        if response.status_code not in (200, 204):
            raise _write_error("DELETE", path, response)

    def _post_comment_with_marker(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post one comment and store its authoritative marker property."""
        _ = input_type
        key = f"{self.project}-{number}"
        path = f"/rest/api/3/issue/{key}/comment"
        response = self._request("POST", path, json={"body": _adf_body(body)})
        if response.status_code != 201:
            raise _write_error("POST", path, response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"POST {path} returned an unexpected body",
                method="POST",
                path=path,
                status_code=201,
                failure_class="jira",
            )
        comment, _ = self._comment_from_payload(key, payload)
        comment = InputComment(
            comment_id=comment.comment_id,
            body=body,
            author_login=self.agent_login,
            author_id=None,
            marker_present=True,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
        )
        property_path = (
            f"/rest/api/3/comment/{comment.comment_id}"
            f"/properties/{MARKER_PROPERTY_KEY}"
        )
        property_response = self._request(
            "PUT", property_path, json=marker_property_value(marker)
        )
        if property_response.status_code not in (200, 201, 204):
            raise _write_error("PUT", property_path, property_response)
        return comment

    def post_claim_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post the Claim Comment bearing the Attempt Marker (§6.1)."""
        return self._post_comment_with_marker(number, input_type, body, marker)

    def post_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post one comment on the issue, body already rendered (§10.7)."""
        return self._post_comment_with_marker(number, input_type, body, marker)

    def _current_labels(self, number: int) -> list[str]:
        """Read the issue's current labels for idempotent label writes."""
        response = self._request(
            "GET", self._issue_path(number), params={"fields": "labels"}
        )
        if response.status_code == 404:
            raise InputNotFound("GET", self._issue_path(number))
        if response.status_code != 200:
            raise _read_error("GET", self._issue_path(number), response)
        payload = response.json()
        fields = payload.get("fields") if isinstance(payload, dict) else None
        fields_map = fields if isinstance(fields, dict) else {}
        labels = fields_map.get("labels") or []
        return [entry for entry in labels if isinstance(entry, str) and entry]

    def remove_label(self, number: int, input_type: InputType, label: str) -> None:
        """Remove one label from the issue (§15.3).

        Idempotent: a label already absent is the successful no-op, not a
        failure. The Worker never applies a Selection Label (§18): the
        runtime filters it before this is called.
        """
        _ = input_type
        if label not in self._current_labels(number):
            return
        path = self._issue_path(number)
        response = self._request(
            "PUT", path, json={"update": {"labels": [{"remove": label}]}}
        )
        if response.status_code == 404:
            return
        if response.status_code not in (200, 204):
            raise _write_error("PUT", path, response)

    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None:
        """Apply end labels declared for the Terminal Outcome (§15.3)."""
        _ = input_type
        if not labels:
            return
        path = self._issue_path(number)
        response = self._request(
            "PUT",
            path,
            json={"update": {"labels": [{"add": entry} for entry in labels]}},
        )
        if response.status_code not in (200, 204):
            raise _write_error("PUT", path, response)


def run_startup_checks(
    *,
    base_url: str,
    email: str | None,
    token: str,
    project: str,
) -> JiraStartupResult:
    """Run every non-mutating check a Jira tracker needs (§18, ADR 0038).

    Adapter-owned: the identity probe (``GET /rest/api/3/myself``,
    resolving the agent's opaque ``accountId`` plus display name) and the
    project read-permission check (``GET /rest/api/3/project/{key}``).
    A 401 from the identity probe means the credential is refused and
    raises :class:`TokenRejected`; anything else failing raises
    :class:`StartupCheckFailed` — never a Jira type core must name.
    """
    from coding_agent.identity.startup import StartupCheckFailed
    from coding_agent.identity.token import TokenRejected

    client = JiraClient(token, base_url=base_url, email=email)
    myself = client.get("/rest/api/3/myself")
    if myself.status_code == 401:
        raise TokenRejected(
            "GET /rest/api/3/myself returned 401: the tracker credential is refused"
        )
    if myself.status_code != 200:
        raise StartupCheckFailed(
            f"GET /rest/api/3/myself returned {myself.status_code}"
        )
    try:
        payload = myself.json()
    except ValueError as error:
        raise StartupCheckFailed(
            "GET /rest/api/3/myself returned an unexpected body"
        ) from error
    if not isinstance(payload, dict):
        raise StartupCheckFailed(
            "GET /rest/api/3/myself returned an unexpected body"
        )
    account_id = _account_id(payload)
    display_name = _display_name(payload, _as_str(payload.get("emailAddress")))
    if not account_id:
        raise StartupCheckFailed(
            "GET /rest/api/3/myself returned no accountId"
        )
    project_path = f"/rest/api/3/project/{project}"
    probe = client.get(project_path)
    if probe.status_code == 401:
        raise TokenRejected(
            f"GET {project_path} returned 401: the tracker credential is refused"
        )
    if probe.status_code != 200:
        raise StartupCheckFailed(f"GET {project_path} returned {probe.status_code}")
    return JiraStartupResult(
        account_id=account_id,
        display_name=display_name or account_id,
        project_key=project,
    )


def make_probe_transport(
    *, base_url: str, email: str | None, token: str
) -> JiraClient:
    """Build the narrow HTTP transport the tracker startup checks need.

    The adapter owns the concrete client: the checks program against a
    structural HTTP surface and never name the client type.
    """
    return JiraClient(token, base_url=base_url, email=email)


__all__ = [
    "JiraStartupResult",
    "JiraTracker",
    "MARKER_PROPERTY_KEY",
    "StepTrackerError",
    "format_jira_marker",
    "jira_issue_tracker",
    "make_issue_tracker",
    "make_probe_transport",
    "marker_property_value",
    "run_startup_checks",
]
