from datetime import datetime, timedelta, timezone

import pytest

from coding_agent.identity.token import (
    TokenRejected,
    assert_fine_grained_token,
    assert_no_oauth_scopes,
    assert_token_expiration,
    parse_expiration,
)


def test_fine_grained_token_accepted() -> None:
    assert_fine_grained_token("github_pat_11ABCDEF")


@pytest.mark.parametrize("token", ["ghp_classic123", "gho_oauth123", "", "not-a-token"])
def test_non_fine_grained_token_rejected(token: str) -> None:
    with pytest.raises(TokenRejected):
        assert_fine_grained_token(token)


def test_no_oauth_scopes_header_accepted() -> None:
    assert_no_oauth_scopes({"Content-Type": "application/json"})


def test_oauth_scopes_header_rejected() -> None:
    with pytest.raises(TokenRejected):
        assert_no_oauth_scopes({"X-OAuth-Scopes": "repo, workflow"})


def test_oauth_scopes_header_rejected_even_when_empty() -> None:
    with pytest.raises(TokenRejected):
        assert_no_oauth_scopes({"X-OAuth-Scopes": ""})


def test_parse_expiration_observed_format() -> None:
    parsed = parse_expiration("2026-09-08 12:00:00 UTC")
    assert parsed == datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_token_expiration_missing_header_rejected() -> None:
    with pytest.raises(TokenRejected):
        assert_token_expiration({}, threshold=timedelta(days=7))


def test_token_expiration_below_threshold_rejected() -> None:
    now = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)
    headers = {"github-authentication-token-expiration": "2026-09-10 00:00:00 UTC"}
    with pytest.raises(TokenRejected):
        assert_token_expiration(headers, threshold=timedelta(days=7), now=now)


def test_token_expiration_above_threshold_accepted() -> None:
    now = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)
    headers = {"github-authentication-token-expiration": "2027-09-10 00:00:00 UTC"}
    result = assert_token_expiration(headers, threshold=timedelta(days=7), now=now)
    assert result.expires_at == datetime(2027, 9, 10, tzinfo=timezone.utc)
    assert result.remaining > timedelta(days=300)
