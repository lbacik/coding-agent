from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FINE_GRAINED_PREFIX = "github_pat_"
EXPIRATION_HEADER = "github-authentication-token-expiration"

DEFAULT_EXPIRATION_THRESHOLD = timedelta(days=7)

# Observed as "2026-09-08 12:00:00 UTC"; not a documented format (contract §10),
# so this is confirmed empirically against the live API rather than assumed.
_EXPIRATION_FORMAT = "%Y-%m-%d %H:%M:%S %z"


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


def parse_expiration(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith(" UTC"):
        value = value[: -len(" UTC")] + " +0000"
    return datetime.strptime(value, _EXPIRATION_FORMAT)


@dataclass(frozen=True)
class TokenExpiration:
    expires_at: datetime
    remaining: timedelta


def assert_token_expiration(
    headers: Mapping[str, str],
    *,
    threshold: timedelta = DEFAULT_EXPIRATION_THRESHOLD,
    now: datetime | None = None,
) -> TokenExpiration:
    """Refuse a token below the configured remaining-lifetime threshold.

    `github-authentication-token-expiration` is present only for fine-grained
    tokens; its absence is itself refused, since a Worker must not run past
    an expiration it cannot see.
    """
    raw = headers.get(EXPIRATION_HEADER)
    if raw is None:
        raise TokenRejected(
            f"GET /user carried no {EXPIRATION_HEADER!r} header; "
            "a fine-grained token's expiration could not be confirmed"
        )
    expires_at = parse_expiration(raw)
    reference = now or datetime.now(timezone.utc)
    remaining = expires_at - reference
    if remaining < threshold:
        raise TokenRejected(
            f"token expires at {expires_at.isoformat()}, {remaining} from now, "
            f"below the {threshold} threshold"
        )
    return TokenExpiration(expires_at=expires_at, remaining=remaining)
