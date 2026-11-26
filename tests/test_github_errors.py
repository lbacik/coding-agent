"""GitHub API failure classification and bounded in-place retry (TDD seam).

Covers ``docs/contract/mvp-runtime-contract.md`` §13 and MVP rows L3-ERR-3
(permission refusal, no retry), L3-ERR-4 (primary rate limit waits until
``x-ratelimit-reset``), L3-ERR-5 (secondary with ``Retry-After`` waits
exactly that long), L3-ERR-6 (secondary without ``Retry-After`` backs off
exponentially) and C-ERR-2 (a 401 is a Credential Failure only when the
``GET /user`` identity probe also returns 401; otherwise it is
infrastructure-class ``github``). The clock and the sleep are injected, so
no test ever sleeps and no test touches the network: ``requests_mock``
queues the scripted responses in order.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from coding_agent.github.client import GitHubClient
from coding_agent.github.errors import (
    CredentialFailure,
    GitHubFailure,
    PermissionRefusal,
    RateLimitExhausted,
    classify_github_failure,
    request_with_retry,
)

TEST_BASE_URL = "https://api.github.test"
ISSUE_PATH = "/repos/octocat/sandbox/issues/30"
ISSUE_URL = f"{TEST_BASE_URL}{ISSUE_PATH}"


class FakeClock:
    """An injected wall clock: ``sleep`` records the wait and advances ``now``."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now_value = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.now_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_value += seconds


def _ok_body() -> dict[str, object]:
    return {"number": 30, "title": "S3.1: agent implement skeleton", "body": "..."}


# Classification: status, header value and documented signal only.


def test_classify_401_is_credential_failure() -> None:
    assert classify_github_failure(401, {}) == "credential-failure"


def test_classify_403_with_remaining_is_permission_refusal() -> None:
    assert (
        classify_github_failure(403, {"x-ratelimit-remaining": "5"})
        == "permission-refusal"
    )


def test_classify_never_reads_the_message_text() -> None:
    """The body is not even an argument: a secondary-sounding message with a
    permission-shaped status and headers still classifies as a refusal."""
    assert (
        classify_github_failure(403, {"x-ratelimit-remaining": "42"})
        == "permission-refusal"
    )


def test_classify_403_with_remaining_zero_is_primary() -> None:
    assert (
        classify_github_failure(
            403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000060"}
        )
        == "primary-rate-limit"
    )


def test_classify_429_with_remaining_zero_is_primary() -> None:
    assert (
        classify_github_failure(429, {"x-ratelimit-remaining": "0"})
        == "primary-rate-limit"
    )


def test_classify_retry_after_takes_precedence_as_secondary() -> None:
    """``Retry-After`` is the documented secondary-limit signal, so it wins
    over a zero ``remaining``: the wait honoured is the header's, exactly."""
    assert (
        classify_github_failure(
            403,
            {
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1700000060",
                "retry-after": "120",
            },
        )
        == "secondary-rate-limit"
    )


def test_classify_429_without_any_signal_is_secondary() -> None:
    """429 is itself a rate signal, so without ``Retry-After`` it backs off
    rather than classifying as a refusal."""
    assert classify_github_failure(429, {}) == "secondary-rate-limit"


def test_classify_403_without_headers_is_permission_refusal() -> None:
    """Mere presence of rate-limit headers never decides: a bare 403 is a
    refusal, never a retry."""
    assert classify_github_failure(403, {}) == "permission-refusal"


def test_classify_unparseable_remaining_is_permission_refusal() -> None:
    assert (
        classify_github_failure(403, {"x-ratelimit-remaining": "lots"})
        == "permission-refusal"
    )


def test_classify_header_names_are_case_insensitive() -> None:
    assert (
        classify_github_failure(
            403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000060"}
        )
        == "primary-rate-limit"
    )
    assert (
        classify_github_failure(403, {"Retry-After": "30"})
        == "secondary-rate-limit"
    )


def test_classify_other_statuses_are_other() -> None:
    assert classify_github_failure(200, {}) == "other"
    assert classify_github_failure(404, {}) == "other"
    assert classify_github_failure(422, {}) == "other"


