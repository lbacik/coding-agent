"""A direct pin is asserted from each response's own evidence (§9.1).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9.1 (a direct
pin's response model must equal the pinned model id exactly — any reported
difference, including a vendor-resolved snapshot, is a pin violation; the
violating response is never used, never retried, its usage charged), §12
(the Attempt ends ``interrupted`` / ``pin-violation`` through the
runtime-only finalisation: the in-flight request is discarded, Sub-agents end
stopped, nothing is committed or pushed) and §16.3 (the Worker halts at once,
as on a Credential Failure; after a restart, recovery finishes the terminal
writes). At startup the probe response goes through the same check: a
mismatch refuses startup with no Attempt opened.

L3 behaviour tests driving these paths through the scripted model's observed
identity (``ScriptedModel(response_models=...)``), never through the pin key.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_DETAIL,
    PIN_VIOLATION_REASON,
    PinViolation,
)
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.graph.agent_step import (
    REPORT_TOOL_NAME,
    SPAWN_TOOL_NAME,
    AgentStepDeps,
    StepAttemptFacts,
    run_agent_step,
)
from coding_agent.runtime.graph.workflow_graph import WorkflowResult, run_workflow
from coding_agent.runtime.step_limits import build_budgets
from coding_agent.runtime.step_recovery import recover_attempt
from coding_agent.worker.run import (
    WorkerLoopConfig,
    is_pin_violation_reason,
    run_worker_loop,
)
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel

LABEL = "agent:implement"


def _pin(model: str = "model-a") -> PinnedModel:
    return PinnedModel(
        provider="anthropic", model=model, endpoint="messages", effort="low"
    )


def _prices() -> TokenPrices:
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


def _ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
    *,
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content=content, tool_calls=list(calls or []), usage_metadata=usage
    )


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {"cache_read": 0, "cache_creation": 0},
    }


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    return _call(
        REPORT_TOOL_NAME,
        {"result": result, "payload_json": json.dumps(payload)},
        call_id,
    )


def _facts(**overrides: object) -> StepAttemptFacts:
    base: dict[str, Any] = {
        "attempt_id": "implement#7/1",
        "workflow_id": "implement",
        "input_type": "issue",
        "input_number": 7,
        "title": "Fix the crash",
        "body": "The worker crashes on startup.",
        "base_revision": "abc123",
        "current_revision": "abc123",
    }
    base.update(overrides)
    return StepAttemptFacts(
        attempt_id=str(base["attempt_id"]),
        workflow_id=str(base["workflow_id"]),
        input_type=str(base["input_type"]),
        input_number=int(base["input_number"]),
        title=str(base["title"]),
        body=str(base["body"]),
        base_revision=str(base["base_revision"]),
        current_revision=str(base["current_revision"]),
    )


def _agent_step(step_id: str = "s") -> V2Step:
    return V2Step(
        id=step_id,
        agent=V2Agent(
            instructions=(),
            tools=("workspace-read", "subagents"),
            inputs=(),
            results={"done": {}},
        ),
        max_executions=1,
        routes={"done": V2RouteTarget(end="completed")},
    )


def _workflow(workflow_id: str = "w", step_id: str = "s") -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type="issue", label="agent:test"),
        ceiling=V2Ceiling(
            cost_usd=200, tokens=100000, tool_calls=100, wall_clock_minutes=50
        ),
        start=step_id,
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps={step_id: _agent_step(step_id)},
    )


def _deps(
    attempt: StepAttemptFacts,
    model: ScriptedModel,
    workspace: Path,
    tmp_path: Path,
    **overrides: Any,
) -> AgentStepDeps:
    params: dict[str, Any] = {
        "attempt": attempt,
        "model": model,
        "workspace": workspace,
        "tree_id": "tree-1",
        "archive_dir": tmp_path / "archive",
        "pin": _pin(),
    }
    params.update(overrides)
    return AgentStepDeps(**params)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = TrackedInput(
            number=number,
            input_type="issue",
            title=f"Add a greeting {number}",
            body="body",
            is_open=True,
            labels=(LABEL,),
            assignees=(),
            created_at=created_at,
            author_login="human",
            author_id=7,
        )
        tracker.labeled_events[(number, LABEL)] = LabeledEvent(
            label=LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at=created_at,
        )
    return tracker


# --- §9.1: the step loop asserts the pin -------------------------------------


def test_step_loop_violation_raises_without_using_the_response(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§9.1: a mismatching step response is never used — it raises before the
    conversation, the retry or the report could touch it."""
    step = _agent_step()
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {})])],
        response_models=["model-b"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)

    with pytest.raises(PinViolation) as excinfo:
        run_agent_step(step, 1, deps, {})

    assert excinfo.value.observed == "model-b"
    assert excinfo.value.expected == "model-a"
    # Never carried back to the model and never retried: one request went
    # out, and the single queued response is spent.
    assert len(model.invocations) == 1
    assert model._outcomes == []
    # Its identity and the violation are recorded beside each other.
    stored = [record for record in ledger.records(attempt.attempt_id)]
    response_events = [record for record in stored if record.kind == "model_response"]
    assert len(response_events) == 1
    assert response_events[0].payload["response_model"] == "model-b"
    violations = [record for record in stored if record.kind == "pin_violation"]
    assert len(violations) == 1
    assert violations[0].payload["expected_model"] == "model-a"
    assert violations[0].payload["observed_model"] == "model-b"


