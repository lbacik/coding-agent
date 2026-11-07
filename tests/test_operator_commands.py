"""Durable Intake and Operator Commands (issue #249).

Contract: operator contract major 1 at ``06ee46c`` §6.2, §7.5, §8 and the
durable-Intake section of ``docs/contract/mvp-runtime-contract.md`` §3.
An operator drains the Worker with ``stop``, undoes the drain with
``resume`` and looks up any command with ``command <request-id>``. Intake
and commands are durable in the Run Ledger's SQLite file, serialise with
the Claim boundary and with finalization through SQLite's write lock, gate
only the decision to open a Claim and never write lifecycle state.

Engine paths are exercised through ``listener._handle_one`` (the same
function the socket thread calls); ``agentctl`` mapping through the entry
point with a stubbed engine; the Claim boundary and finalization through
``RunLedger`` directly.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import commands as operator_commands
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.run import WorkerLoopConfig
from datetime import datetime, timezone


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore]:
    """A ledger plus the instance row mutations need for the repo guard."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository="example-owner/example-repo",
        process_started_at=_started_at(),
    )
    return ledger, store


def _live() -> LiveView:
    """A ready worker's live view: these tests simulate post-recovery status."""
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _request(op: str, **fields: str) -> bytes:
    body: dict[str, Any] = {"op": op}
    body.update(fields)
    return (json.dumps(body) + "\n").encode("utf-8")


def _answer(
    ledger_path: Path, raw: bytes
) -> dict[str, Any] | None:
    return operator_listener._handle_one(raw, ledger_path=ledger_path, live=_live())


