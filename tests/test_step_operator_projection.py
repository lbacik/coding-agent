"""The step-workflow operator-contract projection (contract §19, S9).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §19 (the
operator projection) and §4.5 (Attempt Archives). The native vocabulary is
producer-owned: ``outcome.native`` is the Terminal Outcome, delivery comes
from the ``open-pull-request`` receipt, Attempt ids read
``<workflow>#<N>/<n>``, the native phase is the step id plus the Step
Execution number, the budget projects the four meters from the Attempt
Ceiling and its spend, ``next-issue`` is advertised only by ``issue``
Workers, and the archive carries the ``native.`` step events beside the
Step Artifacts and Execution Summaries.

Matrix row ids: OPC-1 … OPC-7 (OPC-8 is ``conformance/producer-suite.sh``,
which needs Docker and never runs in this suite).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent.ledger.store import RunLedger
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator import listener as operator_listener
from coding_agent.operator import step_projection as projection
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.graph.agent_step import store_step_artifacts
from coding_agent.runtime.graph.workflow_graph import run_workflow
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2FieldType,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "limits": {
                "cost_usd": 20.0,
                "tokens": 100_000,
                "tool_calls": 200,
                "wall_clock_minutes": 60,
            }
        }
    }


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "state" / "run-ledger.sqlite3")


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = _ledger(tmp_path)
    yield handle
    handle.close()


def _live() -> LiveView:
    live = LiveView(
        process_started_at=datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
    )
    live.set_ready()
    return live


def _end(category: str) -> V2RouteTarget:
    return V2RouteTarget(end=category)


def _workflow() -> V2Workflow:
    def step(
        step_id: str,
        results: dict[str, dict[str, V2FieldType]],
        routes: dict[str, str | V2RouteTarget],
        *,
        max_executions: int = 1,
    ) -> V2Step:
        parsed = {
            name: target if isinstance(target, V2RouteTarget) else V2RouteTarget(step=target)
            for name, target in routes.items()
        }
        return V2Step(
            id=step_id,
            agent=V2Agent(results=results),
            max_executions=max_executions,
            routes=parsed,
        )

    return V2Workflow(
        id="w",
        model="sonnet",
        input=V2Input(type="issue", label="agent:test"),
        ceiling=V2Ceiling(
            cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="aaa",
        defaults_routes={
            "error": _end("failed"),
            "limit-exceeded": _end("unfinished"),
        },
        steps={
            "aaa": step("aaa", {"done": {}}, {"done": "zzz"}),
            "zzz": step("zzz", {"done": {}}, {"done": _end("completed")}),
        },
    )


COMPLETED_REASON = "ended at end completed after result opened of step open-pr"


def _open_finished_attempt(
    ledger: RunLedger,
    attempt_id: str,
    *,
    outcome: str | None = "completed",
) -> None:
    """One finished step Attempt: two executions, a route, an end, an outcome."""
    ledger.open_attempt(attempt_id, effective_configuration=_effective_config())
    ledger.open_step_execution(attempt_id, step_id="aaa", seq=1)
    ledger.end_step_execution(
        attempt_id,
        step_id="aaa",
        seq=1,
        result="done",
        usage={
            "cost_usd": 0.02,
            "effective_tokens": 400,
            "tool_calls": 3,
            "wall_clock_seconds": 12.0,
        },
    )
    ledger.record_route_taken(
        attempt_id, from_step="aaa", result="done", to_step="open-pr"
    )
    ledger.open_step_execution(
        attempt_id,
        step_id="open-pr",
        seq=1,
        entry_from_step="aaa",
        entry_result="done",
    )
    ledger.end_step_execution(
        attempt_id,
        step_id="open-pr",
        seq=1,
        result="opened",
        facts={"pull_url": "https://github.example/o/r/pull/7"},
        usage={
            "cost_usd": 0.01,
            "effective_tokens": 100,
            "tool_calls": 1,
            "wall_clock_seconds": 6.0,
        },
    )
    ledger.record_workflow_end(
        attempt_id, from_step="open-pr", result="opened", end="completed"
    )
    if outcome is not None:
        ledger.record_terminal_outcome(
            attempt_id, outcome=outcome, reason=COMPLETED_REASON
        )


# --- OPC-1: outcome.native --------------------------------------------------------


def test_outcome_reason_is_the_workflow_end_without_a_fact() -> None:
    """OPC-1: no limit fact — the reason names the end, step and result."""
    outcome = projection.outcome_for(
        outcome="completed",
        reason="",
        executions=[{"step_id": "zzz", "seq": 1}],
        transitions=[
            {
                "kind": "end",
                "from_step": "zzz",
                "result": "done",
                "to_end": "completed",
            }
        ],
    )
    assert outcome == {
        "native": "completed",
        "reason": "ended at end completed after result done of step zzz",
    }


def test_outcome_reason_prefers_the_limit_fact() -> None:
    """OPC-1: a limit fact beats the recorded end reason."""
    outcome = projection.outcome_for(
        outcome="unfinished",
        reason="ended at end unfinished after result done of step aaa",
        executions=[
            {
                "step_id": "aaa",
                "seq": 1,
                "limit_fact": {
                    "scope": "step",
                    "dimensions": ["tokens"],
                    "moment": "before-request",
                },
            }
        ],
    )
    assert outcome["native"] == "unfinished"
    assert outcome["reason"] == "step limit exceeded (tokens) at before-request"


def test_outcome_reason_keeps_the_recorded_reason() -> None:
    """OPC-1: an explicit recorded reason (an error) is kept verbatim."""
    outcome = projection.outcome_for(
        outcome="failed", reason="publish refused: permission denied"
    )
    assert outcome == {
        "native": "failed",
        "reason": "publish refused: permission denied",
    }


# --- OPC-2: receipt-based delivery ------------------------------------------------


def test_delivery_from_the_open_pull_request_receipt() -> None:
    """OPC-2: the created pull request, number from the receipt, URL from facts."""
    delivery = projection.delivery_for(
        step_effects=[
            {
                "step_id": "open-pr",
                "execution_number": 1,
                "ordinal": 0,
                "status": "receipt",
                "remote_id": "7",
            }
        ],
        executions=[
            {
                "step_id": "open-pr",
                "seq": 1,
                "result": "opened",
                "facts": {"pull_url": "https://github.example/o/r/pull/7"},
            }
        ],
    )
    assert delivery == {"number": 7, "url": "https://github.example/o/r/pull/7"}


def test_delivery_null_without_a_receipt() -> None:
    """OPC-2: an ``opened`` execution without its receipt is unsettled, not delivered."""
    delivery = projection.delivery_for(
        step_effects=[
            {
                "step_id": "open-pr",
                "execution_number": 1,
                "ordinal": 0,
                "status": "intent",
                "remote_id": None,
            }
        ],
        executions=[{"step_id": "open-pr", "seq": 1, "result": "opened"}],
    )
    assert delivery is None


def test_delivery_from_a_high_ordinal_receipt() -> None:
    """OPC-2: a receipt at ordinal >= 4 still counts as delivery."""
    delivery = projection.delivery_for(
        step_effects=[
            {
                "step_id": "open-pr",
                "execution_number": 1,
                "ordinal": 7,
                "status": "receipt",
                "remote_id": "9",
            }
        ],
        executions=[
            {
                "step_id": "open-pr",
                "seq": 1,
                "result": "opened",
                "facts": {"pull_url": "https://github.example/o/r/pull/9"},
            }
        ],
    )
    assert delivery == {"number": 9, "url": "https://github.example/o/r/pull/9"}


def test_delivery_null_for_a_published_review() -> None:
    """OPC-2: major 1 has no field for a published review."""
    delivery = projection.delivery_for(
        step_effects=[
            {
                "step_id": "publish",
                "execution_number": 1,
                "ordinal": 0,
                "status": "receipt",
                "remote_id": "101",
            }
        ],
        executions=[{"step_id": "publish", "seq": 1, "result": "published"}],
    )
    assert delivery is None


# --- OPC-3: attention_required -----------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("failed", True),
        ("unfinished", True),
        ("blocked", True),
        ("interrupted", True),
        ("internal-failure", True),
        ("completed", False),
        ("withdrawn", False),
        ("drift", False),
        (None, False),
    ],
)
def test_attention_required_outcome_rule(
    outcome: str | None, expected: bool
) -> None:
    """OPC-3: the Terminal-Outcome half of the attention rule."""
    assert (
        projection.attention_required_for(outcome=outcome, step_effects=[])
        is expected
    )


def test_attention_required_for_unsettled_and_not_performed() -> None:
    """OPC-3: an unsettled or settled-aside effect forces attention."""
    unsettled = [
        {
            "step_id": "push",
            "execution_number": 1,
            "ordinal": 0,
            "status": "intent",
        }
    ]
    assert (
        projection.attention_required_for(
            outcome="completed", step_effects=unsettled
        )
        is True
    )
    assert (
        projection.attention_required_for(
            outcome="completed",
            step_effects=[
                {
                    "step_id": "push",
                    "execution_number": 1,
                    "ordinal": 0,
                    "status": "not-performed",
                }
            ],
        )
        is True
    )
    settled = [
        {
            "step_id": "push",
            "execution_number": 1,
            "ordinal": 0,
            "status": "intent",
        },
        {
            "step_id": "push",
            "execution_number": 1,
            "ordinal": 0,
            "status": "receipt",
        },
    ]
    assert (
        projection.attention_required_for(
            outcome="completed", step_effects=settled
        )
        is False
    )


# --- OPC-4: native Attempt id and phase -------------------------------------------


def test_step_attempt_id_shape() -> None:
    """OPC-4: ``<workflow>#<N>/<n>`` is a step id; the MVP id is not."""
    assert projection.is_step_attempt_id("implement#42/1") is True
    assert projection.is_step_attempt_id("#42/1") is False


