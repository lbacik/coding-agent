import pytest

from coding_agent.identity.token import (
    TokenRejected,
    assert_fine_grained_token,
    assert_no_oauth_scopes,
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

