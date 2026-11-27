"""The Jira Cloud adapter surface: the client plus the failure classification.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14.4 (every
external write is an Effect Intent settled once; transient failures retry in
place within the shared bound) and [ADR
0038](../../../../docs/adr/0038-the-issue-tracker-and-the-code-host-are-reached-only-through-their-ports.md)
(the Jira tracker vocabulary: one pinned project, the opaque ``accountId``
identity, link-by-key delivery with no transition write).

Row ids: INP-1 … INP-14, INP-19, INP-20.
"""

from coding_agent.jira.client import JiraClient
from coding_agent.jira.errors import (
    CredentialFailure,
    JiraFailure,
    PermissionRefusal,
    RateLimitExhausted,
    classify_jira_failure,
    request_with_retry,
)

__all__ = [
    "CredentialFailure",
    "JiraClient",
    "JiraFailure",
    "PermissionRefusal",
    "RateLimitExhausted",
    "classify_jira_failure",
    "request_with_retry",
]
