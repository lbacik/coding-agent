"""Producer conformance: every ``agentctl`` reply validates (issue #359).

Contract: ``agent-operator-contract`` pinned at tag ``v1.0.4``. Schemas
load from the installed package (no copied schemas); validation uses a
full Draft 2020-12 validator. Each test takes one reply shape the
in-process ``agentctl`` tests produce — success results and every error
code — and asserts it validates. The negative test proves the gate is
real: a corrupted identity is rejected.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coding_agent import agentctl
from coding_agent.conformance.schemas import (
    validate_envelope,
    validate_reply,
    validate_result,
)
from coding_agent.operator.paths import STATE_DIR_ENV_VAR


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = agentctl.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert out.endswith("\n")
    assert out.count("\n") == 1
    return code, json.loads(out)


def _engine_reply(reply: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _fake(_body: dict[str, Any]) -> dict[str, Any]:
        return dict(reply)

    return _fake


def test_error_envelopes_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every error code the contract names validates as an envelope."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    cases: list[list[str]] = [
        [],
        ["--contract", "2", "status"],
        ["--contract", "1", "stop", "--after"],
        [
            "--contract", "1", "handoff", "now",
            "--request-id", "req-5e6f7a8b9c0d",
            "--expect-repo", "example-owner/example-repo",
        ],
        [
            "--contract", "1", "stop",
            "--request-id", "req-1a2b3c4d5e6f",
            "--expect-repo", "example-owner/example-repo",
        ],
        [
            "--contract", "1", "stop",
            "--request-id", "bad!", "--expect-repo", "o/r",
        ],
        ["--contract", "1", "attempts", "show", "#9/9"],
    ]
    for argv in cases:
        _, envelope = _run(list(argv), capsys)
        assert envelope["ok"] is False
        assert validate_envelope(envelope) == []


def test_mutation_success_results_validate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``stop``/``resume``/``next``/``command`` success shapes validate."""
    command = {
        "request_id": "req-3f1c9a2b7d10",
        "sequence": 7,
        "kind": "stop",
        "arguments": {},
        "repository": "example-owner/example-repo",
        "acknowledgement": "accepted",
        "detail": "finish the active attempt, then close intake",
        "created_at": "2026-11-06T09:12:03Z",
        "updated_at": "2026-11-06T09:12:03Z",
    }
    monkeypatch.setattr(
        agentctl, "_query_engine", _engine_reply({"command": command, "replayed": False})
    )
    _, stop_envelope = _run(
        [
            "--contract", "1", "stop",
            "--request-id", "req-3f1c9a2b7d10",
            "--expect-repo", "example-owner/example-repo",
        ],
        capsys,
    )
    assert validate_reply("stop", stop_envelope) == []
    assert validate_reply("resume", stop_envelope) == []
    assert validate_reply("next", stop_envelope) == []

    monkeypatch.setattr(
        agentctl, "_query_engine", _engine_reply({"command": command})
    )
    _, command_envelope = _run(
        ["--contract", "1", "command", "req-3f1c9a2b7d10"], capsys
    )
    assert validate_reply("command", command_envelope) == []