def test_native_phase_prefers_the_live_phase() -> None:
    """OPC-4: the running Attempt reports the live ``<step>#<seq>``."""
    assert (
        projection.native_phase_for(
            [{"step_id": "aaa", "seq": 1}], live_phase="zzz#1"
        )
        == "zzz#1"
    )
    assert (
        projection.native_phase_for([{"step_id": "aaa", "seq": 1}]) == "aaa#1"
    )
    assert projection.native_phase_for([]) is None


def test_live_non_native_phase_with_no_executions_is_none(
    ledger: RunLedger,
) -> None:
    """OPC-4: a stale MVP node name never leaks into ``status`` pre-first-step."""
    ledger.open_attempt("w#1/2", effective_configuration=_effective_config())
    live = _live()
    live.set_claim("w#1/2", 1, "Add a greeting")
    live.set_phase("claim")
    assert projection.is_native_phase("claim") is False
    assert projection.is_native_phase("zzz#1") is True
    current = operator_listener.build_status_result(ledger.path, live)[
        "current_attempt"
    ]
    assert current is not None
    assert current["native_phase"] == "claim"


# --- OPC-5: the budget projection --------------------------------------------------


def test_budget_projects_the_four_meters() -> None:
    """OPC-5: usage against the Attempt Ceiling; a missing side is unavailable."""
    budget = projection.budget_for(
        ceiling={
            "cost_usd": 20.0,
            "tokens": 1000,
            "tool_calls": 10,
            "wall_clock_seconds": 300.0,
        },
        spend={
            "cost_usd": 0.03,
            "effective_tokens": 500,
            "tool_calls": 4,
            "wall_clock_seconds": 18.0,
        },
    )
    assert budget["effective_tokens"] == {"used": 500, "configured": 1000}
    assert budget["cost_usd"] == {"used": 0.03, "configured": 20.0}
    assert budget["tool_calls"] == {"used": 4, "configured": 10}
    assert budget["wall_clock_seconds"] == {
        "used": 18.0,
        "configured": 300.0,
    }
    empty = projection.budget_for(ceiling={}, spend={})
    assert empty["effective_tokens"] == {
        "used": "unavailable",
        "configured": "unavailable",
    }


