"""The operator control listener: reads and durable commands over a Unix socket.

Contract: adoption step 4 of the operator contract (#245, #248, #249,
#252). An
in-process thread started right after the operator store opens. It listens
on a Unix socket in a 0700 directory with the socket at 0600, writable
only by ``agent``, on a tmpfs mount, and serves each connection on its own
thread. It reads only the prepared live view and its own reads of the
store and ledger — never the Worker thread's connection, git, the network
or the provider. A SIGTERM shutdown keeps serving reads until the process
exits: the listener stops only when the Worker exits.

Wire format (private): newline-delimited JSON. The client sends
``{"op": "identity"}``, ``{"op": "status"}``,
``{"op": "stop", "request_id": ..., "expect_repo": ...}``,
``{"op": "resume", "request_id": ..., "expect_repo": ...}`` or
``{"op": "command", "request_id": ...}`` followed by ``\\n``; the
engine answers one JSON object followed by ``\\n``. Anything else —
unparseable bytes, a non-dict, a dict missing its required fields — is a
malformed engine answer and the caller reports ``internal``.

Serialisation (#249): every mutation opens its own connection with a 10 s
busy timeout and commits inside one ``BEGIN IMMEDIATE`` write transaction,
so it serialises with the Claim boundary and with finalization through
SQLite's write lock. A mutation that cannot commit within 10 s answers
``internal`` with ``stored: false``.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coding_agent.operator.live import LiveView

if TYPE_CHECKING:
    from coding_agent.tracker.port import TrackerPort
    from coding_agent.worker.claim import ClaimConfig
from coding_agent.operator.paths import (
    clear_current_socket,
    set_current_socket,
)
from coding_agent.runtime.redaction import Redactable
from coding_agent.operator.store import OPERATOR_BACKEND, agent_version

_LISTEN_BACKLOG = 32
_ACCEPT_TIMEOUT_SECONDS = 0.5
_CONNECTION_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BYTES = 64 * 1024

_MUTATION_TIMEOUT_SECONDS = 10.0
"""Busy timeout for one mutation connection (#249, §8.4).

The listener and the Worker thread each use their own connection;
serialisation is SQLite's write lock, and this timeout keeps a mutation
inside the 10 s budget: where the write lock cannot be won in time the
mutation answers ``internal`` with ``stored: false``.
"""


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
    from coding_agent.agentctl import ADVERTISED_CAPABILITIES

    result: dict[str, Any] = {
        "backend": backend,
        "agent_version": version,
        "version": version,
        "contract": "1.0",
        "repository": repository,
        "target_repository": repository,
        "mode": "worker",
        "process_started_at": process_started_at,
        "capabilities": sorted(ADVERTISED_CAPABILITIES),
    }
    return result


def _read_intake_detail(ledger_path: Path) -> dict[str, Any]:
    """The durable Intake state plus the pending stop, through a fresh connection."""
    from coding_agent.operator.commands import open_ledger_connection, read_intake

    try:
        connection = open_ledger_connection(ledger_path, timeout_seconds=2.0)
    except sqlite3.Error:
        return {"state": "open", "pending_stop": None}
    try:
        try:
            return read_intake(connection)
        except sqlite3.Error:
            return {"state": "open", "pending_stop": None}
    finally:
        connection.close()


def _read_recent_commands(ledger_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` commands, newest last, through a fresh connection."""
    from coding_agent.operator.commands import list_recent_commands, open_ledger_connection

    try:
        connection = open_ledger_connection(ledger_path, timeout_seconds=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            return [record.to_dict() for record in list_recent_commands(connection, limit)]
        except sqlite3.Error:
            return []
    finally:
        connection.close()


def _read_last_halt(ledger_path: Path) -> dict[str, Any] | None:
    """The durable Halt, if any (survives restarts as ``instance.last_halt``)."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT reason, detail, occurred_at FROM operator_halt"
                " WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        occurred = str(row[2])
        return {
            "reason": str(row[0]),
            "detail": str(row[1]),
            "occurred_at": occurred,
            "time": occurred,
        }
    finally:
        connection.close()


def _read_recent_events(
    ledger_path: Path, process_started_at: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The newest ``limit`` Contract Events of this process, oldest first."""
    try:
        from coding_agent.operator import events as contract_events

        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            return contract_events.list_recent_contract_events(
                connection,
                process_started_at=process_started_at,
                limit=limit,
            )
        except sqlite3.Error:
            return []
    finally:
        connection.close()


def build_status_result(ledger_path: Path, live: LiveView) -> dict[str, Any]:
    """The ``status``: instance state, Intake state, Halt, checks, events.

    Instance states: ``starting`` (before readiness, while startup checks or
    recovery run), ``recovering`` (unfinished Attempts from an earlier
    process are being settled), then the §7.2 states ``idle`` (Intake open,
    nothing active), ``active`` (Intake open, an Attempt active),
    ``draining`` (Intake closed, an Attempt still active) and ``paused``
    (Intake closed, nothing active). Readiness — a state other than
    ``starting`` — is reached only after recovery. While a startup check is
    held, ``startup_check`` names it. ``instance.last_halt`` carries the
    durable Halt, if any. ``recent_events`` holds at most the newest 50
    Contract Events of this process, oldest first, every one redacted and
    capped at 300 characters at write time.
    """
    now = _utc_now_iso()
    active = _has_unfinished_attempt(ledger_path)
    intake = _read_intake_detail(ledger_path)
    intake_state = str(intake.get("state") or "open")
    if intake_state not in ("open", "closed"):
        intake_state = "open"
    pending = intake.get("pending_stop")
    pending_ref: dict[str, Any] | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        pending_ref = {
            "request_id": str(pending["request_id"]),
            "sequence": int(pending.get("sequence") or 0),
        }
    startup_check = live.get_startup_check()
    if not live.is_ready():
        if live.is_recovering():
            instance_state = "recovering"
        else:
            instance_state = "starting"
    elif intake_state == "open":
        instance_state = "active" if active else "idle"
    else:
        instance_state = "draining" if active else "paused"
    last_halt = _read_last_halt(ledger_path)
    recent_events = _read_recent_events(
        ledger_path, live.process_started_at_iso()
    )
    result: dict[str, Any] = {
        "generated_at": now,
        "process_started_at": live.process_started_at_iso(),
        "startup_check": startup_check,
        "instance": {
            "state": instance_state,
            "observed_at": now,
            "last_halt": last_halt,
            "startup_check": startup_check,
        },
        "intake": {
            "state": intake_state,
            "pending_stop": pending_ref,
            "observed_at": now,
        },
        "recent_commands": _read_recent_commands(ledger_path),
        "recent_events": recent_events,
    }
    return result


def _serve_mutation(
    *,
    ledger_path: Path,
    kind: str,
    request: dict[str, Any],
    registry: Redactable = None,
) -> dict[str, Any] | None:
    """Commit a ``stop`` or ``resume``; ``None`` where the line is malformed.

    Opens a private connection with the 10 s busy timeout and commits in
    one ``BEGIN IMMEDIATE`` transaction. Success answers
    ``{"command": ..., "replayed": bool}``; guard failures answer
    ``{"error_code": ...}`` with the fields the client maps to its
    envelope. A lock wait past the budget answers ``internal`` with
    ``stored: false`` (§8.4).
    """
    from coding_agent.operator import commands as operator_commands

    request_id = request.get("request_id")
    expect_repo = request.get("expect_repo")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(expect_repo, str) or not expect_repo:
        return None
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=_MUTATION_TIMEOUT_SECONDS
        )
    except sqlite3.Error:
        return {"error_code": "internal", "stored": False}
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            return {"error_code": "internal", "stored": False}
        try:
            instance = connection.execute(
                "SELECT target_repository FROM operator_instance WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            instance = None
        target_repository = (
            str(instance[0]) if instance is not None and instance[0] else ""
        )
        if not target_repository:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        if kind == "stop":
            record, replayed, error = operator_commands.commit_stop(
                connection,
                request_id=request_id,
                expect_repo=expect_repo,
                target_repository=target_repository,
                registry=registry,
            )
        else:
            record, replayed, error = operator_commands.commit_resume(
                connection,
                request_id=request_id,
                expect_repo=expect_repo,
                target_repository=target_repository,
                registry=registry,
            )
        if error is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            code = str(error.get("code") or "internal")
            reply: dict[str, Any] = {"error_code": code}
            if "repository" in error:
                reply["repository"] = error["repository"]
            if code == "internal":
                reply["stored"] = False
            return reply
        assert record is not None
        try:
            connection.commit()
        except sqlite3.Error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        return {"command": record.to_dict(), "replayed": replayed}
    except sqlite3.OperationalError:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        return {"error_code": "internal", "stored": False}
    except sqlite3.Error:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        return {"error_code": "internal", "stored": False}
    finally:
        connection.close()


_NEXT_ELIGIBILITY_TIMEOUT_SECONDS = 5.0
"""Bound for the ``next`` commit-time eligibility read (#252, §8.4).

The listener checks eligibility through its own tracker instance — never
the Worker thread's — with no write lock held, abandoning the read where
it exceeds this timeout. A failed or timed-out read never rejects: the
command commits and eligibility waits for the Claim boundary.
"""


def _validate_next_issue(value: Any) -> int | None:
    """The validated priority issue number, or ``None`` where invalid."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _bounded_next_eligibility(
    tracker_factory: Callable[[], TrackerPort] | None,
    claim_config: ClaimConfig | None,
    issue_number: int,
    timeout_seconds: float,
) -> tuple[bool, str | None]:
    """The commit-time eligibility read: ``(read_ok, reason)`` (#252).

    ``(True, None)`` means issue N may be claimed, ``(True, reason)``
    means it is definitely ineligible, and ``(False, None)`` means the
    read itself failed or timed out — which never rejects. The check
    runs on a daemon thread with its own tracker instance from
    ``tracker_factory`` (never the Worker's), so a hung read cannot
    stall the mutation past ``timeout_seconds``; without a factory or a
    claim configuration the read cannot run and reports failure.
    """
    from coding_agent.worker.claim import next_eligibility_reason

    if tracker_factory is None or claim_config is None:
        return (False, None)
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            tracker = tracker_factory()
            box["reason"] = next_eligibility_reason(
                tracker, claim_config, issue_number
            )
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_run, name="next-eligibility", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive() or "reason" not in box:
        return (False, None)
    reason = box["reason"]
    return (True, reason if isinstance(reason, str) or reason is None else None)


def _serve_next_issue(
    *,
    ledger_path: Path,
    request: dict[str, Any],
    claim_config: ClaimConfig | None = None,
    tracker_factory: Callable[[], TrackerPort] | None = None,
    eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
    registry: Redactable = None,
) -> dict[str, Any] | None:
    """Commit a ``next issue N`` priority; ``None`` where malformed.

    The repo guard and the idempotency pre-check run first with no write
    lock held; then one bounded eligibility read through the listener's
    own tracker instance decides ``rejected`` (definite ineligibility,
    storing nothing) versus commit (eligible, or a failed read whose
    eligibility waits for the Claim boundary). The commit itself runs in
    one ``BEGIN IMMEDIATE`` transaction that re-checks the idempotency,
    supersedes older accepted priorities and inserts the new row
    (§8.4). A lock wait past the budget answers ``internal`` with
    ``stored: false``.
    """
    from coding_agent.operator import commands as operator_commands

    request_id = request.get("request_id")
    expect_repo = request.get("expect_repo")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(expect_repo, str) or not expect_repo:
        return None
    issue_number = _validate_next_issue(request.get("issue"))
    if issue_number is None:
        return {"error_code": "invalid_argument"}
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=_MUTATION_TIMEOUT_SECONDS
        )
    except sqlite3.Error:
        return {"error_code": "internal", "stored": False}
    try:
        try:
            instance = connection.execute(
                "SELECT target_repository FROM operator_instance WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            instance = None
        target_repository = (
            str(instance[0]) if instance is not None and instance[0] else ""
        )
        if not target_repository:
            return {"error_code": "internal", "stored": False}
        if expect_repo.lower() != target_repository.lower():
            return {
                "error_code": "repository_mismatch",
                "repository": target_repository,
            }
        try:
            existing = operator_commands.get_command(connection, request_id)
        except sqlite3.Error:
            return {"error_code": "internal", "stored": False}
        if existing is not None:
            expected = operator_commands.payload_digest(
                operator_commands.KIND_NEXT, {"issue": issue_number}
            )
            if existing.payload_digest != expected:
                return {"error_code": "payload_mismatch"}
            return {"command": existing.to_dict(), "replayed": True}
        read_ok, reason = _bounded_next_eligibility(
            tracker_factory,
            claim_config,
            issue_number,
            eligibility_timeout_seconds,
        )
        if read_ok and reason is not None:
            return {
                "error_code": "rejected",
                "details": {"issue": issue_number, "reason": reason},
            }
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            return {"error_code": "internal", "stored": False}
        try:
            record, replayed, error = operator_commands.commit_next(
                connection,
                request_id=request_id,
                expect_repo=expect_repo,
                target_repository=target_repository,
                issue_number=issue_number,
                registry=registry,
            )
        except sqlite3.OperationalError:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        except sqlite3.Error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        if error is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            code = str(error.get("code") or "internal")
            reply: dict[str, Any] = {"error_code": code}
            if "repository" in error:
                reply["repository"] = error["repository"]
            if code == "internal":
                reply["stored"] = False
            return reply
        assert record is not None
        try:
            connection.commit()
        except sqlite3.Error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        return {"command": record.to_dict(), "replayed": replayed}
    finally:
        connection.close()


def _serve_command_lookup(
    *, ledger_path: Path, request: dict[str, Any]
) -> dict[str, Any] | None:
    """Look up one command by request ID; ``None`` where malformed."""
    from coding_agent.operator import commands as operator_commands

    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=2.0
        )
    except sqlite3.Error:
        # A read path: nothing is stored, so no ``stored`` flag applies.
        return {"error_code": "internal"}
    try:
        try:
            record = operator_commands.get_command(connection, request_id)
        except sqlite3.Error:
            return {"error_code": "internal"}
        if record is None:
            return {"error_code": "not_found"}
        return {"command": record.to_dict()}
    finally:
        connection.close()


def _handle_one(
    raw: bytes,
    *,
    ledger_path: Path,
    live: LiveView,
    registry: Redactable = None,
    claim_config: ClaimConfig | None = None,
    tracker_factory: Callable[[], TrackerPort] | None = None,
    eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
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
    if op == "stop":
        return _serve_mutation(
            ledger_path=ledger_path, kind="stop", request=request, registry=registry
        )
    if op == "resume":
        return _serve_mutation(
            ledger_path=ledger_path,
            kind="resume",
            request=request,
            registry=registry,
        )
    if op == "next":
        return _serve_next_issue(
            ledger_path=ledger_path,
            request=request,
            claim_config=claim_config,
            tracker_factory=tracker_factory,
            eligibility_timeout_seconds=eligibility_timeout_seconds,
            registry=registry,
        )
    if op == "command":
        return _serve_command_lookup(ledger_path=ledger_path, request=request)
    return None


class OperatorListener:
    """The Unix-socket listener thread and its per-connection threads."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        socket_path: Path,
        live: LiveView,
        registry: Redactable = None,
        claim_config: ClaimConfig | None = None,
        tracker_factory: Callable[[], TrackerPort] | None = None,
        eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        """Bind nothing yet; :meth:`start` creates the directory and socket.

        ``claim_config`` and ``tracker_factory`` serve ``next issue N``:
        the commit-time eligibility read runs through a fresh instance
        from the factory — never the Worker thread's tracker — bounded
        by ``eligibility_timeout_seconds``. They may also arrive later
        through :meth:`configure_next_issue` once startup knows the
        Agent Identity; without them the read cannot run and the
        command commits for the Claim boundary to decide.
        """
        self._ledger_path = ledger_path
        self._socket_path = socket_path
        self._live = live
        self._registry = registry
        self._next_issue_source: (
            tuple[ClaimConfig, Callable[[], TrackerPort]] | None
        ) = (
            (claim_config, tracker_factory)
            if claim_config is not None and tracker_factory is not None
            else None
        )
        self._eligibility_timeout_seconds = eligibility_timeout_seconds
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def socket_path(self) -> Path:
        """The socket this listener binds (or would bind)."""
        return self._socket_path

    def configure_next_issue(
        self,
        claim_config: ClaimConfig,
        tracker_factory: Callable[[], TrackerPort],
    ) -> None:
        """Provide the priority eligibility source once startup knows it.

        The factory builds a fresh tracker per commit — never the
        Worker's instance. One atomic attribute store publishes both, so
        connections served mid-startup see either the full pair or no
        pair (and commit for the Claim boundary to decide).
        """
        self._next_issue_source = (claim_config, tracker_factory)

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
            source = self._next_issue_source
            answer = _handle_one(
                line,
                ledger_path=self._ledger_path,
                live=self._live,
                registry=self._registry,
                claim_config=source[0] if source is not None else None,
                tracker_factory=source[1] if source is not None else None,
                eligibility_timeout_seconds=self._eligibility_timeout_seconds,
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
