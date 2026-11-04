"""The GitHub adapter of ``TrackerPort``: the terminal writes reach GitHub.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write, through this port), §4 (Selection: the candidates
are the open issues carrying the Workflow's Selection Label; selection writes
nothing), §5 (the Claim is an assignment read back; the Worker removes only
an assignment the ledger records as ``assigned_by_us`` — the node consults
the ledger before calling ``release_assignment``, so this adapter only ever
removes the Agent Identity's own login; the Claim assignment adds no label
and no comment), §12 (the pull request opens only for a ready Publication Gate
verdict: its title is the Target Issue's title and its body is the recorded
§15 body), §13 (the write sequence; the rendered bodies are recorded in
``outcomes`` before this adapter sends them, so a retried write sends the
same bytes; pull-request identity is the head branch plus the Attempt Marker
in the body, looked up with ``head=<owner>:<branch>`` and ``state=all``;
the lookup also filters by the Agent Identity's numeric id; the marker rule
reads in one direction only — an unmarked pull request from the same branch
is not ours, L3-DEL-21; a closed pull request from this branch is a
withdrawal — the Attempt ends ``abandoned`` and the pull request is never
reopened, L3-DEL-14) and §14 (the terminal writes in C-OUT-1 order: the
comment, the Selection Label removal, ``ready-for-human``, then the
assignment removal; never a draft pull request, P-DEL-10), and §18 (the two
structural abstentions: there is no operation that applies the Selection
Label and none that edits the issue body — the port cannot express them, so
this adapter cannot perform them either; lookups filter by the Agent
Identity's id; the terminal comment is found by a full paginated lookup
filtered to the Agent Identity's numeric id and the Attempt Marker —
a comment by another account carrying our marker is not our receipt,
REC-DEL-6, and a comment past the first hundred is still found, REC-DEL-5).
Every request is classified by §13's "Rate limits and
refusals" table through ``request_with_retry``: a 401 confirmed by the
``GET /user`` identity probe is a Credential Failure, an unconfirmed 401
is infrastructure-class ``github`` (C-ERR-2), a permission-shaped 403 a
permission refusal, rate limits are retried in place; any other status is
read by the operation itself.

MVP row ids: C-TRK-1, C-OUT-1, C-ERR-2, P-DEL-1, P-DEL-10, L3-DEL-14,
L3-DEL-21, P-SEL-1, P-SEL-2, P-SEL-3, P-SEL-4, P-SEL-5, C-SEL-1, P-CLM-1,
P-CLM-2, P-WRK-1, L3-ERR-3, L3-ERR-4, L3-ERR-5, L3-ERR-6,
P-CAN-1, P-CAN-2, W-STP-3, G-REV-3, REC-DEL-5, REC-DEL-6.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.github.errors import request_with_retry
from coding_agent.github.issues import IssueFetchFailed, fetch_issue
from coding_agent.tracker.port import (
    CandidateIssue,
    IssueState,
    PullRequest,
    TerminalComment,
    TrackedIssue,
)
from coding_agent.tracker.selection import parse_blocked_by_references

_PAGE_SIZE = 100
"""One Selection or pull-request page (contract §4 reads paginate)."""

DEFAULT_SELECTION_LABEL = "ready-for-agent"
"""The Selection Label removed after the terminal comment (§4, §14).

