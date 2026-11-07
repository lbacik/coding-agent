"""Producer conformance: every ``agentctl`` reply validates (issue #260).

Contract: ``agent-operator-contract`` major 1 at ``06ee46c``
(``docs/spec.md`` §4-§6, ``examples/agentctl/``). The schemas under
``conformance/schemas/`` are that revision's fixture shapes as JSON
Schema; when upstream ``v1.0.0`` publishes its schemas those files are
replaced and these tests keep loading whatever is on disk, so drift
from the contract fails the build either way.

Each test takes one reply shape the in-process ``agentctl`` tests
produce — success results and every error code — and asserts it
validates. The negative test proves the gate is real: a corrupted
fixture is rejected.
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


def test_deliberate_deviation_fails_validation() -> None:
    """A reply missing a required field is rejected (the drift gate)."""
    envelope: dict[str, Any] = {
        "contract": "1.0",
        "ok": True,
        "result": {
            "backend": "coding-agent",
            "agent_version": "0.1.0",
            "version": "0.1.0",
            "contract": "1.0",
            "repository": "example-owner/example-repo",
            "target_repository": "example-owner/example-repo",
            "mode": "worker",
            "process_started_at": "2026-11-07T00:00:00+00:00",
            "capabilities": ["next-issue", "stop-after"],
        },
    }
    assert validate_reply("identity", envelope) == []
    deviated = copy.deepcopy(envelope)
    del deviated["result"]["capabilities"]
    problems = validate_reply("identity", deviated)
    assert problems != []
    assert any("capabilities" in problem for problem in problems)
