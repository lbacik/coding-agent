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

from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.operator.live import LiveView

if TYPE_CHECKING:
    from coding_agent.inputs.authorisation import IdentityMode
    from coding_agent.inputs.claim import ClaimConfig
    from coding_agent.inputs.port import StepTrackerPort
    from coding_agent.ledger.store import RunLedger
from coding_agent.operator import snapshots as _snapshots
from coding_agent.operator.contract import utc_now_z
from coding_agent.operator.paths import (
    clear_current_socket,
    set_current_socket,
)
from coding_agent.runtime.redaction import Redactable

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
    return utc_now_z()


def build_identity_result(
    ledger_path: Path, live: LiveView, *, input_type: str = "issue"
) -> dict[str, Any]:
    """The ``identity`` result: backend, version, contract, repo, mode, clocks.

    One :class:`RunLedgerReader` per request: the listener never touches
    the Worker thread's handles, satisfying ADR 0026 with one connection
    per request instead of one per table. Assembly lives in
    :mod:`coding_agent.operator.snapshots`.
    """
    with RunLedgerReader(ledger_path) as reader:
        return _snapshots.build_identity(reader, live, input_type=input_type)


_trim_status_snapshot = _snapshots.trim_status_snapshot
"""Cap the serialized snapshot at 256 KiB, in contract order (see snapshots)."""


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
    capped at 300 characters at write time. ``current_attempt`` joins the
    prepared live view (native id, phase, activity) with the ledger's start
    time, pinned ceilings and latest usage flush; ``last_finished`` projects
    the latest finished Attempt with its outcome, pull request and attention
    flag. The snapshot never exceeds 256 KiB: the unbounded tails trim in
    contract order and the snapshot reports ``truncated``.
    ``recent_commands`` carries at most 20 commands, newest last;
    ``intake.pending_stop`` is the pending ``stop``'s ``CommandRef`` or
    ``None``; ``intake.stop_plan`` carries the ``stop --after`` countdown
    (``request_id``, ``remaining`` and ``counted``) or ``None``;
    ``intake.priority`` carries the accepted ``next-issue`` priority
    (``request_id`` and ``issue``) while one waits for the next Claim
    boundary, else ``None``. The snapshot carries the same ``identity``
    object ``identity`` returns.

    One :class:`RunLedgerReader` per request: the listener never touches
    the Worker thread's handles, satisfying ADR 0026 with one connection
    per request instead of one per table. Assembly lives in
    :mod:`coding_agent.operator.snapshots`.
    """
    with RunLedgerReader(ledger_path) as reader:
        return _snapshots.build_status(reader, live)


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
    after: Any = request.get("after", None)
    if kind == "stop" and after is not None:
        from coding_agent.operator.commands import validate_stop_after

        validated_after = validate_stop_after(after)
        if validated_after is None:
            return {"error_code": "invalid_argument"}
        after = validated_after
    elif kind != "stop" and "after" in request:
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
        target_repository = operator_commands.read_target_repository(connection)
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
                after=after,
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
    tracker_factory: Callable[[], StepTrackerPort] | None,
    claim_config: ClaimConfig | None,
    ledger: RunLedger | None,
    identity_mode: IdentityMode | None,
    issue_number: int,
    timeout_seconds: float,
) -> tuple[bool, str | None]:
    """The commit-time eligibility read: ``(read_ok, reason)`` (#252, S10).

    ``(True, None)`` means issue N may be claimed, ``(True, reason)``
    means it is definitely ineligible, and ``(False, None)`` means the
    read itself failed or timed out — which never rejects. The check
    runs on a daemon thread with its own tracker instance from
    ``tracker_factory`` (never the Worker's), so a hung read cannot
    stall the mutation past ``timeout_seconds``; without a factory, a
    claim configuration, the ledger or the identity mode the read cannot
    run and reports failure.
    """
    from coding_agent.inputs.claim import check_eligibility

    if (
        tracker_factory is None
        or claim_config is None
        or ledger is None
        or identity_mode is None
    ):
        return (False, None)
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            tracker = tracker_factory()
            box["reason"] = check_eligibility(
                tracker,
                ledger,
                claim_config,
                issue_number,
                identity_mode=identity_mode,
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
    tracker_factory: Callable[[], StepTrackerPort] | None = None,
    ledger: RunLedger | None = None,
    identity_mode: IdentityMode | None = None,
    eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
    registry: Redactable = None,
    input_type: str = "issue",
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
    if input_type == "pull-request":
        return {"error_code": "unsupported"}
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=_MUTATION_TIMEOUT_SECONDS
        )
    except sqlite3.Error:
        return {"error_code": "internal", "stored": False}
    try:
        target_repository = operator_commands.read_target_repository(connection)
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
                operator_commands.KIND_NEXT_ISSUE, {"issue": issue_number}
            )
            if existing.payload_digest != expected:
                return {"error_code": "payload_mismatch"}
            return {"command": existing.to_dict(), "replayed": True}
        read_ok, reason = _bounded_next_eligibility(
            tracker_factory,
            claim_config,
            ledger,
            identity_mode,
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
    tracker_factory: Callable[[], StepTrackerPort] | None = None,
    ledger: RunLedger | None = None,
    identity_mode: IdentityMode | None = None,
    eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
    input_type: str = "issue",
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
        return build_identity_result(ledger_path, live, input_type=input_type)
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
            ledger=ledger,
            identity_mode=identity_mode,
            eligibility_timeout_seconds=eligibility_timeout_seconds,
            registry=registry,
            input_type=input_type,
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
        tracker_factory: Callable[[], StepTrackerPort] | None = None,
        ledger: RunLedger | None = None,
        identity_mode: IdentityMode | None = None,
        eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
        input_type: str = "issue",
    ) -> None:
        """Bind nothing yet; :meth:`start` creates the directory and socket.

        ``claim_config``, ``tracker_factory``, ``ledger`` and
        ``identity_mode`` serve ``next issue N``: the commit-time
        eligibility read runs through a fresh instance from the factory —
        never the Worker thread's tracker — bounded by
        ``eligibility_timeout_seconds``. They may also arrive later
        through :meth:`configure_next_issue` once startup knows the
        Agent Identity; without them the read cannot run and the
        command commits for the Claim boundary to decide.

        ``input_type`` is the Worker's input type (S9, §19): an ``issue``
        Worker advertises ``next-issue`` while a ``pull-request`` Worker
        answers ``unsupported``. It may also arrive later through
        :meth:`configure_input_type` once startup knows the Workflow.
        """
        self._ledger_path = ledger_path
        self._socket_path = socket_path
        self._live = live
        self._registry = registry
        self._input_type = input_type
        self._next_issue_source: (
            tuple[
                ClaimConfig,
                Callable[[], StepTrackerPort],
                RunLedger,
                IdentityMode,
            ]
            | None
        ) = (
            (claim_config, tracker_factory, ledger, identity_mode)
            if claim_config is not None
            and tracker_factory is not None
            and ledger is not None
            and identity_mode is not None
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
        tracker_factory: Callable[[], StepTrackerPort],
        ledger: RunLedger,
        identity_mode: IdentityMode,
    ) -> None:
        """Provide the priority eligibility source once startup knows it.

        The factory builds a fresh tracker per commit — never the
        Worker's instance. One atomic attribute store publishes all four,
        so connections served mid-startup see either the full tuple or no
        tuple (and commit for the Claim boundary to decide).
        """
        self._next_issue_source = (
            claim_config,
            tracker_factory,
            ledger,
            identity_mode,
        )

    def configure_input_type(self, input_type: str) -> None:
        """Provide the Worker's input type once startup knows the Workflow.

        A ``pull-request`` Worker answers ``next issue N`` with
        ``unsupported`` and omits ``next-issue`` from ``identity``
        capabilities (S9, §19). One atomic attribute store publishes the
        value, so connections served mid-startup see either type.
        """
        self._input_type = input_type

    def start(self) -> None:
        """Create the 0700 directory, bind the 0600 socket, serve threads."""
        directory = self._socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except PermissionError as exc:
            try:
                actual_uid = os.stat(directory).st_uid
            except OSError:
                raise
            expected_uid = os.geteuid()
            if actual_uid != expected_uid:
                raise PermissionError(
                    f"operator socket directory {directory} is owned by"
                    f" uid {actual_uid}, expected uid {expected_uid}:"
                    " the listener enforces 0700, which only the owner can"
                    " set (the compose tmpfs uid/gid must match the image's"
                    " agent user)"
                ) from exc
            raise
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
                ledger=source[2] if source is not None else None,
                identity_mode=source[3] if source is not None else None,
                eligibility_timeout_seconds=self._eligibility_timeout_seconds,
                input_type=self._input_type,
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
