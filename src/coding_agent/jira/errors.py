"""Jira Cloud API failure classification and bounded in-place retry.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14.4 (the
failure vocabulary is neutral; in-place retry is bounded at 3 attempts and
5 minutes, and the waiting counts against the Attempt's wall clock; beyond
the bound the failure is infrastructure-class) and [ADR
0038](../../../../docs/adr/0038-the-issue-tracker-and-the-code-host-are-reached-only-through-their-ports.md)
(the Jira tracker pin: one project, link-by-key delivery).

Row ids: L3-ERR-3…6 analogues on Jira, C-ERR-2 (a 401 is a Credential
Failure only when the identity probe also returns 401; otherwise it is
infrastructure-class ``jira``).

A 401 classifies as a Credential Failure only when the identity probe
confirms it. Halting the Worker on it belongs to the ``agent run`` slice,
which reads ``CredentialFailure`` here.

A 5xx response, a connection error and a timeout are transient (§14.4):
retried in place within the same bound, but only where a repeat cannot
create a second object. Requests that create an object (a comment ``POST``)
are handed back unretried, so the effect driver can re-look them up by
Attempt Marker before repeating them.

This module sits under the ``JiraClient`` every tracker operation uses:
the Jira adapter reads through ``request_with_retry`` (with
``raise_other=False``, so the statuses it reads itself come back to it)
and gets the whole table without extra work. The response body is never
inspected and never copied into an exception: only the status code and the
rate-limit header values decide, so tests inject the clock and the sleep
and nothing really sleeps and no network is used.

The failure types themselves are canonical in
``coding_agent.inputs.errors``: this module keeps only the Jira-specific
translation logic (``classify_jira_failure`` and ``request_with_retry``),
which raises the neutral objects directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

import requests

from coding_agent.inputs.errors import (
    INITIAL_BACKOFF_SECONDS,
    MAX_IN_PLACE_ATTEMPTS,
    MAX_IN_PLACE_WAIT_SECONDS,
    CredentialFailure,
    HostFailure,
    PermissionRefusal,
    RateLimitExhausted,
)
from coding_agent.jira.client import JiraClient

__all__ = [
    "CredentialFailure",
    "HostFailure",
    "INITIAL_BACKOFF_SECONDS",
    "JiraFailure",
    "MAX_IN_PLACE_ATTEMPTS",
    "MAX_IN_PLACE_WAIT_SECONDS",
    "PermissionRefusal",
    "RateLimitExhausted",
    "classify_jira_failure",
    "request_with_retry",
]

JiraFailure = HostFailure
"""Alias of :class:`HostFailure` (``coding_agent.inputs.errors``).

The neutral class object the Jira adapter raises for its
infrastructure-class failures (``failure_class="jira"``); new code imports
``HostFailure`` from ``coding_agent.inputs.errors`` instead.
"""

Classification = Literal[
    "credential-failure",
    "permission-refusal",
    "primary-rate-limit",
    "secondary-rate-limit",
    "transient",
    "other",
]
"""Every Jira API failure class, plus ``other`` for the rest.