def _engine_reply(
    reply: dict[str, Any],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A stubbed ``agentctl._query_engine`` answering one fixed reply."""

    def _stub(body: dict[str, Any]) -> dict[str, Any]:
        _ = body
        return reply

    return _stub


def _no_sleep(seconds: float) -> None:
    """A clock that never sleeps for bounded poll-loop tests."""
    _ = seconds


def test_stop_before_claim_boundary_prevents_claim_and_completes_at_once(
    tmp_path: Path,
) -> None:
    """§8.3: a ``stop`` committed before the boundary keeps the Claim closed."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-1", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
        assert reply["replayed"] is False
        command = reply["command"]
        assert command["acknowledgement"] == "completed"
        assert command["detail"] == "intake closed"
        assert command["sequence"] == 1
        # The Claim boundary now refuses to open.
        assert ledger.intake_is_open() is False
        opened = ledger.open_attempt_if_intake_open(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        assert opened is None
        assert ledger.has_attempt("#40/1") is False
    finally:
        store.close()
        ledger.close()


def test_stop_after_boundary_accepted_then_completed_on_finalize(
    tmp_path: Path,
) -> None:
    """§8.3: a ``stop`` after the boundary stays ``accepted`` until final."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-2", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "accepted"
        assert reply["command"]["detail"] == "finish the active attempt, then close intake"
        assert ledger.intake_is_open() is False
        # Finalization completes the pending stop in the same transaction.
        ledger.set_attempt_state("#40/1", "finished")
        stored = operator_commands.get_command(
            operator_commands.open_ledger_connection(ledger.path, timeout_seconds=2.0),
            "req-stop-2",
        )
        assert stored is not None
        assert stored.acknowledgement == "completed"
        # Intake stays closed: completing never reopens it.
        assert ledger.intake_is_open() is False
    finally:
        store.close()
        ledger.close()


def test_release_finalizes_and_completes_pending_stop(tmp_path: Path) -> None:
    """The Claim read-back release is a final state: the stop completes."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-3", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "accepted"
        ledger.set_attempt_state("#40/1", "released")
        connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            stored = operator_commands.get_command(connection, "req-stop-3")
        finally:
            connection.close()
        assert stored is not None
        assert stored.acknowledgement == "completed"
    finally:
        store.close()
        ledger.close()


def test_stop_writes_no_lifecycle_state(tmp_path: Path) -> None:
    """Commands gate only the Claim decision: no Attempt, outcome or effect."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        before = ledger.table_names()
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-4", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "completed"
        assert ledger.unfinished_attempts() == ()
        assert ledger.has_attempt("#40/1") is False
        assert ledger.table_names() == before
    finally:
        store.close()
        ledger.close()


def test_closed_intake_survives_restart_and_fresh_starts_open(
    tmp_path: Path,
) -> None:
    """Intake persists across restarts; a fresh volume starts open."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-5", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
    finally:
        store.close()
        ledger.close()
    reopened = RunLedger(tmp_path / "run-ledger.sqlite3")
    reopened_store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    try:
        assert reopened.intake_is_open() is False
        assert reopened.open_attempt_if_intake_open(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        ) is None
    finally:
        reopened_store.close()
        reopened.close()
    fresh = RunLedger(tmp_path / "fresh.sqlite3")
    try:
        assert fresh.intake_is_open() is True
    finally:
        fresh.close()


def test_resume_reopens_and_supersedes_pending_stop(tmp_path: Path) -> None:
    """``resume`` reopens Intake and supersedes the pending ``stop``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        stop_reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-6", expect_repo="example-owner/example-repo"),
        )
        assert stop_reply is not None
        resume_reply = _answer(
            ledger.path,
            _request(
                "resume", request_id="req-resume-6", expect_repo="example-owner/example-repo"
            ),
        )
        assert resume_reply is not None
        assert resume_reply["replayed"] is False
        assert resume_reply["command"]["acknowledgement"] == "completed"
        assert resume_reply["command"]["detail"] == "intake reopened"
        assert ledger.intake_is_open() is True
        connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            old = operator_commands.get_command(connection, "req-stop-6")
        finally:
            connection.close()
        assert old is not None
        assert old.acknowledgement == "superseded"
        # The reopened Intake claims again.
        ledger.set_attempt_state("#40/1", "finished")
        opened = ledger.open_attempt_if_intake_open(
            "#41/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        assert opened is not None
    finally:
        store.close()
        ledger.close()


def test_resume_with_intake_open_and_nothing_pending(tmp_path: Path) -> None:
    """An idle ``resume`` completes with a detail saying so; starts nothing."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        reply = _answer(
            ledger.path,
            _request(
                "resume", request_id="req-resume-7", expect_repo="example-owner/example-repo"
            ),
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "completed"
        assert reply["command"]["detail"] == "intake already open"
        assert ledger.unfinished_attempts() == ()
        assert ledger.has_attempt("#40/1") is False
    finally:
        store.close()
        ledger.close()


def test_status_reports_draining_paused_and_recent_commands(
    tmp_path: Path,
) -> None:
    """``status`` carries draining/paused, the pending stop and recents."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        idle = operator_listener.build_status_result(ledger.path, _live())
        assert idle["instance"]["state"] == "idle"
        assert idle["intake"]["state"] == "open"
        assert idle["intake"]["pending_stop"] is None
        assert idle["recent_commands"] == []

        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        active = operator_listener.build_status_result(ledger.path, _live())
        assert active["instance"]["state"] == "active"

        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-8", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
        draining = operator_listener.build_status_result(ledger.path, _live())
        assert draining["instance"]["state"] == "draining"
        assert draining["intake"]["state"] == "closed"
        assert draining["intake"]["pending_stop"] == {
            "request_id": "req-stop-8",
            "sequence": reply["command"]["sequence"],
        }
        assert [entry["request_id"] for entry in draining["recent_commands"]] == [
            "req-stop-8"
        ]

        ledger.set_attempt_state("#40/1", "finished")
        paused = operator_listener.build_status_result(ledger.path, _live())
        assert paused["instance"]["state"] == "paused"
        assert paused["intake"]["state"] == "closed"
        assert paused["intake"]["pending_stop"] is None
        assert [entry["request_id"] for entry in paused["recent_commands"]] == [
            "req-stop-8"
        ]
    finally:
        store.close()
        ledger.close()


def test_wrong_repository_stores_nothing(tmp_path: Path) -> None:
    """A wrong ``--expect-repo`` (case-insensitive) answers mismatch, stores nothing."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-9", expect_repo="example-owner/other-repo"),
        )
        assert reply is not None
        assert reply["error_code"] == "repository_mismatch"
        assert reply["repository"] == "example-owner/example-repo"
        connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            assert operator_commands.get_command(connection, "req-stop-9") is None
            assert operator_commands.list_recent_commands(connection) == []
            assert ledger.intake_is_open() is True
        finally:
            connection.close()
        # Case-insensitive comparison: upper case still matches.
        ok = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-9b", expect_repo="EXAMPLE-OWNER/EXAMPLE-REPO"),
        )
        assert ok is not None
        assert "command" in ok
    finally:
        store.close()
        ledger.close()


