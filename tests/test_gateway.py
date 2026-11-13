"""Gateway pins through the fake Model Gateway (contract §2.1).

The Worker runs a gateway pin by speaking OpenAI Responses to the LiteLLM
Proxy through its existing LangChain adapter; the gateway changes only the
transport, and the Worker still owns the tool loop (ADR 0023). These tests
drive the real adapter over the fake gateway
(``tests/support/fake_gateway.py``), which replays the redacted recordings
in ``tests/fixtures/gateway/`` plus mutated variants:

- the request on the wire: ``store: False``,
  ``include: ["reasoning.encrypted_content"]``, the forced ``tool_choice``
  on the probe, the output cap and the effort;
- the Provider Capability Assertion refusing Meta's forced-tool 400
  (recorded) and the accepted invalid effort (recorded 200);
- the assertion passing on the mutated happy variant, where the forced
  tool is honoured and an invalid effort is refused by the gateway;
- a two-turn tool loop carrying the encrypted reasoning back byte-equal;
- the gateway answering-model assertion (§9.1): every request carries the
  per-request suppression, a happy response records its full identity, and
  every violation case — missing or wrong identity, fallback or retry
  evidence, on a response or on an error — ends the Attempt
  `interrupted` / `pin-violation`, or refuses startup on the probe.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from langchain_core.exceptions import ModelError
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    UsageMetadata,
)
from langchain_core.tools import tool

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.provider.capability import (
    UNHONOURED_EFFORT,
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_REASON,
    PinViolation,
    check_direct_pin,
    check_gateway_error,
    check_gateway_error_exc,
    check_gateway_response,
    gateway_identity_of,
)
from coding_agent.provider.pinned_model import PinnedModel, build_chat_model
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.worker.run import is_pin_violation_reason
from coding_agent.runtime.ceilings import (
    UsageTotals,
    has_unreported_gateway_cache_write,
    response_has_unreported_gateway_cache_write,
    response_usage,
)
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    AgentStepDeps,
    AgentStepOutcome,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.subagent import run_subagent
from coding_agent.runtime.step_limits import (
    AttemptBudgets,
    build_budgets,
    worst_case_bound,
)
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2FieldType,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.fake_gateway import (
    FIXTURE_DIR,
    FakeGatewayTransport,
    load_recording,
    queue_responder,
    recording_response,
)
from support.gateway_assertions import (
    assert_probe_request,
    assert_reasoning_carried_verbatim,
    assert_self_contained_request,
    assert_suppression_on_wire,
)
from support.scripted_model import ScriptedModel

GATEWAY_KEY = "test-gateway-key"
GATEWAY_URL = "http://gateway.test:4000"


def _gateway_pin(**overrides: object) -> PinnedModel:
    fields: dict[str, object] = {
        "provider": "litellm",
        "model": "muse-spark-1.3-contributor",
        "endpoint": "responses",
        "effort": "medium",
        "gateway_url": GATEWAY_URL,
        "output_cap_tokens": 32000,
        "context_threshold_tokens": 100000,
    }
    fields.update(overrides)
    return PinnedModel(**fields)  # type: ignore[arg-type]


def _price_table(pin: PinnedModel) -> dict[str, TokenPrices]:
    return {
        pin.key: TokenPrices(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50)
    }


def _gateway_model(pin: PinnedModel, transport: httpx.BaseTransport) -> Any:
    """The real adapter pointed at the fake gateway."""
    return build_chat_model(pin, api_key=GATEWAY_KEY, transport=transport)


@tool
def get_weather(city: str) -> str:
    """A harmless work tool for the two-turn loop."""
    return f"sunny in {city}"


def test_gateway_pin_passes_capability_with_self_contained_requests() -> None:
    """The mutated happy variant: the gateway honours the forced tool and
    refuses the invalid effort, and every request is self-contained.

    Each request goes to the gateway's ``/v1`` Responses endpoint with the
    gateway key, the effort, the output cap, ``store: False`` and
    ``include: ["reasoning.encrypted_content"]``; the probe forces the
    ``tool_choice``. Response headers land on the message, for the gateway
    identity ticket.
    """
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_probe_ok_200.json"),
                recording_response("litellm_effort_refused_400.json"),
            ]
        )
    )

    result = assert_provider_capability(
        pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
    )

    assert result.pin is pin
    assert result.response.tool_calls[0]["name"] == "capability_probe"
    assert "effort" in result.effort_refusal
    headers = result.response.response_metadata.get("headers", {})
    assert headers.get("x-request-id") == "req-probe-ok"

    assert len(transport.requests) == 2
    for recorded in transport.requests:
        assert recorded.method == "POST"
        assert recorded.authorization == f"Bearer {GATEWAY_KEY}"
    probe, effort_probe = (recorded.body for recorded in transport.requests)
    # The same request-shape assertions the L1 rows run against a real
    # gateway (tests/test_gateway_live.py, GW-1): the probe forces the tool
    # and both legs are self-contained with the suppression sent.
    assert_probe_request(
        probe, model="muse-spark-1.3-contributor", effort="medium", output_cap=32000
    )
    assert_self_contained_request(
        effort_probe,
        model="muse-spark-1.3-contributor",
        effort=UNHONOURED_EFFORT,
        output_cap=32000,
    )
    transport.assert_only_responses_api()


def test_capability_refuses_the_recorded_meta_forced_tool_400() -> None:
    """Recorded: a Meta model behind the gateway refuses the forced tool
    choice with a 400 — the pin is refused, naming the probe."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder([recording_response("litellm_meta_forced_tool_400.json")])
    )

    with pytest.raises(ProviderCapabilityRefused, match="refused the capability probe"):
        assert_provider_capability(
            pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
        )

    # One attempt, not a retried transient: a 400 is not retryable.
    assert len(transport.requests) == 1
    # The probe itself was well-formed: it forced the choice the gateway
    # refused, so the refusal is about honouring, not about the request.
    assert transport.requests[0].body["tool_choice"] == {
        "type": "function",
        "name": "capability_probe",
    }
    transport.assert_only_responses_api()


