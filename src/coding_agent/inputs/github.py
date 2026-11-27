"""The GitHub adapter of ``InputPort`` and ``CodeHostPort``: inputs, Claim and Gates.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: candidates are the open inputs of the Workflow's
type carrying its Selection Label; selection writes nothing), §5.1 (the
latest ``labeled`` event for the Selection Label, tested under the detected
identity mode), §6 (the Claim: the Agent Identity's assignment plus a Claim
Comment bearing an Attempt Marker with ``kind=claim``; among Claim Comments
in force the lowest comment id wins), §7 (Gate re-reads; the base-branch
tip), §14 (Effect Intent keys travel in Attempt Markers; lookups filter by
the Agent Identity's numeric id and read the full paginated list — a write
by another account carrying our marker is not our receipt) and §18 (the two
structural abstentions: no operation applies the Selection Label and none
edits an input's title or body; the numeric account id is the identity;
startup is non-mutating).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.

Every request goes through ``request_with_retry`` (§14.4): a 401 confirmed
by the identity probe raises ``CredentialFailure``, an unconfirmed 401 is
infrastructure-class ``github``, a permission-shaped 403 raises
``PermissionRefusal``, rate limits are retried in place, a transient failure
(a 5xx, a connection error or a timeout) on a safe request is retried in
place, and every other status comes back here to be read by the operation
itself. A write that fails with a 5xx raises ``StepTrackerError`` marked
transient, so the effect driver retries it by Attempt-Marker lookup.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests

from coding_agent.github.client import DEFAULT_BASE_URL, GitHubClient
from coding_agent.github.errors import request_with_retry
from coding_agent.inputs.errors import InputError, InputNotFound
from coding_agent.inputs.errors import StepTrackerError as StepTrackerError
from coding_agent.inputs.marker import AttemptMarker
from coding_agent.inputs.port import (
    AgentFacts,
    CodeHostPort,
    InputComment,
    InputPort,
    InputPullRequest,
    InputReview,
    InputType,
    IssueTrackerPort,
    LabeledEvent,
    TrackedInput,
)
from coding_agent.inputs.profile import GITHUB_PROFILE, CodeHostProfile

if TYPE_CHECKING:
    from coding_agent.identity.startup import StartupCheckResult

_PAGE_SIZE = 100
"""One candidate, comment or pull-request page (reads paginate)."""


# ``StepTrackerError`` is canonical in ``coding_agent.inputs.errors`` and
# re-exported here for one release, so ``from coding_agent.inputs.github
# import StepTrackerError`` keeps resolving to the same class object.


def _write_error(method: str, path: str, response: requests.Response) -> StepTrackerError:
    """Build the write failure for one non-2xx response (§14.4).

    A 5xx marks the error transient: the write may have landed, so the
    effect driver re-looks it up by Attempt Marker before repeating it.
    Any other status is terminal for the write.
    """
    status_code = response.status_code
    return StepTrackerError(
        f"{method} {path} returned {status_code}: {response.text}",
        transient=500 <= status_code <= 599,
        method=method,
        path=path,
        status_code=status_code,
    )


def format_attempt_marker(marker: AttemptMarker) -> str:
    """Render an Attempt Marker to today's HTML comment line (§14.1, §14.4).

    The single marker construction site: ``<!-- coding-agent attempt=…
    kind=… step=… n=… ordinal=… -->`` with the empty-step placeholder
    convention (``step=-``). Byte-identical to every receipt written before
    the structured marker, so recovery keeps matching.
    """
    return (
        f"<!-- coding-agent attempt={marker.attempt} kind={marker.kind} "
        f"step={marker.step or '-'} n={marker.execution_number} "
        f"ordinal={marker.ordinal} -->"
    )


def github_step_tracker(
    *,
    owner: str,
    repo: str,
    token: str,
    agent_login: str,
    agent_id: int | None = None,
) -> GitHubStepTracker:
    """Build the production step tracker, owning its transport (§5, §14, §18).

    The worker startup port owns the transport: callers pass only the
    credential and the Target Repository coordinates, never a client.
    """
    return GitHubStepTracker(
        owner=owner,
        repo=repo,
        client=GitHubClient(token),
        agent_login=agent_login,
        agent_id=agent_id,
    )


def fetch_repository_file(
    *,
    token: str,
    owner: str,
    repo: str,
    rev: str,
    path: str,
) -> requests.Response:
    """GET one repository file at a revision; statuses come back to read.

    The worker startup port owns the transport: callers pass only the
    credential and the Target Repository coordinates, never a client.
    """
    return GitHubClient(token).get(f"/repos/{owner}/{repo}/contents/{path}?ref={rev}")


@dataclass(frozen=True)
class GitHubStepTracker:
    """A ``InputPort`` and ``CodeHostPort`` over the GitHub REST API.

    ``owner`` and ``repo`` name the Target Repository; ``client`` carries
    the Agent Identity's token. ``agent_login`` is the only login assigned
    or unassigned. ``agent_id`` is the Agent Identity's numeric account id,
    which every receipt lookup filters by (§14); where it is ``None`` the
    login decides instead. One object serves the ``inputs`` and the
    ``code_host`` ports: ``pull_requests`` returns ``self``.
    """

    owner: str
    repo: str
    client: GitHubClient
    agent_login: str
    agent_id: int | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    profile: CodeHostProfile = GITHUB_PROFILE

    @property
    def target_repo(self) -> str:
        """The Target Repository, ``owner/name`` (§2.1)."""
        return f"{self.owner}/{self.repo}"

    @property
    def pull_requests(self) -> InputPort:
        """The pull-request facet: pull-request Workflows claim through it.

        Issues and pull requests share the GitHub issues API, so the facet
        is this tracker itself.
        """
        return self

    def delivery_reference(self, ref: str, *, draft: bool = False) -> str:
        """The pull-request-to-issue link line for issue ``ref`` (§10.5).

        Composed through this host's :class:`CodeHostProfile`: core passes
        the semantic reference and never formats vendor text itself.
        """
        return self.profile.delivery_reference(ref, draft=draft)

    def format_marker(self, marker: AttemptMarker) -> str:
        """Render an Attempt Marker to today's HTML comment line (§14.1)."""
        return format_attempt_marker(marker)

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
        return f"/repos/{self.owner}/{self.repo}/issues/{number}"

    def fetch_agent_identity(self) -> AgentFacts:
        """Resolve the Agent Identity: login and numeric id (§18)."""
        response = self._request("GET", "/user")
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET /user returned {response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError("GET /user returned an unexpected body")
        try:
            return AgentFacts(
                login=str(payload["login"]), account_id=int(payload["id"])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise StepTrackerError(
                f"GET /user returned an unexpected body: {error}"
            ) from error

    def fetch_repository_owner_id(self) -> int:
        """Resolve the Target Repository owner's numeric id (§18)."""
        response = self._request("GET", f"/repos/{self.owner}/{self.repo}")
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        owner = payload.get("owner") if isinstance(payload, dict) else None
        owner_id = owner.get("id") if isinstance(owner, dict) else None
        if not isinstance(owner_id, int):
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo} returned no owner id"
            )
        return owner_id

    @staticmethod
    def _input_from_issue_payload(
        payload: dict[str, Any], input_type: InputType
    ) -> TrackedInput:
        """Report one input's guard and Gate facts from its issue JSON."""
        raw_assignees = payload.get("assignees") or []
        raw_labels = payload.get("labels") or []
        user = payload.get("user") or {}
        return TrackedInput(
            number=int(payload["number"]),
            input_type=input_type,
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            is_open=payload.get("state") == "open",
            labels=tuple(
                str(entry.get("name"))
                for entry in raw_labels
                if isinstance(entry, dict) and entry.get("name")
            ),
            assignees=tuple(
                str(entry.get("login"))
                for entry in raw_assignees
                if isinstance(entry, dict) and entry.get("login")
            ),
            created_at=str(payload.get("created_at") or ""),
            author_login=str(user.get("login", "")),
            author_id=int(user["id"]) if isinstance(user.get("id"), int) else None,
        )

    @staticmethod
    def _apply_pull_payload(
        candidate: TrackedInput, payload: dict[str, Any], target_repo: str
    ) -> TrackedInput:
        """Enrich a candidate with its pull-request facts (§5.2, §7.2)."""
        from dataclasses import replace

        head = payload.get("head") or {}
        base = payload.get("base") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        merged_at = payload.get("merged_at")
        return replace(
            candidate,
            draft=bool(payload.get("draft", False)),
            merged=bool(payload.get("merged", False)) or merged_at is not None,
            head_sha=str(head.get("sha") or "") or None,
            head_branch=str(head.get("ref") or "") or None,
            head_repo=str(head_repo) if isinstance(head_repo, str) else target_repo,
            base_branch=str(base.get("ref") or "") or None,
        )

    def _pull_payload(self, number: int) -> dict[str, Any]:
        """Read one pull request's own JSON (§7.2: head, base, draft, merged)."""
        response = self._request("GET", f"/repos/{self.owner}/{self.repo}/pulls/{number}")
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/pulls/{number} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/pulls/{number} returned "
                "an unexpected body"
            )
        return payload

    def fetch_input(self, number: int, input_type: InputType) -> TrackedInput:
        """Read one input with every guard and Gate fact (§5, §7)."""
        response = self._request("GET", self._issue_path(number))
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned an unexpected body"
            )
        candidate = self._input_from_issue_payload(payload, input_type)
        if input_type == "pull-request":
            candidate = self._apply_pull_payload(
                candidate, self._pull_payload(number), self.target_repo
            )
        return candidate

    def _list_labeled_inputs(self, selection_label: str) -> list[dict[str, Any]]:
        """One paginated sweep of the open inputs carrying the label (§5.2)."""
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/issues",
                params={
                    "labels": selection_label,
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": _PAGE_SIZE,
                    "page": page,
                },
            )
            if response.status_code != 200:
                raise StepTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/issues returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise StepTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/issues returned "
                    "an unexpected body"
                )
            for entry in batch:
                if isinstance(entry, dict):
                    found.append(entry)
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return found

    def list_issue_candidates(self, selection_label: str) -> list[TrackedInput]:
        """List the open issues carrying ``selection_label`` (§5.2).

        Read-only: pull requests (which the issues endpoint also lists)
        are filtered out. Selection writes nothing.
        """
        return [
            self._input_from_issue_payload(entry, "issue")
            for entry in self._list_labeled_inputs(selection_label)
            if "pull_request" not in entry
        ]

    def list_pull_request_candidates(self, selection_label: str) -> list[TrackedInput]:
        """List the open pull requests carrying ``selection_label`` (§5.2).

        Read-only: each labelled pull request is read back for its head,
        base, draft and merged facts. Selection writes nothing.
        """
        candidates: list[TrackedInput] = []
        for entry in self._list_labeled_inputs(selection_label):
            if "pull_request" not in entry:
                continue
            candidate = self._input_from_issue_payload(entry, "pull-request")
            try:
                number = int(entry["number"])
            except (KeyError, TypeError, ValueError):
                continue
            candidate = self._apply_pull_payload(
                candidate, self._pull_payload(number), self.target_repo
            )
            candidates.append(candidate)
        return candidates

    def list_open_pull_requests(self) -> list[TrackedInput]:
        """List every open pull request with its body (§5.3).

        Read-only: the ``skip-open-agent-pr`` scan looks for the Agent
        Identity's open pull request bearing an issue's Attempt Marker.
        """
        found: list[TrackedInput] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls",
                params={"state": "open", "per_page": _PAGE_SIZE, "page": page},
            )
            if response.status_code != 200:
                raise StepTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/pulls returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise StepTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/pulls returned "
                    "an unexpected body"
                )
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                user = entry.get("user") or {}
                found.append(
                    TrackedInput(
                        number=int(entry.get("number", 0)),
                        input_type="pull-request",
                        title=str(entry.get("title") or ""),
                        body=str(entry.get("body") or ""),
                        is_open=entry.get("state") == "open",
                        labels=tuple(
                            str(label.get("name"))
                            for label in entry.get("labels") or []
                            if isinstance(label, dict) and label.get("name")
                        ),
                        assignees=tuple(
                            str(assignee.get("login"))
                            for assignee in entry.get("assignees") or []
                            if isinstance(assignee, dict) and assignee.get("login")
                        ),
                        created_at=str(entry.get("created_at") or ""),
                        author_login=str(user.get("login", "")),
                        author_id=(
                            int(user["id"])
                            if isinstance(user.get("id"), int)
                            else None
                        ),
                        draft=bool(entry.get("draft", False)),
                    )
                )
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return found

    def read_labeled_event(
        self, number: int, input_type: InputType, label: str
    ) -> LabeledEvent | None:
        """Read the latest ``labeled`` event for ``label`` (§5.1).

        Pull requests are issues for the events API, so ``input_type``
        changes nothing about the endpoint. The full paginated list is
        searched and the latest matching event wins; ``None`` where no
        such event exists.
        """
        _ = input_type
        latest: LabeledEvent | None = None
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self._issue_path(number)}/events",
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            if response.status_code != 200:
                raise StepTrackerError(
                    f"GET {self._issue_path(number)}/events returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise StepTrackerError(
                    f"GET {self._issue_path(number)}/events returned "
                    "an unexpected body"
                )
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                if entry.get("event") != "labeled":
                    continue
                event_label = entry.get("label") or {}
                if event_label.get("name") != label:
                    continue
                actor = entry.get("actor") or {}
                candidate = LabeledEvent(
                    label=label,
                    actor_login=str(actor.get("login", "")),
                    actor_id=(
                        int(actor["id"])
                        if isinstance(actor.get("id"), int)
                        else None
                    ),
                    actor_type=str(actor.get("type", "")),
                    created_at=str(entry.get("created_at") or ""),
                )
                if latest is None or candidate.created_at >= latest.created_at:
                    latest = candidate
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return latest

    def has_open_blocker(self, number: int, input_type: InputType) -> bool:
        """Whether the input has an open native blocker (§5.2).

        ``issue_dependencies_summary.blocked_by`` counts open blockers only,
        so a positive count decides with no further read.
        """
        _ = input_type
        response = self._request("GET", self._issue_path(number))
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET {self._issue_path(number)} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        summary = payload.get("issue_dependencies_summary") if isinstance(
            payload, dict
        ) else None
        if isinstance(summary, dict):
            blocked_by = summary.get("blocked_by", 0)
            if isinstance(blocked_by, int) and blocked_by > 0:
                return True
            if isinstance(blocked_by, list) and blocked_by:
                return True
        return False

    def fetch_base_tip(self, base_branch: str) -> str:
        """Resolve the base-branch tip at Claim (§7.1, §7.2)."""
        branch = quote(base_branch, safe="")
        response = self._request(
            "GET", f"/repos/{self.owner}/{self.repo}/branches/{branch}"
        )
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/branches/{branch} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/branches/{branch} returned "
                "no commit sha"
            )
        return sha

    def assign_input(self, number: int, input_type: InputType) -> None:
        """Assign the Agent Identity: the Claim's first write (§6)."""
        _ = input_type
        response = self._request(
            "POST",
            f"{self._issue_path(number)}/assignees",
            json={"assignees": [self.agent_login]},
        )
        if response.status_code not in (200, 201):
            raise _write_error(
                "POST", f"{self._issue_path(number)}/assignees", response
            )

    def unassign_input(self, number: int, input_type: InputType) -> None:
        """Remove the Agent Identity's assignment, only the Worker's own (§6)."""
        _ = input_type
        response = self._request(
            "DELETE",
            f"{self._issue_path(number)}/assignees",
            json={"assignees": [self.agent_login]},
        )
        if response.status_code != 200:
            raise _write_error(
                "DELETE", f"{self._issue_path(number)}/assignees", response
            )

    @staticmethod
    def _comment_from_payload(
        payload: dict[str, Any], marker: str | None = None
    ) -> InputComment:
        """Read one listed comment, noting whether our marker is present.

        With a marker to look for, only its presence counts (§14). Without
        one — a posted write, whose body the caller just rendered — the
        flag records whether the body carries an Attempt Marker at all.
        """
        user = payload.get("user") or {}
        body = payload.get("body") or ""
        text = body if isinstance(body, str) else ""
        if marker is not None:
            present = marker in text
        else:
            present = "attempt=" in text and "kind=" in text
        created = payload.get("created_at")
        updated = payload.get("updated_at")
        return InputComment(
            comment_id=int(payload.get("id", 0)),
            body=text,
            author_login=str(user.get("login", "")),
            author_id=int(user["id"]) if isinstance(user.get("id"), int) else None,
            marker_present=present,
            created_at=str(created) if isinstance(created, str) else "",
            updated_at=str(updated) if isinstance(updated, str) else None,
        )

    def _comment_author_matches(self, candidate: InputComment) -> bool:
        """Whether ``candidate`` is authored by the Agent Identity (§14)."""
        if self.agent_id is not None:
            return candidate.author_id == self.agent_id
        return candidate.author_login == self.agent_login

    def post_claim_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post the Claim Comment bearing the Attempt Marker (§6.1)."""
        _ = input_type
        _ = marker
        response = self._request(
            "POST", f"{self._issue_path(number)}/comments", json={"body": body}
        )
        if response.status_code != 201:
            raise _write_error(
                "POST", f"{self._issue_path(number)}/comments", response
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"POST {self._issue_path(number)}/comments returned "
                "an unexpected body"
            )
        return self._comment_from_payload(payload)

    def list_comments(self, number: int, input_type: InputType) -> list[InputComment]:
        """List the input's comments, oldest first (§6.1, §14)."""
        _ = input_type
        found: list[InputComment] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self._issue_path(number)}/comments",
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            if response.status_code != 200:
                raise StepTrackerError(
                    f"GET {self._issue_path(number)}/comments returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise StepTrackerError(
                    f"GET {self._issue_path(number)}/comments returned "
                    "an unexpected body"
                )
            for entry in batch:
                if isinstance(entry, dict):
                    found.append(self._comment_from_payload(entry))
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return found

    def find_comment_by_marker(
        self, number: int, input_type: InputType, marker: AttemptMarker
    ) -> InputComment | None:
        """Find our comment by Attempt Marker (§14)."""
        rendered = self.format_marker(marker)
        for comment in self.list_comments(number, input_type):
            if rendered not in comment.body:
                continue
            if not self._comment_author_matches(comment):
                continue
            return comment
        return None

    def delete_comment(self, comment_id: int) -> None:
        """Delete one of the Worker's own comments (§6.2, §16.2)."""
        response = self._request(
            "DELETE",
            f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}",
        )
        if response.status_code == 404:
            return
        if response.status_code != 204:
            raise _write_error(
                "DELETE",
                f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}",
                response,
            )

    def post_comment(
        self, number: int, input_type: InputType, body: str, marker: AttemptMarker
    ) -> InputComment:
        """Post one comment on the input, body already rendered (§10.7)."""
        posted = self.post_claim_comment(number, input_type, body, marker)
        return InputComment(
            comment_id=posted.comment_id,
            body=posted.body,
            author_login=posted.author_login,
            author_id=posted.author_id,
            marker_present=posted.marker_present,
        )

    def publish_review(
        self, number: int, *, commit_id: str, body: str, marker: AttemptMarker
    ) -> InputReview:
        """Publish a COMMENT-event review against ``commit_id`` (§10.6)."""
        _ = marker
        response = self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls/{number}/reviews",
            json={"commit_id": commit_id, "body": body, "event": "COMMENT"},
        )
        if response.status_code != 200:
            raise _write_error(
                "POST",
                f"/repos/{self.owner}/{self.repo}/pulls/{number}/reviews",
                response,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"POST /repos/{self.owner}/{self.repo}/pulls/{number}/reviews "
                "returned an unexpected body"
            )
        user = payload.get("user") or {}
        return InputReview(
            review_id=int(payload.get("id", 0)),
            body=body,
            author_login=str(user.get("login", self.agent_login)),
            author_id=(
                int(user["id"])
                if isinstance(user.get("id"), int)
                else self.agent_id
            ),
            commit_id=commit_id,
            marker_present="attempt=" in body and "kind=" in body,
        )

    def list_reviews(self, number: int) -> list[InputReview]:
        """List the pull request's reviews, oldest first (§14)."""
        found: list[InputReview] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls/{number}/reviews",
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            if response.status_code != 200:
                raise StepTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/pulls/{number}/reviews "
                    f"returned {response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise StepTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/pulls/{number}/reviews "
                    "returned an unexpected body"
                )
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                user = entry.get("user") or {}
                body = entry.get("body") or ""
                found.append(
                    InputReview(
                        review_id=int(entry.get("id", 0)),
                        body=body if isinstance(body, str) else "",
                        author_login=str(user.get("login", "")),
                        author_id=(
                            int(user["id"])
                            if isinstance(user.get("id"), int)
                            else None
                        ),
                        commit_id=str(entry.get("commit_id") or "") or None,
                        marker_present=False,
                    )
                )
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return found

    def find_review_by_marker(
        self, number: int, marker: AttemptMarker
    ) -> InputReview | None:
        """Find our review by Attempt Marker (§14)."""
        rendered = self.format_marker(marker)
        for review in self.list_reviews(number):
            if rendered not in review.body:
                continue
            if self.agent_id is not None:
                if review.author_id != self.agent_id:
                    continue
            elif review.author_login != self.agent_login:
                continue
            return review
        return None

    @staticmethod
    def _pull_request_from_payload(
        payload: dict[str, Any], marker: str | None = None
    ) -> InputPullRequest:
        """Read one listed pull request, noting whether our marker is present."""
        user = payload.get("user") or {}
        head = payload.get("head") or {}
        base = payload.get("base") or {}
        body = payload.get("body") or ""
        text = body if isinstance(body, str) else ""
        if marker is not None:
            present = marker in text
        else:
            present = "attempt=" in text and "kind=" in text
        return InputPullRequest(
            number=int(payload.get("number", 0)),
            url=str(payload.get("html_url", "")),
            title=str(payload.get("title") or ""),
            head_branch=str(head.get("ref", "")),
            base_branch=str(base.get("ref", "")),
            body=text,
            state=str(payload.get("state", "")),
            author_login=str(user.get("login", "")),
            author_id=(
                int(user["id"]) if isinstance(user.get("id"), int) else None
            ),
            marker_present=present,
            draft=bool(payload.get("draft", False)),
        )

    def _pull_request_author_matches(self, candidate: InputPullRequest) -> bool:
        """Whether ``candidate`` is authored by the Agent Identity (§14)."""
        if self.agent_id is not None:
            return candidate.author_id == self.agent_id
        return candidate.author_login == self.agent_login

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
        """Open one pull request from ``head_branch`` (§10.5, §14)."""
        _ = marker
        response = self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            json={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
                "draft": draft,
            },
        )
        if response.status_code != 201:
            raise _write_error(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", response
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise StepTrackerError(
                f"POST /repos/{self.owner}/{self.repo}/pulls returned "
                "an unexpected body"
            )
        opened = self._pull_request_from_payload(payload)
        return InputPullRequest(
            number=opened.number,
            url=opened.url,
            title=opened.title or title,
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
            state=opened.state or "open",
            author_login=opened.author_login or self.agent_login,
            author_id=(
                opened.author_id if opened.author_id is not None else self.agent_id
            ),
            marker_present=True,
            draft=draft,
        )

    def find_pull_request(
        self, head_branch: str, marker: AttemptMarker
    ) -> InputPullRequest | None:
        """Find our pull request by head branch and Attempt Marker (§14).

        Lists pull requests with ``head=<owner>:<branch>`` and ``state=all``,
        then returns the one whose body carries the rendered marker and
        whose author is the Agent Identity — open preferred where both
        states match. An unmarked pull request from the same branch is not
        ours: the lookup never adopts it and returns ``None`` instead.
        """
        rendered = self.format_marker(marker)
        response = self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls",
            params={
                "head": f"{self.owner}:{head_branch}",
                "state": "all",
                "per_page": _PAGE_SIZE,
                "page": 1,
            },
        )
        if response.status_code != 200:
            raise StepTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/pulls returned "
                f"{response.status_code}: {response.text}"
            )
        batch = response.json()
        items = batch if isinstance(batch, list) else []
        matching: list[InputPullRequest] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            candidate = self._pull_request_from_payload(entry, rendered)
            if candidate.head_branch != head_branch:
                continue
            if not candidate.marker_present:
                continue
            if not self._pull_request_author_matches(candidate):
                continue
            matching.append(candidate)
        for candidate in matching:
            if candidate.state == "open":
                return candidate
        return matching[0] if matching else None

    def remove_label(self, number: int, input_type: InputType, label: str) -> None:
        """Remove one label from the input (§15.3).

        Issues and pull requests share the issues label API. A 404 is the
        idempotent absent case.
        """
        _ = input_type
        response = self._request(
            "DELETE",
            f"{self._issue_path(number)}/labels/{quote(label, safe='')}",
        )
        if response.status_code in (200, 404):
            return
        raise _write_error(
            "DELETE",
            f"{self._issue_path(number)}/labels/{label}",
            response,
        )

    def add_labels(
        self, number: int, input_type: InputType, labels: tuple[str, ...]
    ) -> None:
        """Apply end labels declared for the Terminal Outcome (§15.3)."""
        _ = input_type
        if not labels:
            return
        response = self._request(
            "POST",
            f"{self._issue_path(number)}/labels",
            json={"labels": list(labels)},
        )
        if response.status_code != 200:
            raise _write_error(
                "POST", f"{self._issue_path(number)}/labels", response
            )