def test_replay_returns_stored_record_and_payload_mismatch(tmp_path: Path) -> None:
    """Same ID and payload replays; same ID with other payload mismatches."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        first = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-10", expect_repo="example-owner/example-repo"),
        )
        assert first is not None
        second = _answer(
            ledger.path,
            _request(
                "stop",
                request_id="req-stop-10",
                expect_repo="Example-Owner/Example-Repo",
            ),
        )
        assert second is not None
        assert second["replayed"] is True
        assert second["command"] == first["command"]
        # A resume under the stop's ID is a different payload.
        clash = _answer(
            ledger.path,
            _request(
                "resume", request_id="req-stop-10", expect_repo="example-owner/example-repo"
            ),
        )
        assert clash is not None
        assert clash["error_code"] == "payload_mismatch"
    finally:
        store.close()
        ledger.close()


def test_sequence_increases_strictly_in_commit_order(tmp_path: Path) -> None:
    """``sequence`` increases strictly in commit order."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ids = ["req-seq-1", "req-seq-2", "req-seq-3"]
        kinds = ["resume", "stop", "resume"]
        sequences: list[int] = []
        for request_id, kind in zip(ids, kinds, strict=True):
            reply = _answer(
                ledger.path,
                _request(
                    kind, request_id=request_id, expect_repo="example-owner/example-repo"
                ),
            )
            assert reply is not None
            sequences.append(int(reply["command"]["sequence"]))
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
    finally:
        store.close()
        ledger.close()


def test_mutation_busy_timeout_is_ten_seconds(tmp_path: Path) -> None:
    """The mutation connection's busy timeout keeps it inside the 10 s budget."""
    path = tmp_path / "run-ledger.sqlite3"
    connection = operator_commands.open_ledger_connection(path, timeout_seconds=10.0)
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        assert int(row[0]) == 10000
    finally:
        connection.close()


def test_command_lookup_and_not_found(tmp_path: Path) -> None:
    """``command <request-id>`` returns the stored record; unknown is ``not_found``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        created = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-11", expect_repo="example-owner/example-repo"),
        )
        assert created is not None
        found = _answer(ledger.path, _request("command", request_id="req-stop-11"))
        assert found is not None
        assert found["command"] == created["command"]
        missing = _answer(ledger.path, _request("command", request_id="req-unknown"))
        assert missing is not None
        assert missing["error_code"] == "not_found"
    finally:
        store.close()
        ledger.close()


def test_no_command_left_accepted_after_restart_once_final(tmp_path: Path) -> None:
    """Reconciliation completes ``accepted`` commands whose Attempt is final."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-12", expect_repo="example-owner/example-repo"),
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "accepted"
        # The Attempt finalizes without the completing transaction (an
        # earlier process dying mid-path): restart reconciliation resolves it.
        raw = sqlite3.connect(ledger.path)
        try:
            raw.execute("UPDATE attempts SET state = 'finished' WHERE attempt_id = '#40/1'")
            raw.commit()
        finally:
            raw.close()
        ledger.reconcile_accepted_commands()
        connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            stored = operator_commands.get_command(connection, "req-stop-12")
            remaining = connection.execute(
                "SELECT request_id FROM operator_commands WHERE acknowledgement = 'accepted'"
            ).fetchall()
        finally:
            connection.close()
        assert stored is not None
        assert stored.acknowledgement == "completed"
        assert remaining == []
        # A pending stop with an unfinished Attempt stays pending.
        ledger.open_attempt(
            "#41/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        second = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-13", expect_repo="example-owner/example-repo"),
        )
        assert second is not None
        ledger.reconcile_accepted_commands()
        pending_connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            pending = operator_commands.get_command(pending_connection, "req-stop-13")
        finally:
            pending_connection.close()
        assert pending is not None
        assert pending.acknowledgement == "accepted"
    finally:
        store.close()
        ledger.close()


