"""Read-source parity: one interface, live socket and offline copy agree.

Contract: issue #458. ``agentctl`` serves its read commands through one
:class:`~coding_agent.operator.sources.ReadSource` interface with two
adapters — :class:`~coding_agent.operator.sources.LiveSource` (socket)
and :class:`~coding_agent.operator.sources.OfflineSource` (Offline Read
copy via :mod:`coding_agent.operator.snapshots`) — instead of writing each
command twice. For a fixture ledger the live and offline commands print
identical JSON apart from the fields the contract defines as live-only.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import STATE_DIR_ENV_VAR
from coding_agent.operator.sources import LiveSource, OfflineSource
from coding_agent.operator.store import OperatorStore

REPO = "example-owner/example-repo"


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _ledger_with_instance(state_dir: Path) -> tuple[RunLedger, OperatorStore]:
    ledger_path = state_dir / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository=REPO, process_started_at=_started_at()
    )
    return ledger, store


def _effective_config() -> dict[str, Any]:
    return {"repo": REPO, "issue": 7}


def _fixture_ledger(state_dir: Path) -> tuple[RunLedger, OperatorStore]:
    """A fixture ledger: one stop command plus one open attempt with evidence."""
    from coding_agent.operator import archive as attempt_archives

    ledger, store = _ledger_with_instance(state_dir)
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    raw = (
        json.dumps({"op": "stop", "request_id": "req-src-1", "expect_repo": REPO})
        + "\n"
    ).encode("utf-8")
    reply = operator_listener._handle_one(
        raw, ledger_path=state_dir / "run-ledger.sqlite3", live=live
    )
    assert reply is not None and "command" in reply
    ledger.open_attempt("#7/1", effective_configuration=_effective_config())
    archive = attempt_archives.archive_dir_for(state_dir, REPO, "#7/1")
    (archive / "notes.txt").write_text("draft", encoding="utf-8")
    return ledger, store


def _run_live(
    argv: list[str],
    state_dir: Path,
    live_result: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    """Run a live ``agentctl`` command with the socket stubbed to ``live_result``."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(state_dir))
    monkeypatch.setattr(
        agentctl, "_query_engine", lambda _body: copy.deepcopy(live_result)
    )
    code = agentctl.main(["--contract", "1"] + argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    envelope = json.loads(captured.out)
    assert envelope["ok"] is True
    return code, envelope["result"]


def _run_offline(
    argv: list[str],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(state_dir))
    code = agentctl.main(["--contract", "1", "--offline"] + argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    envelope = json.loads(captured.out)
    assert envelope["ok"] is True
    return code, envelope["result"]


def _scrub_identity(identity: dict[str, Any]) -> dict[str, Any]:
    scrubbed = copy.deepcopy(identity)
    scrubbed["mode"] = "*"
    scrubbed["process_started_at"] = "*"
    return scrubbed


def _scrub_status(status: dict[str, Any]) -> dict[str, Any]:
    scrubbed = copy.deepcopy(status)
    scrubbed["generated_at"] = "*"
    identity = dict(scrubbed.get("identity") or {})
    identity["mode"] = "*"
    identity["process_started_at"] = "*"
    scrubbed["identity"] = identity
    instance = dict(scrubbed.get("instance") or {})
    instance["state"] = "*"
    instance["startup_check"] = "*"
    instance["observed_at"] = "*"
    scrubbed["instance"] = instance
    intake = dict(scrubbed.get("intake") or {})
    intake["observed_at"] = "*"
    scrubbed["intake"] = intake
    scrubbed["current_attempt"] = None
    scrubbed["recent_events"] = []
    return scrubbed


def test_no_offline_twins_for_identity_or_status() -> None:
    assert not hasattr(agentctl, "_serve_identity_offline")
    assert not hasattr(agentctl, "_serve_status_offline")


def test_agentctl_has_no_raw_sql() -> None:
    text = Path(agentctl.__file__).read_text(encoding="utf-8")
    assert "sqlite3.connect" not in text
    assert "SELECT" not in text
    assert "SELECT " not in text


def test_sources_satisfy_protocol_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    ledger, store = _fixture_ledger(tmp_path)
    try:
        ledger_path = tmp_path / "run-ledger.sqlite3"
        live_result = operator_listener.build_identity_result(
            ledger_path, LiveView(process_started_at=_started_at())
        )
        live = LiveSource(
            query=lambda _body: copy.deepcopy(live_result),
            ledger_path=ledger_path,
            state_dir=tmp_path,
        )
        offline = OfflineSource(ledger_path=ledger_path, state_dir=tmp_path)
        assert isinstance(live.identity()["backend"], str)
        assert isinstance(offline.identity()["backend"], str)
        page = offline.attempts_page(20, None)
        assert set(page.keys()) == {"attempts", "next_cursor"}
        assert page["attempts"] != []
        shown = offline.show_attempt("#7/1")
        assert shown is not None and "attempt" in shown and "manifest" in shown
        assert offline.show_attempt("#7/unknown") is None
    finally:
        store.close()
        ledger.close()


def test_identity_live_offline_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, store = _fixture_ledger(tmp_path)
    try:
        ledger_path = tmp_path / "run-ledger.sqlite3"
        live = LiveView(process_started_at=_started_at())
        live.set_ready()
        live_result = operator_listener.build_identity_result(ledger_path, live)
        code_live, live_out = _run_live(
            ["identity"], tmp_path, live_result, monkeypatch, capsys
        )
        code_off, offline_out = _run_offline(
            ["identity"], tmp_path, monkeypatch, capsys
        )
        assert code_live == 0
        assert code_off == 0
        assert live_out["mode"] == "online"
        assert isinstance(live_out["process_started_at"], str)
        assert offline_out["mode"] == "offline"
        assert offline_out["process_started_at"] is None
        assert _scrub_identity(live_out) == _scrub_identity(offline_out)
    finally:
        store.close()
        ledger.close()


def test_status_live_offline_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, store = _fixture_ledger(tmp_path)
    try:
        ledger_path = tmp_path / "run-ledger.sqlite3"
        live = LiveView(process_started_at=_started_at())
        live.set_ready()
        live_result = operator_listener.build_status_result(ledger_path, live)
        code_live, live_out = _run_live(
            ["status"], tmp_path, live_result, monkeypatch, capsys
        )
        code_off, offline_out = _run_offline(
            ["status"], tmp_path, monkeypatch, capsys
        )
        assert code_live == 0
        assert code_off == 0
        assert live_out["identity"]["mode"] == "online"
        assert offline_out["identity"]["mode"] == "offline"
        assert offline_out["instance"]["state"] == "stopped"
        assert offline_out["current_attempt"] is None
        assert offline_out["recent_events"] == []
        assert live_out["instance"]["state"] != "stopped"
        assert _scrub_status(live_out) == _scrub_status(offline_out)
    finally:
        store.close()
        ledger.close()


def test_attempts_list_live_offline_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, store = _fixture_ledger(tmp_path)
    try:
        monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
        code_live, live_out = _run_live(
            ["attempts", "list"],
            tmp_path,
            {},
            monkeypatch,
            capsys,
        )
        assert code_live == 0
        code_off, offline_out = _run_offline(
            ["attempts", "list"], tmp_path, monkeypatch, capsys
        )
        assert code_off == 0
        assert live_out == offline_out
        assert [e["attempt_id"] for e in live_out["attempts"]] == ["#7/1"]
    finally:
        store.close()
        ledger.close()


def test_attempts_show_live_offline_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, store = _fixture_ledger(tmp_path)
    try:
        monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
        code_live, live_out = _run_live(
            ["attempts", "show", "#7/1"],
            tmp_path,
            {},
            monkeypatch,
            capsys,
        )
        assert code_live == 0
        code_off, offline_out = _run_offline(
            ["attempts", "show", "#7/1"], tmp_path, monkeypatch, capsys
        )
        assert code_off == 0
        assert live_out == offline_out
        assert live_out["attempt"]["attempt_id"] == "#7/1"
        assert {e["path"] for e in live_out["manifest"]} >= {"notes.txt"}
    finally:
        store.close()
        ledger.close()
