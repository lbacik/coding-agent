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
    for _ in range(3):
        try:
            fd = os.open(lock_path, os.O_RDONLY)
            break
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            return False
    else:
        return False
    try:
        for _ in range(3):
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if exc.errno in (errno.EWOULDBLOCK, errno.EACCES):
                    return True
                return False
        else:
            return False
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
    from coding_agent.operator.contract import utc_now_z

    return utc_now_z()


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
    """The offline ``identity`` result for an instance row (or none).

    Contract ``v1.0.4`` shape: the same document ``identity`` returns,
    with ``mode`` ``offline`` and ``process_started_at`` ``None`` (the
    Workflow Configuration is not mounted). The envelope's ``contract``
    stays the ``"major.minor"`` string; only this nested object uses the
    ``{"major", "minor"}`` shape.
    """
    from coding_agent.operator.contract import IDENTITY_CONTRACT
    from coding_agent.operator.store import OPERATOR_BACKEND, agent_version

    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    return {
        "backend": backend,
        "agent_version": version,
        "contract": dict(IDENTITY_CONTRACT),
        "repository": repository,
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
    """The durable Intake state from the ledger copy, open where absent.

    Includes the ``stop_plan`` (``None`` where none is recorded) and the
    ``priority`` (``{request_id, issue}`` while an accepted ``next-issue``
    command waits for the next Claim boundary, else ``None``).
    """
    from coding_agent.operator.commands import pending_next_priority, read_intake

    try:
        connection = sqlite3.connect(str(copy_path), timeout=2.0)
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {copy_path}") from exc
    try:
        try:
            intake = read_intake(connection)
            try:
                intake["priority"] = pending_next_priority(connection)
            except sqlite3.Error:
                intake["priority"] = None
            if "stop_plan" not in intake:
                intake["stop_plan"] = None
            return intake
        except sqlite3.Error:
            return {
                "state": "open",
                "pending_stop": None,
                "stop_plan": None,
                "priority": None,
            }
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


def _offline_intake_projection(intake: dict[str, Any], now: str) -> dict[str, Any]:
    """The ``v1.0.4`` intake projection for an offline status snapshot."""
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
    raw_plan = intake.get("stop_plan")
    plan_ref: dict[str, Any] | None = None
    if isinstance(raw_plan, dict) and raw_plan.get("request_id"):
        try:
            plan_ref = {
                "request_id": str(raw_plan["request_id"]),
                "remaining": int(raw_plan.get("remaining") or 0),
                "counted": int(raw_plan.get("counted") or 0),
            }
        except (TypeError, ValueError):
            plan_ref = None
    raw_priority = intake.get("priority")
    priority_ref: dict[str, Any] | None = None
    if (
        isinstance(raw_priority, dict)
        and raw_priority.get("request_id")
        and isinstance(raw_priority.get("issue"), int)
        and not isinstance(raw_priority.get("issue"), bool)
        and int(raw_priority["issue"]) >= 1
    ):
        priority_ref = {
            "request_id": str(raw_priority["request_id"]),
            "issue": int(raw_priority["issue"]),
        }
    return {
        "state": intake_state,
        "pending_stop": pending_ref,
        "stop_plan": plan_ref,
        "priority": priority_ref,
        "observed_at": now,
    }


def _fresh_offline_status(now: str) -> dict[str, Any]:
    """The offline ``status`` for a volume with no recorded instance rows."""
    from coding_agent.operator import listener as operator_listener

    result: dict[str, Any] = {
        "generated_at": now,
        "identity": _identity_from_stored(None),
        "instance": {
            "state": "stopped",
            "startup_check": None,
            "last_halt": None,
            "observed_at": now,
        },
        "intake": {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
            "observed_at": now,
        },
        "current_attempt": None,
        "last_finished": None,
        "recent_commands": [],
        "recent_events": [],
    }
    return operator_listener._trim_status_snapshot(result)


def load_offline_status(source_ledger: Path) -> dict[str, Any]:
    """The offline ``status`` result: ``stopped`` with the last recorded rows.

    Instance state is always ``stopped``: there is no live process to be
    ``starting`` or ``active``. The snapshot carries the same ``identity``
    object ``identity`` returns (``mode`` ``offline``). Intake,
    ``last_halt``, ``last_finished`` and ``recent_commands`` are read from
    the ledger copy as last recorded; ``current_attempt`` is ``None`` and
    ``recent_events`` is empty (no live process owns events). A missing
    ledger file reads as a fresh volume: Intake open, nothing halted or
    finished, no commands. The snapshot is capped at 256 KiB in the same
    contract order as the live one.
    """
    from coding_agent.operator import listener as operator_listener

    now = _utc_now_iso()
    if not source_ledger.is_file():
        return _fresh_offline_status(now)
    from coding_agent.ledger.offline import open_offline_ledger

    try:
        with open_offline_ledger(source_ledger) as ledger:
            copy_path = ledger.path
            stored = _read_instance_row(copy_path)
            intake = _read_intake(copy_path)
            last_halt = operator_listener._read_last_halt(copy_path)
            last_finished = operator_listener._build_last_finished_v1(
                copy_path, now
            )
            recent_commands = _read_commands(copy_path)
    except FileNotFoundError:
        return _fresh_offline_status(now)
    result = {
        "generated_at": now,
        "identity": _identity_from_stored(stored),
        "instance": {
            "state": "stopped",
            "startup_check": None,
            "last_halt": last_halt,
            "observed_at": now,
        },
        "intake": _offline_intake_projection(intake, now),
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
    source_ledger: Path,
    *,
    limit: int,
    before: str | None,
    state_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One offline ``attempts list`` page, newest first, via the copy.

    A missing ledger file means no Attempt ever opened: the page is empty.
    ``state_dir`` is the real state directory for the sealed lookup (the
    ledger copy lives in a temporary directory); where omitted the copy's
    own parent is used. Raises :class:`InvalidCursor` for a cursor that
    does not decode and :class:`ValueError` for a ``limit`` outside
    ``1..100`` (same as online).
    """
    from coding_agent.operator.attempts import _summaries_v1_for_page
    from coding_agent.operator.attempts import (
        decode_before_cursor,
        encode_before_cursor,
    )
    from coding_agent.operator.attempts import _check_limit
    from coding_agent.operator.contract import utc_now_z

    _check_limit(limit)
    bound: int | None = None
    if before is not None:
        bound = decode_before_cursor(before)
    if not source_ledger.is_file():
        return ([], None)
    from coding_agent.ledger.offline import open_offline_ledger

    import sqlite3 as _sqlite3

    with open_offline_ledger(source_ledger) as ledger:
        try:
            connection = _sqlite3.connect(str(ledger.path), timeout=2.0)
        except _sqlite3.Error as exc:
            raise OSError(f"cannot read the Run Ledger at {source_ledger}") from exc
        try:
            try:
                if bound is None:
                    rows = connection.execute(
                        "SELECT rowid, attempt_id, state, created_at, fingerprint"
                        " FROM attempts ORDER BY rowid DESC LIMIT ?",
                        (limit + 1,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT rowid, attempt_id, state, created_at, fingerprint"
                        " FROM attempts WHERE rowid < ? ORDER BY rowid DESC LIMIT ?",
                        (bound, limit + 1),
                    ).fetchall()
            except _sqlite3.Error:
                return ([], None)
            page_rows = list(rows[:limit])
            observed_at = utc_now_z()
            summaries = _summaries_v1_for_page(
                ledger.path,
                page_rows,
                observed_at,
                state_dir=state_dir,
            )
            next_cursor: str | None = None
            if len(rows) > limit and page_rows:
                next_cursor = encode_before_cursor(int(page_rows[-1][0]))
            return (summaries, next_cursor)
        finally:
            connection.close()


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
