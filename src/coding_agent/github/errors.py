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
exponentially within the same bound), C-ERR-2 (a 401 is a Credential
Failure only when the ``GET /user`` identity probe also returns 401;
otherwise it is infrastructure-class ``github``).

A 401 classifies as a Credential Failure only when the identity probe
confirms it (contract §13). Halting the Worker on it (P-WRK-1, L3-ERR-8)
belongs to the ``agent run`` slice, which reads ``CredentialFailure`` here.

A 5xx response, a connection error and a timeout are transient (step-workflow
contract §14.4): retried in place within the same bound, but only where a
repeat cannot create a second object. Requests that create an object are
handed back unretried, so the effect driver can re-look them up by Attempt
Marker before repeating them.

This module sits under the ``GitHubClient`` every tracker operation uses:
the GitHub adapter and the Target Issue read call ``request_with_retry``
instead of ``GitHubClient.request`` (with ``raise_other=False``, so the
statuses they read themselves come back to them) and get the whole §13
table without extra work. The
response body is never inspected: only the status code and the rate-limit
header values decide, so tests inject the clock and the sleep and nothing
really sleeps and no network is used.

The failure types themselves are canonical in
``coding_agent.inputs.errors``: this module keeps only the GitHub-specific
translation logic (``classify_github_failure`` and ``request_with_retry``),
which raises the neutral objects directly. The old adapter names are
re-exported here as aliases for one release — ``GitHubFailure`` is the
very same class object as ``HostFailure`` — so every existing ``isinstance``
check keeps passing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.inputs.errors import (
    INITIAL_BACKOFF_SECONDS,
    MAX_IN_PLACE_ATTEMPTS,
    MAX_IN_PLACE_WAIT_SECONDS,
    CredentialFailure,
    HostFailure,
    PermissionRefusal,
    RateLimitExhausted,
)

__all__ = [
    "CredentialFailure",
    "GitHubFailure",
    "HostFailure",
    "INITIAL_BACKOFF_SECONDS",
    "MAX_IN_PLACE_ATTEMPTS",
    "MAX_IN_PLACE_WAIT_SECONDS",
    "PermissionRefusal",
    "RateLimitExhausted",
    "classify_github_failure",
    "request_with_retry",
]

GitHubFailure = HostFailure
"""Deprecated alias of :class:`HostFailure` (``coding_agent.inputs.errors``).

Kept for one release so ``from coding_agent.github.errors import
GitHubFailure`` keeps resolving to the very same class object; new code
imports ``HostFailure`` from ``coding_agent.inputs.errors`` instead.
"""