def test_startup_reconciliation_runs_before_first_claim(tmp_path: Path) -> None:
    """The poll loop reconciles before its first Claim boundary (no-op here)."""
    from support.fake_tracker import FakeTracker

    from coding_agent.worker import run as worker_run

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        raw = sqlite3.connect(ledger.path)
        try:
            raw.execute(
                "INSERT INTO operator_commands"
                " (request_id, kind, arguments, payload_digest, repository,"
                "  acknowledgement, detail, created_at, updated_at)"
                " VALUES ('req-orphan', 'stop', '{}', 'digest',"
                "  'example-owner/example-repo', 'accepted', 'd', 't', 't')"
            )
            raw.commit()
        finally:
            raw.close()
        ledger.set_attempt_state("#40/1", "finished")
        # The loop's first act resolves the orphan without opening a Claim.
        tracker = FakeTracker(agent_login="owner")
        tracker.candidates.clear()
        worker_run._poll_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(),
            run_one_attempt=_never_run,
            sleep=_no_sleep,
            max_sweeps=1,
        )
        connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            stored = operator_commands.get_command(connection, "req-orphan")
        finally:
            connection.close()
        assert stored is not None
        assert stored.acknowledgement == "completed"
    finally:
        store.close()
        ledger.close()


def _loop_config() -> WorkerLoopConfig:
    return WorkerLoopConfig(
        claim_config=ClaimConfig(selection_label="ready-for-agent"),
        poll_interval_seconds=0.0,
    )


def _never_run(claimed: ClaimedAttempt) -> NoReturn:
    raise AssertionError(f"no Attempt should run: {claimed!r}")


def _run_agentctl(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    code = agentctl.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    return code, json.loads(captured.out)


def test_agentctl_stop_success_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 carries ``{"command": ..., "replayed": ...}``."""
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
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "stop",
            "--request-id",
            "req-3f1c9a2b7d10",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 0
    assert envelope == {
        "contract": "1.0",
        "ok": True,
        "result": {"command": command, "replayed": False},
    }


def test_agentctl_stop_replayed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A replayed commit still exits 0 with ``replayed: true``."""
    command = {
        "request_id": "req-3f1c9a2b7d10",
        "sequence": 7,
        "kind": "stop",
        "arguments": {},
        "repository": "example-owner/example-repo",
        "acknowledgement": "completed",
        "detail": "finish the active attempt, then close intake",
        "created_at": "2026-11-06T09:12:03Z",
        "updated_at": "2026-11-06T09:40:17Z",
    }
    monkeypatch.setattr(
        agentctl, "_query_engine", _engine_reply({"command": command, "replayed": True})
    )
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "stop",
            "--request-id",
            "req-3f1c9a2b7d10",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 0
    assert envelope["result"] == {"command": command, "replayed": True}


def test_agentctl_stop_repository_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrong repository exits 1 and names the instance's repository."""
    monkeypatch.setattr(
        agentctl,
        "_query_engine",
        _engine_reply(
            {
                "error_code": "repository_mismatch",
                "repository": "example-owner/example-repo",
            }
        ),
    )
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "resume",
            "--request-id",
            "req-9a0b1c2d3e4f",
            "--expect-repo",
            "example-owner/other-repo",
        ],
        capsys,
    )
    assert code == 1
    assert envelope == {
        "contract": "1.0",
        "ok": False,
        "error": {
            "code": "repository_mismatch",
            "message": (
                "expected example-owner/other-repo,"
                " this instance targets example-owner/example-repo"
            ),
            "retryable": False,
            "request_id": "req-9a0b1c2d3e4f",
            "details": {"repository": "example-owner/example-repo"},
        },
    }


def test_agentctl_stop_payload_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reusing an ID with another payload exits 1."""
    monkeypatch.setattr(
        agentctl, "_query_engine", _engine_reply({"error_code": "payload_mismatch"})
    )
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "stop",
            "--request-id",
            "req-3f1c9a2b7d10",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 1
    assert envelope["error"]["code"] == "payload_mismatch"
    assert envelope["error"]["request_id"] == "req-3f1c9a2b7d10"
    assert envelope["error"]["retryable"] is False


def test_agentctl_mutation_internal_stored_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mutation that cannot commit exits 7 with ``details.stored: false``."""
    monkeypatch.setattr(
        agentctl,
        "_query_engine",
        _engine_reply({"error_code": "internal", "stored": False}),
    )
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "stop",
            "--request-id",
            "req-3f1c9a2b7d10",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 7
    assert envelope["error"]["code"] == "internal"
    assert envelope["error"]["details"] == {"stored": False}
    assert envelope["error"]["retryable"] is True


