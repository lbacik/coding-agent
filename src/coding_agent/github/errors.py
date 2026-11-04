"""GitHub API failure classification and bounded in-place retry.

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 ("Rate limits and
refusals": classification is by status, header value and documented error
signal, never by message text; in-place retry is bounded at 3 attempts and
5 minutes, and the waiting counts against the Attempt's wall clock; beyond
the bound the failure is infrastructure-class (``github``)).

MVP row ids: L3-ERR-3 (a 403 with ``x-ratelimit-remaining > 0`` is a
permission refusal with no retry), L3-ERR-4 (a 403 or 429 with remaining 0
waits until ``x-ratelimit-reset`` and retries in place within the bound),
L3-ERR-5 (a secondary rate limit with ``Retry-After`` waits exactly that
long), L3-ERR-6 (a secondary rate limit without ``Retry-After`` backs off
exponentially within the same bound).

A 401 classifies as a Credential Failure. Halting the Worker on it (P-WRK-1,
L3-ERR-8) belongs to the ``agent run`` slice, which reads
``CredentialFailure`` here.

This module sits under the ``GitHubClient`` every tracker operation uses:
future adapter slices call ``request_with_retry`` instead of
``GitHubClient.request`` and get the whole §13 table without extra work. The
response body is never inspected: only the status code and the rate-limit
header values decide, so tests inject the clock and the sleep and nothing
really sleeps and no network is used.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

import requests

from coding_agent.github.client import GitHubClient

Classification = Literal[
    "credential-failure",
    "permission-refusal",
    "primary-rate-limit",
    "secondary-rate-limit",
    "other",
]
"""Every GitHub API failure class §13 names, plus ``other`` for the rest.