def test_classify_5xx_statuses_are_transient() -> None:
    """§14.4: a 5xx response is transient, whatever the message text."""
    assert classify_github_failure(500, {}) == "transient"
    assert classify_github_failure(502, {}) == "transient"
    assert classify_github_failure(503, {}) == "transient"
    assert classify_github_failure(504, {}) == "transient"
    assert classify_github_failure(599, {}) == "transient"


# In-place retry through the client.


def test_success_needs_no_retry(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, json=_ok_body())

    response = request_with_retry(client, "GET", "/repos/octocat/sandbox/issues/30")

    assert response.status_code == 200
    assert clock.sleeps == []


def test_permission_refusal_never_retries(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        status_code=403,
        headers={"x-ratelimit-remaining": "5"},
        json={"message": "Resource not accessible by personal access token"},
    )

    with pytest.raises(PermissionRefusal):
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def _probe_returns_401(requests_mock: Any) -> None:
    """The identity probe confirms the refusal (C-ERR-2)."""
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        status_code=401,
        json={"message": "Bad credentials"},
    )


def _assert_unconfirmed_is_infrastructure(exc: GitHubFailure) -> None:
    """An unconfirmed 401 is infrastructure-class ``github`` (C-ERR-2)."""
    assert not isinstance(exc, CredentialFailure)
    assert exc.failure_class == "github"
    assert exc.reason == "infrastructure-failure"


def test_credential_failure_never_retries(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=401, json={"message": "Bad credentials"})
    _probe_returns_401(requests_mock)

    with pytest.raises(CredentialFailure):
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert requests_mock.call_count == 2
    assert clock.sleeps == []


def test_confirmed_401_reports_the_original_call(
    client: GitHubClient, requests_mock: Any
) -> None:
    """C-ERR-2: the confirmed failure names the original call, not the probe."""
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=401, json={"message": "Bad credentials"})
    _probe_returns_401(requests_mock)

    with pytest.raises(CredentialFailure) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert (exc_info.value.method, exc_info.value.path) == ("GET", ISSUE_PATH)


def test_unconfirmed_401_when_the_probe_succeeds_is_infrastructure(
    client: GitHubClient, requests_mock: Any
) -> None:
    """C-ERR-2: the probe succeeds, so the token still authenticates and the
    original 401 is infrastructure-class ``github``, never a halt."""
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=401, json={"message": "Bad credentials"})
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        status_code=200,
        json={"id": 1, "login": "octocat"},
    )

    with pytest.raises(GitHubFailure) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    _assert_unconfirmed_is_infrastructure(exc_info.value)
    assert requests_mock.call_count == 2
    assert clock.sleeps == []


def test_unconfirmed_401_when_the_probe_fails_another_way_is_infrastructure(
    client: GitHubClient, requests_mock: Any
) -> None:
    """C-ERR-2: the probe 500s, so the 401 stays unconfirmed — an unconfirmed
    401 never halts."""
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=401, json={"message": "Bad credentials"})
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        status_code=500,
        json={"message": "Server Error"},
    )

    with pytest.raises(GitHubFailure) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    _assert_unconfirmed_is_infrastructure(exc_info.value)
    assert clock.sleeps == []


def test_unconfirmed_401_when_the_probe_errors_on_the_network_is_infrastructure(
    client: GitHubClient, requests_mock: Any
) -> None:
    """C-ERR-2: a probe network error cannot confirm the refusal, so the 401
    is infrastructure-class ``github``."""
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=401, json={"message": "Bad credentials"})
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        exc=requests.exceptions.ConnectionError("boom"),
    )

    with pytest.raises(GitHubFailure) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    _assert_unconfirmed_is_infrastructure(exc_info.value)
    assert clock.sleeps == []


def test_a_401_on_get_user_is_its_own_confirmation(
    client: GitHubClient, requests_mock: Any
) -> None:
    """C-ERR-2: the probe is never the call being classified — a 401 on
    ``GET /user`` raises ``CredentialFailure`` with no second call."""
    clock = FakeClock()
    requests_mock.get(
        f"{TEST_BASE_URL}/user",
        status_code=401,
        json={"message": "Bad credentials"},
    )

    with pytest.raises(CredentialFailure):
        request_with_retry(
            client,
            "GET",
            "/user",
            now=clock.now,
            sleep=clock.sleep,
        )

    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_primary_rate_limit_waits_until_reset(
    client: GitHubClient, requests_mock: Any
) -> None:
    """L3-ERR-4: a 403 with remaining 0 waits until ``x-ratelimit-reset``
    and retries in place."""
    clock = FakeClock()
    reset = str(int(clock.now_value + 60))
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": reset,
                },
                "json": {"message": "API rate limit exceeded"},
            },
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert clock.sleeps == [60.0]
    assert requests_mock.call_count == 2


