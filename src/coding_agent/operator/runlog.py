"""Worker container log: one line per committed Contract Event and model turn.

Contract: stdout/stderr are not part of the operator contract — this module
changes no contract shape, no stored row and no ``events.jsonl`` content.
Every Attempt-scoped event prints with its Attempt id prefix, e.g.
``run: [implement#106/2] claim opened``; every other event prints as
``run: <text>``. ``halted`` and error-level events go to stderr, everything
else to stdout. All lines print with ``flush=True`` (the image already sets
``PYTHONUNBUFFERED=1``) under one process-wide lock, so the operator
listener thread and the Worker thread never interleave inside a line.

Only committed events print: the emit path queues, the commit point flushes,
and a rolled-back transaction discards. The stored text — already redacted
and capped at 300 characters by the single producer — is what prints, never
a second unredacted rendering.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Mapping
from typing import Any


_PRINT_LOCK = threading.Lock()
"""One lock for every container-log line in this process (Worker + listener)."""


_QUEUE_LOCK = threading.Lock()
_PENDING: dict[int, list[dict[str, Any]]] = {}
"""Records queued in an uncommitted transaction, keyed by ``id(connection)``."""


_ENABLED_LOCK = threading.Lock()
_ENABLED = False
"""Whether container-log lines print in this process.

Only the Worker enables this (see :func:`set_container_log_enabled`):
``agentctl`` and the offline readers own stdout for contract JSON, so the
ledger and store paths they share with the Worker must stay silent there.
The default is off so library and CLI use never pollutes contract output;
the Worker Supervisor turns it on once at startup, covering its own thread
and the operator listener thread.
"""


def set_container_log_enabled(enabled: bool) -> None:
    """Turn Worker container-log printing on or off for this process."""
    global _ENABLED
    with _ENABLED_LOCK:
        _ENABLED = bool(enabled)


def container_log_enabled() -> bool:
    """Whether Worker container-log lines print in this process."""
    with _ENABLED_LOCK:
        return _ENABLED


def log_turns_enabled() -> bool:
    """Whether per-turn model lines print (``CODING_AGENT_LOG_TURNS``).

    Unset or any value other than ``"0"`` means on (the default); ``"0"``
    turns only the per-turn lines off. Contract Event lines always print.
    """
    return os.environ.get("CODING_AGENT_LOG_TURNS") != "0"


def format_contract_line(record: Mapping[str, Any]) -> str:
    """One ``run:`` line for a committed Contract Event record."""
    text = str(record.get("text", ""))
    attempt_id = record.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        return f"run: [{attempt_id}] {text}"
    return f"run: {text}"


def _is_stderr(record: Mapping[str, Any]) -> bool:
    """Whether the record prints to stderr (``halted`` or error level)."""
    if str(record.get("event", "")) == "halted":
        return True
    return str(record.get("level", "")) == "error"


def print_contract_event(record: Mapping[str, Any]) -> None:
    """Print one committed Contract Event record (best-effort, never raises).

    Silent unless :func:`set_container_log_enabled` turned printing on:
    the ledger paths also serve ``agentctl`` and the offline readers, which
    own stdout for contract JSON.
    """
    try:
        if not container_log_enabled():
            return
        line = format_contract_line(record)
        stream = sys.stderr if _is_stderr(record) else sys.stdout
        with _PRINT_LOCK:
            print(line, file=stream, flush=True)
    except Exception:
        pass


def queue_pending_event(
    connection: object, record: Mapping[str, Any]
) -> None:
    """Queue a record emitted inside an uncommitted transaction.

    The commit point flushes with :func:`flush_pending_events`; a rollback
    discards with :func:`discard_pending_events`. Never raises.
    """
    try:
        with _QUEUE_LOCK:
            _PENDING.setdefault(id(connection), []).append(dict(record))
    except Exception:
        pass


def flush_pending_events(connection: object) -> None:
    """Print every event queued on ``connection`` (after its commit).

    Silent unless :func:`set_container_log_enabled` turned printing on: a
    non-Worker process drops the queued records instead of printing them.
    """
    try:
        with _QUEUE_LOCK:
            records = _PENDING.pop(id(connection), [])
    except Exception:
        return
    if not container_log_enabled():
        return
    for record in records:
        print_contract_event(record)


def discard_pending_events(connection: object) -> None:
    """Drop queued events without printing (after a rollback)."""
    try:
        with _QUEUE_LOCK:
            _PENDING.pop(id(connection), None)
    except Exception:
        pass


def print_line(text: str, *, stderr: bool = False) -> None:
    """Print one ``run:`` line to the chosen stream (never raises).

    Silent unless :func:`set_container_log_enabled` turned printing on.
    """
    try:
        if not container_log_enabled():
            return
        stream = sys.stderr if stderr else sys.stdout
        with _PRINT_LOCK:
            print(text, file=stream, flush=True)
    except Exception:
        pass


def format_tokens(count: int) -> str:
    """A compact token count: ``41000`` → ``41k``, ``1200`` → ``1.2k``."""
    if count >= 1000 and count % 1000 == 0:
        return f"{count // 1000}k"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)