def test_capability_refuses_the_recorded_accepted_invalid_effort() -> None:
    """Recorded: the gateway answers the invalid-effort probe with a 200 —
    the effort setting does not reach the provider, so the pin is refused."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_probe_ok_200.json"),
                recording_response("litellm_effort_accepted_200.json"),
            ]
        )
    )

    with pytest.raises(ProviderCapabilityRefused, match="effort is not honoured"):
        assert_provider_capability(
            pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
        )

    assert len(transport.requests) == 2
    assert transport.requests[1].body["reasoning"] == {"effort": UNHONOURED_EFFORT}
    transport.assert_only_responses_api()


def test_two_turn_tool_loop_carries_encrypted_reasoning_byte_equal() -> None:
    """The first turn's ``reasoning.encrypted_content`` block travels inside
    the second request byte-equal — verbatim means a self-contained turn,
    never a reference to server-side state (ADR 0010)."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_turn1_200.json"),
                recording_response("litellm_turn2_200.json"),
            ]
        )
    )
    model: Any = _gateway_model(pin, transport)
    bound: Any = model.bind_tools([get_weather])

    first = bound.invoke([HumanMessage(content="what is the weather in paris?")])
    assert isinstance(first, AIMessage)
    assert first.tool_calls and first.tool_calls[0]["name"] == "get_weather"
    encrypted = [
        block.get("encrypted_content")
        for block in first.content
        if isinstance(block, dict) and block.get("type") == "reasoning"
    ]
    assert encrypted == ["cmVkYWN0ZWQtdHVybi0xLWVuY3J5cHRlZC1ieXRlcw=="]

    history = [
        HumanMessage(content="what is the weather in paris?"),
        first,
        ToolMessage(content="sunny, 21C", tool_call_id=first.tool_calls[0]["id"]),
    ]
    second = bound.invoke(history)
    assert "Sunny in Paris." in str(second.content)

    assert len(transport.requests) == 2
    for recorded in transport.requests:
        # Per-request suppression (§9.1): the Worker's own provider retry is
        # the only retry — the gateway itself retries and falls back never.
        assert_suppression_on_wire(recorded.body)
    # The same verbatim assertion the L1 rows run against a real gateway
    # (tests/test_gateway_live.py, GW-3): the turn travels inside the second
    # request byte-equal, including the encrypted reasoning.
    assert_reasoning_carried_verbatim(first, transport.requests[1].body)
    turn_two_input = transport.requests[1].body["input"]
    reasoning_blocks = [
        block
        for block in turn_two_input
        if isinstance(block, dict) and block.get("type") == "reasoning"
    ]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0]["encrypted_content"] == encrypted[0]
    assert reasoning_blocks[0]["id"] == "rs_turn_1"
    assert transport.requests[1].body["max_output_tokens"] == 32000
    transport.assert_only_responses_api()


def test_no_gateway_metadata_endpoint_is_ever_called() -> None:
    """The adapter speaks only the Responses API: ``/model/info`` and
    ``/v1/models`` are gateway declarations, never read (ADR 0009)."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_turn1_200.json"),
                recording_response("litellm_turn2_200.json"),
            ]
        )
    )
    model: Any = _gateway_model(pin, transport)

    model.invoke("hi")
    model.invoke("hi again")

    assert [recorded.path for recorded in transport.requests] == [
        "/v1/responses",
        "/v1/responses",
    ]
    transport.assert_only_responses_api()


def test_vendored_recordings_are_redacted_of_keys_and_addresses() -> None:
    """The fixtures carry no credential values and no real addresses."""
    forbidden = [
        r"sk-[A-Za-z0-9]",
        r"ghp_",
        r"github_pat",
        r"BEGIN [A-Z ]*PRIVATE KEY",
        r"192\.168\.\d",
        r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        r"pi\.lan",
        r"[Bb]earer [A-Za-z0-9._-]{8,}",
    ]
    recordings = sorted(FIXTURE_DIR.glob("litellm_*.json"))
    assert len(recordings) >= 6
    for path in recordings:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not re.search(pattern, text), f"{path.name} matches {pattern!r}"


def test_direct_pins_take_no_injected_transport() -> None:
    """The transport seam is gateway-only: direct pins are unaffected."""
    direct = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
    )
    with pytest.raises(ValueError, match="gateway-only"):
        build_chat_model(direct, api_key="key", transport=httpx.MockTransport(lambda request: httpx.Response(200)))


# --- Gateway truthful metering (contract §11.4) -------------------------------


def _gateway_prices() -> TokenPrices:
    """The gateway pin's per-bucket prices: write bills above input."""
    return TokenPrices(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50)


def _unreported_usage() -> dict[str, object]:
    """Recorded turn-1 shape: no cache-write field, so unreported (§11.4)."""
    return {
        "input_tokens": 48,
        "output_tokens": 16,
        "total_tokens": 64,
        "input_token_details": {},
    }


def _reported_usage() -> dict[str, object]:
    """Mutated shape: 30 cached and 20 written, used as reported (§11.4)."""
    return {
        "input_tokens": 48,
        "output_tokens": 16,
        "total_tokens": 64,
        "input_token_details": {"cache_read": 30, "cache_creation": 20},
    }


def _zero_write_usage() -> dict[str, object]:
    """A present ``cache_creation`` of ``0`` is reported, never unreported."""
    return {
        "input_tokens": 48,
        "output_tokens": 16,
        "total_tokens": 64,
        "input_token_details": {"cache_read": 30, "cache_creation": 0},
    }


def _mutated_turn1_response(
    *, cached_tokens: int | None, cache_write_tokens: int | None
) -> httpx.Response:
    """The recorded turn-1 response with a rewritten raw usage block.

    The adapter normalises raw ``input_tokens_details`` (``cached_tokens``,
    ``cache_write_tokens``) into ``input_token_details`` (``cache_read``,
    ``cache_creation``); absent raw fields stay absent, which is exactly the
    unreported-cache-write shape under test.
    """
    status, headers, body = load_recording("litellm_turn1_200.json")
    body = copy.deepcopy(body)
    details: dict[str, int] = {}
    if cached_tokens is not None:
        details["cached_tokens"] = cached_tokens
    if cache_write_tokens is not None:
        details["cache_write_tokens"] = cache_write_tokens
    body["usage"]["input_tokens_details"] = details
    return httpx.Response(status, json=body, headers=headers)


