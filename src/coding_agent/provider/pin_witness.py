"""One pin witness per conversation records each response's answering model.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.1 and §13
(the Run Ledger records the model that answered each response, and a pin
violation ends the Attempt ``interrupted`` / ``pin-violation``).

One witness per conversation — one per Step Execution, one per Sub-agent
keyed by its ``call_id`` — owns the response numbering and the event label
(``"model response N in step#seq"`` or ``"… of sub-agent 'x'"``). It records
a non-violating response's ``model_response`` event and returns the observed
identity; for a violation, whether from a response (identity or effort) or a
gateway error, it records the ``model_response`` and ``pin_violation`` events
and **returns** the :class:`PinViolation`. The caller writes
``raise witness.…(…)``, and the gateway-error path chains ``from exc``.

The witness only *calls* the verdict functions
(:func:`check_direct_pin`, :func:`check_reported_effort`,
:func:`check_gateway_error_exc`); it holds no identity rules of its own
(ADR 0029). The caller still decides when to settle spend and when to
record, so the step loop (settle before record, success only after the limit
check) and the Sub-agent (record first, settle from the carried response)
keep their order.
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import BaseMessage

from coding_agent.provider.observed_identity import (
    MODEL_RESPONSE_KIND,
    PIN_VIOLATION_KIND,
    EffortCheck,
    GatewayIdentity,
    PinCheck,
    PinViolation,
    check_direct_pin,
    check_gateway_error_exc,
    check_reported_effort,
    gateway_identity_of,
    observed_response_model,
    reported_effort_of,
)
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.context_handoff import ConversationContext

RecordEvent = Callable[[str, str, dict[str, object]], None]
"""The Run Ledger writer: ``(kind, detail, payload)``.

