"""Provider-neutral tracker/host failure types for the step-workflow runtime.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14.4 and
``docs/contract/mvp-runtime-contract.md`` §13 (rate limits and refusals),
plus step-workflow §13/§18 (a confirmed bad credential halts the Worker; a
permission refusal ends the Attempt ``failed``).
This module is the single canonical home for the failure vocabulary the
core programs against: the effect intent driver, the step publication /
recovery / finalisation paths and the worker run loop import these names,
never the concrete adapter's. The GitHub adapter's translation logic
(``coding_agent.github.errors``) raises these objects directly and
re-exports the old adapter names as aliases for one release, so every
existing ``isinstance`` check keeps passing: there is exactly one class
object per name.

Row ids: L3-ERR-3…6, C-ERR-2 (classification outcomes and reason /
failure-class values are unchanged from the adapter originals);
INP-1 … INP-20, OP-14 (with S7b), REC-22 (port operations raising them).
"""

from __future__ import annotations

from coding_agent.inputs.retry import (
    INITIAL_BACKOFF_SECONDS,
    MAX_IN_PLACE_ATTEMPTS,
    MAX_IN_PLACE_WAIT_SECONDS,
)

__all__ = [
    "CredentialFailure",
    "HostFailure",
    "INITIAL_BACKOFF_SECONDS",
    "InputError",
    "InputNotFound",
    "MAX_IN_PLACE_ATTEMPTS",
    "MAX_IN_PLACE_WAIT_SECONDS",
    "PermissionRefusal",
    "RateLimitExhausted",
    "StepTrackerError",
    "TransientInputError",
]


class HostFailure(Exception):
    """A classified host API failure.

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
        transient: bool = False,
    ) -> None:
        super().__init__(f"{method} {path} returned {status_code} ({reason})")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.reason = reason
        self.failure_class = failure_class
        self.transient = transient


InputError = HostFailure
"""Alias of :class:`HostFailure`: the neutral port-operation failure.

The Workflow Inputs ports document their failures as ``InputError``; the
step-workflow runtime historically names the same class ``HostFailure``.
Both names resolve to the very same class object, so core code catching
either spelling catches the adapter's failures.
"""


class TransientInputError(HostFailure):
    """A transient port failure: safe to repeat after a marker lookup (§14.4).

    A write that failed with a 5xx may have landed, so the effect driver
    re-looks it up by Attempt Marker before repeating it.
    """

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        *,
        reason: str = "transient-failure",
        failure_class: str = "github",
    ) -> None:
        super().__init__(
            method,
            path,
            status_code,
            reason=reason,
            failure_class=failure_class,
            transient=True,
        )


class CredentialFailure(HostFailure):
    """A 401 confirmed by the identity probe: the token no longer
    authenticates (contract §13, C-ERR-2).

    Its own class, because the Worker halts on it (P-WRK-1); halting belongs
    to the ``agent run`` slice, which reads this class. The confirmation
    itself happens in the adapter's ``request_with_retry``: a 401 whose
    ``GET /user`` probe succeeds, or fails any other way, is an
    infrastructure-class ``HostFailure`` instead, never this class.
    """

    def __init__(self, method: str, path: str, status_code: int = 401) -> None:
        super().__init__(
            method,
            path,
            status_code,
            reason="credential-failure",
            failure_class="credential",
        )


class PermissionRefusal(HostFailure):
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


class RateLimitExhausted(HostFailure):
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


class InputNotFound(HostFailure):
    """The port reports the read target absent (an HTTP 404).

    Read callers that treat absence as configuration — the deferred project
    profile read — catch this class specifically; every other read failure
    stays a generic :class:`HostFailure`.
    """

    def __init__(self, method: str, path: str, status_code: int = 404) -> None:
        super().__init__(
            method,
            path,
            status_code,
            reason="not-found",
            failure_class="not-found",
        )


class StepTrackerError(Exception):
    """A step-workflow tracker read or write failed; the recorded intent stands.

    ``transient`` marks a write failure the effect driver may repeat after
    re-looking the write up by Attempt Marker: a 5xx response (§14.4). The
    ``method``, ``path`` and ``status_code`` of the failed call travel along
    so the driver can record an infrastructure-class receipt on exhaustion.
    ``None`` where the failure has no HTTP exchange (a connection error or
    timeout propagates raw instead) or the status is unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        method: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.method = method
        self.path = path
        self.status_code = status_code
