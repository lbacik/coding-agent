from __future__ import annotations

from collections.abc import Mapping

FINE_GRAINED_PREFIX = "github_pat_"


class TokenRejected(Exception):
    """A credential failed one of the startup token assertions (contract §10)."""


def assert_fine_grained_token(token: str) -> None:
    """Refuse anything but a fine-grained PAT, before any network call.

    A classic (`ghp_`) or OAuth (`gho_`) token cannot have the Workflows
    permission excluded from outside, so it is refused on sight.
    """
    if not token.startswith(FINE_GRAINED_PREFIX):
        raise TokenRejected(
            f"credential must use the {FINE_GRAINED_PREFIX!r} prefix; "
            "a classic (ghp_) or OAuth (gho_) token is refused"
        )


def assert_no_oauth_scopes(headers: Mapping[str, str]) -> None:
    """Refuse a token whose `GET /user` response carries `X-OAuth-Scopes`.

    Its presence means a scoped (classic or OAuth) token was supplied,
    whose `workflow` scope cannot be excluded from outside.
    """
    if "X-OAuth-Scopes" in headers:
        raise TokenRejected(
            "GET /user carried an X-OAuth-Scopes header, meaning a scoped "
            "(classic or OAuth) token was supplied instead of a fine-grained one"
        )