def test_primary_rate_limit_on_429_waits_until_reset(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    reset = str(int(clock.now_value + 45))
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 429,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": reset,
                },
                "json": {},
            },
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert clock.sleeps == [45.0]


def test_attempt_bound_exhausts_after_three_attempts(
    client: GitHubClient, requests_mock: Any
) -> None:
    """The attempt bound: two waits, three requests, then infrastructure."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(clock.now_value + 60)),
                },
                "json": {},
            },
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(clock.now_value + 120)),
                },
                "json": {},
            },
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(clock.now_value + 180)),
                },
                "json": {},
            },
        ],
    )

    with pytest.raises(RateLimitExhausted):
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert requests_mock.call_count == 3
    assert clock.sleeps == [60.0, 60.0]


def test_time_bound_exhausts_without_sleeping_past_it(
    client: GitHubClient, requests_mock: Any
) -> None:
    """The 5-minute bound: a reset an hour out fails fast instead of waiting."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        status_code=403,
        headers={
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(clock.now_value + 3600)),
        },
        json={"message": "API rate limit exceeded"},
    )

    with pytest.raises(RateLimitExhausted):
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_exhaustion_is_infrastructure_class_github(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        status_code=429,
        headers={"retry-after": "600"},
        json={"message": "You have exceeded a secondary rate limit"},
    )

    with pytest.raises(RateLimitExhausted) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert exc_info.value.failure_class == "github"
    assert exc_info.value.reason == "infrastructure-failure"


def test_secondary_with_retry_after_waits_exactly(
    client: GitHubClient, requests_mock: Any
) -> None:
    """L3-ERR-5: ``Retry-After`` is honoured to the second, even with a
    positive ``remaining``."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "7",
                    "retry-after": "120",
                },
                "json": {"message": "You have exceeded a secondary rate limit"},
            },
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert clock.sleeps == [120.0]


def test_secondary_without_retry_after_backs_off_exponentially(
    client: GitHubClient, requests_mock: Any
) -> None:
    """L3-ERR-6: a 429 with no ``Retry-After`` waits 1s, then 2s."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {"status_code": 429, "json": {}},
            {"status_code": 429, "json": {}},
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert clock.sleeps == [1.0, 2.0]


def test_invalid_retry_after_falls_back_to_backoff(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 429,
                "headers": {"retry-after": "soon"},
                "json": {},
            },
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert clock.sleeps == [1.0]


def test_reset_in_the_past_retries_immediately(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(clock.now_value - 10)),
                },
                "json": {},
            },
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert clock.sleeps == [0.0]


def test_other_status_is_infrastructure_without_retry(
    client: GitHubClient, requests_mock: Any
) -> None:
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=422, json={"message": "boom"})

    with pytest.raises(GitHubFailure) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert exc_info.value.failure_class == "github"
    assert exc_info.value.reason == "infrastructure-failure"
    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_transient_on_a_safe_request_retries_then_exhausts(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a 5xx on a repeat-safe request retries in place within the
    bound; beyond it the failure is infrastructure-class ``github``."""
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=503, json={"message": "boom"})

    with pytest.raises(RateLimitExhausted) as exc_info:
        request_with_retry(
            client,
            "GET",
            ISSUE_PATH,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert exc_info.value.failure_class == "github"
    assert exc_info.value.reason == "infrastructure-failure"
    assert requests_mock.call_count == 3
    assert clock.sleeps == [1.0, 2.0]


def test_transient_on_a_safe_request_succeeds_without_a_lookup(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a 5xx on a GET retries in place and returns the success."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {"status_code": 503, "json": {"message": "boom"}},
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert requests_mock.call_count == 2
    assert clock.sleeps == [1.0]


def test_transient_on_a_label_write_retries_in_place(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a 5xx on an idempotent label write retries in place."""
    clock = FakeClock()
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/labels",
        [
            {"status_code": 502, "json": {"message": "boom"}},
            {"status_code": 200, "json": {}},
        ],
    )

    response = request_with_retry(
        client,
        "POST",
        "/repos/octocat/sandbox/issues/30/labels",
        now=clock.now,
        sleep=clock.sleep,
        json={"labels": ["ready-for-human"]},
    )

    assert response.status_code == 200
    assert requests_mock.call_count == 2
    assert clock.sleeps == [1.0]