# --- OPC-6: capabilities -------------------------------------------------------------


def test_capabilities_follow_the_input_type() -> None:
    """OPC-6: only an ``issue`` Worker advertises ``next-issue``."""
    assert projection.capabilities_for("issue") == ["next-issue", "stop-after"]
    assert projection.capabilities_for(None) == ["next-issue", "stop-after"]
    assert projection.capabilities_for("pull-request") == ["stop-after"]


def test_identity_capabilities_follow_the_input_type(tmp_path: Path) -> None:
    """OPC-6: ``identity`` carries the Worker's capabilities."""
    ledger = _ledger(tmp_path)
    try:
        live = _live()
        assert operator_listener.build_identity_result(
            ledger.path, live
        )["capabilities"] == ["next-issue", "stop-after"]
        assert operator_listener.build_identity_result(
            ledger.path, live, input_type="pull-request"
        )["capabilities"] == ["stop-after"]
    finally:
        ledger.close()


def test_next_issue_is_unsupported_for_pull_request_workers(
    tmp_path: Path,
) -> None:
    """OPC-6: a ``pull-request`` Worker answers ``next`` with ``unsupported``."""
    ledger = _ledger(tmp_path)
    try:
        live = _live()
        request = {
            "request_id": "req-1",
            "expect_repo": "owner/repo",
            "issue": 3,
        }
        answer = operator_listener._handle_one(
            json.dumps({"op": "next", **request}).encode(),
            ledger_path=ledger.path,
            live=live,
            input_type="pull-request",
        )
        assert answer == {"error_code": "unsupported"}
    finally:
        ledger.close()