def test_agentctl_command_lookup_shapes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``command`` answers the record, and ``not_found`` for unknown IDs."""
    command = {
        "request_id": "req-3f1c9a2b7d10",
        "sequence": 7,
        "kind": "stop",
        "arguments": {},
        "repository": "example-owner/example-repo",
        "acknowledgement": "completed",
        "detail": "intake closed",
        "created_at": "2026-11-06T09:12:03Z",
        "updated_at": "2026-11-06T09:40:17Z",
    }
    monkeypatch.setattr(agentctl, "_query_engine", _engine_reply({"command": command}))
    code, envelope = _run_agentctl(
        ["--contract", "1", "command", "req-3f1c9a2b7d10"], capsys
    )
    assert code == 0
    assert envelope["result"] == {"command": command}
    monkeypatch.setattr(
        agentctl, "_query_engine", _engine_reply({"error_code": "not_found"})
    )
    code2, envelope2 = _run_agentctl(
        ["--contract", "1", "command", "req-000000000000"], capsys
    )
    assert code2 == 5
    assert envelope2 == {
        "contract": "1.0",
        "ok": False,
        "error": {
            "code": "not_found",
            "message": "no command is recorded for req-000000000000",
            "retryable": False,
        },
    }


def test_agentctl_mutations_require_request_id_and_expect_repo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``stop``/``resume`` without the mutation flags answer ``usage``."""
    for argv in (
        ["--contract", "1", "stop"],
        ["--contract", "1", "stop", "--request-id", "req-1"],
        ["--contract", "1", "resume", "--expect-repo", "example-owner/example-repo"],
        ["--contract", "1", "command"],
    ):
        code, envelope = _run_agentctl(list(argv), capsys)
        assert code == 2
        assert envelope["error"]["code"] == "usage"


def test_command_lookup_internal_carries_no_stored_flag(tmp_path: Path) -> None:
    """A ``command`` read failure answers ``internal`` with no ``stored`` flag."""
    reply = _answer(
        tmp_path / "missing-dir" / "run-ledger.sqlite3",
        _request("command", request_id="req-3f1c9a2b7d10"),
    )
    assert reply == {"error_code": "internal"}


def test_agentctl_command_internal_has_no_stored_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``command`` read failure exits 7 without ``details.stored``."""
    monkeypatch.setattr(
        agentctl, "_query_engine", _engine_reply({"error_code": "internal"})
    )
    code, envelope = _run_agentctl(
        ["--contract", "1", "command", "req-3f1c9a2b7d10"], capsys
    )
    assert code == 7
    assert envelope["error"]["code"] == "internal"
    assert "details" not in envelope["error"]


def test_startup_reconciliation_retries_on_later_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed startup reconciliation is retried until it succeeds."""
    from support.fake_tracker import FakeTracker

    from coding_agent.worker import run as worker_run

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        raw = sqlite3.connect(ledger.path)
        try:
            raw.execute(
                "INSERT INTO operator_commands"
                " (request_id, kind, arguments, payload_digest, repository,"
                "  acknowledgement, detail, created_at, updated_at)"
                " VALUES ('req-retry-orphan', 'stop', '{}', 'digest',"
                "  'example-owner/example-repo', 'accepted', 'd', 't', 't')"
            )
            raw.commit()
        finally:
            raw.close()
        ledger.set_attempt_state("#40/1", "finished")
        real_reconcile = ledger.reconcile_accepted_commands
        calls = 0

        def _flaky_reconcile() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            real_reconcile()

        monkeypatch.setattr(
            ledger, "reconcile_accepted_commands", _flaky_reconcile
        )
        tracker = FakeTracker(agent_login="owner")
        tracker.candidates.clear()
        worker_run._poll_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(),
            run_one_attempt=_never_run,
            sleep=_no_sleep,
            max_sweeps=2,
        )
        assert calls >= 2
        connection = operator_commands.open_ledger_connection(
            ledger.path, timeout_seconds=2.0
        )
        try:
            stored = operator_commands.get_command(connection, "req-retry-orphan")
        finally:
            connection.close()
        assert stored is not None
        assert stored.acknowledgement == "completed"
    finally:
        store.close()
        ledger.close()


