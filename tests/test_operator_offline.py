"""Offline Read of a stopped Worker (issue #257).

Contract: parent #245 (adoption step 4) and ``docs/contract/mvp-runtime-contract.md``
§3 (Offline Read). An operator runs ``agentctl --contract 1 --offline <operation>``
in a throwaway container with every volume mounted read-only and no network.
It serves ``identity``, ``status``, ``command`` and ``attempts *``: the reader
never takes the volume lock and never writes, probing the lock with a
non-blocking shared lock (a live holder answers ``rejected`` with
``details.reason: "instance_running"``); the ledger is read through the copy
approach; ``identity`` carries the Target Repository and version with
``mode: "offline"`` and ``process_started_at: None``; ``status`` reports
``stopped`` with the last recorded rows and empty ``recent_events``;
mutations answer ``unsupported``; the state directory is unchanged.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator import listener as operator_listener
from coding_agent.operator import offline as offline_reader
from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import STATE_DIR_ENV_VAR
from coding_agent.operator.store import OperatorStore, agent_version
from coding_agent.worker.volume import VOLUME_LOCK_FILE_NAME

REPO = "example-owner/example-repo"


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _ledger_with_instance(state_dir: Path) -> tuple[RunLedger, OperatorStore]:
    """A ledger on ``state_dir`` with this start's Target Repository recorded."""
    ledger_path = state_dir / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository=REPO, process_started_at=_started_at()
    )
    return ledger, store


def _effective_config() -> dict[str, Any]:
    return {"repo": REPO, "issue": 7}


def _record_stop(ledger_path: Path, request_id: str) -> dict[str, Any]:
    """Commit a ``stop`` command durably through the listener seam."""
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    raw = (
        json.dumps(
            {"op": "stop", "request_id": request_id, "expect_repo": REPO}
        )
        + "\n"
    ).encode("utf-8")
    reply = operator_listener._handle_one(raw, ledger_path=ledger_path, live=live)
    assert reply is not None
    assert "command" in reply
    result: dict[str, Any] = dict(reply)
    return result