# --- OPC-1 … OPC-5 through status ----------------------------------------------------


def test_status_last_finished_projects_the_step_attempt(
    ledger: RunLedger,
) -> None:
    """OPC-1/2/3/4/5: ``last_finished`` for a step Attempt."""
    _open_finished_attempt(ledger, "implement#42/1")
    ledger.record_step_effect_receipt(
        "implement#42/1", "open-pr", 1, 0, remote_id="7"
    )
    result = operator_listener.build_status_result(ledger.path, _live())
    last = result["last_finished"]
    assert last is not None
    assert last["attempt_id"] == "implement#42/1"
    assert last["lifecycle_state"] == "outcome-recorded"
    assert last["outcome"] == {
        "native": "completed",
        "reason": COMPLETED_REASON,
        "label": f"completed ({COMPLETED_REASON})",
    }
    assert last["delivery"] == {
        "pull_request": {
            "number": 7,
            "url": "https://github.example/o/r/pull/7",
        }
    }
    assert last["attention_required"] is False


def test_status_last_finished_failed_needs_attention(
    ledger: RunLedger,
) -> None:
    """OPC-3: ``failed`` (and a SIGTERM ``interrupted``/``shutdown``) needs attention."""
    _open_finished_attempt(ledger, "implement#42/1", outcome="failed")
    live = _live()
    last = operator_listener.build_status_result(ledger.path, live)[
        "last_finished"
    ]
    assert last is not None
    assert last["outcome"] == {
        "native": "failed",
        "reason": COMPLETED_REASON,
        "label": f"failed ({COMPLETED_REASON})",
    }
    assert last["attention_required"] is True


