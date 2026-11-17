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

Reads go through :mod:`coding_agent.operator.snapshots` on a
:class:`~coding_agent.ledger.reader.RunLedgerReader`: this module holds no
SQL and calls no private function of the listener or the attempts module.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
from pathlib import Path
from typing import Any

from coding_agent.ledger.offline import open_offline_ledger
from coding_agent.operator import snapshots as _snapshots
from coding_agent.operator.store import read_instance_repository, read_instance_row
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


def load_offline_identity(source_ledger: Path) -> dict[str, Any]:
    """The offline ``identity`` result for the ledger at ``source_ledger``.

    Reads through the copy approach so an uncheckpointed WAL shows the
    latest committed instance row. A missing ledger file means no instance
    was ever recorded: the result carries the empty repository with the
    package version. Raises :class:`OSError` where the copy cannot be read.
    """
    if not source_ledger.is_file():
        return _snapshots.offline_identity_from_stored(None)

    try:
        with open_offline_ledger(source_ledger) as ledger:
            stored = read_instance_row(ledger.connection)
    except FileNotFoundError:
        return _snapshots.offline_identity_from_stored(None)
    return _snapshots.offline_identity_from_stored(stored)


def load_offline_status(
    source_ledger: Path, state_dir: Path | None = None
) -> dict[str, Any]:
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

    ``state_dir`` is the real state directory for the sealed lookup (the
    ledger copy lives in a temporary directory, so the copy's own parent
    is not the archive root); where omitted the copy's parent is used.
    """
    now = _utc_now_iso()
    if not source_ledger.is_file():
        return _snapshots.fresh_offline_status(now)

    try:
        with open_offline_ledger(source_ledger) as ledger:
            return _snapshots.build_offline_status(ledger, state_dir=state_dir)
    except FileNotFoundError:
        return _snapshots.fresh_offline_status(now)


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
    from coding_agent.operator.commands import get_command

    with open_offline_ledger(source_ledger) as ledger:
        try:
            record = get_command(ledger.connection, request_id)
        except sqlite3.Error as exc:
            raise OSError(f"cannot read the Run Ledger at {source_ledger}") from exc
        if record is None:
            return None
        return record.to_dict()


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
    if not source_ledger.is_file():
        return ([], None)

    with open_offline_ledger(source_ledger) as ledger:
        return _snapshots.attempts_page(
            ledger, limit=limit, before=before, state_dir=state_dir
        )


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
    from coding_agent.operator.attempts import archive_dir_for, show_attempt

    with open_offline_ledger(source_ledger) as ledger:
        shown = show_attempt(
            ledger_path=ledger.path, state_dir=state_dir, attempt_id=attempt_id
        )
        if shown is None:
            return None
        repository = read_instance_repository(ledger.connection)
        archive_dir: Path | None = None
        if repository is not None:
            try:
                archive_dir = archive_dir_for(state_dir, repository, attempt_id)
            except ValueError:
                archive_dir = None
        return (shown, archive_dir)
