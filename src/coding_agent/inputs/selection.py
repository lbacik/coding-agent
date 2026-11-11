"""Selection guards, filters and order (contract §5.2–§5.4).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5.2 (a
candidate is an open input of the Workflow's type carrying its Selection
Label; it is excluded when any mandatory guard holds — not open, the
Selection Label absent or unauthorised, any assignee, an open native
blocker, a fork head for pull requests, another Workflow's Claim in force;
YAML cannot remove them), §5.3 (``require-all``, ``require-any`` and
``exclude`` narrow, never authorise; ``drafts`` excludes drafts by default;
``skip-open-agent-pr`` skips an issue with the Agent Identity's open pull
request bearing that issue's Attempt Marker — a marker substring by another
author does not exclude), and §5.4 (order ``(created_at, number)``
ascending).

Row ids: INP-4, INP-6, INP-8 … INP-12, INP-19, INP-20.

This module is pure: it takes reported input facts and answers which
reason excludes a candidate, or ``None`` where none does. It performs no
network call and no ledger write, which is what makes "selection writes
nothing" structural rather than promised.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from coding_agent.inputs.port import InputType, TrackedInput

DRAFT_POLICIES: tuple[str, ...] = ("include", "exclude")
"""The ``drafts`` selector option (§5.3): ``exclude`` by default."""


def exclusion_reason(
    candidate: TrackedInput,
    *,
    selection_label: str,
    label_authorised: bool,
    blocker_open: bool,
    claim_in_force: bool,
    target_repo: str | None = None,
    agent_pr_open: bool = False,
    require_all: Sequence[str] = (),
    require_any: Sequence[str] = (),
    exclude: Sequence[str] = (),
    drafts: str = "exclude",
) -> str | None:
    """The reason ``candidate`` is excluded, or ``None`` where selectable.

    The mandatory guards run in contract §5.2 order, then the Workflow's
    filters (§5.3): ``require-all`` (every listed label present),
    ``require-any`` (at least one present) and ``exclude`` (none present).
    ``blocker_open`` reports whether the input has an open native blocker,
    ``claim_in_force`` whether another Workflow's Claim holds the input,
    and ``agent_pr_open`` whether the Agent Identity's open pull request
    bears this issue's Attempt Marker (``skip-open-agent-pr``) — all read
    by the caller, never here, so this function stays pure.
    ``label_authorised`` is the §5.1 actor test on the latest ``labeled``
    event for the Selection Label; ``drafts`` applies to pull-request
    inputs only.
    """
    if not candidate.is_open:
        return "closed"
    if selection_label not in candidate.labels:
        return "selection-label"
    if not label_authorised:
        return "label-authorisation"
    if candidate.assignees:
        return "assigned"
    if blocker_open:
        return "blocker"
    if (
        candidate.input_type == "pull-request"
        and target_repo is not None
        and candidate.head_repo is not None
        and candidate.head_repo != target_repo
    ):
        return "fork"
    if claim_in_force:
        return "claim"
    if candidate.input_type == "pull-request" and drafts == "exclude":
        if candidate.draft:
            return "draft"
    if agent_pr_open:
        return "agent-pr"
    labels = set(candidate.labels)
    if any(required not in labels for required in require_all):
        return "require-all"
    if require_any and not any(required in labels for required in require_any):
        return "require-any"
    if any(excluded in labels for excluded in exclude):
        return "exclude"
    return None


def sort_inputs(
    candidates: Iterable[TrackedInput],
) -> list[TrackedInput]:
    """The contract §5.4 order: ``(created_at, number)`` ascending."""
    return sorted(candidates, key=lambda candidate: (candidate.created_at, candidate.number))


def open_agent_pr_for_issue(
    pull_requests: Iterable[TrackedInput],
    issue_number: int,
    *,
    agent_login: str,
    agent_id: int | None,
) -> TrackedInput | None:
    """The Agent Identity's open pull request bearing an issue's marker (§5.3).

    Only an open pull request authored by the Agent Identity — filtered by
    the numeric id where it is known, otherwise by login — whose body
    carries that issue's Attempt Marker (``#<issue>/``) excludes the issue
    under ``skip-open-agent-pr``. An open pull request merely containing an
    Attempt-Marker substring, by another author, does not exclude (INP-11);
    a pull-request Workflow inherits no such exclusion, so a pull request
    an implementation Attempt opened stays eligible for review (INP-4).
    """
    needle = f"#{issue_number}/"
    for pull_request in pull_requests:
        if pull_request.input_type != "pull-request":
            continue
        if not pull_request.is_open:
            continue
        if needle not in pull_request.body:
            continue
        if input_author_matches(
            pull_request, agent_login=agent_login, agent_id=agent_id
        ):
            return pull_request
    return None


def input_author_matches(
    candidate: TrackedInput, *, agent_login: str, agent_id: int | None
) -> bool:
    """Whether ``candidate`` is authored by the Agent Identity (§14).

    The numeric id decides where it is known; otherwise the login does. A
    pull request by another account is never ours, even when its body
    carries our marker (INP-11).
    """
    if agent_id is not None:
        return candidate.author_id == agent_id
    return candidate.author_login == agent_login
