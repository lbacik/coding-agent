"""One model request in an `agent` Step Execution, driven directly.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.1 (the pin
is asserted from each response's own evidence), §11.4 (admission with holds
and settlement), §11.5 (deadlines), §11.7 (the ``limit-exceeded`` fact) and
§13.2 (Ledger events).

Part of #487 (slice 3 of 4). A pure unit suite: every path of
``request_model_response`` is driven here directly, with a scripted model
and the existing ``AgentStepDeps`` test builder, without running
``run_agent_step``. The pin verdict functions are called by the module, never
copied here — the tests only assert their outcomes.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.exceptions import ModelError, ModelRateLimitError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.pin_witness import PinWitness
from coding_agent.runtime.context_handoff import estimate_context_tokens
from coding_agent.runtime.graph.agent_model_request import request_model_response
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    AgentStepOutcome,
    ExecutionTally,
)
from coding_agent.runtime.step_limits import worst_case_bound
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.agent_step_deps import (
    make_agent_step_deps,
    make_budgets,
    make_prices,
    make_workflow,
)
from support.scripted_model import ScriptedModel


def _step() -> WorkflowStep:
    """One `agent` step: the request path never reports, so one result suffices."""
    return WorkflowStep(
        id="process",
        agent=Agent(
            instructions=(),
            tools=(),
            inputs=(),
            results={"done": {"note": "string"}},
        ),
        max_executions=1,
        limits={},
        reserved=False,
        routes={"done": RouteTarget(end="completed")},
    )


def _workflow(tokens: float = 100000.0) -> Workflow:
    """A workflow with a generous ceiling except the token bound under test."""
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            tokens=tokens,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
    )


def _ai(
    *,
    usage: dict[str, object] | None = None,
    content: str = "",
) -> AIMessage:
    """One scripted assistant turn, usage or none."""
    return AIMessage(content=content, tool_calls=[], usage_metadata=usage)


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    """Complete usage metadata: both counts present (§11.4)."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _harness(
    tmp_path: Path,
    model: Any,
    *,
    tokens: float = 100000.0,
    content: str = "hello",
    code_owned_model_timeout: float | None = None,
) -> tuple[AgentStepDeps, ExecutionTally, PinWitness, list[BaseMessage], Any]:
    """Deps, tally, witness, messages and bound model the way the loop builds them."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    step = _step()
    budgets = make_budgets(
        _workflow(tokens) if tokens != 100000.0 else make_workflow(),
        attempt_id="implement#7/1",
        prices=make_prices(),
        output_cap=0,
    )
    if code_owned_model_timeout is not None:
        budgets.code_owned_model_timeout = code_owned_model_timeout
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        budgets=budgets,
    )
    tally = ExecutionTally(
        deps=deps,
        step=step,
        seq=1,
        baseline=deps.budgets.step_totals(step.id),
    )
    witness = PinWitness(
        record_event=tally.record_event,
        attempt_id=deps.attempt.attempt_id,
        pin=deps.pin,
        step_id=step.id,
        seq=1,
    )
    messages: list[BaseMessage] = [HumanMessage(content=content)]
    bound_model = deps.model.bind_tools([])
    return deps, tally, witness, messages, bound_model


def _worst_case(deps: AgentStepDeps, messages: list[BaseMessage]) -> int:
    """The admission bound the request holds while in flight (§11.4)."""
    bound_tokens, _ = worst_case_bound(
        estimate_context_tokens(list(messages), [], []),
        deps.output_cap,
        deps.prices,
    )
    return bound_tokens


def test_success_with_full_usage_settles_actual(tmp_path: Path) -> None:
    """A complete response settles its hold with its actual meter spend (§11.4)."""
    model = ScriptedModel(
        [_ai(usage=_usage(10, 5))],
        response_models=["test-model"],
    )
    deps, tally, witness, messages, bound = _harness(tmp_path, model)

    result = request_model_response(
        deps, tally, witness, "process", 1, bound, messages, []
    )

    assert isinstance(result, AIMessage)
    assert messages == [messages[0], result]
    assert tally.model_calls == 1
    totals = deps.budgets.step_totals("process")
    assert (totals.tokens, totals.input_tokens, totals.output_tokens) == (15, 10, 5)
    assert deps.budgets.open_hold_ids() == ()
    assert [event["kind"] for event in tally.events] == ["model_response"]
    payload = tally.events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["response_model"] == "test-model"


def test_missing_usage_charges_worst_case(tmp_path: Path) -> None:
    """A response without usage settles at its hold's worst case, never zero (§11.4)."""
    model = ScriptedModel(
        [_ai()],
        response_models=["test-model"],
    )
    deps, tally, witness, messages, bound = _harness(tmp_path, model)
    expected = _worst_case(deps, messages)

    result = request_model_response(
        deps, tally, witness, "process", 1, bound, messages, []
    )

    assert isinstance(result, AIMessage)
    assert messages == [messages[0], result]
    assert tally.model_calls == 1
    assert deps.budgets.step_totals("process").tokens == expected
    assert expected > 0
    assert deps.budgets.open_hold_ids() == ()
    # Missing usage is not a pin violation: the response is still recorded.
    assert [event["kind"] for event in tally.events] == ["model_response"]