The parity Workflow's selector label; ``agent run`` pins the configured one
instead.
"""

DEFAULT_READY_FOR_HUMAN = "ready-for-human"
"""The lifecycle label added on every outcome without a pull request (§14)."""


class GitHubTrackerError(Exception):
    """A GitHub terminal write failed; the recorded outcome still stands."""


@dataclass(frozen=True)
class GitHubTracker:
    """A ``TrackerPort`` that performs the terminal writes on GitHub.

    ``selection_label`` is the Workflow's Selection Label, removed after the
    terminal comment; ``ready_for_human`` is the lifecycle label added on
    every outcome without a pull request (contract §2.1: only its name is
    configurable). ``agent_login`` is the Agent Identity's login, the only
    assignee ``release_assignment`` ever removes — never another assignee.
    ``agent_id`` is the Agent Identity's numeric account id, which the
    pull-request lookup filters by (§13, §18); where it is ``None`` the
    login decides instead.

    Every request goes through ``request_with_retry`` (§13): a 401
    confirmed by the identity probe raises ``CredentialFailure``, an
    unconfirmed 401 raises infrastructure-class ``GitHubFailure`` (C-ERR-2),
    a permission-shaped 403 raises ``PermissionRefusal``, rate limits are
    retried in place on ``sleep``/``now``, and every other status comes
    back here to be read by the operation itself.
    """

    owner: str
    repo: str
    client: GitHubClient
    agent_login: str
    selection_label: str = DEFAULT_SELECTION_LABEL
    ready_for_human: str = DEFAULT_READY_FOR_HUMAN
    agent_id: int | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One §13-classified call; statuses outside 401/403/429 are returned."""
        return request_with_retry(
            self.client,
            method,
            path,
            sleep=self.sleep,
            now=self.now,
            raise_other=False,
            **kwargs,
        )

    def _issue_path(self, issue_number: int) -> str:
        return f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        """Read the Target Issue's number, title and body (HTTP ``GET``)."""
        fetched = fetch_issue(
            self.client, self.owner, self.repo, issue_number, sleep=self.sleep, now=self.now
        )
        return TrackedIssue(
            number=fetched.number, title=fetched.title, body=fetched.body
        )

    @staticmethod
    def _candidate_from_payload(payload: dict[str, Any]) -> CandidateIssue:
        """Report one issue's guard facts (§4) from its JSON."""
        raw_assignees = payload.get("assignees") or []
        raw_labels = payload.get("labels") or []
        return CandidateIssue(
            number=int(payload["number"]),
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            is_open=payload.get("state") == "open",
            assignees=tuple(
                str(entry.get("login"))
                for entry in raw_assignees
                if isinstance(entry, dict) and entry.get("login")
            ),
            labels=tuple(
                str(entry.get("name"))
                for entry in raw_labels
                if isinstance(entry, dict) and entry.get("name")
            ),
            created_at=str(payload.get("created_at") or ""),
        )

    def list_candidates(self, selection_label: str) -> list[CandidateIssue]:
        """List the open issues carrying ``selection_label`` (§4).

        Read-only: one paginated ``GET`` per page, pull requests filtered out
        (the issues endpoint lists them too). Selection writes nothing.
        """
        found: list[CandidateIssue] = []
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
                raise GitHubTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/issues returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            for entry in batch:
                if isinstance(entry, dict) and "pull_request" in entry:
                    continue
                found.append(self._candidate_from_payload(entry))
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return found

    def fetch_candidate(self, issue_number: int) -> CandidateIssue:
        """Re-read one candidate with every guard fact (§4, §5 read-back)."""
        response = self._request("GET", self._issue_path(issue_number))
        if response.status_code != 200:
            raise IssueFetchFailed(
                f"GET {self._issue_path(issue_number)} returned "
                f"{response.status_code}: {response.text}"
            )
        return self._candidate_from_payload(response.json())

    def read_issue_state(self, issue_number: int) -> IssueState:
        """Re-read the issue's open state and labels for a Gate (§11).

        Read-only: one ``GET`` on the issue; a Gate compares the labels
        against the Selection Label to detect a human cancellation
        (P-CAN-1, W-STP-3, G-REV-3). Selection writes nothing.
        """
        payload = self._issue_payload(issue_number)
        raw_labels = payload.get("labels") or []
        return IssueState(
            number=issue_number,
            is_open=payload.get("state") == "open",
            labels=tuple(
                str(entry.get("name"))
                for entry in raw_labels
                if isinstance(entry, dict) and entry.get("name")
            ),
        )

    def _issue_payload(self, issue_number: int) -> dict[str, Any]:
        """Read one issue's raw JSON (§4: the guard facts live here)."""
        response = self._request("GET", self._issue_path(issue_number))
        if response.status_code != 200:
            raise IssueFetchFailed(
                f"GET {self._issue_path(issue_number)} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubTrackerError(
                f"GET {self._issue_path(issue_number)} returned an unexpected body"
            )
        return payload

    def has_open_blocker(self, issue_number: int) -> bool:
        """Whether the issue has an open native blocker (§4).

        ``issue_dependencies_summary.blocked_by`` counts open blockers only,
        so a positive count decides with no further read. A non-empty list
        shape decides the same way: any reported blocker blocks. Where native
        dependencies are unavailable the ``Blocked by: #n`` fallback line
        names the blockers and each referenced issue is read back: open
        blocks, closed or missing does not.
        """
        payload = self._issue_payload(issue_number)
        summary = payload.get("issue_dependencies_summary")
        if isinstance(summary, dict):
            blocked_by = summary.get("blocked_by", 0)
            if isinstance(blocked_by, int) and blocked_by > 0:
                return True
            if isinstance(blocked_by, list) and blocked_by:
                return True
        body = payload.get("body")
        references = parse_blocked_by_references(
            body if isinstance(body, str) else ""
        )
        for referenced in references:
            response = self._request("GET", self._issue_path(referenced))
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                raise GitHubTrackerError(
                    f"GET {self._issue_path(referenced)} returned "
                    f"{response.status_code}: {response.text}"
                )
            ref_payload = response.json()
            if isinstance(ref_payload, dict) and ref_payload.get("state") == "open":
                return True
        return False

    def has_open_attempt_pull_request(self, issue_number: int) -> bool:
        """Whether an open pull request carries an Attempt Marker for it (§4).

        Read-only: one paginated ``GET`` per page over the open pull
        requests, scanning each body for ``attempt=#<issue>/``. Selection
        writes nothing.
        """
        needle = f"attempt=#{issue_number}/"
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls",
                params={
                    "state": "open",
                    "per_page": _PAGE_SIZE,
                    "page": page,
                },
            )
            if response.status_code != 200:
                raise GitHubTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/pulls returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubTrackerError(
                    f"GET /repos/{self.owner}/{self.repo}/pulls returned "
                    "an unexpected body"
                )
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                body = entry.get("body")
                if isinstance(body, str) and needle in body:
                    return True
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return False

    def assign_issue(self, issue_number: int) -> None:
        """Assign the Agent Identity: the Claim's one write (§5, P-CLM-1).

        Adds no label and writes no comment — the request carries only the
        Agent Identity's login.
        """
        response = self._request(
            "POST",
            f"{self._issue_path(issue_number)}/assignees",
            json={"assignees": [self.agent_login]},
        )
        if response.status_code not in (200, 201):
            raise GitHubTrackerError(
                f"POST {self._issue_path(issue_number)}/assignees returned "
                f"{response.status_code}: {response.text}"
            )

    def fetch_base_revision(self, base_branch: str) -> str:
        """Resolve the Base Revision from the configured base branch (§5)."""
        branch = quote(base_branch, safe="")
        response = self._request("GET", f"/repos/{self.owner}/{self.repo}/branches/{branch}")
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/branches/{branch} returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise GitHubTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/branches/{branch} returned "
                "no commit sha"
            )
        return sha

    def post_terminal_comment(self, issue_number: int, body: str) -> None:
        """Post the one terminal comment, with the bytes already recorded in
        ``outcomes`` (§14): a retried write sends the same ``body``."""
        response = self._request(
            "POST",
            f"{self._issue_path(issue_number)}/comments", json={"body": body}
        )
        if response.status_code != 201:
            raise GitHubTrackerError(
                f"POST {self._issue_path(issue_number)}/comments returned "
                f"{response.status_code}: {response.text}"
            )

    def _comment_from_payload(
        self, payload: dict[str, Any], marker: str
    ) -> TerminalComment:
        """Read one listed comment, noting whether our marker is present."""
        user = payload.get("user") or {}
        body = payload.get("body") or ""
        return TerminalComment(
            comment_id=int(payload.get("id", 0)),
            body=body if isinstance(body, str) else "",
            author_login=str(user.get("login", "")),
            author_id=int(user["id"]) if isinstance(user.get("id"), int) else None,
            marker_present=marker in (body if isinstance(body, str) else ""),
        )

    def _comment_author_matches(self, candidate: TerminalComment) -> bool:
        """Whether ``candidate`` is authored by the Agent Identity (§13, §18).

        The numeric id decides where it is known; otherwise the login does.
        A comment by another account is not our receipt even when its body
        carries our marker (REC-DEL-6).
        """
        if self.agent_id is not None:
            return candidate.author_id == self.agent_id
        return candidate.author_login == self.agent_login

    def find_terminal_comment(
        self, issue_number: int, marker: str
    ) -> TerminalComment | None:
        """Find our terminal comment by Attempt Marker (§13).

        Reads the full paginated comment list — page after page until a
        short page ends it — and returns the comment whose body carries
        ``marker`` and whose author is the Agent Identity. An issue with
        more than 100 comments still finds ours wherever it sits (REC-DEL-5);
        a comment by another account carrying our marker is never adopted
        (REC-DEL-6). ``None`` means the comment is absent: recovery may write
        it, and it writes exactly one.
        """
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self._issue_path(issue_number)}/comments",
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            if response.status_code != 200:
                raise GitHubTrackerError(
                    f"GET {self._issue_path(issue_number)}/comments returned "
                    f"{response.status_code}: {response.text}"
                )
            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubTrackerError(
                    f"GET {self._issue_path(issue_number)}/comments returned "
                    "an unexpected body"
                )
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                candidate = self._comment_from_payload(entry, marker)
                if not candidate.marker_present:
                    continue
                if not self._comment_author_matches(candidate):
                    continue
                return candidate
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return None

    def remove_selection_label(self, issue_number: int) -> None:
        """Remove the Selection Label when still present (C-OUT-1).

        A ``404`` means the label is already absent — the write is naturally
        idempotent (§13) — so it is the successful no-op, not a failure.
        """
        label = quote(self.selection_label, safe="")
        response = self._request(
            "DELETE",
            f"{self._issue_path(issue_number)}/labels/{label}"
        )
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"DELETE {self._issue_path(issue_number)}/labels/{label} returned "
                f"{response.status_code}: {response.text}"
            )

    def add_ready_for_human(self, issue_number: int) -> None:
        """Add ``ready-for-human`` on every outcome without a pull request."""
        response = self._request(
            "POST",
            f"{self._issue_path(issue_number)}/labels",
            json={"labels": [self.ready_for_human]},
        )
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"POST {self._issue_path(issue_number)}/labels returned "
                f"{response.status_code}: {response.text}"
            )

    def release_assignment(self, issue_number: int) -> None:
        """Remove the Worker's own assignment: the Agent Identity's login only.

        The caller (the ``terminal_writes`` node) performs this write only
        when the ledger records the assignment as ``assigned_by_us`` (§5); an
        assignment with no open Attempt is a human's and is never removed.
        """
        response = self._request(
            "DELETE",
            f"{self._issue_path(issue_number)}/assignees",
            json={"assignees": [self.agent_login]},
        )
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"DELETE {self._issue_path(issue_number)}/assignees returned "
                f"{response.status_code}: {response.text}"
            )

    def _pull_request_from_payload(self, payload: dict[str, Any], marker: str) -> PullRequest:
        """Read one listed pull request, noting whether our marker is present."""
        user = payload.get("user") or {}
        body = payload.get("body") or ""
        return PullRequest(
            number=int(payload.get("number", 0)),
            url=str(payload.get("html_url", "")),
            title=str(payload.get("title", "")),
            head_branch=str((payload.get("head") or {}).get("ref", "")),
            base_branch=str((payload.get("base") or {}).get("ref", "")),
            body=body if isinstance(body, str) else "",
            state=str(payload.get("state", "")),
            author_login=str(user.get("login", "")),
            author_id=int(user["id"]) if isinstance(user.get("id"), int) else None,
            marker_present=marker in (body if isinstance(body, str) else ""),
            draft=bool(payload.get("draft", False)),
        )

    def _author_matches(self, candidate: PullRequest) -> bool:
        """Whether ``candidate`` is authored by the Agent Identity (§13, §18).

        The numeric id decides where it is known; otherwise the login does.
        A pull request by another account is not our receipt even when its
        body carries our marker.
        """
        if self.agent_id is not None:
            return candidate.author_id == self.agent_id
        return candidate.author_login == self.agent_login

    def find_pull_request(self, head_branch: str, marker: str) -> PullRequest | None:
        """Look a pull request up by head branch and Attempt Marker (§13).

        Lists pull requests with ``head=<owner>:<branch>`` and ``state=all``,
        then returns the one whose body carries ``marker`` and whose author
        is the Agent Identity — open preferred where both states match. An
        unmarked pull request from the same branch is not ours (L3-DEL-21):
        the lookup never adopts it and returns ``None`` instead.
        """
        response = self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls",
            params={
                "head": f"{self.owner}:{head_branch}",
                "state": "all",
                "per_page": "100",
            },
        )
        if response.status_code != 200:
            raise GitHubTrackerError(
                f"GET /repos/{self.owner}/{self.repo}/pulls returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        items = payload if isinstance(payload, list) else []
        matching: list[PullRequest] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            candidate = self._pull_request_from_payload(entry, marker)
            if candidate.head_branch != head_branch:
                continue
            if not candidate.marker_present:
                continue
            if not self._author_matches(candidate):
                continue
            matching.append(candidate)
        for candidate in matching:
            if candidate.state == "open":
                return candidate
        return matching[0] if matching else None

    def open_pull_request(
        self, *, title: str, head_branch: str, base_branch: str, body: str
    ) -> PullRequest:
        """Open one non-draft pull request from ``head_branch`` (§13, §15).

        The title is the Target Issue's title and the body is the recorded
        §15 body. Never a draft (P-DEL-10): the request sends no ``draft``
        field at all, so the platform cannot create anything but a ready
        pull request.
        """
        response = self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            json={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
            },
        )
        if response.status_code != 201:
            raise GitHubTrackerError(
                f"POST /repos/{self.owner}/{self.repo}/pulls returned "
                f"{response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubTrackerError(
                f"POST /repos/{self.owner}/{self.repo}/pulls returned "
                "an unexpected body"
            )
        user = payload.get("user") or {}
        return PullRequest(
            number=int(payload.get("number", 0)),
            url=str(payload.get("html_url", "")),
            title=str(payload.get("title", title)),
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
            state=str(payload.get("state", "open")),
            author_login=str(user.get("login", self.agent_login)),
            author_id=(
                int(user["id"]) if isinstance(user.get("id"), int) else self.agent_id
            ),
            marker_present=True,
            draft=False,
        )