``credential-failure`` and ``permission-refusal`` are terminal for the
Attempt; ``primary-rate-limit`` and ``secondary-rate-limit`` are transient
and retried in place; ``other`` (any status outside 401/403/429) is an
infrastructure-class failure with no retry.
"""

MAX_IN_PLACE_ATTEMPTS = 3
"""Total requests per operation before the bound is exhausted (contract §13)."""

MAX_IN_PLACE_WAIT_SECONDS = 5 * 60.0
"""Total waited seconds per operation before the bound is exhausted (§13)."""

INITIAL_BACKOFF_SECONDS = 1.0
"""First wait of the exponential backoff for a secondary limit without ``Retry-After``."""


class GitHubFailure(Exception):
    """A classified GitHub API failure.

    ``reason`` is the outcome reason the Attempt records (``failed`` is the
    Terminal Outcome for every pre-recording failure, contract §14);
    ``failure_class`` is ``github`` for infrastructure-class failures. Only
    exception text derived from the status, the path and the header values
    is stored: the response body is never copied here.
    """

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        *,
        reason: str,
        failure_class: str,
    ) -> None:
        super().__init__(f"{method} {path} returned {status_code} ({reason})")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.reason = reason
        self.failure_class = failure_class


class CredentialFailure(GitHubFailure):
    """A 401: the token no longer authenticates (contract §13).

    Its own class, because the Worker halts on it (P-WRK-1); confirming the
    refusal through the identity probe and halting both belong to the
    ``agent run`` slice, which reads this class from here.
    """

    def __init__(self, method: str, path: str, status_code: int = 401) -> None:
        super().__init__(
            method,
            path,
            status_code,
            reason="credential-failure",
            failure_class="credential",
        )


class PermissionRefusal(GitHubFailure):
    """A 403 the rate headers do not explain (contract §13, L3-ERR-3).

    Non-transient: the Attempt ends ``failed`` and the Worker continues. The
    error text is never consulted, so a refusal carrying rate-sounding words
    still lands here when ``x-ratelimit-remaining > 0`` (or is absent).
    """

    def __init__(self, method: str, path: str, status_code: int = 403) -> None:
        super().__init__(
            method,
            path,
            status_code,
            reason="permission-refusal",
            failure_class="permission",
        )


class RateLimitExhausted(GitHubFailure):
    """A transient limit whose in-place retry hit the 3-attempt / 5-minute
    bound (contract §13): infrastructure-class (``github``)."""

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        *,
        attempts: int,
        waited_seconds: float,
    ) -> None:
        super().__init__(
            method,
            path,
            status_code,
            reason="infrastructure-failure",
            failure_class="github",
        )
        self.attempts = attempts
        self.waited_seconds = waited_seconds


def _lowered(headers: Mapping[str, str]) -> dict[str, str]:
    """Header names lowercased: GitHub sends ``X-RateLimit-*`` and ``Retry-After``."""
    return {name.lower(): value for name, value in headers.items()}


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


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


def classify_github_failure(
    status_code: int, headers: Mapping[str, str]
) -> Classification:
    """Classify one GitHub API failure by status and header values (contract
    §13, L3-ERR-3…6). The response body is not an argument on purpose: the
    text of the error message is never used.

    ``Retry-After`` is GitHub's documented secondary-limit signal, so on a
    403 or 429 it takes precedence over ``x-ratelimit-remaining``. A 429
    without ``Retry-After`` is itself a rate signal and classifies as a
    secondary limit (L3-ERR-6 backoff). A 403 with no usable rate signal —
    positive, missing or unparseable ``remaining`` and no ``Retry-After`` —
    is a permission refusal (L3-ERR-3), never a retry.
    """
    if status_code == 401:
        return "credential-failure"
    if status_code not in (403, 429):
        return "other"
    lowered = _lowered(headers)
    if _parse_float(lowered.get("retry-after")) is not None:
        return "secondary-rate-limit"
    if _parse_int(lowered.get("x-ratelimit-remaining")) == 0:
        return "primary-rate-limit"
    if status_code == 429:
        return "secondary-rate-limit"
    return "permission-refusal"


def _retry_delay_seconds(
    classification: Classification,
    headers: Mapping[str, str],
    now: Callable[[], float],
    initial_backoff_seconds: float,
    attempts_made: int,
) -> float:
    """How long to wait before the next in-place attempt.

    Primary: until ``x-ratelimit-reset`` (L3-ERR-4). Secondary: exactly
    ``Retry-After`` when present (L3-ERR-5), else exponential backoff
    (L3-ERR-6). A primary limit with a missing or unparseable reset falls
    back to the backoff rather than spinning; a reset already past waits 0.
    """
    lowered = _lowered(headers)
    if classification == "primary-rate-limit":
        reset = _parse_int(lowered.get("x-ratelimit-reset"))
        if reset is not None:
            return max(0.0, float(reset) - now())
    elif classification == "secondary-rate-limit":
        retry_after = _parse_float(lowered.get("retry-after"))
        if retry_after is not None:
            return retry_after
    return initial_backoff_seconds * (2.0 ** (attempts_made - 1))


def request_with_retry(
    client: GitHubClient,
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
    """One GitHub API call with the §13 classification and bounded in-place
    retry (MVP rows L3-ERR-3…6).

    Success (any status below 400) returns the response untouched. A 401
    raises ``CredentialFailure`` and a permission-shaped 403 raises
    ``PermissionRefusal``, both with no retry; any other non-retryable
    status raises infrastructure-class ``GitHubFailure``. Primary and
    secondary limits are retried in place within ``max_attempts`` total
    requests and ``max_wait_seconds`` total waiting; beyond either bound the
    failure raises ``RateLimitExhausted`` (infrastructure-class ``github``)
    without sleeping past the budget.

    ``raise_other=False`` hands a response classified ``other`` (any status
    outside 401/403/429, such as a 404 or a 5xx) back to the caller instead
    of raising, for callers that read those statuses themselves — the
    tracker adapter's 404-is-absent rules. 401, 403 and 429 are classified
    and retried the same either way.

    The retry blocks on ``sleep``, so every waited second counts against the
    Attempt wall clock observing the call. ``now`` and ``sleep`` are
    injectable so tests script the clock instead of sleeping.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
    attempts = 0
    waited_seconds = 0.0
    while True:
        response = client.request(method, path, **kwargs)
        attempts += 1
        if response.status_code < 400:
            return response
        classification = classify_github_failure(response.status_code, response.headers)
        if classification == "credential-failure":
            raise CredentialFailure(method, path, response.status_code)
        if classification == "permission-refusal":
            raise PermissionRefusal(method, path, response.status_code)
        if classification == "other":
            if not raise_other:
                return response
            raise GitHubFailure(
                method,
                path,
                response.status_code,
                reason="infrastructure-failure",
                failure_class="github",
            )
        if attempts >= max_attempts:
            raise RateLimitExhausted(
                method,
                path,
                response.status_code,
                attempts=attempts,
                waited_seconds=waited_seconds,
            )
        delay = _retry_delay_seconds(
            classification,
            response.headers,
            now,
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
            )
        sleep(delay)
        waited_seconds += delay
