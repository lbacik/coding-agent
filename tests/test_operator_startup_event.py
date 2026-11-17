"""OperatorStore.emit_startup_event (slice A of #434).

Contract: adoption step 4 of the operator contract (#245). The Worker
supervisor emits the startup Contract Events (``process_started``,
``startup_check``, ``ready``) through this method instead of reaching
into the store's private lock and connection. Contract ``v1.0.4``
extras: ``process_started`` carries ``agent_version``,
``startup_check`` carries ``check`` and ``result``. With no
``process_started_at`` recorded nothing is emitted and nothing is
raised; a failed emit still never stops startup (the supervisor's
best-effort wrapper prints the same stderr line it always has).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent.operator.events import list_recent_contract_events
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.worker import supervisor


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _store_with_instance(tmp_path: Path) -> OperatorStore:
    """A store whose instance row records this start's ``process_started_at``."""
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    store.write_instance_metadata(
        target_repository="example-owner/example-repo",
        process_started_at=_started_at(),
    )
    return store


def _event_rows(ledger_path: Path) -> list[Any]:
    """Raw ``operator_events`` rows in insert order (for column assertions)."""
    connection = sqlite3.connect(str(ledger_path))
    try:
        return connection.execute(
            "SELECT type, text, check_name, result, agent_version"
            " FROM operator_events ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def test_startup_check_carries_check_and_result(tmp_path: Path) -> None:
    """``startup_check`` stores and reads back its ``check``/``result``."""
    store = _store_with_instance(tmp_path)
    try:
        store.emit_startup_event(
            event_type="startup_check",
            text="skill-bundle",
            check="skill-bundle",
            result="started",
        )
        rows = _event_rows(store.path)
        assert len(rows) == 1
        assert rows[0][0] == "startup_check"
        assert rows[0][1] == "skill-bundle"
        assert rows[0][2] == "skill-bundle"
        assert rows[0][3] == "started"
        connection = sqlite3.connect(str(store.path))
        try:
            instance = connection.execute(
                "SELECT process_started_at FROM operator_instance WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        assert instance is not None
        read_connection = sqlite3.connect(str(store.path))
        try:
            events = list_recent_contract_events(
                read_connection, process_started_at=str(instance[0])
            )
        finally:
            read_connection.close()
        assert len(events) == 1
        assert events[0]["event"] == "startup_check"
        assert events[0]["check"] == "skill-bundle"
        assert events[0]["result"] == "started"
    finally:
        store.close()


def test_process_started_carries_agent_version(tmp_path: Path) -> None:
    """``process_started`` stores and reads back its ``agent_version``."""
    store = _store_with_instance(tmp_path)
    try:
        store.emit_startup_event(
            event_type="process_started",
            text="worker started on example-owner/example-repo",
            agent_version="9.9.9",
        )
        rows = _event_rows(store.path)
        assert len(rows) == 1
        assert rows[0][0] == "process_started"
        assert rows[0][4] == "9.9.9"
        connection = sqlite3.connect(str(store.path))
        try:
            instance = connection.execute(
                "SELECT process_started_at FROM operator_instance WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        assert instance is not None
        read_connection = sqlite3.connect(str(store.path))
        try:
            events = list_recent_contract_events(
                read_connection, process_started_at=str(instance[0])
            )
        finally:
            read_connection.close()
        assert len(events) == 1
        assert events[0]["event"] == "process_started"
        assert events[0]["agent_version"] == "9.9.9"
    finally:
        store.close()


def test_no_instance_row_emits_nothing(tmp_path: Path) -> None:
    """No instance row: nothing is emitted and nothing is raised."""
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    try:
        store.emit_startup_event(
            event_type="startup_check",
            text="skill-bundle",
            check="skill-bundle",
            result="started",
        )
        assert _event_rows(store.path) == []
    finally:
        store.close()


def test_empty_process_started_at_emits_nothing(tmp_path: Path) -> None:
    """An instance row without ``process_started_at`` emits nothing."""
    store = _store_with_instance(tmp_path)
    try:
        connection = sqlite3.connect(str(store.path))
        try:
            connection.execute(
                "UPDATE operator_instance SET process_started_at = '' WHERE id = 1"
            )
            connection.commit()
        finally:
            connection.close()
        store.emit_startup_event(
            event_type="ready",
            text="ready",
        )
        assert _event_rows(store.path) == []
    finally:
        store.close()


def test_supervisor_wrapper_stays_best_effort(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed emit prints the same stderr line and never stops startup."""
    store = _store_with_instance(tmp_path)
    try:

        def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("disk gone")

        monkeypatch.setattr(
            "coding_agent.operator.events.emit_contract_event", _boom
        )
        supervisor._emit_startup_event(
            store,
            event_type="ready",
            text="ready",
            registry=SecretRegistry({}),
        )
        captured = capsys.readouterr()
        assert captured.err == "run: could not emit ready (disk gone)\n"
    finally:
        store.close()