def test_admission_refused_returns_limit_without_model_call(tmp_path: Path) -> None:
    """A refused admission ends the step: the request is never sent (§11.4)."""
    model = ScriptedModel(
        [_ai(usage=_usage(10, 5))],
        response_models=["test-model"],
    )
    deps, tally, witness, messages, bound = _harness(
        tmp_path, model, tokens=5.0, content="hello " * 100
    )

    result = request_model_response(
        deps, tally, witness, "process", 1, bound, messages, []
    )

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "limit-exceeded"
    assert "the worst-case bound exceeds the" in result.detail
    assert model.invocations == []
    assert len(messages) == 1
    assert tally.model_calls == 0
    assert tally.events == []
    assert deps.budgets.open_hold_ids() == ()


class _FailOnceModel:
    """A model failing the first invoke, then answering: the retry seam."""

    def __init__(self, error: Exception, response: AIMessage) -> None:
        self._error = error
        self._response = response
        self.calls = 0

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> _FailOnceModel:
        _ = (tools, tool_choice)
        return self

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        _ = kwargs
        self.calls += 1
        if self.calls == 1:
            raise self._error
        return self._response


def _stamped(content: AIMessage) -> AIMessage:
    """The scripted answer carrying the pinned identity, like ScriptedModel stamps it."""
    metadata = dict(content.response_metadata or {})
    metadata["model"] = "test-model"
    return content.model_copy(update={"response_metadata": metadata})


def test_retryable_error_then_success_charges_first_hold(tmp_path: Path) -> None:
    """A retryable failure charges its hold; the retry holds anew and settles (§11.4)."""
    model = _FailOnceModel(
        ModelRateLimitError("slow down"),
        _stamped(_ai(usage=_usage(10, 5))),
    )
    deps, tally, witness, messages, bound = _harness(tmp_path, model)
    expected_first = _worst_case(deps, messages)

    result = request_model_response(
        deps, tally, witness, "process", 1, bound, messages, []
    )

    assert isinstance(result, AIMessage)
    assert model.calls == 2
    assert messages == [messages[0], result]
    assert tally.model_calls == 1
    # Two holds: the first charged at worst case, the retry settled at actual.
    assert deps.budgets.step_totals("process").tokens == expected_first + 15
    assert deps.budgets.open_hold_ids() == ()


def test_non_retryable_error_reraised_and_hold_charged(tmp_path: Path) -> None:
    """A non-retryable failure is re-raised: one call, worst-case charge (§11.4)."""
    model = _FailOnceModel(ModelError("boom"), _stamped(_ai(usage=_usage(10, 5))))
    deps, tally, witness, messages, bound = _harness(tmp_path, model)
    expected = _worst_case(deps, messages)

    with pytest.raises(ModelError, match="boom"):
        request_model_response(deps, tally, witness, "process", 1, bound, messages, [])

    assert model.calls == 1
    assert len(messages) == 1
    assert tally.model_calls == 0
    assert deps.budgets.step_totals("process").tokens == expected
    assert deps.budgets.open_hold_ids() == ()


def test_per_request_timeout_returns_deadline(tmp_path: Path) -> None:
    """A request running past its model timeout ends on the deadline (§11.5)."""
    model = ScriptedModel(
        [_ai(usage=_usage(10, 5))],
        response_models=["test-model"],
    )
    deps, tally, witness, messages, bound = _harness(
        tmp_path, model, code_owned_model_timeout=0.0
    )
    expected = _worst_case(deps, messages)

    result = request_model_response(
        deps, tally, witness, "process", 1, bound, messages, []
    )

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "limit-exceeded"
    assert result.detail == "the model request ran into the remaining time"
    assert result.limit_fact is not None
    assert result.limit_fact["moment"] == "deadline"
    assert len(messages) == 1
    assert tally.model_calls == 0
    assert deps.budgets.step_totals("process").tokens == expected
    assert deps.budgets.open_hold_ids() == ()


def test_crossed_limit_after_spend_returns_before_model_response(
    tmp_path: Path,
) -> None:
    """The spend check fires before the ``model_response`` event (#527 stays open)."""
    model = ScriptedModel(
        [_ai(usage=_usage(5000, 10))],
        response_models=["test-model"],
    )
    deps, tally, witness, messages, bound = _harness(tmp_path, model, tokens=1000.0)

    result = request_model_response(
        deps, tally, witness, "process", 1, bound, messages, []
    )

    assert isinstance(result, AgentStepOutcome)
    assert result.status == "limit-exceeded"
    assert "the spend crossed the" in result.detail
    assert tally.model_calls == 1
    # The response still joins the conversation, but no event records it yet.
    assert len(messages) == 2
    assert isinstance(messages[1], AIMessage)
    assert tally.events == []
    assert deps.budgets.open_hold_ids() == ()


def test_identity_pin_violation_raises_and_settles_actual(tmp_path: Path) -> None:
    """A mismatching response is never used: raised, charged at actual (§9.1)."""
    model = ScriptedModel(
        [_ai(usage=_usage(10, 5))],
        response_models=["other-model"],
    )
    deps, tally, witness, messages, bound = _harness(
        tmp_path, model, content="x" * 4000
    )

    with pytest.raises(PinViolation) as excinfo:
        request_model_response(deps, tally, witness, "process", 1, bound, messages, [])

    assert excinfo.value.observed == "other-model"
    assert excinfo.value.expected == "test-model"
    assert tally.model_calls == 1
    # The violation settles at actual spend, never the worst case.
    assert deps.budgets.step_totals("process").tokens == 15
    assert deps.budgets.open_hold_ids() == ()
    assert len(messages) == 1
    assert [event["kind"] for event in tally.events] == [
        "model_response",
        "pin_violation",
    ]