def _stop_after_request(request_id: str, repo: str, after: int) -> bytes:
    """A ``stop --after N`` engine request line."""
    body: dict[str, Any] = {
        "op": "stop",
        "request_id": request_id,
        "expect_repo": repo,
        "after": after,
    }
    return (json.dumps(body) + "\n").encode("utf-8")


def _stored_command(ledger_path: Path, request_id: str) -> operator_commands.CommandRecord | None:
    """Read one command through a fresh operator connection."""
    connection = operator_commands.open_ledger_connection(
        ledger_path, timeout_seconds=2.0
    )
    try:
        return operator_commands.get_command(connection, request_id)
    finally:
        connection.close()


def _stop_plan(ledger_path: Path) -> dict[str, Any] | None:
    """The durable stop plan through a fresh operator connection."""
    connection = operator_commands.open_ledger_connection(
        ledger_path, timeout_seconds=2.0
    )
    try:
        intake = operator_commands.read_intake(connection)
    finally:
        connection.close()
    plan = intake.get("stop_plan")
    assert plan is None or isinstance(plan, dict)
    return plan


def test_stop_after_two_counts_outcomes_then_closes(tmp_path: Path) -> None:
    """``stop --after 2`` lets two Attempts finish with an outcome, then drains."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-1", "example-owner/example-repo", 2
            ),
        )
        assert reply is not None
        assert reply["replayed"] is False
        command = reply["command"]
        assert command["acknowledgement"] == "accepted"
        assert command["arguments"] == {"after": 2}
        assert ledger.intake_is_open() is True

        status = operator_listener.build_status_result(ledger.path, _live())
        assert status["intake"]["stop_plan"] == {
            "request_id": "req-after-1",
            "sequence": command["sequence"],
            "remaining": 2,
            "counted": 0,
        }

        # First finish with a recorded Terminal Outcome counts.
        ledger.record_terminal_outcome(
            "#40/1", outcome="delivered", reason="first"
        )
        ledger.set_attempt_state("#40/1", "finished")
        plan = _stop_plan(ledger.path)
        assert plan is not None
        assert plan["remaining"] == 1
        assert plan["counted"] == 1
        stored = _stored_command(ledger.path, "req-after-1")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
        assert ledger.intake_is_open() is True

        # Intake stays open mid-plan: the next Attempt can still be claimed.
        opened = ledger.open_attempt_if_intake_open(
            "#40/2", effective_configuration={"repo": "example-owner/example-repo"}
        )
        assert opened is not None
        ledger.record_terminal_outcome(
            "#40/2", outcome="delivered", reason="second"
        )
        ledger.set_attempt_state("#40/2", "finished")
        assert _stop_plan(ledger.path) is None
        stored = _stored_command(ledger.path, "req-after-1")
        assert stored is not None
        assert stored.acknowledgement == "completed"
        assert ledger.intake_is_open() is False

        # The drain holds: no further Claim opens.
        assert (
            ledger.open_attempt_if_intake_open(
                "#40/3",
                effective_configuration={"repo": "example-owner/example-repo"},
            )
            is None
        )
    finally:
        store.close()
        ledger.close()


def test_stop_after_released_claim_does_not_count(tmp_path: Path) -> None:
    """A Claim ``released`` before confirmation never counts toward the plan."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-2", "example-owner/example-repo", 1
            ),
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "accepted"

        ledger.set_attempt_state("#40/1", "released")
        plan = _stop_plan(ledger.path)
        assert plan is not None
        assert plan["remaining"] == 1
        assert plan["counted"] == 0
        stored = _stored_command(ledger.path, "req-after-2")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
        assert ledger.intake_is_open() is True
    finally:
        store.close()
        ledger.close()