def _invoke_turn1(transport: FakeGatewayTransport) -> AIMessage:
    """The real adapter's first turn over ``transport``."""
    model: Any = _gateway_model(_gateway_pin(), transport)
    bound: Any = model.bind_tools([get_weather])
    first = bound.invoke([HumanMessage(content="what is the weather in paris?")])
    assert isinstance(first, AIMessage)
    return first


def test_recorded_gateway_response_prices_unreported_remainder_at_max() -> None:
    """Recorded usage (no cache-write field) prices the remainder at max.

    The recorded turn-1 usage (48 in, 16 out, no detail fields) normalises
    to an empty ``input_token_details``: the uncached remainder (48) prices
    at max(input, cache-write) = 2.50, for 0.00028. Token buckets stay as
    reported and effective tokens stay true; the direct control prices the
    same usage at the plain input rate (0.000256).
    """
    first = _invoke_turn1(
        FakeGatewayTransport(
            queue_responder([recording_response("litellm_turn1_200.json")])
        )
    )
    usage = first.usage_metadata
    assert usage is not None
    assert dict(usage.get("input_token_details") or {}) == {}
    assert has_unreported_gateway_cache_write(usage, is_gateway=True) is True
    assert response_has_unreported_gateway_cache_write(first, is_gateway=True) is True
    assert (
        response_has_unreported_gateway_cache_write(first, is_gateway=False)
        is False
    )
    prices = _gateway_prices()
    spent = response_usage(first, prices, is_gateway=True)
    assert spent.input_tokens == 48
    assert spent.output_tokens == 16
    assert spent.cache_read_tokens == 0
    assert spent.cache_write_tokens == 0
    assert spent.tokens == 64
    assert spent.effective_tokens == 64
    assert spent.cost_usd == pytest.approx(0.00028)
    assert spent.cost_input_usd == pytest.approx(48 / 1_000_000 * 2.50)
    assert spent.cost_output_usd == pytest.approx(16 / 1_000_000 * 10.0)
    direct = response_usage(first, prices)
    assert direct.cost_usd == pytest.approx(0.000256)
    assert direct.cost_input_usd == pytest.approx(48 / 1_000_000 * 2.0)


def test_gateway_response_with_reported_cache_write_prices_as_reported() -> None:
    """A present cache-write field is used as reported: gateway == direct."""
    first = _invoke_turn1(
        FakeGatewayTransport(
            queue_responder(
                [_mutated_turn1_response(cached_tokens=30, cache_write_tokens=20)]
            )
        )
    )
    usage = first.usage_metadata
    assert usage is not None
    assert dict(usage.get("input_token_details") or {}) == {
        "cache_read": 30,
        "cache_creation": 20,
    }
    assert has_unreported_gateway_cache_write(usage, is_gateway=True) is False
    prices = _gateway_prices()
    spent = response_usage(first, prices, is_gateway=True)
    assert spent.cost_usd == pytest.approx(0.000216)
    assert response_usage(first, prices).cost_usd == pytest.approx(0.000216)
    assert (spent.cache_read_tokens, spent.cache_write_tokens) == (30, 20)
    assert spent.effective_tokens == 34


def test_gateway_response_with_zero_cache_write_counts_as_reported() -> None:
    """Presence, not value: ``cache_write_tokens: 0`` is reported (§11.4)."""
    first = _invoke_turn1(
        FakeGatewayTransport(
            queue_responder(
                [_mutated_turn1_response(cached_tokens=30, cache_write_tokens=0)]
            )
        )
    )
    usage = first.usage_metadata
    assert usage is not None
    assert (usage.get("input_token_details") or {}).get("cache_creation") == 0
    assert has_unreported_gateway_cache_write(usage, is_gateway=True) is False
    prices = _gateway_prices()
    assert response_usage(first, prices, is_gateway=True).cost_usd == pytest.approx(
        0.000202
    )
    assert response_usage(first, prices).cost_usd == pytest.approx(0.000202)


def test_gateway_ignores_litellm_response_cost_header() -> None:
    """``x-litellm-response-cost`` is never read, not even as evidence."""
    first = _invoke_turn1(
        FakeGatewayTransport(
            queue_responder(
                [
                    recording_response(
                        "litellm_turn1_200.json",
                        extra_headers={"x-litellm-response-cost": "0.999"},
                    )
                ]
            )
        )
    )
    spent = response_usage(first, _gateway_prices(), is_gateway=True)
    assert spent.cost_usd == pytest.approx(0.00028)


def test_unreported_cache_write_presence_not_value() -> None:
    """Only a gateway pin with an absent field is unreported (§11.4)."""
    absent = cast(
        UsageMetadata,
        {
            "input_tokens": 48,
            "output_tokens": 16,
            "total_tokens": 64,
            "input_token_details": {},
        },
    )
    assert has_unreported_gateway_cache_write(absent, is_gateway=True) is True
    for details in (
        {"cache_creation": 0},
        {"cache_creation": 20, "cache_read": 30},
        {"ephemeral_5m_input_tokens": 0},
    ):
        present = cast(
            UsageMetadata,
            {
                "input_tokens": 48,
                "output_tokens": 16,
                "total_tokens": 64,
                "input_token_details": details,
            },
        )
        assert has_unreported_gateway_cache_write(present, is_gateway=True) is False
    # Direct pins never report unreported cache writes: absent fields count
    # as zero there, unchanged. Missing usage is not an unreported write —
    # it settles at the worst case instead.
    assert has_unreported_gateway_cache_write(absent, is_gateway=False) is False
    assert has_unreported_gateway_cache_write(None, is_gateway=True) is False


def test_gateway_admission_bound_prices_estimate_at_max_rate() -> None:
    """Admission holds the gateway estimate at max(input, cache-write)."""
    prices = _gateway_prices()
    gateway_tokens, gateway_cost = worst_case_bound(
        10000, 1000, prices, is_gateway=True
    )
    direct_tokens, direct_cost = worst_case_bound(10000, 1000, prices)
    assert (gateway_tokens, direct_tokens) == (11000, 11000)
    assert gateway_cost == pytest.approx(0.035)
    assert direct_cost == pytest.approx(0.03)