def test_identity_and_status_results_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Online and offline ``identity``/``status`` results validate."""
    from datetime import datetime, timezone

    from coding_agent.operator import listener as operator_listener
    from coding_agent.operator.live import LiveView
    from coding_agent.operator.store import OperatorStore

    ledger_path = tmp_path / "run-ledger.sqlite3"
    store = OperatorStore(ledger_path)
    started = datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
    store.write_instance_metadata(
        target_repository="example-owner/example-repo", process_started_at=started
    )
    live = LiveView(process_started_at=started)
    live.set_ready()
    try:
        identity = operator_listener.build_identity_result(ledger_path, live)
        assert validate_result("identity", identity) == []
        status = operator_listener.build_status_result(ledger_path, live)
        assert validate_result("status", status) == []
    finally:
        store.close()

    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    _, offline_identity = _run(["--contract", "1", "--offline", "identity"], capsys)
    assert offline_identity["ok"] is True
    assert validate_reply("identity", offline_identity) == []
    assert offline_identity["result"]["mode"] == "offline"

    _, offline_status = _run(["--contract", "1", "--offline", "status"], capsys)
    assert offline_status["ok"] is True
    assert validate_reply("status", offline_status) == []
    assert offline_status["result"]["instance"]["state"] == "stopped"


def test_attempts_results_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``attempts list`` validates; unknown ``show`` is a valid error."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    _, envelope = _run(["--contract", "1", "attempts", "list", "--limit", "20"], capsys)
    assert envelope["ok"] is True
    assert validate_reply("attempts-list", envelope) == []

    code, missing = _run(["--contract", "1", "attempts", "show", "#9/9"], capsys)
    assert code == 5
    assert validate_envelope(missing) == []
    assert missing["error"]["code"] == "not_found"


def _valid_identity_envelope() -> dict[str, Any]:
    """One valid ``identity`` envelope in ``v1.0.4`` shape."""
    return {
        "contract": "1.0",
        "ok": True,
        "result": {
            "backend": "coding-agent",
            "agent_version": "0.1.0",
            "contract": {"major": 1, "minor": 0},
            "repository": "example-owner/example-repo",
            "mode": "online",
            "process_started_at": "2026-11-07T00:00:00Z",
            "capabilities": ["next-issue", "stop-after"],
        },
    }


def _assert_no_plus_offset(value: Any) -> None:
    """No timestamp in a contract reply carries a ``+00:00`` offset."""
    if isinstance(value, str):
        assert "+00:00" not in value, f"non-Z timestamp: {value!r}"
    elif isinstance(value, dict):
        for item in value.values():
            _assert_no_plus_offset(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_plus_offset(item)


def test_deliberate_deviation_fails_validation() -> None:
    """A corrupted identity (``contract`` as a string) is rejected."""
    envelope = _valid_identity_envelope()
    assert validate_reply("identity", envelope) == []
    _assert_no_plus_offset(envelope)
    deviated = copy.deepcopy(envelope)
    deviated["result"]["contract"] = "1.0"
    problems = validate_reply("identity", deviated)
    assert problems != []
    assert any("contract" in problem for problem in problems)


def test_status_states_and_attempts_pages_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status in each contract state, attempts pages and show validate."""
    from datetime import datetime, timezone

    from coding_agent.ledger.store import RunLedger
    from coding_agent.operator import listener as operator_listener
    from coding_agent.operator.live import LiveView
    from coding_agent.operator.store import OperatorStore
    from coding_agent.runtime.ceilings import UsageTotals
    from coding_agent.ledger.store import BudgetSnapshot

    def _fresh_live() -> LiveView:
        live = LiveView(
            process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
        )
        live.set_ready()
        return live

    def _check_status(
        ledger_path: Path, live: LiveView, where: str
    ) -> dict[str, Any]:
        status = operator_listener.build_status_result(ledger_path, live)
        problems = validate_result("status", status)
        assert problems == [], f"{where}: {problems}"
        _assert_no_plus_offset(status)
        assert status["identity"]["contract"] == {"major": 1, "minor": 0}
        return status

    # Idle with nothing pending.
    ledger_path = tmp_path / "idle-ledger.sqlite3"
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository="example-owner/example-repo",
        process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc),
    )
    try:
        _check_status(ledger_path, _fresh_live(), "idle")
    finally:
        store.close()

    # Pending stop, stop-after plan, next-issue priority, halt, event, attempt.
    ledger_path = tmp_path / "full-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    try:
        store.write_instance_metadata(
            target_repository="example-owner/example-repo",
            process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc),
        )
        live = _fresh_live()
        ledger.open_attempt(
            "#40/1",
            effective_configuration={
                "workflow": {
                    "limits": {
                        "wall_clock_minutes": 60,
                        "cost_usd": 5.0,
                        "tokens": 100_000,
                        "tool_calls": 200,
                    }
                }
            },
        )
        ledger.record_usage(
            "#40/1",
            BudgetSnapshot(
                elapsed_seconds=90.0,
                usage=UsageTotals(
                    tokens=1200,
                    effective_tokens=1000,
                    cost_usd=0.02,
                    tool_calls=7,
                    input_tokens=800,
                    output_tokens=200,
                    cache_read_tokens=200,
                    cache_write_tokens=0,
                    cost_input_usd=0.01,
                    cost_output_usd=0.005,
                    cost_cache_read_usd=0.001,
                    cost_cache_write_usd=0.0,
                ),
            ),
        )
        live.set_claim("#40/1", 40, "Add a greeting")
        live.set_phase("run_step")
        import json as _json

        from coding_agent.operator import commands as operator_commands

        def _commit_stop(
            request_id: str, after: Any = None
        ) -> None:
            connection = operator_commands.open_ledger_connection(
                ledger_path, timeout_seconds=2.0
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                record, _replayed, error = operator_commands.commit_stop(
                    connection,
                    request_id=request_id,
                    expect_repo="example-owner/example-repo",
                    target_repository="example-owner/example-repo",
                    after=after,
                )
                assert error is None, f"commit_stop {request_id}: {error}"
                assert record is not None
                connection.commit()
            finally:
                connection.close()

        def _commit_next(request_id: str, issue: int) -> None:
            connection = operator_commands.open_ledger_connection(
                ledger_path, timeout_seconds=2.0
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                record, _replayed, error = operator_commands.commit_next(
                    connection,
                    request_id=request_id,
                    expect_repo="example-owner/example-repo",
                    target_repository="example-owner/example-repo",
                    issue_number=issue,
                )
                assert error is None, f"commit_next {request_id}: {error}"
                assert record is not None
                connection.commit()
            finally:
                connection.close()

        def _answer_stop(request_id: str, after: Any = None) -> dict[str, Any]:
            body: dict[str, Any] = {
                "op": "stop",
                "request_id": request_id,
                "expect_repo": "example-owner/example-repo",
            }
            if after is not None:
                body["after"] = after
            raw = (_json.dumps(body) + "\n").encode()
            reply = operator_listener._handle_one(
                raw, ledger_path=ledger_path, live=live
            )
            assert reply is not None
            return reply

        _answer_stop("req-pending-stop-1")
        status = _check_status(ledger_path, live, "pending-stop")
        assert status["intake"]["pending_stop"] is not None
        # Resolve the pending stop by finalizing the attempt, then plan.
        ledger.set_attempt_state("#40/1", "finished")
        status = _check_status(ledger_path, live, "stop-completed")
        # Re-open for the stop-after plan check.
        ledger.open_attempt(
            "#41/2",
            effective_configuration={
                "workflow": {
                    "limits": {
                        "wall_clock_minutes": 60,
                        "cost_usd": 5.0,
                        "tokens": 100_000,
                        "tool_calls": 200,
                    }
                }
            },
        )
        live.set_claim("#41/2", 41, "Second attempt")
        _commit_stop("req-after-plan-1", after=2)
        status = _check_status(ledger_path, live, "stop-after-plan")
        assert status["intake"]["stop_plan"] is not None
        assert status["intake"]["stop_plan"]["request_id"] == "req-after-plan-1"
        _commit_next("req-priority-1", 43)
        status = _check_status(ledger_path, live, "next-issue-priority")
        assert status["intake"]["priority"] == {
            "request_id": "req-priority-1",
            "issue": 43,
        }
        ledger.record_halt(reason="credential-failure", detail="token rejected")
        ledger.emit_contract_event(
            process_started_at=live.process_started_at_iso(),
            event_type="ready",
            text="ready",
        )
        status = _check_status(ledger_path, live, "halt-event-attempt")
        assert status["instance"]["last_halt"] is not None
        assert status["instance"]["last_halt"]["reason"] == "credential-failure"
        assert len(status["recent_events"]) >= 1
        assert status["current_attempt"] is not None
        assert status["current_attempt"]["attempt_id"] == "#40/1"
    finally:
        try:
            ledger.close()
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass

    # Attempts list first and last pages plus show validate.
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    _, first = _run(["--contract", "1", "attempts", "list", "--limit", "1"], capsys)
    assert first["ok"] is True
    assert validate_reply("attempts-list", first) == []
    _assert_no_plus_offset(first)
    cursor = first["result"].get("next_cursor")
    if cursor:
        _, last = _run(
            ["--contract", "1", "attempts", "list", "--limit", "1", "--before", cursor],
            capsys,
        )
        if last["ok"] is True:
            assert validate_reply("attempts-list", last) == []
            _assert_no_plus_offset(last)
    code, missing = _run(["--contract", "1", "attempts", "show", "#9/9"], capsys)
    assert code == 5
    assert validate_envelope(missing) == []