def test_step_loop_violation_charges_the_response_usage(
    workspace: Path, tmp_path: Path
) -> None:
    """§9.1, §11.4: the violating response settles its hold with its actual
    meter spend — charged, never the worst case."""
    workflow = _workflow()
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {})], usage=_usage(10, 5))],
        response_models=["model-b"],
    )
    budgets = build_budgets(
        workflow, prices=_prices(), output_cap=0, attempt_id=attempt.attempt_id
    )
    deps = _deps(attempt, model, workspace, tmp_path, budgets=budgets)

    with pytest.raises(PinViolation):
        run_agent_step(workflow.steps["s"], 1, deps, {})

    totals = budgets.step_totals("s")
    assert totals.tokens == 15
    assert totals.input_tokens == 10
    assert totals.output_tokens == 5


def test_step_loop_matching_response_behaves_as_before(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A response echoing the pinned id exactly is unchanged: reported."""
    step = _agent_step()
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {})])],
        response_models=["model-a"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)

    outcome = run_agent_step(step, 1, deps, {})

    assert outcome.status == "reported"
    stored = [record for record in ledger.records(attempt.attempt_id)]
    assert [record.kind for record in stored].count("pin_violation") == 0


def test_step_loop_unreported_response_is_not_a_violation(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """A response carrying no model reports nothing to assert against."""
    step = _agent_step()
    attempt = _facts()
    model = ScriptedModel(
        [_ai([_report_call("done", {})])],
        response_models=[None],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)

    outcome = run_agent_step(step, 1, deps, {})

    assert outcome.status == "reported"


# --- §12: the Attempt ends interrupted / pin-violation ------------------------


def test_workflow_violation_ends_interrupted_without_commit_or_push(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§12: the violating Attempt ends ``interrupted`` / ``pin-violation``
    through the runtime-only finalisation — no retry, no commit, no push."""
    workflow = _workflow()
    attempt = _facts(attempt_id="w#1/1", workflow_id="w")
    model = ScriptedModel(
        [_ai([_report_call("done", {})])],
        response_models=["model-b"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)

    result = run_workflow(
        workflow=workflow, attempt_id="w#1/1", ledger=ledger, agent_deps=deps
    )

    assert result.outcome == "interrupted"
    assert result.reason == PIN_VIOLATION_REASON
    assert is_pin_violation_reason(result.reason) is True
    assert is_pin_violation_reason("credential-failure") is False
    # Never retried: the single queued response is spent on the first request.
    assert len(model.invocations) == 1
    # The runtime-only finalisation: every open execution is closed as
    # interrupted, nothing is committed or pushed, and the terminal writes
    # are recorded for the Supervisor (and recovery) to perform.
    stored = ledger.get_outcome("w#1/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("interrupted", "pin-violation")
    assert PIN_VIOLATION_DETAIL in (stored.terminal_comment_body or "")
    assert list(stored.writes) == [
        "terminal-comment",
        "remove-selection-label",
        "add-end-labels",
        "release-assignment",
    ]
    assert workspace.joinpath("README.md").read_text(encoding="utf-8") == "hello\n"


def test_subagent_violation_ends_stopped_with_no_report(
    ledger: RunLedger, workspace: Path, tmp_path: Path
) -> None:
    """§9.8, §12: a violating Sub-agent ends stopped with no report; the
    Attempt still ends ``interrupted`` / ``pin-violation``."""
    workflow = _workflow()
    attempt = _facts(attempt_id="w#2/1", workflow_id="w")
    model = ScriptedModel(
        [
            _ai(
                [
                    _call(
                        SPAWN_TOOL_NAME,
                        {"description": "look", "instructions": "read files"},
                        "s1",
                    )
                ]
            ),
            _ai(
                [
                    _call(
                        "report_subagent",
                        {"report": "findings"},
                        "sub-report-1",
                    )
                ]
            ),
        ],
        response_models=["model-a", "model-other"],
    )
    deps = _deps(attempt, model, workspace, tmp_path, ledger=ledger)

    result = run_workflow(
        workflow=workflow, attempt_id="w#2/1", ledger=ledger, agent_deps=deps
    )

    assert (result.outcome, result.reason) == ("interrupted", "pin-violation")
    stored = [record for record in ledger.records(attempt.attempt_id)]
    violations = [record for record in stored if record.kind == "pin_violation"]
    assert len(violations) == 1
    assert violations[0].payload["call_id"] == "s1"
    ended = [record for record in stored if record.kind == "subagent_ended"]
    assert len(ended) == 1
    assert ended[0].payload["reported"] is False
    assert ended[0].payload["answering_model"] == "model-other"


# --- §16.3: the Worker halts; recovery finishes the writes --------------------


def test_worker_halts_at_once_on_a_recorded_pin_violation(
    ledger: RunLedger,
) -> None:
    """§16.3: a pin violation halts the Worker like a Credential Failure —
    the next candidate is never claimed."""
    tracker = _tracker_with((31, "2026-10-01T00:00:00Z"), (32, "2026-10-02T00:00:00Z"))
    ran: list[int] = []

    def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
        ran.append(claimed.input_number)
        ledger.record_terminal_outcome(
            claimed.attempt_id, outcome="interrupted", reason="pin-violation"
        )
        return WorkflowResult(
            attempt_id=claimed.attempt_id,
            outcome="interrupted",
            reason="pin-violation",
        )

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=WorkerLoopConfig(
            claim_config=ClaimConfig(
                workflow_id="implement",
                input_type="issue",
                selection_label=LABEL,
                base_branch="main",
                agent_login="coding-agent",
                agent_id=999,
                effective_configuration={"workflow": "implement"},
            ),
            workflow=_workflow("implement"),
            poll_interval_seconds=60.0,
        ),
        run_one_attempt=run_one,
        sleep=lambda seconds: None,
        max_attempts=5,
    )

    assert exit_code == 1
    assert ran == [31]
    outcome = ledger.get_outcome("implement#31/1")
    assert outcome is not None
    assert outcome.reason == "pin-violation"


def test_recovery_finishes_a_recorded_pin_violation(
    ledger: RunLedger,
) -> None:
    """§16.1, REC-13: after the halt, a restart finishes the recorded
    outcome — the terminal writes land, never re-decided."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "w"})
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    ledger.record_terminal_outcome(
        "implement#7/1",
        outcome="interrupted",
        reason="pin-violation",
        terminal_comment_body="wip",
        writes=[
            "terminal-comment",
            "remove-selection-label",
            "add-end-labels",
            "release-assignment",
        ],
    )
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    recovered = recover_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id="implement#7/1",
        number=7,
        input_type="issue",
        selection_label=LABEL,
    )

    assert recovered.finished is True
    assert (recovered.outcome, recovered.reason) == ("interrupted", "pin-violation")
    assert ledger.get_attempt("implement#7/1").state == "finished"
    assert tracker.call_names().count("post_comment") == 1


# --- Startup: the probe is asserted the same way --------------------------------


def test_probe_mismatch_refuses_startup_with_no_attempt_opened(
    tmp_path: Path,
) -> None:
    """§4, §9.1: a probe response naming another model refuses startup —
    no Attempt is opened and the effort probe never runs."""
    from langchain_core.messages import BaseMessage

    from coding_agent.provider.price_table import PriceTable

    pin = _pin()
    price_table: PriceTable = {pin.key: _prices()}

    class _ProbeModel:
        calls = 0

        def bind_tools(
            self, tools: Any, tool_choice: str | None = None
        ) -> "_ProbeModel":
            return self

        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            type(self).calls += 1
            assert type(self).calls == 1, "no second call after the refusal"
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "capability_probe",
                        "args": {"city": "Oslo"},
                        "id": "probe-1",
                        "type": "tool_call",
                    }
                ],
                usage_metadata=_usage(10, 5),
                response_metadata={"model": "model-b"},
            )

    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"
    with pytest.raises(ProviderCapabilityRefused, match="must report the pinned"):
        assert_provider_capability(pin, _ProbeModel(), price_table)  # type: ignore[arg-type]

    assert not ledger_path.exists()


def test_probe_match_still_passes() -> None:
    """A probe echoing the pinned id passes the identity check (the effort
    probe then runs as before)."""
    from langchain_core.exceptions import ModelInvalidRequestError
    from langchain_core.messages import BaseMessage

    from coding_agent.provider.price_table import PriceTable

    pin = _pin()
    price_table: PriceTable = {pin.key: _prices()}

    class _ProbeModel:
        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(
            self, tools: Any, tool_choice: str | None = None
        ) -> "_ProbeModel":
            return self

        def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "capability_probe",
                            "args": {"city": "Oslo"},
                            "id": "probe-1",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata=_usage(10, 5),
                    response_metadata={"model": "model-a"},
                )
            raise ModelInvalidRequestError("reasoning_effort is invalid: foo")

    result = assert_provider_capability(pin, _ProbeModel(), price_table)  # type: ignore[arg-type]
    assert result.pin is pin