Classification = Literal[
    "credential-failure",
    "permission-refusal",
    "primary-rate-limit",
    "secondary-rate-limit",
    "transient",
    "other",
]
"""Every GitHub API failure class §13 and step-workflow §14.4 name, plus
``other`` for the rest.

``credential-failure`` and ``permission-refusal`` are terminal for the
Attempt; ``primary-rate-limit``, ``secondary-rate-limit`` and ``transient``
are retried in place within the bound (§14.4: a 5xx, a connection error or
a timeout); ``other`` (any status outside 401/403/429/5xx) is an
infrastructure-class failure with no retry.
"""


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
    is a permission refusal (L3-ERR-3), never a retry. A 401 classifies here
    as ``credential-failure``; ``request_with_retry`` then confirms it
    through the ``GET /user`` identity probe (C-ERR-2) before raising. A
    5xx classifies as ``transient`` (step-workflow contract §14.4).
    """
    if status_code == 401:
        return "credential-failure"
    if status_code not in (403, 429):
        if 500 <= status_code <= 599:
            return "transient"
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
    (L3-ERR-6). A transient failure (§14.4) backs off exponentially from the
    initial backoff, like a secondary limit without ``Retry-After``. A
    primary limit with a missing or unparseable reset falls back to the
    backoff rather than spinning; a reset already past waits 0.
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


def _identity_probe_confirms(client: GitHubClient) -> bool:
    """Whether a ``GET /user`` probe confirms the token is refused (§13, C-ERR-2).

    One unretried ``GET /user`` with the same client, reading only the
    status, never the body. ``True`` only when the probe itself returns 401;
    a success, any other status, or a network error means the original 401
    is not a Credential Failure. The probe is never the call being
    classified: a 401 on ``GET /user`` is its own confirmation and never
    reaches this helper.
    """
    try:
        probe = client.request("GET", "/user")
    except Exception:
        return False
    return probe.status_code == 401


def _is_idempotent_request(method: str, path: str) -> bool:
    """Whether repeating the request cannot create a second object (§14.4).

    Reads (``GET``) and removals (``DELETE``) are safe to repeat, as are the
    label and assignee writes GitHub applies idempotently (``POST`` to a
    ``.../labels`` or ``.../assignees`` endpoint). Every other mutating
    request — a comment, a review, a pull request — creates an object, so
    only the effect driver may repeat it, after re-looking it up by Attempt
    Marker.
    """
    if method in ("GET", "DELETE"):
        return True
    if method == "POST" and (
        path.endswith("/labels") or path.endswith("/assignees")
    ):
        return True
    return False


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
    retry (MVP rows L3-ERR-3…6, C-ERR-2), plus the §14.4 transient class.

    Success (any status below 400) returns the response untouched. A 401
    confirmed by the ``GET /user`` identity probe raises
    ``CredentialFailure``; an unconfirmed 401 — the probe succeeds or fails
    any other way — raises infrastructure-class ``GitHubFailure`` instead
    (C-ERR-2). A permission-shaped 403 raises ``PermissionRefusal``; the
    confirmed 401 and the refusal skip retry. Any other non-retryable
    status raises infrastructure-class ``GitHubFailure``. Primary and
    secondary limits are retried in place within ``max_attempts`` total
    requests and ``max_wait_seconds`` total waiting; beyond either bound the
    failure raises ``RateLimitExhausted`` (infrastructure-class ``github``)
    without sleeping past the budget.

    A transient failure (§14.4) — a 5xx, a connection error or a timeout —
    is retried in place within the same bound, but only where a repeat
    cannot create a second object (reads, removals, label and assignee
    writes). A transient failure on a request that creates an object is
    handed back unretried (``raise_other=False``) or raised at once as
    infrastructure-class ``GitHubFailure`` (``raise_other=True``): only the
    effect driver repeats those, after re-looking the write up by Attempt
    Marker. Exhausting the transient bound raises ``RateLimitExhausted``
    where a status was received, else infrastructure-class
    ``GitHubFailure`` (no response ever arrived); both carry
    ``failure_class="github"``.

    ``raise_other=False`` hands a response classified ``other`` (any status
    outside 401/403/429/5xx, such as a 404) — and a transient response on a
    request that creates an object — back to the caller instead of raising,
    for callers that read those statuses themselves — the tracker adapter's
    404-is-absent rules. 401, 403 and 429 are classified and retried the
    same either way.

    The retry blocks on ``sleep``, so every waited second counts against the
    Attempt wall clock observing the call. ``now`` and ``sleep`` are
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
                raise GitHubFailure(
                    method,
                    path,
                    0,
                    reason="infrastructure-failure",
                    failure_class="github",
                ) from exc
            delay = initial_backoff_seconds * (2.0 ** (attempts - 1))
            if waited_seconds + delay > max_wait_seconds:
                raise GitHubFailure(
                    method,
                    path,
                    0,
                    reason="infrastructure-failure",
                    failure_class="github",
                ) from exc
            sleep(delay)
            waited_seconds += delay
            continue
        attempts += 1
        if response.status_code < 400:
            return response
        classification = classify_github_failure(response.status_code, response.headers)
        if classification == "credential-failure":
            if method == "GET" and path == "/user":
                # The probe is never the call being classified: a 401 on
                # ``GET /user`` is its own confirmation (§13, C-ERR-2).
                raise CredentialFailure(method, path, response.status_code)
            if _identity_probe_confirms(client):
                raise CredentialFailure(method, path, response.status_code)
            raise GitHubFailure(
                method,
                path,
                response.status_code,
                reason="infrastructure-failure",
                failure_class="github",
            )
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
        if classification == "transient" and not _is_idempotent_request(
            method, path
        ):
            # A request that creates an object is never repeated blindly:
            # hand the transient response back (or raise it at once) so the
            # effect driver can re-look it up by Attempt Marker first.
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