In production this is :meth:`ExecutionTally.record_event`, which appends to
the Step Execution's in-memory events and records to the Run Ledger. In tests
it is a fake collector. The witness records ``model_response`` and
``pin_violation`` events only through this callable.
"""


def _gateway_ledger_fragment(identity: GatewayIdentity) -> dict[str, object]:
    """One gateway response's observed identity for the Run Ledger (§13).

    Every gateway response records its model group, model id, model name,
    attempted fallbacks, attempted retries, call id and the observed gateway
    version; violating responses record them too.
    """
    return {
        "gateway": {
            "model_group": identity.model_group,
            "model_id": identity.model_id,
            "model_name": identity.model_name,
            "attempted_fallbacks": identity.attempted_fallbacks,
            "attempted_retries": identity.attempted_retries,
            "call_id": identity.call_id,
            "litellm_version": identity.version,
        }
    }


def _pin_violation_payload(
    attempt_id: str,
    step_id: str,
    seq: int,
    call_id: str | None,
    response_index: int,
    check: PinCheck,
    effort: EffortCheck | None = None,
) -> dict[str, object]:
    """The ``pin_violation`` ledger event payload (§9.1, §13).

    ``effort`` is the response's reported-effort check where one ran
    (``None`` on a failed request, which carries no response): a reported
    effort that differs from the pinned effort is a pin violation under
    the same reason, so both evidences travel together.
    """
    payload: dict[str, object] = {
        "attempt_id": attempt_id,
        "step_id": step_id,
        "seq": seq,
        "response_index": response_index,
        "expected_model": check.expected,
        "observed_model": check.observed,
        "reported_effort": effort.reported if effort is not None else None,
        "expected_effort": effort.expected if effort is not None else None,
    }
    if call_id is not None:
        payload["call_id"] = call_id
    if check.gateway is not None:
        payload.update(_gateway_ledger_fragment(check.gateway))
    return payload


class PinWitness:
    """One conversation's pin witness: numbering, labels, recording (§9.1).

    Built from only what it uses — the Run Ledger writer, the Attempt id,
    the :class:`PinnedModel`, the Step Execution (step id, sequence number)
    and the Sub-agent's ``call_id`` where there is one — never from
    ``AgentStepDeps``.
    """

    def __init__(
        self,
        *,
        record_event: RecordEvent,
        attempt_id: str,
        pin: PinnedModel,
        step_id: str,
        seq: int,
        call_id: str | None = None,
    ) -> None:
        self._record_event = record_event
        self._attempt_id = attempt_id
        self._pin = pin
        self._step_id = step_id
        self._seq = seq
        self._call_id = call_id
        self._response_index = 0

    @property
    def response_index(self) -> int:
        """The next response's index: how many responses are recorded so far."""
        return self._response_index

    def _response_detail(self, index: int) -> str:
        if self._call_id is not None:
            return f"model response {index} of sub-agent {self._call_id!r}"
        return f"model response {index} in {self._step_id}#{self._seq}"

    def _violation_detail(self) -> str:
        if self._call_id is not None:
            return f"pin violation in sub-agent {self._call_id!r}"
        return f"pin violation in {self._step_id}#{self._seq}"

    def _model_response_payload(
        self,
        index: int,
        observed: str | None,
        reported_effort: str | None,
        gateway: GatewayIdentity | None,
        *,
        context: ConversationContext | None = None,
        usable_threshold: int | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt_id": self._attempt_id,
            "step_id": self._step_id,
            "seq": self._seq,
            "response_index": index,
            "response_model": observed,
            "reported_effort": reported_effort,
        }
        if self._call_id is not None:
            payload["call_id"] = self._call_id
        if gateway is not None:
            payload.update(_gateway_ledger_fragment(gateway))
        if context is not None:
            payload["conversation_context"] = context.to_record()
            payload["usable_threshold"] = usable_threshold
        return payload

    def record_success(
        self,
        response: BaseMessage,
        *,
        context: ConversationContext | None = None,
        usable_threshold: int | None = None,
    ) -> str | None:
        """Record a non-violating response's ``model_response`` event (§13).

        Returns the observed identity: the gateway model group through a
        Model Gateway, else the response's reported model (``None`` where
        unreported). The caller guarantees the response is non-violating
        (it decided when to settle spend and when the limit check passed);
        the witness asserts as much from the verdict functions.
        """
        check = check_direct_pin(self._pin, response)
        effort = check_reported_effort(self._pin, response)
        assert not check.violated, f"pin violation recorded as success: {check!r}"
        assert not effort.violated, f"effort violation recorded as success: {effort!r}"
        observed: str | None
        gateway: GatewayIdentity | None = None
        if self._pin.is_gateway:
            gateway = gateway_identity_of(response)
            # check_direct_pin already took the gateway branch, so its
            # observed model is the model group; the full identity here
            # carries the recorded fragment. They agree by construction.
            observed = gateway.model_group
        else:
            observed = observed_response_model(response)
        index = self._response_index
        self._record_event(
            MODEL_RESPONSE_KIND,
            self._response_detail(index),
            self._model_response_payload(
                index,
                observed,
                reported_effort_of(response),
                gateway,
                context=context,
                usable_threshold=usable_threshold,
            ),
        )
        self._response_index += 1
        return observed

    def violation_from_response(
        self,
        response: BaseMessage,
        *,
        context: ConversationContext | None = None,
        usable_threshold: int | None = None,
    ) -> PinViolation:
        """Record a violating response's events and return its violation (§9.1).

        Calls the identity and reported-effort verdicts, records the
        ``model_response`` event (with both evidences) and the
        ``pin_violation`` event, and returns the :class:`PinViolation`.
        The caller writes ``raise witness.violation_from_response(response)``.
        At least one verdict must be violated; the caller decides when to
        settle spend before calling.
        """
        check = check_direct_pin(self._pin, response)
        effort = check_reported_effort(self._pin, response)
        assert check.violated or effort.violated, (
            f"no pin violation to report: {check!r} {effort!r}"
        )
        index = self._response_index
        self._record_event(
            MODEL_RESPONSE_KIND,
            self._response_detail(index),
            self._model_response_payload(
                index,
                check.observed,
                effort.reported,
                check.gateway,
                context=context,
                usable_threshold=usable_threshold,
            ),
        )
        self._response_index += 1
        self._record_event(
            PIN_VIOLATION_KIND,
            self._violation_detail(),
            _pin_violation_payload(
                self._attempt_id,
                self._step_id,
                self._seq,
                self._call_id,
                index,
                check,
                effort,
            ),
        )
        return PinViolation(
            observed=check.observed,
            expected=check.expected,
            step_id=self._step_id,
            seq=self._seq,
            call_id=self._call_id,
            response_index=index,
            response=response,
            gateway=check.gateway,
            reported_effort=effort.reported,
            expected_effort=effort.expected,
        )

    def violation_from_error(
        self,
        exc: BaseException,
        *,
        context: ConversationContext | None = None,
        usable_threshold: int | None = None,
    ) -> PinViolation:
        """Record a gateway error's violation events and return it (§9.1).

        Calls :func:`check_gateway_error_exc` over the failed request's
        ``x-litellm-*`` headers: retry or fallback evidence above ``0`` is
        a violation that fails closed. Records the ``model_response`` event
        (with no reported effort and no carried response) and the
        ``pin_violation`` event, and returns the :class:`PinViolation`.
        The caller writes ``raise witness.violation_from_error(exc) from exc``.
        """
        check = check_gateway_error_exc(self._pin, exc)
        assert check.violated, f"no gateway-error pin violation: {check!r}"
        assert check.gateway is not None
        index = self._response_index
        self._record_event(
            MODEL_RESPONSE_KIND,
            self._response_detail(index),
            self._model_response_payload(
                index,
                check.observed,
                None,
                check.gateway,
                context=context,
                usable_threshold=usable_threshold,
            ),
        )
        self._response_index += 1
        self._record_event(
            PIN_VIOLATION_KIND,
            self._violation_detail(),
            _pin_violation_payload(
                self._attempt_id,
                self._step_id,
                self._seq,
                self._call_id,
                index,
                check,
                None,
            ),
        )
        return PinViolation(
            observed=check.observed,
            expected=check.expected,
            step_id=self._step_id,
            seq=self._seq,
            call_id=self._call_id,
            response_index=index,
            response=None,
            gateway=check.gateway,
        )