def make_step_tracker(
    *,
    owner: str,
    repo: str,
    token: str,
    agent_login: str,
    agent_id: int | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> GitHubStepTracker:
    """Build the production step tracker for the Agent Identity (§5, §14, §18).

    The adapter owns the concrete client: core startup and the port
    factory pass the token, never the client type. No configuration can
    select a fake: production always builds this one.
    """
    return GitHubStepTracker(
        owner=owner,
        repo=repo,
        client=GitHubClient(token, base_url=base_url),
        agent_login=agent_login,
        agent_id=agent_id,
    )


def run_startup_checks(
    token: str,
    owner: str,
    repo: str,
    *,
    rate_limit_minimum: int | None = None,
) -> StartupCheckResult:
    """Run every non-mutating check contract §18 "Worker startup" requires.

    Adapter-owned: the token-shape assertion, the identity resolution, the
    rate-limit headroom read and the push-permission probe all live
    GitHub-side. Raises :class:`StartupCheckFailed` or
    :class:`TokenRejected` from the identity layer — never a GitHub type
    core must name.
    """
    from coding_agent.identity.startup import (
        DEFAULT_RATE_LIMIT_MINIMUM,
        run_startup_checks as _identity_startup_checks,
    )

    return _identity_startup_checks(
        GitHubClient(token),
        token,
        owner,
        repo,
        rate_limit_minimum=(
            DEFAULT_RATE_LIMIT_MINIMUM
            if rate_limit_minimum is None
            else rate_limit_minimum
        ),
    )


def read_text_at_revision(
    *,
    token: str,
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> str:
    """Read a text file at a revision through the Contents API (§10.2).

    The deferred project-profile read: the bytes at ``path`` as committed
    at ``ref``, decoded as UTF-8. Raises :class:`InputNotFound` where the
    path is absent at that revision and :class:`InputError` where the read
    otherwise fails. The Contents response shape and its base64 decoding
    are adapter-side; the caller maps the neutral errors to its own
    refusal. Mirrors the previously raw read: no retry classification, so
    a 401 reads as a failed read, never a halt.
    """
    client = GitHubClient(token)
    api_path = f"/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    try:
        response = client.get(api_path)
    except requests.RequestException as exc:
        raise InputError(
            "GET", api_path, 0, reason="read-failed", failure_class="github"
        ) from exc
    if response.status_code == 404:
        raise InputNotFound("GET", api_path)
    if response.status_code != 200:
        raise InputError(
            "GET",
            api_path,
            response.status_code,
            reason="read-failed",
            failure_class="github",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise InputError(
            "GET", api_path, 200, reason="bad-body", failure_class="github"
        ) from exc
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str):
        raise InputError(
            "GET", api_path, 200, reason="bad-body", failure_class="github"
        )
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise InputError(
            "GET", api_path, 200, reason="bad-body", failure_class="github"
        ) from exc


def make_probe_transport(token: str) -> GitHubClient:
    """Build the narrow HTTP transport the preflight probes need.

    The adapter owns the concrete client: the probes program against a
    structural HTTP surface and never name the client type.
    """
    return GitHubClient(token)
