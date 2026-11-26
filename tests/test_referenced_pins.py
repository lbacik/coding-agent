"""Referenced pins: one assertion per pin, per-pin status, pin-bound requests.

Contract: step-workflow contract §4.1 step 7 (the Provider Capability
Assertion runs for every distinct pin the selected Workflow references) and
§9.1 (every pin-bound check reads the requesting conversation's pin).

Part of #679 (first of two slices). No YAML can express two pins yet, so the
startup tests drive a two-``models`` configuration with ``referenced_pins``
monkeypatched to name both — the test seam the Advisor slice replaces with
real per-step pins.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.operator import offline as offline_reader
from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    ProviderCapabilityReport,
)
from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.pin_witness import PinWitness
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.graph.agent_model_request import (
    RequestPin,
    run_model_request,
)
from coding_agent.runtime.graph.agent_step import ExecutionTally
from coding_agent.workflow.schema import (
    Agent,
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
from test_worker_startup_progress import _fakes, _run
from worker_fakes import WorkerFakes
from coding_agent.identity.startup import Identity

IDENTITY = Identity(account_id=999, login="coding-agent")

_TWO_PIN_CONFIG = """\
version: 4

target:
  kind: github
  repository: example-owner/example-repo
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000
  opus:
    provider: openai
    model: gpt-5.6-luna
    endpoint: responses
    effort: medium
    api-key-env: OPENAI_API_KEY
    prices-usd-per-mtok: {input: 5.00, output: 25.00, cache-read: 0.50, cache-write: 6.25}
    context-window-tokens: 200000
    context-threshold-tokens: 40000
    output-cap-tokens: 16000

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: sonnet
    input: {type: issue, label: agent:implement}
    limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      work:
        agent:
          tools: [workspace-read]
          results:
            done: {}
        routes: {done: {end: completed}}
"""


def _write_two_pin_config(tmp_path: Path) -> Path:
    """A v4 configuration with two ``models`` entries, one Workflow pin."""
    path = tmp_path / "workflow.yml"
    path.write_text(_TWO_PIN_CONFIG, encoding="utf-8")
    return path


def _tracker() -> Any:
    from support.fake_step_tracker import FakeStepTracker

    return FakeStepTracker(
        agent_login=IDENTITY.login, agent_id=IDENTITY.account_id
    )


def _two_pin_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fakes: WorkerFakes,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, Path, str]:
    """Run the Worker over the two-pin config; return exit, state dir, stderr."""
    from test_worker_startup_progress import _write_run_matrix

    config = _write_two_pin_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setattr(
        Workflow, "referenced_pins", property(lambda self: ("sonnet", "opus"))
    )
    exit_code, _ = _run(
        tmp_path,
        monkeypatch,
        fakes.github.tracker or _tracker(),
        config=config,
        max_sweeps=1,
        fakes=fakes,
    )
    err = capsys.readouterr().err
    return exit_code, tmp_path / "state", err


def test_referenced_pins_is_the_workflow_pin_today() -> None:
    """The single source names exactly the Workflow's own pin (§4.1)."""
    assert make_workflow().referenced_pins == ("sonnet",)


def test_startup_asserts_every_referenced_pin_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both pins are probed in assertion order; both reports persist by pin id."""
    fakes = _fakes(monkeypatch, _tracker(), ScriptedModel([]))
    exit_code, state_dir, _ = _two_pin_run(tmp_path, monkeypatch, fakes, capsys)

    assert exit_code == 0
    assert [pin.model for pin in fakes.model.build_calls] == [
        "claude-sonnet-5",
        "gpt-5.6-luna",
    ]
    result = offline_reader.load_offline_status(
        state_dir / "run-ledger.sqlite3", state_dir
    )
    capability = result["provider_capability"]
    assert isinstance(capability, dict)
    assert set(capability) == {"sonnet", "opus"}
    assert capability["sonnet"]["pin_key"] == "anthropic:claude-sonnet-5@messages"
    assert capability["opus"]["pin_key"] == "openai:gpt-5.6-luna@responses"


def test_startup_refusal_names_the_second_pin_and_keeps_both_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An opus refusal names the pin, opens no Attempt, keeps both reports."""
    fakes = _fakes(monkeypatch, _tracker(), ScriptedModel([]))
    real_assert = fakes.model.assert_provider_capability
    opus_report = ProviderCapabilityReport(
        pin_key="openai:gpt-5.6-luna@responses",
        ran_at="2026-11-18T00:00:00Z",
        checks=(),
    )

    def _assert_per_pin(
        pin: PinnedModel, model: Any, price_table: Any, *, max_tokens: int | None
    ) -> Any:
        if pin.model == "gpt-5.6-luna":
            raise ProviderCapabilityRefused(
                "opus refused the capability probe", report=opus_report
            )
        return real_assert(pin, model, price_table, max_tokens=max_tokens)

    monkeypatch.setattr(
        fakes.model, "assert_provider_capability", _assert_per_pin
    )
    exit_code, state_dir, err = _two_pin_run(tmp_path, monkeypatch, fakes, capsys)

    assert exit_code == 1
    assert "run: startup refused at provider-capability after " in err
    assert "provider-capability-refused: pin 'opus'" in err
    # The Workflow's own pin was still probed first, in order.
    assert [pin.model for pin in fakes.model.build_calls] == [
        "claude-sonnet-5",
        "gpt-5.6-luna",
    ]
    # No Attempt opened: the Run Ledger holds no Attempt at all.
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        assert ledger.unfinished_attempts() == ()
    finally:
        ledger.close()
    # Both reports persist: the sonnet pass and the opus refusal.
    result = offline_reader.load_offline_status(
        state_dir / "run-ledger.sqlite3", state_dir
    )
    capability = result["provider_capability"]
    assert isinstance(capability, dict)
    assert set(capability) == {"sonnet", "opus"}
    assert (
        capability["sonnet"]["pin_key"] == "anthropic:claude-sonnet-5@messages"
    )
    assert capability["opus"] == opus_report.to_dict()
    assert result["instance"]["last_halt"] is not None
    assert (
        result["instance"]["last_halt"]["reason"] == "startup-check-refused"
    )


