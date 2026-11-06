"""Offline Read of a stopped Worker without starting it (adoption step 4, #257).

Contract: parent #245 and ``docs/contract/mvp-runtime-contract.md`` §3
(Offline Read). An operator runs ``agentctl --contract 1 --offline
<operation>`` in a throwaway container with every volume mounted read-only
and no network. It serves ``identity``, ``status``, ``command`` and
``attempts *``.

The reader never takes the volume lock and never writes. It first probes
the lock with a non-blocking shared lock: a live Worker holds it
exclusively, so the probe fails and the call answers ``rejected`` with
``details.reason: "instance_running"``. The ledger is read through the copy
approach proven in #246 (:mod:`coding_agent.ledger.offline`): the database
file and any ``-wal`` file are copied to a private temporary directory and
the copy is opened, so even an uncheckpointed WAL shows the latest
committed state while the source directory gains no file.

``identity`` carries the Target Repository and version from the instance
metadata, with ``mode: "offline"`` and ``process_started_at: None``,
because the Workflow Configuration is not mounted. ``status`` reports
``stopped``, ``last_halt``, ``last_finished``, ``recent_commands`` and
Intake as last recorded; ``recent_events`` is empty. Mutations answer
``unsupported``.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.worker.volume import VOLUME_LOCK_FILE_NAME

INSTANCE_RUNNING_REASON = "instance_running"


def instance_running(state_dir: Path) -> bool:
    """Whether a live Worker holds the volume lock (non-blocking probe).

    Opens the lock file read-only (never creating it) and tries a shared
    non-blocking ``flock``. A live Worker holds the same file exclusively,
    so the shared probe fails with ``EWOULDBLOCK``/``EACCES``. A missing
    lock file, a missing state directory or any other open failure means
    no live holder. The probe never takes the exclusive lock, never
    creates the file and never writes: the source is unchanged afterwards.
    """
    lock_path = state_dir / VOLUME_LOCK_FILE_NAME
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EACCES):
                return True
            return False
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_instance_row(copy_path: Path) -> dict[str, str] | None:
    """This start's instance metadata from the ledger copy, if recorded."""
    try:
        connection = sqlite3.connect(str(copy_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT target_repository, backend, agent_version,"
                " process_started_at FROM operator_instance WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {
            "target_repository": str(row[0]),
            "backend": str(row[1]),
            "agent_version": str(row[2]),
            "process_started_at": str(row[3]),
        }
    finally:
        connection.close()


def _offline_capabilities() -> list[str]:
    """The advertised capabilities, imported lazily to avoid a cycle."""
    from coding_agent.agentctl import ADVERTISED_CAPABILITIES

    return sorted(ADVERTISED_CAPABILITIES)


def _identity_from_stored(stored: dict[str, str] | None) -> dict[str, Any]:
    """The offline ``identity`` result for an instance row (or none)."""
    from coding_agent.operator.store import OPERATOR_BACKEND, agent_version

    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    return {
        "backend": backend,
        "agent_version": version,
        "version": version,
        "contract": "1.0",
        "repository": repository,
        "target_repository": repository,
        "mode": "offline",
        "process_started_at": None,
        "capabilities": _offline_capabilities(),
    }


def load_offline_identity(source_ledger: Path) -> dict[str, Any]:
    """The offline ``identity`` result for the ledger at ``source_ledger``.

    Reads through the copy approach so an uncheckpointed WAL shows the
    latest committed instance row. A missing ledger file means no instance
    was ever recorded: the result carries the empty repository with the
    package version. Raises :class:`OSError` where the copy cannot be read.
    """
    if not source_ledger.is_file():
        return _identity_from_stored(None)
    from coding_agent.ledger.offline import open_offline_ledger

    try:
        with open_offline_ledger(source_ledger) as ledger:
            stored = _read_instance_row(ledger.path)
    except FileNotFoundError:
        return _identity_from_stored(None)
    return _identity_from_stored(stored)


def _read_intake(copy_path: Path) -> dict[str, Any]:
    """The durable Intake state from the ledger copy, open where absent."""
    from coding_agent.operator.commands import read_intake

    try:
        connection = sqlite3.connect(str(copy_path), timeout=2.0)
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {copy_path}") from exc
    try:
        try:
            return read_intake(connection)
        except sqlite3.Error:
            return {"state": "open", "pending_stop": None}
    finally:
        connection.close()


def _read_commands(copy_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` commands from the copy, newest last."""
    from coding_agent.operator.commands import list_recent_commands

    try:
        connection = sqlite3.connect(str(copy_path), timeout=2.0)
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {copy_path}") from exc
    try:
        try:
            return [record.to_dict() for record in list_recent_commands(connection, limit)]
        except sqlite3.Error:
            return []
    finally:
        connection.close()


def load_offline_status(source_ledger: Path) -> dict[str, Any]:
    """The offline ``status`` result: ``stopped`` with the last recorded rows.

    Instance state is always ``stopped``: there is no live process to be
    ``starting`` or ``active``. Intake, ``last_halt``, ``last_finished``
    and ``recent_commands`` are read from the ledger copy as last recorded;
    ``current_attempt`` is ``None`` and ``recent_events`` is empty (no live
    process owns events). A missing ledger file reads as a fresh volume:
    Intake open, nothing halted or finished, no commands. The snapshot is
    capped at 256 KiB in the same contract order as the live one.
    """
    from coding_agent.operator import listener as operator_listener

    now = _utc_now_iso()
    if not source_ledger.is_file():
        result: dict[str, Any] = {
            "generated_at": now,
            "process_started_at": None,
            "startup_check": None,
            "instance": {
                "state": "stopped",
                "observed_at": now,
                "last_halt": None,
                "startup_check": None,
            },
            "intake": {"state": "open", "pending_stop": None, "observed_at": now},
            "current_attempt": None,
            "last_finished": None,
            "recent_commands": [],
            "recent_events": [],
        }
        return operator_listener._trim_status_snapshot(result)
    from coding_agent.ledger.offline import open_offline_ledger

    try:
        with open_offline_ledger(source_ledger) as ledger:
            copy_path = ledger.path
            intake = _read_intake(copy_path)
            intake_state = str(intake.get("state") or "open")
            if intake_state not in ("open", "closed"):
                intake_state = "open"
            pending = intake.get("pending_stop")
            pending_ref: dict[str, Any] | None = None
            if isinstance(pending, dict) and pending.get("request_id"):
                try:
                    pending_ref = {
                        "request_id": str(pending["request_id"]),
                        "sequence": int(pending.get("sequence") or 0),
                    }
                except (TypeError, ValueError):
                    pending_ref = None
            last_halt = operator_listener._read_last_halt(copy_path)
            last_finished = operator_listener._build_last_finished(copy_path, now)
            recent_commands = _read_commands(copy_path)
    except FileNotFoundError:
        result = {
            "generated_at": now,
            "process_started_at": None,
            "startup_check": None,
            "instance": {
                "state": "stopped",
                "observed_at": now,
                "last_halt": None,
                "startup_check": None,
            },
            "intake": {"state": "open", "pending_stop": None, "observed_at": now},
            "current_attempt": None,
            "last_finished": None,
            "recent_commands": [],
            "recent_events": [],
        }
        return operator_listener._trim_status_snapshot(result)
    result = {
        "generated_at": now,
        "process_started_at": None,
        "startup_check": None,
        "instance": {
            "state": "stopped",
            "observed_at": now,
            "last_halt": last_halt,
            "startup_check": None,
        },
        "intake": {
            "state": intake_state,
            "pending_stop": pending_ref,
            "observed_at": now,
        },
        "current_attempt": None,
        "last_finished": last_finished,
        "recent_commands": recent_commands,
        "recent_events": [],
    }
    return operator_listener._trim_status_snapshot(result)


def load_offline_command(
    source_ledger: Path, request_id: str
) -> dict[str, Any] | None:
    """The stored command for ``request_id`` from the ledger copy, if any.

    ``None`` where no command is recorded — including where the ledger file
    is missing — so the caller answers ``not_found``. Raises
    :class:`OSError` where the copy cannot be read.
    """
    if not source_ledger.is_file():
        return None
    from coding_agent.ledger.offline import open_offline_ledger
    from coding_agent.operator.commands import get_command

    with open_offline_ledger(source_ledger) as ledger:
        try:
            connection = sqlite3.connect(str(ledger.path), timeout=2.0)
        except sqlite3.Error as exc:
            raise OSError(f"cannot read the Run Ledger at {source_ledger}") from exc
        try:
            try:
                record = get_command(connection, request_id)
            except sqlite3.Error as exc:
                raise OSError(f"cannot read the Run Ledger at {source_ledger}") from exc
            if record is None:
                return None
            return record.to_dict()
        finally:
            connection.close()


def load_offline_attempts_page(
    source_ledger: Path, *, limit: int, before: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """One offline ``attempts list`` page, newest first, via the copy.

    A missing ledger file means no Attempt ever opened: the page is empty.
    Raises :class:`InvalidCursor` for a cursor that does not decode and
    :class:`ValueError` for a ``limit`` outside ``1..100`` (same as online).
    """
    from coding_agent.operator.attempts import InvalidCursor, list_attempts_page

    _ = InvalidCursor
    if not source_ledger.is_file():
        return ([], None)
    from coding_agent.ledger.offline import open_offline_ledger

    with open_offline_ledger(source_ledger) as ledger:
        return list_attempts_page(ledger.path, limit=limit, before=before)


def load_offline_attempt(
    source_ledger: Path, state_dir: Path, attempt_id: str
) -> dict[str, Any] | None:
    """The offline ``attempts show`` payload, or ``None`` where unknown.

    The summary comes from the ledger copy; the manifest is computed on
    read from the real state directory through the same archive code path
    as online. A missing ledger file holds no Attempt.
    """
    if not source_ledger.is_file():
        return None
    from coding_agent.ledger.offline import open_offline_ledger
    from coding_agent.operator.attempts import show_attempt

    with open_offline_ledger(source_ledger) as ledger:
        return show_attempt(
            ledger_path=ledger.path, state_dir=state_dir, attempt_id=attempt_id
        )


def load_offline_export(
    source_ledger: Path, state_dir: Path, attempt_id: str
) -> tuple[dict[str, Any], Path | None] | None:
    """The offline ``attempts export`` inputs, or ``None`` where unknown.

    Returns ``(shown, archive_dir)`` where ``shown`` is the
    ``attempts show`` payload from the copy (summary plus manifest) and
    ``archive_dir`` is the Attempt Archive under the real state directory
    (``None`` where the Target Repository is not recorded). The caller
    streams the tar from those through the same export code path as online.
    A missing ledger file holds no Attempt.
    """
    if not source_ledger.is_file():
        return None
    import sqlite3 as _sqlite3

    from coding_agent.ledger.offline import open_offline_ledger
    from coding_agent.operator.attempts import show_attempt

    with open_offline_ledger(source_ledger) as ledger:
        shown = show_attempt(
            ledger_path=ledger.path, state_dir=state_dir, attempt_id=attempt_id
        )
        if shown is None:
            return None
        repository: str | None = None
        try:
            connection = _sqlite3.connect(str(ledger.path), timeout=2.0)
            try:
                row = connection.execute(
                    "SELECT target_repository FROM operator_instance WHERE id = 1"
                ).fetchone()
            finally:
                connection.close()
            if row is not None and row[0]:
                repository = str(row[0])
        except (OSError, _sqlite3.Error):
            repository = None
        archive_dir: Path | None = None
        if repository is not None:
            try:
                from coding_agent.operator.attempts import (
                    archive_dir_for as _archive_dir_for,
                )

                archive_dir = _archive_dir_for(state_dir, repository, attempt_id)
            except ValueError:
                archive_dir = None
        return (shown, archive_dir)
