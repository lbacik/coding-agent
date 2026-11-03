"""The GitHub adapter surface: the client plus the §13 failure classification.

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (rate limits and
refusals). MVP row ids: L3-ERR-3, L3-ERR-4, L3-ERR-5, L3-ERR-6.
"""

from coding_agent.github.client import GitHubClient
from coding_agent.github.errors import (
    CredentialFailure,
    GitHubFailure,
    PermissionRefusal,
    RateLimitExhausted,
    classify_github_failure,
    request_with_retry,
)

__all__ = [
    "CredentialFailure",
    "GitHubClient",
    "GitHubFailure",
    "PermissionRefusal",
    "RateLimitExhausted",
    "classify_github_failure",
    "request_with_retry",
]