def _second_pin() -> tuple[PinnedModel, TokenPrices, ContextWindowConfig]:
    """A distinct second pin: other identity, prices, cap and threshold."""
    pin = PinnedModel(
        provider="anthropic",
        model="advisor-model",
        endpoint="messages",
        effort="medium",
    )
    prices = TokenPrices(input=3.0, output=7.0, cache_read=0.3, cache_write=3.5)
    window = ContextWindowConfig(threshold_tokens=50000)
    return pin, prices, window


class _CapturingModel(ScriptedModel):
    """A ScriptedModel with typed access to the recorded invoke kwargs.

    The base ``FakeChatModel.invoke`` already records every call's kwargs
    in ``invoke_kwargs`` — one entry per request — so this subclass only
    redeclares the attribute type and adds no recording of its own.
    """

    invoke_kwargs: list[dict[str, Any]]

    def __init__(
        self,
        outcomes: list[AIMessage],
        *,
        response_models: Sequence[str | None] | None = None,
    ) -> None:
        super().__init__(outcomes, response_models=response_models)


def _request_step() -> WorkflowStep:
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


def test_request_on_second_pin_holds_and_settles_at_its_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission, ``max_tokens`` and the guard read the requesting pin (§9.1)."""
    pin, prices, window = _second_pin()
    request_pin = RequestPin.from_pin(
        pin, prices, output_cap=500, context_window=window
    )
    usage = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}
    model = _CapturingModel(
        [AIMessage(content="ok", usage_metadata=dict(usage))],
        response_models=[pin.model],
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    step = _request_step()
    budgets = make_budgets(
        make_workflow(), attempt_id="implement#7/1", prices=make_prices()
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        budgets=budgets,
        output_cap=111,
        context_window=None,
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
        pin=pin,
        step_id=step.id,
        seq=1,
    )
    seen: dict[str, Any] = {}
    original_admit = deps.budgets.admit_request

    def _spy(step_id: str, estimate: int, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return original_admit(step_id, estimate, **kwargs)

    monkeypatch.setattr(deps.budgets, "admit_request", _spy)
    messages: list[BaseMessage] = [HumanMessage(content="hello")]
    result = run_model_request(
        deps,
        witness,
        step.id,
        1,
        model.bind_tools([]),
        messages,
        [],
        request_pin=request_pin,
    )

    assert isinstance(result, AIMessage)
    # The Admission hold priced the second pin: its cap, prices and kind.
    assert seen["output_cap"] == 500
    assert seen["prices"] == prices
    assert seen["is_gateway"] is False
    # The provider call carried the second pin's output cap.
    assert model.invoke_kwargs == [{"max_tokens": 500}]
    # Settlement charged the second pin's prices, not the Attempt's.
    assert deps.budgets.attempt_totals().cost_usd == pytest.approx(
        1000 / 1_000_000 * 3.0 + 200 / 1_000_000 * 7.0
    )
    # The recorded Usable Threshold is the second pin's default.
    responses = [event for event in tally.events if event["kind"] == "model_response"]
    assert len(responses) == 1
    payload = responses[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["usable_threshold"] == 50000 - 1000 - 1000


def test_identity_matching_workflow_pin_but_not_conversation_pin_violates(
    tmp_path: Path,
) -> None:
    """A response naming the Workflow pin fails the conversation's check (§9.1)."""
    pin, prices, window = _second_pin()
    request_pin = RequestPin.from_pin(
        pin, prices, output_cap=500, context_window=window
    )
    usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    model = ScriptedModel(
        [AIMessage(content="ok", usage_metadata=dict(usage))],
        response_models=["test-model"],
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    step = _request_step()
    budgets = make_budgets(
        make_workflow(), attempt_id="implement#7/1", prices=make_prices()
    )
    deps = make_agent_step_deps(
        model=model,
        workspace=workspace,
        tmp_path=tmp_path,
        budgets=budgets,
    )
    assert deps.pin.model == "test-model"
    tally = ExecutionTally(
        deps=deps,
        step=step,
        seq=1,
        baseline=deps.budgets.step_totals(step.id),
    )
    witness = PinWitness(
        record_event=tally.record_event,
        attempt_id=deps.attempt.attempt_id,
        pin=pin,
        step_id=step.id,
        seq=1,
    )
    messages: list[BaseMessage] = [HumanMessage(content="hello")]
    with pytest.raises(PinViolation):
        run_model_request(
            deps,
            witness,
            step.id,
            1,
            model.bind_tools([]),
            messages,
            [],
            request_pin=request_pin,
        )
    # The existing reaction: the response is discarded and its usage charged.
    assert len(messages) == 1
    assert deps.budgets.attempt_totals().cost_usd > 0