class _FakeClock:
    """A deterministic wall clock for gateway budget tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _gateway_workflow() -> V2Workflow:
    step = V2Step(
        id="process",
        agent=V2Agent(tools=(), results={"done": {}}),
        max_executions=1,
        limits={},
        reserved=False,
        routes={
            "done": V2RouteTarget(end="completed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
            "error": V2RouteTarget(end="failed"),
        },
    )
    return V2Workflow(
        id="implement",
        model="gateway",
        input=V2Input(type="issue", label="agent:test"),
        ceiling=V2Ceiling(
            cost_usd=100000.0,
            tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps={"process": step},
    )


def _gateway_facts(attempt_id: str) -> StepAttemptFacts:
    return StepAttemptFacts(
        attempt_id=attempt_id,
        workflow_id="implement",
        input_type="issue",
        input_number=7,
        title="Fix the crash",
        body="The worker crashes on startup.",
        base_revision="abc123",
        current_revision="abc123",
    )


def _report_ai(
    result: str, payload: dict[str, object], usage: dict[str, object] | None
) -> AIMessage:
    return AIMessage(
        content="reporting",
        tool_calls=[
            {
                "name": REPORT_TOOL_NAME,
                "args": {"result": result, "payload_json": json.dumps(payload)},
                "id": "report-1",
                "type": "tool_call",
            }
        ],
        usage_metadata=usage,
    )


def _run_gateway_step(
    ledger: RunLedger,
    workspace: Path,
    tmp_path: Path,
    attempt_id: str,
    usage: dict[str, object] | None,
    *,
    is_gateway: bool,
) -> tuple[AgentStepOutcome, AttemptBudgets]:
    """Run one gateway (or direct-control) Step Execution to its report."""
    clock = _FakeClock()
    workflow = _gateway_workflow()
    prices = _gateway_prices()
    budgets = build_budgets(
        workflow,
        prices=prices,
        output_cap=0,
        attempt_id=attempt_id,
        opened_at=clock.now,
        clock=clock,
        is_gateway=is_gateway,
    )
    ledger.open_attempt(attempt_id, effective_configuration={})
    model = ScriptedModel([_report_ai("done", {}, usage)])
    deps = AgentStepDeps(
        attempt=_gateway_facts(attempt_id),
        model=model,
        workspace=workspace,
        tree_id="tree-1",
        archive_dir=tmp_path / "archive",
        budgets=budgets,
        prices=prices,
        ledger=ledger,
        is_gateway=is_gateway,
    )
    return run_agent_step(workflow.steps["process"], 1, deps, {}), budgets


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def test_agent_step_gateway_settlement_prices_unreported_remainder_at_max(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A gateway Step Execution settles the remainder at max and marks it."""
    outcome, budgets = _run_gateway_step(
        ledger,
        workspace,
        tmp_path,
        "implement#7/1",
        _unreported_usage(),
        is_gateway=True,
    )
    assert outcome.status == "reported"
    step = budgets.step_totals("process")
    assert step.tokens == 64
    assert step.effective_tokens == 64
    assert step.input_tokens == 48
    assert step.output_tokens == 16
    assert step.cache_read_tokens == 0
    assert step.cache_write_tokens == 0
    assert step.cost_usd == pytest.approx(0.00028)
    assert ledger.get_responses_with_unreported_cache_write("implement#7/1") == 1
    assert ledger.get_responses_without_usage("implement#7/1") == 0


def test_agent_step_gateway_reported_cache_write_has_no_marker(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A reported cache write settles as reported, with no ledger marker."""
    outcome, budgets = _run_gateway_step(
        ledger,
        workspace,
        tmp_path,
        "implement#7/1",
        _reported_usage(),
        is_gateway=True,
    )
    assert outcome.status == "reported"
    assert budgets.step_totals("process").cost_usd == pytest.approx(0.000216)
    assert ledger.get_responses_with_unreported_cache_write("implement#7/1") == 0


def test_agent_step_gateway_zero_cache_write_has_no_marker(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A zero cache write is reported: exact price, no marker."""
    outcome, budgets = _run_gateway_step(
        ledger,
        workspace,
        tmp_path,
        "implement#7/1",
        _zero_write_usage(),
        is_gateway=True,
    )
    assert outcome.status == "reported"
    assert budgets.step_totals("process").cost_usd == pytest.approx(0.000202)
    assert ledger.get_responses_with_unreported_cache_write("implement#7/1") == 0


def test_agent_step_direct_control_prices_unreported_shape_at_input_rate(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """Direct pins are unchanged: absent fields count as zero, no marker."""
    outcome, budgets = _run_gateway_step(
        ledger,
        workspace,
        tmp_path,
        "implement#7/1",
        _unreported_usage(),
        is_gateway=False,
    )
    assert outcome.status == "reported"
    assert budgets.step_totals("process").cost_usd == pytest.approx(0.000256)
    assert ledger.get_responses_with_unreported_cache_write("implement#7/1") == 0


def test_subagent_gateway_unreported_cache_write_prices_max_and_fires_callback(
    tmp_path: Path,
) -> None:
    """``run_subagent`` prices the gateway remainder at max and reports it."""
    reported: list[UsageTotals] = []
    markers = 0

    def _note_unreported() -> None:
        nonlocal markers
        markers += 1

    message = _report_ai("no findings", {}, None)
    message.content = ""
    message.tool_calls = [
        {
            "name": "report_subagent",
            "args": {"report": "no findings"},
            "id": "sub-report-1",
            "type": "tool_call",
        }
    ]
    message.usage_metadata = _unreported_usage()  # type: ignore[assignment]
    model = ScriptedModel([message])
    result = run_subagent(
        model,
        description="review the change",
        instructions="review the change",
        call_id="call-1",
        workspace=tmp_path,
        price=_gateway_prices(),
        is_gateway=True,
        on_usage=reported.append,
        on_unreported_cache_write=_note_unreported,
    )
    assert result.status == "reported"
    assert markers == 1
    assert result.usage.cost_usd == pytest.approx(0.00028)
    assert sum(item.cost_usd for item in reported) == pytest.approx(0.00028)


def test_subagent_direct_control_prices_unreported_shape_at_input_rate(
    tmp_path: Path,
) -> None:
    """The direct Sub-agent path is unchanged: input rate, no callback."""
    markers = 0

    def _note_unreported() -> None:
        nonlocal markers
        markers += 1

    message = _report_ai("no findings", {}, None)
    message.content = ""
    message.tool_calls = [
        {
            "name": "report_subagent",
            "args": {"report": "no findings"},
            "id": "sub-report-1",
            "type": "tool_call",
        }
    ]
    message.usage_metadata = _unreported_usage()  # type: ignore[assignment]
    model = ScriptedModel([message])
    result = run_subagent(
        model,
        description="review the change",
        instructions="review the change",
        call_id="call-1",
        workspace=tmp_path,
        price=_gateway_prices(),
        on_unreported_cache_write=_note_unreported,
    )
    assert result.status == "reported"
    assert markers == 0
    assert result.usage.cost_usd == pytest.approx(0.000256)


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _live() -> LiveView:
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "limits": {
                "wall_clock_minutes": 60,
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
            }
        }
    }