def _run_offline(
    argv: list[str],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Run ``agentctl --contract 1 --offline ...`` against ``state_dir``."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(state_dir))
    code = agentctl.main(["--contract", "1", "--offline"] + argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, captured.out


def _parse_envelope(stdout: str) -> dict[str, Any]:
    assert stdout.endswith("\n")
    assert stdout.count("\n") == 1
    envelope: dict[str, Any] = json.loads(stdout)
    return envelope


def _file_hashes(state_dir: Path) -> dict[str, str]:
    """SHA-256 over every file under ``state_dir``, keyed by relative path."""
    hashes: dict[str, str] = {}
    for path in sorted(state_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            hashes[str(path.relative_to(state_dir))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def _hold_exclusive_lock(state_dir: Path) -> int:
    """Hold the volume lock exclusively, as a live Worker would."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        state_dir / VOLUME_LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o644
    )
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_offline_identity_reports_stopped_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the Worker exits, offline ``identity`` carries the recorded repo."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger_path = tmp_path / "run-ledger.sqlite3"
        result = offline_reader.load_offline_identity(ledger_path)
        assert result["backend"] == "coding-agent"
        assert result["target_repository"] == REPO
        assert result["repository"] == REPO
        assert result["mode"] == "offline"
        assert result["process_started_at"] is None
        assert result["contract"] == "1.0"
        assert result["agent_version"] == agent_version()

        code, out = _run_offline(["identity"], tmp_path, monkeypatch, capsys)
        assert code == 0
        envelope = _parse_envelope(out)
        assert envelope["ok"] is True
        identity = envelope["result"]
        assert identity["mode"] == "offline"
        assert identity["process_started_at"] is None
        assert identity["target_repository"] == REPO
        assert identity["backend"] == "coding-agent"
    finally:
        store.close()
        ledger.close()


def test_offline_status_after_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the Worker exits, offline ``status`` is stopped with last rows."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        _record_stop(tmp_path / "run-ledger.sqlite3", "req-offline-1")
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())

        code, out = _run_offline(["status"], tmp_path, monkeypatch, capsys)
        assert code == 0
        envelope = _parse_envelope(out)
        assert envelope["ok"] is True
        status = envelope["result"]
        assert status["instance"]["state"] == "stopped"
        assert status["process_started_at"] is None
        assert status["current_attempt"] is None
        assert status["recent_events"] == []
        assert status["intake"]["state"] == "closed"
        assert len(status["recent_commands"]) == 1
        assert status["recent_commands"][0]["request_id"] == "req-offline-1"
        assert status["instance"]["last_halt"] is None
    finally:
        store.close()
        ledger.close()


def test_offline_status_shows_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After a Halt, offline ``status`` shows that Halt."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        store.record_halt(reason="OOM", detail="out of memory")
        code, out = _run_offline(["status"], tmp_path, monkeypatch, capsys)
        assert code == 0
        status = _parse_envelope(out)["result"]
        assert status["instance"]["state"] == "stopped"
        halt = status["instance"]["last_halt"]
        assert halt is not None
        assert halt["reason"] == "OOM"
    finally:
        store.close()
        ledger.close()


def test_offline_reads_uncheckpointed_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A WAL left by a Worker that did not shut down cleanly stays visible."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        _record_stop(tmp_path / "run-ledger.sqlite3", "req-wal-1")
        ledger.open_attempt("#7/9", effective_configuration=_effective_config())
        # The writer connection stays open: committed frames sit in an
        # uncheckpointed WAL, exactly as after an unclean shutdown.
        ledger_path = tmp_path / "run-ledger.sqlite3"
        status = offline_reader.load_offline_status(ledger_path)
        assert status["instance"]["state"] == "stopped"
        assert [c["request_id"] for c in status["recent_commands"]] == [
            "req-wal-1"
        ]
        page, _ = offline_reader.load_offline_attempts_page(
            ledger_path, limit=20, before=None
        )
        assert [entry["attempt_id"] for entry in page] == ["#7/9"]

        code, out = _run_offline(
            ["attempts", "list"], tmp_path, monkeypatch, capsys
        )
        assert code == 0
        envelope = _parse_envelope(out)
        assert [e["attempt_id"] for e in envelope["result"]["attempts"]] == [
            "#7/9"
        ]
    finally:
        store.close()
        ledger.close()


def test_offline_command_and_attempts_show_share_archive_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline ``command`` and ``attempts show`` read the same archive path."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        _record_stop(tmp_path / "run-ledger.sqlite3", "req-offline-2")
        ledger.open_attempt("#7/3", effective_configuration=_effective_config())
        archive = attempt_archives.archive_dir_for(tmp_path, REPO, "#7/3")
        (archive / "notes.txt").write_text("draft", encoding="utf-8")
        ledger_path = tmp_path / "run-ledger.sqlite3"

        online = attempt_archives.show_attempt(
            ledger_path=ledger_path, state_dir=tmp_path, attempt_id="#7/3"
        )
        offline = offline_reader.load_offline_attempt(
            ledger_path, tmp_path, "#7/3"
        )
        assert online is not None and offline is not None
        assert offline == online

        code, out = _run_offline(
            ["command", "req-offline-2"], tmp_path, monkeypatch, capsys
        )
        assert code == 0
        command = _parse_envelope(out)["result"]["command"]
        assert command["request_id"] == "req-offline-2"

        code2, out2 = _run_offline(
            ["attempts", "show", "#7/3"], tmp_path, monkeypatch, capsys
        )
        assert code2 == 0
        shown = _parse_envelope(out2)["result"]
        assert shown["attempt"]["attempt_id"] == "#7/3"
        assert {e["path"] for e in shown["manifest"]} >= {"notes.txt"}

        code3, out3 = _run_offline(
            ["command", "req-unknown"], tmp_path, monkeypatch, capsys
        )
        assert code3 == 5
        assert _parse_envelope(out3)["error"]["code"] == "not_found"
    finally:
        store.close()
        ledger.close()


def test_offline_export_streams_tar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline ``attempts export`` streams the same tar as online."""
    import sys

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/4", effective_configuration=_effective_config())
        archive = attempt_archives.archive_dir_for(tmp_path, REPO, "#7/4")
        (archive / "a.txt").write_text("hello", encoding="utf-8")
        ledger.record_terminal_outcome("#7/4", outcome="failed", reason="x")
        ledger.set_attempt_state("#7/4", "finished")

        monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
        buffer = io.BytesIO()

        class _Stdout:
            def __init__(self) -> None:
                self.buffer = buffer

            def write(self, text: str) -> int:
                raise AssertionError("export must not emit JSON text")

            def flush(self) -> None:
                return None

        monkeypatch.setattr(sys, "stdout", _Stdout())
        code = agentctl.main(
            ["--contract", "1", "--offline", "attempts", "export", "#7/4"]
        )
        assert code == 0
        raw = buffer.getvalue()
        assert len(raw) > 0
        reader = tarfile.open(fileobj=io.BytesIO(raw), mode="r")
        try:
            names = reader.getnames()
            assert names[0] == "manifest.json"
            assert "a.txt" in names
        finally:
            reader.close()
    finally:
        store.close()
        ledger.close()


def test_offline_mutations_answer_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline ``stop``, ``resume``, ``stop --after`` and ``next`` are refused."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        cases = [
            ["stop", "--request-id", "req-m1", "--expect-repo", REPO],
            ["resume", "--request-id", "req-m2", "--expect-repo", REPO],
            [
                "stop",
                "--after",
                "3",
                "--request-id",
                "req-m3",
                "--expect-repo",
                REPO,
            ],
            [
                "next",
                "issue",
                "43",
                "--request-id",
                "req-m4",
                "--expect-repo",
                REPO,
            ],
        ]
        for argv in cases:
            code, out = _run_offline(list(argv), tmp_path, monkeypatch, capsys)
            assert code == 3
            assert _parse_envelope(out)["error"]["code"] == "unsupported"
    finally:
        store.close()
        ledger.close()


def test_offline_locked_dir_answers_instance_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every offline call against a running Worker's lock is rejected."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/5", effective_configuration=_effective_config())
        fd = _hold_exclusive_lock(tmp_path)
        try:
            assert offline_reader.instance_running(tmp_path) is True
            argv_cases = [
                ["identity"],
                ["status"],
                ["command", "req-x"],
                ["attempts", "list"],
                ["attempts", "show", "#7/5"],
                ["attempts", "export", "#7/5"],
                ["stop", "--request-id", "req-x", "--expect-repo", REPO],
                ["resume", "--request-id", "req-x", "--expect-repo", REPO],
                [
                    "stop",
                    "--after",
                    "3",
                    "--request-id",
                    "req-x",
                    "--expect-repo",
                    REPO,
                ],
                [
                    "next",
                    "issue",
                    "7",
                    "--request-id",
                    "req-x",
                    "--expect-repo",
                    REPO,
                ],
                ["handoff", "now", "--request-id", "req-x",
                 "--expect-repo", REPO],
                ["recovery", "retry", "#7/5", "--request-id", "req-x",
                 "--expect-repo", REPO],
                [
                    "recovery",
                    "release",
                    "#7/5",
                    "--saved-at",
                    "2026-11-06T08:00:00+00:00",
                    "--request-id",
                    "req-x",
                    "--expect-repo",
                    REPO,
                ],
                ["errors", "reset", "--request-id", "req-x",
                 "--expect-repo", REPO],
                # The probe runs first: even malformed calls are rejected
                # while a live holder owns the lock, not usage/invalid.
                ["stop"],
                ["stop", "--request-id", "bad id!", "--expect-repo", REPO],
                ["bogus-op"],
            ]
            for argv in argv_cases:
                code, out = _run_offline(
                    list(argv), tmp_path, monkeypatch, capsys
                )
                assert code == 1, argv
                envelope = _parse_envelope(out)
                assert envelope["error"]["code"] == "rejected", argv
                assert envelope["error"]["details"] == {
                    "reason": "instance_running"
                }, argv
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        store.close()
        ledger.close()


def test_offline_leaves_state_dir_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No offline call writes: hashes before and after match exactly."""
    import sys

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        _record_stop(tmp_path / "run-ledger.sqlite3", "req-frozen-1")
        ledger.open_attempt("#7/6", effective_configuration=_effective_config())
        archive = attempt_archives.archive_dir_for(tmp_path, REPO, "#7/6")
        (archive / "notes.txt").write_text("draft", encoding="utf-8")
    finally:
        store.close()
        ledger.close()

    before = _file_hashes(tmp_path)
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))

    code, _ = _run_offline(["identity"], tmp_path, monkeypatch, capsys)
    assert code == 0
    code, _ = _run_offline(["status"], tmp_path, monkeypatch, capsys)
    assert code == 0
    code, _ = _run_offline(
        ["command", "req-frozen-1"], tmp_path, monkeypatch, capsys
    )
    assert code == 0
    code, _ = _run_offline(
        ["attempts", "list"], tmp_path, monkeypatch, capsys
    )
    assert code == 0
    code, _ = _run_offline(
        ["attempts", "show", "#7/6"], tmp_path, monkeypatch, capsys
    )
    assert code == 0
    # Offline mutations are refused without writing anything either.
    for argv in (
        ["stop", "--request-id", "req-frozen-2", "--expect-repo", REPO],
        ["resume", "--request-id", "req-frozen-3", "--expect-repo", REPO],
        [
            "next",
            "issue",
            "7",
            "--request-id",
            "req-frozen-4",
            "--expect-repo",
            REPO,
        ],
    ):
        mutation_code, _ = _run_offline(
            list(argv), tmp_path, monkeypatch, capsys
        )
        assert mutation_code == 3

    buffer = io.BytesIO()

    class _Stdout:
        def __init__(self) -> None:
            self.buffer = buffer

        def write(self, text: str) -> int:
            raise AssertionError("export must not emit JSON text")

        def flush(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdout", _Stdout())
    assert (
        agentctl.main(
            ["--contract", "1", "--offline", "attempts", "export", "#7/6"]
        )
        == 0
    )
    assert len(buffer.getvalue()) > 0
    assert _file_hashes(tmp_path) == before


def test_offline_read_only_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every offline read works with the state directory mounted read-only."""
    import sys

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        _record_stop(tmp_path / "run-ledger.sqlite3", "req-ro-1")
        ledger.open_attempt("#7/7", effective_configuration=_effective_config())
    finally:
        store.close()
        ledger.close()

    os.chmod(tmp_path, 0o555)
    try:
        code, out = _run_offline(["identity"], tmp_path, monkeypatch, capsys)
        assert code == 0
        assert _parse_envelope(out)["result"]["mode"] == "offline"
        code, out = _run_offline(["status"], tmp_path, monkeypatch, capsys)
        assert code == 0
        assert _parse_envelope(out)["result"]["instance"]["state"] == "stopped"
        code, out = _run_offline(
            ["command", "req-ro-1"], tmp_path, monkeypatch, capsys
        )
        assert code == 0
        assert _parse_envelope(out)["result"]["command"]["request_id"] == "req-ro-1"
        code, out = _run_offline(
            ["attempts", "list"], tmp_path, monkeypatch, capsys
        )
        assert code == 0
        assert _parse_envelope(out)["result"]["attempts"][0]["attempt_id"] == "#7/7"
        code, out = _run_offline(
            ["attempts", "show", "#7/7"], tmp_path, monkeypatch, capsys
        )
        assert code == 0

        buffer = io.BytesIO()

        class _Stdout:
            def __init__(self) -> None:
                self.buffer = buffer

            def write(self, text: str) -> int:
                raise AssertionError("export must not emit JSON text")

            def flush(self) -> None:
                return None

        monkeypatch.setattr(sys, "stdout", _Stdout())
        assert (
            agentctl.main(
                ["--contract", "1", "--offline", "attempts", "export", "#7/7"]
            )
            == 0
        )
        assert len(buffer.getvalue()) > 0
    finally:
        os.chmod(tmp_path, 0o755)
