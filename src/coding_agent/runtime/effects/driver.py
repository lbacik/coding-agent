"""The single intent → lookup → write → receipt sequence (§14, §14.2).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14.1 (every
write has an Effect Intent keyed ``(Attempt, step, n, ordinal)`` with the
rendered payload and expected target state, written after the Gate
immediately before the write, and an Effect Receipt) and §14.2 (settlement
on restart: look the write up remotely by its Effect Intent key; a landed
write records the receipt and is never written twice; an absent write is
performed once where the factual preconditions hold, otherwise recorded
``not-performed``; a human-closed own pull request is never re-created).

The driver records the Effect Intent only on the forward path; settlement
finds the intent row already recorded. A failed precondition or settle
check records ``not-performed`` with its reason. An exception from the
lookup or the write reports ``failed`` and the driver records nothing for
it: the entry points keep their own failure policy (forward maps it to
``not-performed`` plus an ``error`` Step Result; settlement re-raises so the
intent stays open and the restart loop counts the failure). A
:class:`CredentialFailure` always propagates, never wrapped.

A transient failure on the write (a 5xx, a connection error or a timeout,
§14.4) is retried in place within the §14.4 bound — 3 attempts, 5 minutes
total wait, exponential backoff from the initial backoff — as lookup first,
then perform again: the write is re-looked up by its Attempt Marker, and a
landed write records its receipt instead of being written twice. The clock
and the sleep are injectable so tests script them instead of sleeping.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

from coding_agent.github.errors import (
    INITIAL_BACKOFF_SECONDS,
    MAX_IN_PLACE_ATTEMPTS,
    MAX_IN_PLACE_WAIT_SECONDS,
    CredentialFailure,
    GitHubFailure,
    RateLimitExhausted,
)
from coding_agent.inputs.gates import GateResult
from coding_agent.inputs.github import StepTrackerError
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import RunLedger, step_effect_marker

if TYPE_CHECKING:
    from coding_agent.runtime.effects.modules import EffectModule


@dataclass(frozen=True)
class EffectContext:
    """Where the write lands: the input the Attempt was claimed for."""

    number: int
    input_type: InputType


@dataclass(frozen=True)
class Landed:
    """The write had already landed: the receipt is recorded, nothing written."""

    remote_id: str | None
    effect: Any


@dataclass(frozen=True)
class Performed:
    """The absent write was performed once: the receipt is recorded."""

    remote_id: str | None
    effect: Any


@dataclass(frozen=True)
class NotPerformed:
    """A precondition or settle check refused the write: ``not-performed``."""

    reason: str


@dataclass(frozen=True)
class Failed:
    """The lookup or the write raised: the driver recorded nothing."""

    error: Exception


EffectOutcome = Landed | Performed | NotPerformed | Failed
"""What one driven effect did: landed, performed, not-performed, or failed."""


_TRANSIENT_STATUS_MESSAGE = re.compile(r"\breturned 5\d{2}\b")
"""A tracker failure message reporting a 5xx status (§14.4)."""


def _is_transient_perform_error(exc: BaseException) -> bool:
    """Whether a failed write may be repeated after a marker lookup (§14.4).

    Transient is a 5xx, a connection error or a timeout. The tracker marks
    its own 5xx write failures; an infrastructure-class ``github`` failure
    carrying a 5xx status counts too, while an exhausted bound
    (:class:`RateLimitExhausted`), a refusal and a credential failure never
    retry. A tracker message in the ``"<method> <path> returned <status>"``
    shape reporting a 5xx counts as transient even where the flag is absent.
    """
    if isinstance(
        exc, (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError)
    ):
        return True
    if isinstance(exc, RateLimitExhausted):
        return False
    if isinstance(exc, GitHubFailure):
        return exc.failure_class == "github" and 500 <= exc.status_code <= 599
    if isinstance(exc, StepTrackerError):
        if exc.transient:
            return True
        return _TRANSIENT_STATUS_MESSAGE.search(str(exc)) is not None
    return False


def _transient_exhaustion_error(
    operation: str, ctx: EffectContext, last: BaseException
) -> GitHubFailure:
    """The infrastructure-class failure a transient bound ends with (§14.4).

    Exhaustion reports ``infrastructure-failure`` / ``github`` like an
    exhausted rate limit, carrying the failed call's method, path and status
    where the failure names them (``0`` where no response ever arrived).
    """
    if isinstance(last, GitHubFailure):
        return GitHubFailure(
            last.method,
            last.path,
            last.status_code,
            reason="infrastructure-failure",
            failure_class="github",
        )
    method = getattr(last, "method", None)
    path = getattr(last, "path", None)
    status_code = getattr(last, "status_code", None)
    return GitHubFailure(
        method if isinstance(method, str) else "POST",
        path if isinstance(path, str) else f"{operation} on {ctx.input_type} {ctx.number}",
        status_code if isinstance(status_code, int) else 0,
        reason="infrastructure-failure",
        failure_class="github",
    )


def drive_effect(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    attempt_id: str,
    step_id: str,
    seq: int,
    ordinal: int,
    number: int,
    input_type: InputType,
    module: EffectModule[Any],
    target: Any,
    body: str,
    intent_payload: str,
    record_intent: bool,
    gate: Callable[[], GateResult] | None = None,
    claim_active: bool = True,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_IN_PLACE_ATTEMPTS,
    max_wait_seconds: float = MAX_IN_PLACE_WAIT_SECONDS,
    initial_backoff_seconds: float = INITIAL_BACKOFF_SECONDS,
) -> EffectOutcome:
    """Run one marker-looked-up effect through its module (§14.1, §14.2, §14.4).

    Where ``record_intent`` holds (the forward path: publication, the
    terminal comment) the Effect Intent is recorded before anything else;
    settlement finds it already recorded and instead re-checks the active
    Claim and the Gate before writing. The Attempt Marker key travels in the
    write's body, so the lookup key is the marker, never the text.

    A transient failure on the write (a 5xx, a connection error or a
    timeout) is retried within the §14.4 bound — ``max_attempts`` total
    writes, ``max_wait_seconds`` total waiting, exponential backoff from
    ``initial_backoff_seconds`` — as lookup before each repeat: a write
    that landed records its receipt and is reported landed, otherwise the
    write is performed again, so one landed write never becomes two.
    Exhaustion returns ``Failed`` with an infrastructure-class ``github``
    failure, exactly like an exhausted rate limit. ``now`` and ``sleep``
    are injectable so tests script the clock instead of sleeping.
    """
    if record_intent:
        ledger.record_step_effect_intent(
            attempt_id,
            step_id,
            seq,
            ordinal,
            rendered_payload=intent_payload,
            expected_target=module.target_to_dict(target),
        )
    ctx = EffectContext(number=number, input_type=input_type)
    marker = step_effect_marker(
        attempt_id, step_id, seq, ordinal, kind=module.marker_kind
    )
    try:
        existing = module.find(tracker, ctx, target, marker)
    except CredentialFailure:
        raise
    except Exception as exc:
        return Failed(exc)
    if existing is not None:
        remote = module.remote_id(existing)
        ledger.record_step_effect_receipt(
            attempt_id, step_id, seq, ordinal, remote_id=remote
        )
        return Landed(remote_id=remote, effect=existing)
    try:
        precondition = module.check(tracker, ctx, target)
    except CredentialFailure:
        raise
    except Exception as exc:
        return Failed(exc)
    if precondition is not None:
        ledger.record_step_effect_not_performed(
            attempt_id, step_id, seq, ordinal, reason=precondition
        )
        return NotPerformed(precondition)
    if not record_intent:
        if not claim_active:
            reason = "withdrawn: no active Claim for this Attempt"
            ledger.record_step_effect_not_performed(
                attempt_id, step_id, seq, ordinal, reason=reason
            )
            return NotPerformed(reason)
        if gate is not None:
            verdict = gate()
            if verdict.status != "ok":
                reason = f"{verdict.status}: {verdict.reason or verdict.status}"
                ledger.record_step_effect_not_performed(
                    attempt_id, step_id, seq, ordinal, reason=reason
                )
                return NotPerformed(reason)
    try:
        written = module.perform(tracker, ctx, target, body)
    except CredentialFailure:
        raise
    except Exception as exc:
        return Failed(exc)
    remote = module.remote_id(written)
    ledger.record_step_effect_receipt(
        attempt_id, step_id, seq, ordinal, remote_id=remote
    )
    return Performed(remote_id=remote, effect=written)