def _step_claim(live: LiveView, attempt_id: str = "implement#40/1") -> LiveView:
    live.set_claim(attempt_id, 40, "Add a greeting")
    live.set_phase("process#1")
    return live


def _record_step_execution(
    ledger: RunLedger, attempt_id: str = "implement#40/1"
) -> None:
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())
    ledger.open_step_execution(attempt_id, "process", 1)
    ledger.end_step_execution(
        attempt_id,
        "process",
        1,
        result="done",
        usage={
            "tokens": 1200,
            "effective_tokens": 1000,
            "cost_usd": 0.02,
            "tool_calls": 7,
        },
    )


def test_step_snapshot_marks_unreported_cache_write_upper_bound_current(
    tmp_path: Path,
) -> None:
    """A step Attempt with an unreported cache write reports upper-bound."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _record_step_execution(ledger)
        ledger.record_response_with_unreported_cache_write("implement#40/1")
        result = operator_listener.build_status_result(
            ledger.path, _step_claim(_live())
        )
        current = result["current_attempt"]
        assert current is not None
        assert current["attempt_id"] == "implement#40/1"
        assert ledger.get_responses_with_unreported_cache_write("implement#40/1") == 1
        assert ledger.get_responses_without_usage("implement#40/1") == 0
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        assert limits["effective_tokens"]["used"]["basis"] == "estimated"
        reason = limits["effective_tokens"]["used"].get("reason")
        assert isinstance(reason, str) and "cache write" in reason
        assert limits["cost_usd"]["used"]["value"] == 0.02
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
    finally:
        ledger.close()


def test_step_snapshot_combines_missing_and_unreported_reasons(
    tmp_path: Path,
) -> None:
    """Both counters name both reasons, in worst-case-then-cache-write order."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _record_step_execution(ledger)
        ledger.record_response_without_usage("implement#40/1")
        ledger.record_response_with_unreported_cache_write("implement#40/1")
        result = operator_listener.build_status_result(
            ledger.path, _step_claim(_live())
        )
        current = result["current_attempt"]
        assert current is not None
        assert ledger.get_responses_without_usage("implement#40/1") == 1
        assert ledger.get_responses_with_unreported_cache_write("implement#40/1") == 1
        limits = {entry["name"]: entry for entry in current["budget"]["limits"]}
        reason = limits["cost_usd"]["used"].get("reason")
        assert isinstance(reason, str)
        assert "worst-case bound" in reason
        assert "cache write" in reason
        assert reason.index("worst-case bound") < reason.index("cache write")
        assert limits["cost_usd"]["used"]["basis"] == "estimated"
    finally:
        ledger.close()


def test_step_snapshot_marks_unreported_cache_write_upper_bound_last_finished(
    tmp_path: Path,
) -> None:
    """``last_finished`` on the step path carries the same bound and field."""
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    try:
        _record_step_execution(ledger)
        ledger.record_response_with_unreported_cache_write("implement#40/1")
        ledger.record_terminal_outcome(
            "implement#40/1", outcome="delivered", reason="verified-completion"
        )
        ledger.set_attempt_state("implement#40/1", "finished")
        result = operator_listener.build_status_result(ledger.path, _live())
        assert result["current_attempt"] is None
        last = result["last_finished"]
        assert last is not None
        assert last["attempt_id"] == "implement#40/1"
        assert ledger.get_responses_with_unreported_cache_write("implement#40/1") == 1
        assert ledger.get_responses_without_usage("implement#40/1") == 0
        assert last["outcome"] is not None
    finally:
        ledger.close()


def test_unreported_cache_write_counter_is_durable_and_legacy_reads_zero(
    tmp_path: Path,
) -> None:
    """The marker survives a Worker restart; old ledgers read as exact."""
    path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(path)
    try:
        ledger.open_attempt("#40/1", effective_configuration=_effective_config())
        assert ledger.get_responses_with_unreported_cache_write("#40/1") == 0
        assert ledger.record_response_with_unreported_cache_write("#40/1") == 1
        assert ledger.get_responses_with_unreported_cache_write("#40/unknown") == 0
    finally:
        ledger.close()
    reopened = RunLedger(path)
    try:
        assert reopened.get_responses_with_unreported_cache_write("#40/1") == 1
    finally:
        reopened.close()
    legacy_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy_path)
    try:
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                effective_configuration TEXT NOT NULL,
                effective_configuration_digest TEXT NOT NULL,
                fingerprint TEXT,
                base_revision TEXT,
                agent_identity_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    legacy = RunLedger(legacy_path)
    try:
        assert legacy.get_responses_with_unreported_cache_write("#40/1") == 0
    finally:
        legacy.close()


# --- Gateway answering-model assertion (contract §9.1) -------------------------


def _gateway_headers(**overrides: str) -> dict[str, str]:
    """Valid identity headers for the pinned test model, mutated per test."""
    headers = {
        "x-litellm-model-group": "muse-spark-1.3-contributor",
        "x-litellm-model-id": "gateway-test-deployment-1",
        "x-litellm-model-name": "gateway-test-model",
        "x-litellm-attempted-fallbacks": "0",
        "x-litellm-attempted-retries": "0",
        "x-litellm-version": "1.99.0",
    }
    headers.update(overrides)
    return headers