``credential-failure`` and ``permission-refusal`` are terminal for the
Attempt; ``primary-rate-limit``, ``secondary-rate-limit`` and ``transient``
are retried in place within the bound (§14.4: a 5xx, a connection error or
a timeout); ``other`` (any status outside 401/403/404/429/5xx, including
the 404 reads handle themselves) is an infrastructure-class failure with
no retry.
"""


def _lowered(headers: Mapping[str, str]) -> dict[str, str]:
    """Header names lowercased: Jira sends ``Retry-After`` after a 429."""
    return {name.lower(): value for name, value in headers.items()}


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def classify_jira_failure(
    status_code: int, headers: Mapping[str, str]
) -> Classification:
    """Classify one Jira API failure by status and header values (§14.4).

    The response body is not an argument on purpose: the text of the error
    message is never used.

    ``Retry-After`` is Jira's documented secondary-limit signal, so on a
    403 or 429 it takes precedence. A 429 without ``Retry-After`` is itself
    a rate signal and classifies as a secondary limit (exponential
    backoff). A 403 with no usable rate signal is a permission refusal,
    never a retry. A 401 classifies here as ``credential-failure``;
    ``request_with_retry`` then confirms it through the ``GET
    /rest/api/3/myself`` identity probe before raising. A 404 classifies
    as ``other``: reads handle absence themselves. A 5xx classifies as
    ``transient`` (§14.4).
    """
    if status_code == 401:
        return "credential-failure"
    if status_code == 404:
        return "other"
    if status_code not in (403, 429):
        if 500 <= status_code <= 599:
            return "transient"
        return "other"
    lowered = _lowered(headers)
    if _parse_float(lowered.get("retry-after")) is not None:
        return "secondary-rate-limit"
    if status_code == 429:
        return "secondary-rate-limit"
    return "permission-refusal"


def _retry_delay_seconds(
    classification: Classification,
    headers: Mapping[str, str],
    initial_backoff_seconds: float,
    attempts_made: int,
) -> float:
    """How long to wait before the next in-place attempt.

    Secondary: exactly ``Retry-After`` when present, else exponential
    backoff. A transient failure (§14.4) backs off exponentially from the
    initial backoff, like a secondary limit without ``Retry-After``. A
    ``Retry-After`` already past waits 0.
    """
    lowered = _lowered(headers)
    if classification == "secondary-rate-limit":
        retry_after = _parse_float(lowered.get("retry-after"))
        if retry_after is not None:
            return retry_after
    return initial_backoff_seconds * (2.0 ** (attempts_made - 1))


def _identity_probe_confirms(client: JiraClient) -> bool:
    """Whether a ``GET /rest/api/3/myself`` probe confirms the refusal.

    One unretried ``GET`` with the same client, reading only the status,
    never the body. ``True`` only when the probe itself returns 401; a
    success, any other status, or a network error means the original 401
    is not a Credential Failure. The probe is never the call being
    classified: a 401 on ``/myself`` is its own confirmation and never
    reaches this helper.
    """
    try:
        probe = client.request("GET", "/rest/api/3/myself")
    except Exception:
        return False
    return probe.status_code == 401


def _is_idempotent_request(method: str, path: str) -> bool:
    """Whether repeating the request cannot create a second object (§14.4).

    Reads (``GET``) and removals (``DELETE``) are safe to repeat, as are
    the label and assignee writes Jira applies idempotently (``PUT`` to
    the issue or its assignee sub-resource). Every ``POST`` — a comment,
    or the comment-property write beside it — creates an object, so only
    the effect driver may repeat it, after re-looking it up by Attempt
    Marker.
    """
    _ = path
    return method in ("GET", "DELETE", "PUT")


def request_with_retry(
    client: JiraClient,
    method: str,
    path: str,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_IN_PLACE_ATTEMPTS,
    max_wait_seconds: float = MAX_IN_PLACE_WAIT_SECONDS,
    initial_backoff_seconds: float = INITIAL_BACKOFF_SECONDS,
    raise_other: bool = True,
    **kwargs: Any,
) -> requests.Response:
    """One Jira API call with classification and bounded in-place retry.

    Success (any status below 400) returns the response untouched. A 401
    confirmed by the ``GET /rest/api/3/myself`` identity probe raises
    ``CredentialFailure``; an unconfirmed 401 — the probe succeeds or fails
    any other way — raises infrastructure-class ``JiraFailure`` instead. A
    permission-shaped 403 raises ``PermissionRefusal``; the confirmed 401
    and the refusal skip retry. Any other non-retryable status raises
    infrastructure-class ``JiraFailure``. Rate limits are retried in place
    within ``max_attempts`` total requests and ``max_wait_seconds`` total
    waiting; beyond either bound the failure raises ``RateLimitExhausted``
    (infrastructure-class ``jira``) without sleeping past the budget.

    A transient failure (§14.4) — a 5xx, a connection error or a timeout —
    is retried in place within the same bound, but only where a repeat
    cannot create a second object (reads, removals, label and assignee
    writes). A transient failure on a request that creates an object is
    handed back unretried (``raise_other=False``) or raised at once as
    infrastructure-class ``JiraFailure`` (``raise_other=True``): only the
    effect driver repeats those, after re-looking the write up by Attempt
    Marker. Exhausting the transient bound raises ``RateLimitExhausted``
    where a status was received, else infrastructure-class
    ``JiraFailure`` (no response ever arrived); both carry
    ``failure_class="jira"``.

    ``raise_other=False`` hands a response classified ``other`` (a 404, or
    any status outside 401/403/429/5xx) — and a transient response on a
    request that creates an object — back to the caller instead of raising,
    for callers that read those statuses themselves — the tracker
    adapter's 404-is-absent rules. 401, 403 and 429 are classified and
    retried the same either way.

    The retry blocks on ``sleep``, so every waited second counts against
    the Attempt wall clock observing the call. ``now`` and ``sleep`` are
    injectable so tests script the clock instead of sleeping.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
    attempts = 0
    waited_seconds = 0.0
    while True:
        try:
            response = client.request(method, path, **kwargs)
        except (
            requests.ConnectionError,
            requests.Timeout,
            ConnectionError,
            TimeoutError,
        ) as exc:
            # No response to hand back: only repeat where a repeat is
            # safe; otherwise the raw error propagates to the effect
            # driver, which re-looks the write up before repeating it.
            if not _is_idempotent_request(method, path):
                raise
            attempts += 1
            if attempts >= max_attempts:
                raise JiraFailure(
                    method,
                    path,
                    0,
                    reason="infrastructure-failure",
                    failure_class="jira",
                ) from exc
            delay = initial_backoff_seconds * (2.0 ** (attempts - 1))
            if waited_seconds + delay > max_wait_seconds:
                raise JiraFailure(
                    method,
                    path,
                    0,
                    reason="infrastructure-failure",
                    failure_class="jira",
                ) from exc
            sleep(delay)
            waited_seconds += delay
            continue
        attempts += 1
        if response.status_code < 400:
            return response
        classification = classify_jira_failure(response.status_code, response.headers)
        if classification == "credential-failure":
            if method == "GET" and path == "/rest/api/3/myself":
                # The probe is never the call being classified: a 401 on
                # ``/myself`` is its own confirmation.
                raise CredentialFailure(method, path, response.status_code)
            if _identity_probe_confirms(client):
                raise CredentialFailure(method, path, response.status_code)
            raise JiraFailure(
                method,
                path,
                response.status_code,
                reason="infrastructure-failure",
                failure_class="jira",
            )
        if classification == "permission-refusal":
            raise PermissionRefusal(method, path, response.status_code)
        if classification == "other":
            if not raise_other:
                return response
            raise JiraFailure(
                method,
                path,
                response.status_code,
                reason="infrastructure-failure",
                failure_class="jira",
            )
        if classification == "transient" and not _is_idempotent_request(
            method, path
        ):
            # A request that creates an object is never repeated blindly:
            # hand the transient response back (or raise it at once) so the
            # effect driver can re-look it up by Attempt Marker first.
            if not raise_other:
                return response
            raise JiraFailure(
                method,
                path,
                response.status_code,
                reason="infrastructure-failure",
                failure_class="jira",
            )
        if attempts >= max_attempts:
            raise RateLimitExhausted(
                method,
                path,
                response.status_code,
                attempts=attempts,
                waited_seconds=waited_seconds,
                failure_class="jira",
            )
        delay = _retry_delay_seconds(
            classification,
            response.headers,
            initial_backoff_seconds,
            attempts,
        )
        if waited_seconds + delay > max_wait_seconds:
            raise RateLimitExhausted(
                method,
                path,
                response.status_code,
                attempts=attempts,
                waited_seconds=waited_seconds,
                failure_class="jira",
            )
        sleep(delay)
        waited_seconds += delay