def test_stop_after_finished_without_outcome_does_not_count(
    tmp_path: Path,
) -> None:
    """A ``finished`` Attempt with no recorded outcome does not count."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-3", "example-owner/example-repo", 1
            ),
        )
        assert reply is not None
        ledger.set_attempt_state("#40/1", "finished")
        plan = _stop_plan(ledger.path)
        assert plan is not None
        assert plan["remaining"] == 1
        assert plan["counted"] == 0
        stored = _stored_command(ledger.path, "req-after-3")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
    finally:
        store.close()
        ledger.close()


def test_stop_after_supersession(tmp_path: Path) -> None:
    """A newer plan, ``stop`` or ``resume`` supersedes the pending plan."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        first = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-4", "example-owner/example-repo", 2
            ),
        )
        assert first is not None
        assert first["command"]["acknowledgement"] == "accepted"

        # A newer ``stop --after`` supersedes the previous plan.
        second = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-5", "example-owner/example-repo", 1
            ),
        )
        assert second is not None
        assert second["command"]["acknowledgement"] == "accepted"
        older = _stored_command(ledger.path, "req-after-4")
        assert older is not None
        assert older.acknowledgement == "superseded"
        plan = _stop_plan(ledger.path)
        assert plan is not None
        assert plan["request_id"] == "req-after-5"

        # A plain ``stop`` supersedes the pending plan.
        plain = _answer(
            ledger.path,
            _request("stop", request_id="req-stop-plain", expect_repo="example-owner/example-repo"),
        )
        assert plain is not None
        assert plain["command"]["acknowledgement"] == "accepted"
        superseded_plan = _stored_command(ledger.path, "req-after-5")
        assert superseded_plan is not None
        assert superseded_plan.acknowledgement == "superseded"

        # ``resume`` supersedes the pending stop and reopens Intake.
        resumed = _answer(
            ledger.path,
            _request("resume", request_id="req-resume-1", expect_repo="example-owner/example-repo"),
        )
        assert resumed is not None
        assert resumed["command"]["acknowledgement"] == "completed"
        superseded_stop = _stored_command(ledger.path, "req-stop-plain")
        assert superseded_stop is not None
        assert superseded_stop.acknowledgement == "superseded"
        assert ledger.intake_is_open() is True
    finally:
        store.close()
        ledger.close()


def test_resume_directly_supersedes_pending_plan(tmp_path: Path) -> None:
    """``resume`` supersedes a live stop plan and reopens Intake."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        first = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-6", "example-owner/example-repo", 2
            ),
        )
        assert first is not None
        assert first["command"]["acknowledgement"] == "accepted"
        assert _stop_plan(ledger.path) is not None

        resumed = _answer(
            ledger.path,
            _request("resume", request_id="req-resume-2", expect_repo="example-owner/example-repo"),
        )
        assert resumed is not None
        assert resumed["command"]["acknowledgement"] == "completed"
        older = _stored_command(ledger.path, "req-after-6")
        assert older is not None
        assert older.acknowledgement == "superseded"
        assert _stop_plan(ledger.path) is None
        assert ledger.intake_is_open() is True
    finally:
        store.close()
        ledger.close()


def test_stop_after_invalid_argument_stores_nothing(tmp_path: Path) -> None:
    """``--after 0`` and values above 999 answer ``invalid_argument``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        for request_id, after in (("req-after-0", 0), ("req-after-1000", 1000)):
            body: dict[str, Any] = {
                "op": "stop",
                "request_id": request_id,
                "expect_repo": "example-owner/example-repo",
                "after": after,
            }
            reply = _answer(ledger.path, (json.dumps(body) + "\n").encode("utf-8"))
            assert reply is not None
            assert reply == {"error_code": "invalid_argument"}
            assert _stored_command(ledger.path, request_id) is None
        assert _stop_plan(ledger.path) is None
        assert ledger.intake_is_open() is True
    finally:
        store.close()
        ledger.close()


def test_stop_plan_survives_restart_and_reconciliation(tmp_path: Path) -> None:
    """The plan (``remaining``/``counted``) is durable across a restart."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt(
            "#40/1", effective_configuration={"repo": "example-owner/example-repo"}
        )
        reply = _answer(
            ledger.path,
            _stop_after_request(
                "req-after-6", "example-owner/example-repo", 2
            ),
        )
        assert reply is not None
        ledger.record_terminal_outcome(
            "#40/1", outcome="delivered", reason="first"
        )
        ledger.set_attempt_state("#40/1", "finished")
    finally:
        store.close()
        ledger.close()

    reopened = RunLedger(ledger.path)
    reopened_store = OperatorStore(ledger.path)
    try:
        plan = _stop_plan(ledger.path)
        assert plan is not None
        assert plan["request_id"] == "req-after-6"
        assert plan["remaining"] == 1
        assert plan["counted"] == 1
        # Restart reconciliation never completes the pending plan.
        reopened.reconcile_accepted_commands()
        stored = _stored_command(ledger.path, "req-after-6")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
        assert _stop_plan(ledger.path) is not None
    finally:
        reopened_store.close()
        reopened.close()