def _gateway_ai(
    headers: dict[str, str] | None, *, model: str = "gateway-test-model"
) -> AIMessage:
    """A gateway response carrying ``headers`` (``None``: no headers at all).

    The body ``model`` is never identity evidence: it stays off the pinned
    name on purpose, so a passing check proves the headers decided.
    """
    metadata: dict[str, object] = {"model": model}
    if headers is not None:
        metadata["headers"] = headers
    return AIMessage(content="", response_metadata=metadata)


def test_gateway_identity_happy_response_passes_and_records() -> None:
    """A recorded happy response passes and records every identity field."""
    pin = _gateway_pin()
    check = check_direct_pin(pin, _gateway_ai(_gateway_headers()))

    assert check.violated is False
    assert check.observed == "muse-spark-1.3-contributor"
    assert check.expected == "muse-spark-1.3-contributor"
    assert check.gateway is not None
    assert check.gateway.model_group == "muse-spark-1.3-contributor"
    assert check.gateway.model_id == "gateway-test-deployment-1"
    assert check.gateway.model_name == "gateway-test-model"
    assert check.gateway.attempted_fallbacks == 0
    assert check.gateway.attempted_retries == 0
    assert check.gateway.version == "1.99.0"


def test_gateway_identity_headers_are_case_insensitive() -> None:
    """Header names match case-insensitively, as on the wire."""
    pin = _gateway_pin()
    check = check_gateway_response(
        pin,
        _gateway_ai(
            {
                "X-LiteLLM-Model-Group": "muse-spark-1.3-contributor",
                "X-LiteLLM-Attempted-Fallbacks": "0",
            }
        ),
    )

    assert check.violated is False
    assert check.gateway is not None
    assert check.gateway.attempted_retries == 0


def test_gateway_identity_missing_retry_header_means_zero() -> None:
    """A missing retry header means 0: the happy path without it passes."""
    headers = _gateway_headers()
    del headers["x-litellm-attempted-retries"]
    check = check_gateway_response(_gateway_pin(), _gateway_ai(headers))

    assert check.violated is False
    assert check.gateway is not None
    assert check.gateway.attempted_retries == 0


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"x-litellm-attempted-fallbacks": "0"},
        {"x-litellm-model-group": "muse-spark-1.3-contributor"},
        {"x-litellm-model-group": "another-model",
         "x-litellm-attempted-fallbacks": "0"},
        {"x-litellm-model-group": "muse-spark-1.3-contributor",
         "x-litellm-attempted-fallbacks": "1"},
        {"x-litellm-model-group": "muse-spark-1.3-contributor",
         "x-litellm-attempted-fallbacks": "0",
         "x-litellm-attempted-retries": "2"},
        {"x-litellm-model-group": "muse-spark-1.3-contributor",
         "x-litellm-attempted-fallbacks": "many"},
    ],
    ids=[
        "no-headers",
        "empty-headers",
        "missing-group",
        "missing-fallbacks",
        "wrong-group",
        "fallbacks-1",
        "retries-2",
        "unparseable-fallbacks",
    ],
)
def test_gateway_identity_success_violations_fail_closed(
    headers: dict[str, str] | None,
) -> None:
    """Each success violation case fails closed — including unparseable."""
    check = check_gateway_response(_gateway_pin(), _gateway_ai(headers))

    assert check.violated is True


class _FailingGatewayRequest(Exception):
    """A failed gateway request carrying headers, like the real adapter."""

    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("synthesised gateway failure")
        self.response = httpx.Response(500, json={}, headers=headers)


def test_gateway_error_without_identity_headers_is_not_a_violation() -> None:
    """An error without identity headers is classified by status, as today."""
    pin = _gateway_pin()

    assert check_gateway_error(pin, {}).violated is False
    assert check_gateway_error_exc(pin, _FailingGatewayRequest({})).violated is False


def test_gateway_error_group_mismatch_alone_is_not_a_violation() -> None:
    """Only the identity check covers successful responses: a wrong group on
    an error, without retry or fallback evidence, is classified by status."""
    pin = _gateway_pin()
    headers = {
        "x-litellm-model-group": "another-model",
        "x-litellm-attempted-fallbacks": "0",
    }

    assert check_gateway_error(pin, headers).violated is False


@pytest.mark.parametrize(
    "headers",
    [
        {"x-litellm-attempted-retries": "2"},
        {"x-litellm-attempted-fallbacks": "1"},
        {"x-litellm-model-group": "muse-spark-1.3-contributor",
         "x-litellm-attempted-retries": "2"},
        {"x-litellm-attempted-retries": "many"},
    ],
    ids=["retries-2", "fallbacks-1", "group-plus-retries", "unparseable"],
)
def test_gateway_error_evidence_is_a_violation(headers: dict[str, str]) -> None:
    """Retry or fallback evidence above 0 on an error is still a violation —
    including present-but-unparseable counts, which fail closed."""
    pin = _gateway_pin()

    direct = check_gateway_error(pin, headers)
    via_exc = check_gateway_error_exc(pin, _FailingGatewayRequest(headers))

    assert direct.violated is True
    assert via_exc.violated is True
    assert via_exc.gateway is not None


def _gateway_step_deps(
    pin: PinnedModel,
    transport: FakeGatewayTransport,
    attempt: StepAttemptFacts,
    workspace: Path,
    tmp_path: Path,
    ledger: RunLedger,
    workflow: V2Workflow,
) -> AgentStepDeps:
    """Step deps driving the real adapter over the fake gateway."""
    clock = _FakeClock()
    budgets = build_budgets(
        workflow,
        prices=_gateway_prices(),
        output_cap=0,
        attempt_id=attempt.attempt_id,
        opened_at=clock.now,
        clock=clock,
        is_gateway=True,
    )
    ledger.open_attempt(attempt.attempt_id, effective_configuration={})
    return AgentStepDeps(
        attempt=attempt,
        model=_gateway_model(pin, transport),
        workspace=workspace,
        tree_id="tree-1",
        archive_dir=tmp_path / "archive",
        budgets=budgets,
        prices=_gateway_prices(),
        ledger=ledger,
        is_gateway=True,
        pin=pin,
    )