def test_transient_on_an_object_creating_request_is_not_repeated(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a 5xx on a request that creates an object is handed back
    unretried under ``raise_other=False``: only the effect driver repeats
    it, after re-looking it up by Attempt Marker."""
    clock = FakeClock()
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/comments",
        status_code=503,
        json={"message": "boom"},
    )

    response = request_with_retry(
        client,
        "POST",
        "/repos/octocat/sandbox/issues/30/comments",
        now=clock.now,
        sleep=clock.sleep,
        raise_other=False,
        json={"body": "hello"},
    )

    assert response.status_code == 503
    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_transient_on_an_object_creating_request_raises_at_once(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: the same request under ``raise_other=True`` raises at once
    as infrastructure-class ``github`` instead of retrying blindly."""
    clock = FakeClock()
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/comments",
        status_code=504,
        json={"message": "boom"},
    )

    with pytest.raises(GitHubFailure) as exc_info:
        request_with_retry(
            client,
            "POST",
            "/repos/octocat/sandbox/issues/30/comments",
            now=clock.now,
            sleep=clock.sleep,
            json={"body": "hello"},
        )

    assert exc_info.value.failure_class == "github"
    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_connection_error_on_a_safe_request_retries_like_a_5xx(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a connection error is the same transient class as a 5xx."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {"exc": requests.ConnectionError("connection reset")},
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert requests_mock.call_count == 2
    assert clock.sleeps == [1.0]


def test_builtin_network_error_on_a_safe_request_retries_like_a_5xx(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a builtin connection error or timeout that escapes the transport
    unwrapped retries in place just like a 5xx."""
    clock = FakeClock()
    requests_mock.get(
        ISSUE_URL,
        [
            {"exc": ConnectionError("connection reset by peer")},
            {"exc": TimeoutError("timed out")},
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert response.status_code == 200
    assert requests_mock.call_count == 3
    assert clock.sleeps == [1.0, 2.0]


def test_timeout_on_an_object_creating_request_propagates_to_the_driver(
    client: GitHubClient, requests_mock: Any
) -> None:
    """§14.4: a timeout on a request that creates an object propagates raw:
    the effect driver re-looks the write up before repeating it."""
    clock = FakeClock()
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/comments",
        exc=requests.Timeout("timed out"),
    )

    with pytest.raises(requests.Timeout):
        request_with_retry(
            client,
            "POST",
            "/repos/octocat/sandbox/issues/30/comments",
            now=clock.now,
            sleep=clock.sleep,
            raise_other=False,
            json={"body": "hello"},
        )

    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_404_is_returned_unretried(
    client: GitHubClient, requests_mock: Any
) -> None:
    """``raise_other=False`` still hands a 404 back after one request, so
    the tracker adapter's 404-is-absent rules keep working."""
    clock = FakeClock()
    requests_mock.get(ISSUE_URL, status_code=404, json={"message": "Not Found"})

    response = request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
        raise_other=False,
    )

    assert response.status_code == 404
    assert requests_mock.call_count == 1
    assert clock.sleeps == []


def test_wait_is_charged_to_the_wall_clock(
    client: GitHubClient, requests_mock: Any
) -> None:
    """The retry blocks on the injected sleep, so the Attempt wall clock
    observing the call sees every waited second."""
    clock = FakeClock()
    start = clock.now()
    reset = str(int(start + 60))
    requests_mock.get(
        ISSUE_URL,
        [
            {
                "status_code": 403,
                "headers": {
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": reset,
                },
                "json": {},
            },
            {"status_code": 200, "json": _ok_body()},
        ],
    )

    request_with_retry(
        client,
        "GET",
        ISSUE_PATH,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert clock.now() - start == 60.0
    assert sum(clock.sleeps) == 60.0
