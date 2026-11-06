"""The operator control listener: ``identity`` and ``status`` over a Unix socket.

Contract: adoption step 4 of the operator contract (#245, #248). An
in-process thread started right after the operator store opens. It listens
on a Unix socket in a 0700 directory with the socket at 0600, writable
only by ``agent``, on a tmpfs mount, and serves each connection on its own
thread. It reads only the prepared live view and its own reads of the
store and ledger — never the Worker thread's connection, git, the network
or the provider. A SIGTERM shutdown keeps serving reads until the process
exits: the listener stops only when the Worker exits.

Wire format (private): newline-delimited JSON. The client sends
``{"op": "identity"}`` or ``{"op": "status"}`` followed by ``\\n``; the
engine answers one JSON object followed by ``\\n``. Anything else —
unparseable bytes, a non-dict, a dict missing its required fields — is a
malformed engine answer and the caller reports ``internal``.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import (
    clear_current_socket,
    set_current_socket,
)
from coding_agent.operator.store import OPERATOR_BACKEND, agent_version

_LISTEN_BACKLOG = 32
_ACCEPT_TIMEOUT_SECONDS = 0.5
_CONNECTION_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BYTES = 64 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_instance_row(ledger_path: Path) -> dict[str, str] | None:
    """This start's instance metadata through a fresh connection.

    A new connection per request: the listener never touches the Worker
    thread's handles, and a blocked Worker-side write cannot stall the
    read past SQLite's own locking (WAL readers do not block writers).
    ``None`` where the file or row does not exist yet (early startup).
    """
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
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


def _read_intake_state(ledger_path: Path) -> str:
    """The durable Intake state, ``open`` unless a row says otherwise."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return "open"
    try:
        try:
            row = connection.execute(
                "SELECT state FROM operator_intake WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return "open"
        if row is None:
            return "open"
        state = str(row[0])
        return state if state else "open"
    finally:
        connection.close()


def _has_unfinished_attempt(ledger_path: Path) -> bool:
    """Whether the ledger holds an Attempt neither finished nor released."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        try:
            row = connection.execute(
                "SELECT 1 FROM attempts"
                " WHERE state NOT IN ('finished', 'released') LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None
    finally:
        connection.close()


def build_identity_result(ledger_path: Path, live: LiveView) -> dict[str, Any]:
    """The ``identity`` result: backend, version, contract, repo, mode, clocks."""
    stored = _read_instance_row(ledger_path)
    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    process_started_at = live.process_started_at_iso()
    result: dict[str, Any] = {
        "backend": backend,
        "agent_version": version,
        "version": version,
        "contract": "1.0",
        "repository": repository,
        "target_repository": repository,
        "mode": "worker",
        "process_started_at": process_started_at,
        "capabilities": [],
    }
    return result


def build_status_result(ledger_path: Path, live: LiveView) -> dict[str, Any]:
    """The minimal ``status``: idle/active instance, open Intake, clocks."""
    now = _utc_now_iso()
    active = _has_unfinished_attempt(ledger_path)
    intake_state = _read_intake_state(ledger_path)
    result: dict[str, Any] = {
        "generated_at": now,
        "process_started_at": live.process_started_at_iso(),
        "instance": {
            "state": "active" if active else "idle",
            "observed_at": now,
        },
        "intake": {
            "state": intake_state,
            "observed_at": now,
        },
    }
    return result


def _handle_one(
    raw: bytes, *, ledger_path: Path, live: LiveView
) -> dict[str, Any] | None:
    """Answer one request line; ``None`` where the line itself is malformed."""
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict):
        return None
    op = request.get("op")
    if op == "identity":
        return build_identity_result(ledger_path, live)
    if op == "status":
        return build_status_result(ledger_path, live)
    return None


class OperatorListener:
    """The Unix-socket listener thread and its per-connection threads."""

    def __init__(
        self, *, ledger_path: Path, socket_path: Path, live: LiveView
    ) -> None:
        """Bind nothing yet; :meth:`start` creates the directory and socket."""
        self._ledger_path = ledger_path
        self._socket_path = socket_path
        self._live = live
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def socket_path(self) -> Path:
        """The socket this listener binds (or would bind)."""
        return self._socket_path

    def start(self) -> None:
        """Create the 0700 directory, bind the 0600 socket, serve threads."""
        directory = self._socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self._socket_path))
            server.listen(_LISTEN_BACKLOG)
            server.settimeout(_ACCEPT_TIMEOUT_SECONDS)
            os.chmod(self._socket_path, 0o600)
        except OSError:
            server.close()
            raise
        with self._lock:
            self._socket = server
        set_current_socket(self._socket_path)
        thread = threading.Thread(
            target=self._serve, name="operator-listener", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop accepting; existing connections drain. Idempotent."""
        with self._lock:
            server = self._socket
            self._socket = None
            thread = self._thread
            self._thread = None
        self._stop.set()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        clear_current_socket(self._socket_path)
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _serve(self) -> None:
        """Accept connections until :meth:`stop`; each gets its own thread."""
        while not self._stop.is_set():
            with self._lock:
                server = self._socket
            if server is None:
                return
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                name="operator-conn",
                daemon=True,
            )
            worker.start()

    def _serve_connection(self, conn: socket.socket) -> None:
        """Serve one connection: one request line, one answer line."""
        try:
            conn.settimeout(_CONNECTION_TIMEOUT_SECONDS)
            data = b""
            while not data.endswith(b"\n"):
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    return
                except OSError:
                    return
                if not chunk:
                    break
                data += chunk
                if len(data) > _MAX_REQUEST_BYTES + 1:
                    return
            line = data.split(b"\n", 1)[0].strip()
            if not line:
                return
            answer = _handle_one(
                line, ledger_path=self._ledger_path, live=self._live
            )
            if answer is None:
                # Malformed op: still answer with JSON so the client can
                # classify it as an engine failure (``internal``) rather
                # than a transport one. An empty/invalid shape on read is
                # treated the same way by the client's validation.
                payload = {"malformed": True}
            else:
                payload = answer
            try:
                conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