def _gateway_violation_fixtures() -> (
    list[tuple[str, str | None, dict[str, object]]]
):
    return [
        (
            "litellm_step_report_no_identity_200.json",
            None,
            {
                "model_group": None,
                "model_id": None,
                "model_name": None,
                "attempted_fallbacks": None,
                "attempted_retries": 0,
                "call_id": "resp-step-report-no-identity",
                "litellm_version": None,
            },
        ),
        (
            "litellm_step_report_wrong_group_200.json",
            "another-model",
            {
                "model_group": "another-model",
                "model_id": "gateway-test-deployment-2",
                "model_name": "gateway-test-model",
                "attempted_fallbacks": 0,
                "attempted_retries": 0,
                "call_id": "call-step-report-wrong-group-1",
                "litellm_version": "1.99.0",
            },
        ),
        (
            "litellm_step_report_fallbacks_1_200.json",
            "muse-spark-1.3-contributor",
            {
                "model_group": "muse-spark-1.3-contributor",
                "model_id": "gateway-test-deployment-1",
                "model_name": "gateway-test-model",
                "attempted_fallbacks": 1,
                "attempted_retries": 0,
                "call_id": "call-step-report-fallbacks-1",
                "litellm_version": "1.99.0",
            },
        ),
        (
            "litellm_step_report_retries_2_200.json",
            "muse-spark-1.3-contributor",
            {
                "model_group": "muse-spark-1.3-contributor",
                "model_id": "gateway-test-deployment-1",
                "model_name": "gateway-test-model",
                "attempted_fallbacks": 0,
                "attempted_retries": 2,
                "call_id": "call-step-report-retries-2",
                "litellm_version": "1.99.0",
            },
        ),
    ]


def test_gateway_step_happy_response_records_full_identity(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A recorded happy response passes and records every ledger field."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder([recording_response("litellm_step_report_200.json")])
    )
    workflow = _gateway_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    outcome = run_agent_step(workflow.steps["process"], 1, deps, {})

    assert outcome.status == "reported"
    assert_suppression_on_wire(transport.requests[0].body)
    stored = ledger.records("implement#7/1")
    responses = [record for record in stored if record.kind == "model_response"]
    assert len(responses) == 1
    payload = responses[0].payload
    assert payload["response_model"] == "muse-spark-1.3-contributor"
    assert payload["gateway"] == {
        "model_group": "muse-spark-1.3-contributor",
        "model_id": "gateway-test-deployment-1",
        "model_name": "gateway-test-model",
        "attempted_fallbacks": 0,
        "attempted_retries": 0,
        "call_id": "call-step-report-1",
        "litellm_version": "1.99.0",
    }
    assert [record for record in stored if record.kind == "pin_violation"] == []


@pytest.mark.parametrize(
    "fixture_name,observed,expected_gateway", _gateway_violation_fixtures()
)
def test_gateway_step_violations_halt_without_using_the_response(
    ledger: RunLedger,
    workspace: Path,
    tmp_path: Path,
    fixture_name: str,
    observed: str | None,
    expected_gateway: dict[str, object],
) -> None:
    """Each success violation case ends the Attempt without using the reply."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder([recording_response(fixture_name)])
    )
    workflow = _gateway_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    with pytest.raises(PinViolation) as excinfo:
        run_agent_step(workflow.steps["process"], 1, deps, {})

    assert excinfo.value.observed == observed
    assert excinfo.value.expected == "muse-spark-1.3-contributor"
    assert excinfo.value.gateway is not None
    # Never used and never retried: one request went out, bearing the
    # per-request suppression, and the reply was discarded.
    assert len(transport.requests) == 1
    assert_suppression_on_wire(transport.requests[0].body)
    stored = ledger.records("implement#7/1")
    responses = [record for record in stored if record.kind == "model_response"]
    assert len(responses) == 1
    assert responses[0].payload["response_model"] == observed
    assert responses[0].payload["gateway"] == expected_gateway
    violations = [record for record in stored if record.kind == "pin_violation"]
    assert len(violations) == 1
    assert violations[0].payload["observed_model"] == observed
    assert violations[0].payload["gateway"] == expected_gateway


@pytest.mark.parametrize(
    "fixture_name",
    ["litellm_error_retry_500.json", "litellm_error_fallback_500.json"],
)
def test_gateway_step_error_violations_fail_closed(
    ledger: RunLedger,
    workspace: Path,
    tmp_path: Path,
    fixture_name: str,
) -> None:
    """An error carrying retry or fallback evidence fails closed at once."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder([recording_response(fixture_name)])
    )
    workflow = _gateway_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    with pytest.raises(PinViolation) as excinfo:
        run_agent_step(workflow.steps["process"], 1, deps, {})

    assert excinfo.value.response is None
    assert excinfo.value.gateway is not None
    # Fail closed: the first failure is the verdict — no provider retry.
    assert len(transport.requests) == 1
    stored = ledger.records("implement#7/1")
    assert len([r for r in stored if r.kind == "pin_violation"]) == 1
    # The hold settles at its worst case: the failure is still charged.
    assert deps.budgets is not None
    assert deps.budgets.step_totals("process").tokens > 0


