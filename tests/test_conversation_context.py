"""The Conversation Context: one anchored measure for every context decision (#573).

Contract ``docs/contract/step-workflow-runtime-contract.md`` §§9.8–9.9,
11.4, 13.2 and [ADR 0031](../docs/adr/0031-the-conversation-context-is-anchored-on-provider-reported-input.md):
the Conversation Context of the next request is the
``usage_metadata.input_tokens`` of the same conversation's most recent
request that reported usage, plus ``bytes/4`` of every message appended
since (basis ``anchored``) — or, with no measured response yet, today's
``bytes/4`` of the whole request (basis ``estimated``). Every context
decision and Admission hold compares this measure, and every
``model_response`` event records it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from coding_agent.provider.pin_witness import PinWitness
from coding_agent.runtime.conversation_context import (
    ConversationContextState,
    estimate_context_tokens,
    measure_conversation_context,
    recorded_conversation_context,
)
from coding_agent.runtime.graph.agent_model_request import run_model_request
from coding_agent.runtime.graph.subagent import run_subagent
from coding_agent.runtime.step_limits import worst_case_bound
from support.agent_step_deps import make_agent_step_deps, make_pin, make_prices
from support.scripted_model import ScriptedModel

ATTEMPT_ID = "implement#7/1"


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _ai(
    content: str = "",
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(content=content, usage_metadata=usage)


def test_opening_measures_estimated() -> None:
    """With no measured response yet, the Context is the whole-request estimate."""
    messages: list[BaseMessage] = [HumanMessage(content="hello")]
    context = measure_conversation_context(messages, ())
    assert context.basis == "estimated"
    assert context.value == estimate_context_tokens(messages, [], [])
    assert context.anchor_input_tokens is None
    assert context.anchor_request_ref is None


def test_anchored_after_measured_response() -> None:
    """The anchor plus ``bytes/4`` of the messages appended since; output ignored."""
    state = ConversationContextState()
    first: list[BaseMessage] = [HumanMessage(content="hello")]
    response = _ai("first answer", _usage(4000, 200))
    state.note_response(response, len(first), "req-1")
    second: list[BaseMessage] = [*first, response, HumanMessage(content="more work")]
    context = state.measure(second, ())
    assert context.basis == "anchored"
    assert context.anchor_input_tokens == 4000
    assert context.anchor_request_ref == "req-1"
    assert context.appended_estimate_tokens > 0
    assert context.value == 4000 + context.appended_estimate_tokens

    # A sibling response reporting far more output anchors the same value:
    # output_tokens is never added.
    other_state = ConversationContextState()
    other_state.note_response(_ai("other", _usage(4000, 99999)), len(first), "req-9")
    other = other_state.measure(second, ())
    assert other.value == context.value


def test_no_usage_response_keeps_anchor() -> None:
    """A response without usage does not reset the anchor."""
    state = ConversationContextState()
    first: list[BaseMessage] = [HumanMessage(content="hello")]
    state.note_response(_ai("answer", _usage(4000, 200)), len(first), "req-1")
    second: list[BaseMessage] = [*first, _ai("answer", _usage(4000, 200))]
    anchored = state.measure(second, ())
    assert anchored.basis == "anchored"

    # A later turn whose response carries no usage leaves the anchor standing.
    state.note_response(_ai("no usage here", None), len(second), "req-2")
    third: list[BaseMessage] = [*second, HumanMessage(content="continue")]
    kept = state.measure(third, ())
    assert kept.basis == "anchored"
    assert kept.anchor_input_tokens == 4000
    assert kept.anchor_request_ref == "req-1"
    assert kept.value == 4000 + kept.appended_estimate_tokens
    assert kept.appended_estimate_tokens > anchored.appended_estimate_tokens


def test_fresh_state_is_estimated() -> None:
    """A fresh anchoring state — a new Step Execution — measures ``estimated``."""
    state = ConversationContextState()
    first: list[BaseMessage] = [HumanMessage(content="hello")]
    state.note_response(_ai("answer", _usage(4000, 200)), len(first), "req-1")
    assert state.measure([*first, _ai("x", None)], ()).basis == "anchored"

    fresh = ConversationContextState()
    messages: list[BaseMessage] = [HumanMessage(content="hello")]
    context = fresh.measure(messages, ())
    assert context.basis == "estimated"
    assert context.value == estimate_context_tokens(messages, [], [])


def test_subagent_anchored_on_own_responses_only() -> None:
    """A Sub-agent's first request is estimated; its anchor never sees the main's."""
    main_state = ConversationContextState()
    opening: list[BaseMessage] = [HumanMessage(content="main work")]
    main_state.note_response(_ai("main answer", _usage(4000, 200)), len(opening), "main-1")

    member_state = ConversationContextState()
    member_opening: list[BaseMessage] = [HumanMessage(content="member work")]
    first = member_state.measure(member_opening, ())
    assert first.basis == "estimated"

    member_state.note_response(
        _ai("member answer", _usage(9000, 10)), len(member_opening), "subagent:m#response-1"
    )
    member_next: list[BaseMessage] = [
        *member_opening,
        _ai("member answer", _usage(9000, 10)),
        HumanMessage(content="follow-up"),
    ]
    anchored = member_state.measure(member_next, ())
    assert anchored.basis == "anchored"
    assert anchored.anchor_input_tokens == 9000
    assert anchored.value == 9000 + anchored.appended_estimate_tokens


def test_hold_admits_the_compared_measure_plus_output_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Admission hold is admitted at the compared Context, not a re-estimate."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    response = _ai("answer", _usage(4000, 200))
    model = ScriptedModel([response], response_models=["test-model"])
    deps = make_agent_step_deps(
        model=model, workspace=workspace, tmp_path=tmp_path, pin=make_pin()
    )
    events: list[dict[str, object]] = []

    def _record(kind: str, detail: str, payload: dict[str, object]) -> None:
        events.append({"kind": kind, "payload": payload})

    witness = PinWitness(
        record_event=_record,
        attempt_id=deps.attempt.attempt_id,
        pin=deps.pin,
        step_id="process",
        seq=1,
    )
    state = ConversationContextState()
    messages: list[BaseMessage] = [HumanMessage(content="hello")]
    expected = measure_conversation_context(messages, [], state)

    captured: list[int] = []
    original_admit = deps.budgets.admit_request

    def _spy(step_id: str, estimate: int, **kwargs: Any) -> Any:
        captured.append(estimate)
        return original_admit(step_id, estimate, **kwargs)

    monkeypatch.setattr(deps.budgets, "admit_request", _spy)
    result = run_model_request(
        deps,
        witness,
        "process",
        1,
        model.bind_tools([]),
        messages,
        [],
        context_state=state,
    )
    assert isinstance(result, AIMessage)
    assert captured == [expected.value]

    # The recorded model_response carries the compared Context and threshold.
    responses = [event for event in events if event["kind"] == "model_response"]
    assert len(responses) == 1
    payload = responses[0]["payload"]
    assert isinstance(payload, dict)
    recorded = payload["conversation_context"]
    assert isinstance(recorded, dict)
    assert recorded["value"] == expected.value
    assert recorded["basis"] == "estimated"


def test_hold_stays_uncached_input_plus_output_cap() -> None:
    """The worst-case bound prices the whole Context as uncached input plus the cap."""
    prices = make_prices()
    value, cap = 5047, 200
    bound_tokens, bound_cost = worst_case_bound(value, cap, prices)
    assert bound_tokens == value + cap
    assert bound_cost == pytest.approx(value / 1_000_000 * prices.input + cap / 1_000_000 * prices.output)

    # A gateway pin prices the same measure at max(input, cache-write): no discount.
    gateway_tokens, gateway_cost = worst_case_bound(value, cap, prices, is_gateway=True)
    assert gateway_tokens == value + cap
    assert gateway_cost == pytest.approx(
        value / 1_000_000 * max(prices.input, prices.cache_write)
        + cap / 1_000_000 * prices.output
    )


def test_archived_record_without_context_reads_unavailable() -> None:
    """Ledger records from before the Context travelled read ``unavailable``, never zero."""
    assert recorded_conversation_context({}).basis == "unavailable"
    assert recorded_conversation_context({}).value is None
    assert recorded_conversation_context({"conversation_context": {}}).value is None
    assert (
        recorded_conversation_context({"conversation_context": {"value": 0}}).basis
        == "unavailable"
    )
    bad: dict[str, object] = {"conversation_context": {"value": True, "basis": "anchored"}}
    assert recorded_conversation_context(bad).basis == "unavailable"


def test_subagent_context_window_ending_carries_compared_context(tmp_path: Path) -> None:
    """A guard-ended Sub-agent carries the crossing Context value and basis."""
    from coding_agent.runtime.conversation_context import ContextWindowConfig

    workspace = tmp_path / "work"
    workspace.mkdir()
    window = ContextWindowConfig(
        threshold_tokens=3000,
        safety_margin_tokens=1000,
        request_overhead_tokens=1000,
    )
    assert window.usable_threshold == 1000
    result = run_subagent(
        ScriptedModel([]),
        description="huge",
        instructions="x" * 8000,
        call_id="spawn-1",
        workspace=workspace,
        context_window=window,
    )
    assert result.status == "no-report"
    assert result.reason == "context-window"
    assert result.end_context is not None
    assert result.end_context.basis == "estimated"
    assert result.end_context.value > window.usable_threshold
    reread = recorded_conversation_context(
        {"conversation_context": result.end_context.to_record()}
    )
    assert (reread.value, reread.basis) == (
        result.end_context.value,
        "estimated",
    )