def test_status_current_attempt_reports_the_native_phase(
    ledger: RunLedger,
) -> None:
    """OPC-4/5: the running Attempt carries ``<workflow>#<N>/<n>`` and the native phase."""
    ledger.open_attempt("w#1/1", effective_configuration=_effective_config())
    live = _live()
    live.set_claim("w#1/1", 1, "Add a greeting")
    phases: list[str] = []

    def hook(phase: str) -> None:
        phases.append(phase)
        live.set_phase(phase)

    result = run_workflow(
        workflow=_workflow(),
        attempt_id="w#1/1",
        ledger=ledger,
        script={"aaa": ["done"], "zzz": ["done"]},
        phase_hook=hook,
    )
    assert result.outcome == "completed"
    assert phases[0] == "aaa#1"
    assert phases[-1] == "zzz#1"
    current = operator_listener.build_status_result(ledger.path, live)[
        "current_attempt"
    ]
    assert current is not None
    assert current["attempt_id"] == "w#1/1"
    assert current["issue"] == {"number": 1, "title": "Add a greeting"}
    assert current["native_phase"] == "zzz#1"
    assert current["budget"]["enforced_tokens"] == "effective"


# --- attempts show -------------------------------------------------------------------


def test_show_attempt_projects_the_step_attempt(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OPC-1 … OPC-5 through ``attempts show``; the MVP path is unchanged."""
    _open_finished_attempt(ledger, "implement#42/1")
    ledger.record_step_effect_receipt(
        "implement#42/1", "open-pr", 1, 0, remote_id="7"
    )
    state_dir = tmp_path / "state"
    shown = attempt_archives.show_attempt(
        ledger_path=ledger.path,
        state_dir=state_dir,
        attempt_id="implement#42/1",
    )
    assert shown is not None
    attempt = shown["attempt"]
    assert attempt["outcome"] == {
        "native": "completed",
        "reason": COMPLETED_REASON,
        "label": f"completed ({COMPLETED_REASON})",
    }
    assert attempt["attention_required"] is False
    assert attempt["delivery"] == {
        "pull_request": {
            "number": 7,
            "url": "https://github.example/o/r/pull/7",
        }
    }


# --- OPC-7: archives --------------------------------------------------------------------


def test_archive_carries_native_events_artifacts_and_summaries(
    tmp_path: Path, ledger: RunLedger
) -> None:
    """OPC-7: ``native.`` step events in ``events.jsonl``; artifacts and
    Execution Summaries in the manifest as ``record`` entries."""
    store = OperatorStore(ledger.path)
    try:
        store.write_instance_metadata(target_repository="owner/repo")
    finally:
        store.close()
    ledger.open_attempt("w#1/1", effective_configuration=_effective_config())
    live = _live()
    live.set_claim("w#1/1", 1, "Add a greeting")
    result = run_workflow(
        workflow=_workflow(),
        attempt_id="w#1/1",
        ledger=ledger,
        script={"aaa": ["done"], "zzz": ["done"]},
        phase_hook=live.set_phase,
    )
    assert result.outcome == "completed"
    archive = attempt_archives.archive_dir_for(
        tmp_path / "state", "owner/repo", "w#1/1"
    )
    stored = store_step_artifacts(
        step_id="aaa",
        seq=1,
        result_name="done",
        payload={"doc": "the plan"},
        schema={"doc": "document"},
        archive_dir=archive,
    )
    assert stored["doc"].endswith("aaa.1.doc.md")
    lines = attempt_archives.read_archive_lines(archive)
    types = [line.get("event", line.get("type")) for line in lines]
    assert attempt_archives.NATIVE_STEP_OPENED in types
    assert attempt_archives.NATIVE_STEP_ENDED in types
    assert attempt_archives.NATIVE_ROUTE_TAKEN in types
    assert attempt_archives.NATIVE_WORKFLOW_END in types
    assert attempt_archives.EVENT_PHASE_CHANGED in types
    manifest = attempt_archives.build_manifest(archive)
    by_path = {entry.path: entry for entry in manifest}
    assert by_path["aaa.1.doc.md"].kind == "record"
    summaries = sorted(
        path for path in by_path if path.endswith(".facts.json")
    )
    assert summaries, "expected an Execution Summary in the manifest"
    assert all(
        by_path[path].kind == "record" for path in summaries
    )
    assert all(
        by_path[path].media_type == "application/json" for path in summaries
    )