def test_gateway_step_plain_error_is_classified_by_status(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """An error without identity headers is retried and raised, not a halt."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder([recording_response("litellm_error_plain_500.json")] * 3)
    )
    workflow = _gateway_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    with pytest.raises(ModelError):
        run_agent_step(workflow.steps["process"], 1, deps, {})

    # Classified by status as today: the retryable 500 runs the full Provider
    # retry, and no violation is recorded anywhere.
    assert len(transport.requests) == 3
    stored = ledger.records("implement#7/1")
    assert [r for r in stored if r.kind == "pin_violation"] == []


def _probe_no_identity_response() -> httpx.Response:
    """The happy probe body with the identity headers stripped."""
    status, _, body = load_recording("litellm_probe_ok_200.json")
    return httpx.Response(
        status, json=body, headers={"x-request-id": "req-probe-no-identity"}
    )


@pytest.mark.parametrize(
    "mutate",
    ["wrong-group", "no-identity", "fallbacks-1", "retries-2"],
    ids=["wrong-group", "no-identity", "fallbacks-1", "retries-2"],
)
def test_gateway_probe_violations_refuse_startup(mutate: str) -> None:
    """A violating probe response refuses startup with no Attempt opened."""
    pin = _gateway_pin()
    if mutate == "wrong-group":
        queued: httpx.Response = recording_response(
            "litellm_probe_ok_200.json",
            extra_headers={"x-litellm-model-group": "another-model"},
        )
    elif mutate == "no-identity":
        queued = _probe_no_identity_response()
    elif mutate == "retries-2":
        queued = recording_response(
            "litellm_probe_ok_200.json",
            extra_headers={"x-litellm-attempted-retries": "2"},
        )
    else:
        queued = recording_response(
            "litellm_probe_ok_200.json",
            extra_headers={"x-litellm-attempted-fallbacks": "1"},
        )
    transport = FakeGatewayTransport(queue_responder([queued]))

    with pytest.raises(ProviderCapabilityRefused, match="another model answered"):
        assert_provider_capability(
            pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
        )

    assert len(transport.requests) == 1
    transport.assert_only_responses_api()


def test_gateway_effort_probe_error_evidence_refuses_startup() -> None:
    """Retry evidence on the invalid-effort probe refuses startup too."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [recording_response("litellm_probe_ok_200.json")]
            + [recording_response("litellm_error_retry_500.json")] * 3
        )
    )

    with pytest.raises(
        ProviderCapabilityRefused, match="carried retry.*evidence"
    ):
        assert_provider_capability(
            pin, _gateway_model(pin, transport), _price_table(pin), max_tokens=32000
        )

    # One clean probe, then the effort probe's full provider retry — every
    # leg carries the suppression the gateway ignored.
    assert len(transport.requests) == 4
    for recorded in transport.requests:
        assert recorded.body["disable_fallbacks"] is True
        assert recorded.body["num_retries"] == 0
    transport.assert_only_responses_api()


def _gateway_spawn_workflow() -> V2Workflow:
    """A one-step workflow whose agent may spawn Sub-agents and report."""
    step = V2Step(
        id="process",
        agent=V2Agent(
            instructions=(),
            tools=("workspace-read", "subagents"),
            inputs=(),
            results={"done": {}},
        ),
        max_executions=1,
        routes={"done": V2RouteTarget(end="completed")},
    )
    return V2Workflow(
        id="implement",
        model="gateway",
        input=V2Input(type="issue", label="agent:test"),
        ceiling=V2Ceiling(
            cost_usd=100000.0,
            tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps={"process": step},
    )


def test_gateway_subagent_violation_ends_stopped(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A violating Sub-agent response ends stopped; the Attempt still halts."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [
                recording_response("litellm_spawn_ok_200.json"),
                recording_response(
                    "litellm_subagent_report_200.json",
                    extra_headers={"x-litellm-model-group": "another-model"},
                ),
            ]
        )
    )
    workflow = _gateway_spawn_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    with pytest.raises(PinViolation) as excinfo:
        run_agent_step(workflow.steps["process"], 1, deps, {})

    assert excinfo.value.call_id == "call_spawn_ok"
    assert excinfo.value.observed == "another-model"
    assert excinfo.value.gateway is not None
    stored = ledger.records("implement#7/1")
    violations = [record for record in stored if record.kind == "pin_violation"]
    assert len(violations) == 1
    assert violations[0].payload["call_id"] == "call_spawn_ok"
    ended = [record for record in stored if record.kind == "subagent_ended"]
    assert len(ended) == 1
    assert ended[0].payload["reported"] is False
    assert ended[0].payload["answering_model"] == "another-model"


def test_gateway_subagent_error_violation_charges_worst_case(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A failed Sub-agent request with retry evidence fails closed too."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [recording_response("litellm_spawn_ok_200.json")]
            + [recording_response("litellm_error_retry_500.json")] * 3
        )
    )
    workflow = _gateway_spawn_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    with pytest.raises(PinViolation) as excinfo:
        run_agent_step(workflow.steps["process"], 1, deps, {})

    assert excinfo.value.call_id == "call_spawn_ok"
    assert excinfo.value.response is None
    assert excinfo.value.gateway is not None
    assert excinfo.value.gateway.attempted_retries == 2
    # The spawn plus the Sub-agent's full provider retry — then closed.
    assert len(transport.requests) == 4
    stored = ledger.records("implement#7/1")
    assert len([r for r in stored if r.kind == "pin_violation"]) == 1
    assert deps.budgets is not None
    assert deps.budgets.step_totals("process").tokens > 0


def test_gateway_violation_ends_attempt_interrupted(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """End to end: a violating gateway response ends the Attempt
    ``interrupted`` / ``pin-violation`` — the Worker halt reason."""
    pin = _gateway_pin()
    transport = FakeGatewayTransport(
        queue_responder(
            [recording_response("litellm_step_report_wrong_group_200.json")]
        )
    )
    workflow = _gateway_workflow()
    deps = _gateway_step_deps(
        pin, transport, _gateway_facts("implement#7/1"), workspace, tmp_path, ledger, workflow
    )

    result = run_workflow(
        workflow=workflow,
        attempt_id="implement#7/1",
        ledger=ledger,
        agent_deps=deps,
    )

    assert result.outcome == "interrupted"
    assert result.reason == PIN_VIOLATION_REASON
    assert is_pin_violation_reason(result.reason) is True
    assert is_pin_violation_reason("credential-failure") is False
    stored = ledger.get_outcome("implement#7/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "pin-violation")


def test_synthesised_error_fixtures_are_marked() -> None:
    """The error-with-evidence fixtures are marked synthesised until #337
    replaces them with real recordings; mutated variants are not."""
    synthesised = (
        "litellm_error_retry_500.json",
        "litellm_error_fallback_500.json",
        "litellm_error_plain_500.json",
    )
    for name in synthesised:
        raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        assert raw["_synthesised"] is True
    recorded = (
        "litellm_probe_ok_200.json",
        "litellm_step_report_200.json",
        "litellm_step_report_no_identity_200.json",
        "litellm_step_report_wrong_group_200.json",
        "litellm_step_report_fallbacks_1_200.json",
        "litellm_step_report_retries_2_200.json",
        "litellm_spawn_ok_200.json",
        "litellm_subagent_report_200.json",
    )
    for name in recorded:
        raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        assert "_synthesised" not in raw
