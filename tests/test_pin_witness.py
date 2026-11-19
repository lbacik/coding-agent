"""The pin witness records each response's answering model (ADR 0029, §9.1).

One witness per conversation owns the response numbering and the event
label, records a non-violating response's ``model_response`` event, and for
a violation records both events and returns the ``PinViolation`` — without
running ``run_agent_step``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.pin_witness import PinWitness
from coding_agent.provider.pinned_model import PinnedModel


def _direct_pin() -> PinnedModel:
    return PinnedModel(
        provider="anthropic", model="model-a", endpoint="messages", effort="low"
    )


def _gateway_pin() -> PinnedModel:
    return PinnedModel(
        provider="litellm",
        model="group-a",
        endpoint="responses",
        effort="medium",
        gateway_url="http://pi.lan:4000",
        output_cap_tokens=32000,
        context_threshold_tokens=100000,
    )


def _ai(
    *,
    model: str | None = None,
    effort: str | None = None,
    headers: dict[str, str] | None = None,
    response_id: str | None = None,
) -> BaseMessage:
    metadata: dict[str, Any] = {}
    if model is not None:
        metadata["model"] = model
    if effort is not None:
        metadata["reasoning_effort"] = effort
    if headers is not None:
        metadata["headers"] = dict(headers)
    if response_id is not None:
        metadata["id"] = response_id
    return AIMessage(content="", response_metadata=metadata)


def _gateway_headers(
    group: str | None = "group-a",
    *,
    fallbacks: str = "0",
    retries: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if group is not None:
        headers["x-litellm-model-group"] = group
    headers["x-litellm-attempted-fallbacks"] = fallbacks
    if retries is not None:
        headers["x-litellm-attempted-retries"] = retries
    return headers


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def __call__(
        self, kind: str, detail: str, payload: dict[str, object]
    ) -> None:
        self.events.append((kind, detail, dict(payload)))


class _GatewayError(Exception):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("gateway request failed")
        self.response = _GatewayResponse(headers)


class _GatewayResponse:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = dict(headers)


def _witness(
    recorder: _Recorder,
    pin: PinnedModel,
    *,
    call_id: str | None = None,
) -> PinWitness:
    return PinWitness(
        record_event=recorder,
        attempt_id="implement#7/1",
        pin=pin,
        step_id="s",
        seq=1,
        call_id=call_id,
    )


def test_record_success_numbers_two_direct_responses() -> None:
    recorder = _Recorder()
    witness = _witness(recorder, _direct_pin())

    first = witness.record_success(_ai(model="model-a"))
    second = witness.record_success(_ai(model="model-a"))

    assert first == "model-a"
    assert second == "model-a"
    assert witness.response_index == 2
    assert [kind for kind, _, _ in recorder.events] == [
        "model_response",
        "model_response",
    ]
    assert [detail for _, detail, _ in recorder.events] == [
        "model response 0 in s#1",
        "model response 1 in s#1",
    ]
    first_payload = recorder.events[0][2]
    assert first_payload["attempt_id"] == "implement#7/1"
    assert first_payload["step_id"] == "s"
    assert first_payload["seq"] == 1
    assert first_payload["response_index"] == 0
    assert first_payload["response_model"] == "model-a"
    assert first_payload["reported_effort"] is None
    assert "call_id" not in first_payload
    assert recorder.events[1][2]["response_index"] == 1


def test_identity_violation_records_both_events_and_returns() -> None:
    recorder = _Recorder()
    witness = _witness(recorder, _direct_pin())

    exc = witness.violation_from_response(_ai(model="model-b"))

    assert isinstance(exc, PinViolation)
    assert exc.observed == "model-b"
    assert exc.expected == "model-a"
    assert exc.step_id == "s"
    assert exc.seq == 1
    assert exc.call_id is None
    assert exc.response_index == 0
    assert witness.response_index == 1
    assert [kind for kind, _, _ in recorder.events] == [
        "model_response",
        "pin_violation",
    ]
    assert [detail for _, detail, _ in recorder.events] == [
        "model response 0 in s#1",
        "pin violation in s#1",
    ]
    response_payload = recorder.events[0][2]
    assert response_payload["response_model"] == "model-b"
    violation_payload = recorder.events[1][2]
    assert violation_payload["expected_model"] == "model-a"
    assert violation_payload["observed_model"] == "model-b"


def test_effort_violation_carries_both_evidences() -> None:
    recorder = _Recorder()
    witness = _witness(recorder, _direct_pin())

    exc = witness.violation_from_response(_ai(model="model-a", effort="high"))

    assert exc.reported_effort == "high"
    assert exc.expected_effort == "low"
    assert exc.observed == "model-a"
    assert [kind for kind, _, _ in recorder.events] == [
        "model_response",
        "pin_violation",
    ]
    assert recorder.events[0][2]["reported_effort"] == "high"
    assert recorder.events[1][2]["reported_effort"] == "high"
    assert recorder.events[1][2]["expected_effort"] == "low"


def test_gateway_error_violation_records_identity_fragment() -> None:
    recorder = _Recorder()
    witness = _witness(recorder, _gateway_pin())

    exc = witness.violation_from_error(
        _GatewayError(_gateway_headers("group-a", fallbacks="1"))
    )

    assert exc.response is None
    assert exc.gateway is not None
    assert exc.gateway.attempted_fallbacks == 1
    assert [kind for kind, _, _ in recorder.events] == [
        "model_response",
        "pin_violation",
    ]
    response_payload = recorder.events[0][2]
    assert response_payload["reported_effort"] is None
    assert response_payload["response_model"] == "group-a"
    gateway = response_payload["gateway"]
    assert isinstance(gateway, dict)
    assert gateway["attempted_fallbacks"] == 1
    assert recorder.events[1][2]["observed_model"] == "group-a"


def test_subagent_witness_labels_with_call_id() -> None:
    recorder = _Recorder()
    witness = _witness(recorder, _direct_pin(), call_id="s1")

    observed = witness.record_success(_ai(model="model-a"))
    exc = witness.violation_from_response(_ai(model="model-other"))

    assert observed == "model-a"
    assert exc.call_id == "s1"
    assert exc.response_index == 1
    assert recorder.events[0][1] == "model response 0 of sub-agent 's1'"
    assert recorder.events[0][2]["call_id"] == "s1"
    assert recorder.events[1][1] == "model response 1 of sub-agent 's1'"
    assert recorder.events[2][1] == "pin violation in sub-agent 's1'"
    assert recorder.events[2][2]["call_id"] == "s1"


def test_gateway_success_returns_model_group() -> None:
    recorder = _Recorder()
    witness = _witness(recorder, _gateway_pin())

    observed = witness.record_success(
        _ai(headers=_gateway_headers("group-a"), response_id="resp-1")
    )

    assert observed == "group-a"
    assert recorder.events[0][2]["response_model"] == "group-a"
    gateway = recorder.events[0][2]["gateway"]
    assert isinstance(gateway, dict)
    assert gateway["model_group"] == "group-a"
