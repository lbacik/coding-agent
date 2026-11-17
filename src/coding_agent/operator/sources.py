"""Read sources for ``agentctl``: one interface, live and offline adapters.

Contract: agent-operator-contract major 1 (``docs/spec.md`` §4). ``agentctl``
serves its read commands through one :class:`ReadSource` interface with two
adapters instead of writing each command twice. :class:`LiveSource` serves
``identity`` and ``status`` from the running Worker's listener over its
Unix socket and ``attempts`` volume-direct from the live ledger file (the
listener's socket protocol carries no attempts operations, so the live
attempts path reads the same :mod:`coding_agent.operator.snapshots`
builders the listener uses). :class:`OfflineSource` opens the Offline Read
copy (:func:`coding_agent.ledger.offline.open_offline_ledger` →
:class:`~coding_agent.ledger.reader.RunLedgerReader`) and calls
:mod:`coding_agent.operator.snapshots`, the same builders the live
listener uses, so live and offline output cannot drift apart.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

ENGINE_TIMEOUT_SECONDS = 5.0
"""Seconds to wait for one listener reply before giving up."""

ENGINE_MAX_REPLY_BYTES = 1024 * 1024
"""Largest accepted listener reply: one Status Snapshot caps at 256 KiB."""


class EngineUnreachable(Exception):
    """The socket is missing, refused, or closed without answering."""


class EngineMalformed(Exception):
    """The engine answered, but the answer is not a usable result."""


def query_engine(request_body: dict[str, Any]) -> dict[str, Any]:
    """Ask the live Worker over the operator Unix socket.

    ``request_body`` carries the op and its fields; the engine answers one
    JSON object. Only a Unix socket is ever dialled: there is no network
    listener. Raises :class:`EngineUnreachable` where the socket is
    missing, refused or closed without answering, and
    :class:`EngineMalformed` where the answer is not a usable result dict.
    """
    from coding_agent.operator.paths import client_socket_path

    path = client_socket_path()
    request = (json.dumps(request_body) + "\n").encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(ENGINE_TIMEOUT_SECONDS)
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise EngineUnreachable(str(exc)) from exc
        try:
            client.sendall(request)
        except OSError as exc:
            raise EngineUnreachable(str(exc)) from exc
        data = b""
        while not data.endswith(b"\n"):
            try:
                chunk = client.recv(4096)
            except (TimeoutError, OSError) as exc:
                raise EngineUnreachable(str(exc)) from exc
            if not chunk:
                break
            data += chunk
            if len(data) > ENGINE_MAX_REPLY_BYTES + 1:
                raise EngineMalformed("engine reply too large")
        if not data.strip():
            raise EngineUnreachable("engine closed without answering")
        try:
            reply: Any = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineMalformed(str(exc)) from exc
        if not isinstance(reply, dict):
            raise EngineMalformed("engine reply is not an object")
        if reply.get("malformed") is True:
            raise EngineMalformed("engine could not serve the operation")
        return reply
    finally:
        try:
            client.close()
        except OSError:
            pass


class ReadSource(Protocol):
    """One interface for every ``agentctl`` read command.

    Each method returns the operator-contract dict the envelope carries as
    ``result`` (for ``attempts list`` the ``{"attempts", "next_cursor"}``
    document). ``show_attempt`` returns ``None`` where no Attempt is
    recorded for the id. Errors propagate: engine errors for the live
    socket, ``OSError``/``sqlite3.Error`` for unreadable ledgers,
    ``InvalidCursor``/``ValueError`` for bad ``attempts list`` arguments.
    """

    def identity(self) -> dict[str, Any]:
        """The ``identity`` result document."""
        ...

    def status(self) -> dict[str, Any]:
        """The ``status`` result document."""
        ...

    def attempts_page(
        self, limit: int, before: str | None = None
    ) -> dict[str, Any]:
        """One ``attempts list`` page: ``{"attempts", "next_cursor"}``."""
        ...

    def show_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """The ``attempts show`` payload, or ``None`` where unknown."""
        ...


QueryFn = Callable[[dict[str, Any]], dict[str, Any]]
"""One listener round-trip: request body in, decoded response out."""


class LiveSource:
    """Reads from the running Worker: socket for identity/status.

    ``identity`` and ``status`` go over the listener socket through
    ``query`` (``agentctl`` passes its ``_query_engine`` so in-process
    tests keep stubbing one seam). The listener's socket protocol carries
    no attempts operations, so ``attempts_page`` and ``show_attempt`` read
    the live ledger file volume-direct through
    :mod:`coding_agent.operator.attempts`, which assembles via the same
    :mod:`coding_agent.operator.snapshots` builders the listener and
    :class:`OfflineSource` use — exactly what ``agentctl`` did before
    this interface existed.
    """

    def __init__(
        self,
        query: QueryFn,
        ledger_path: Path,
        state_dir: Path,
    ) -> None:
        """Bind the socket round-trip and the live volume paths."""
        self._query = query
        self._ledger_path = ledger_path
        self._state_dir = state_dir

    def identity(self) -> dict[str, Any]:
        """The live ``identity`` result: the decoded socket response."""
        return self._query({"op": "identity"})

    def status(self) -> dict[str, Any]:
        """The live ``status`` result: the decoded socket response."""
        return self._query({"op": "status"})

    def attempts_page(
        self, limit: int, before: str | None = None
    ) -> dict[str, Any]:
        """One live ``attempts list`` page, newest first, volume-direct."""
        from coding_agent.operator import attempts as attempt_archives

        page, next_cursor = attempt_archives.list_attempts_page(
            self._ledger_path, limit=limit, before=before
        )
        return {"attempts": page, "next_cursor": next_cursor}

    def show_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """The live ``attempts show`` payload, or ``None`` where unknown."""
        from coding_agent.operator import attempts as attempt_archives

        return attempt_archives.show_attempt(
            ledger_path=self._ledger_path,
            state_dir=self._state_dir,
            attempt_id=attempt_id,
        )


class OfflineSource:
    """Reads from the Offline Read copy without starting the Worker.

    Every method opens the ledger copy
    (:func:`coding_agent.ledger.offline.open_offline_ledger` →
    :class:`~coding_agent.ledger.reader.RunLedgerReader`) and calls
    :mod:`coding_agent.operator.snapshots`, the same builders the live
    listener uses. A missing ledger file reads as a fresh volume: empty
    identity repository, empty attempts page, unknown attempt.
    """

    def __init__(self, ledger_path: Path, state_dir: Path) -> None:
        """Bind the source ledger file and the real state directory."""
        self._ledger_path = ledger_path
        self._state_dir = state_dir

    def identity(self) -> dict[str, Any]:
        """The offline ``identity`` result: repo and version, mode offline."""
        from coding_agent.ledger.offline import open_offline_ledger
        from coding_agent.operator import snapshots as _snapshots
        from coding_agent.operator.store import read_instance_row

        if not self._ledger_path.is_file():
            return _snapshots.offline_identity_from_stored(None)
        with open_offline_ledger(self._ledger_path) as ledger:
            stored = read_instance_row(ledger.connection)
        return _snapshots.offline_identity_from_stored(stored)

    def status(self) -> dict[str, Any]:
        """The offline ``status``: ``stopped`` with the last recorded rows."""
        from coding_agent.ledger.offline import open_offline_ledger
        from coding_agent.operator import snapshots as _snapshots

        if not self._ledger_path.is_file():
            return _snapshots.fresh_offline_status(_snapshots_utc_now())
        with open_offline_ledger(self._ledger_path) as ledger:
            return _snapshots.build_offline_status(
                ledger, state_dir=self._state_dir
            )

    def attempts_page(
        self, limit: int, before: str | None = None
    ) -> dict[str, Any]:
        """One offline ``attempts list`` page, newest first, via the copy.

        Calls :func:`coding_agent.operator.snapshots.attempts_page` on the
        copy — the same builder
        :func:`coding_agent.operator.attempts.list_attempts_page` (used by
        :class:`LiveSource`) delegates to. ``state_dir`` is the real state
        directory: the copy lives in a temporary directory, so the default
        (the ledger's own parent) would misresolve the sealed lookup.
        """
        from coding_agent.ledger.offline import open_offline_ledger
        from coding_agent.operator import snapshots as _snapshots

        if not self._ledger_path.is_file():
            return {"attempts": [], "next_cursor": None}
        with open_offline_ledger(self._ledger_path) as ledger:
            page, next_cursor = _snapshots.attempts_page(
                ledger, limit=limit, before=before, state_dir=self._state_dir
            )
        return {"attempts": page, "next_cursor": next_cursor}

    def show_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """The offline ``attempts show`` payload, or ``None`` where unknown."""
        from coding_agent.ledger.offline import open_offline_ledger
        from coding_agent.operator.attempts import show_attempt

        if not self._ledger_path.is_file():
            return None
        with open_offline_ledger(self._ledger_path) as ledger:
            # The copy's path stands in for the ledger file (read-only);
            # the archive lookup still uses the real ``state_dir`` — the
            # same shape ``offline.load_offline_attempt`` has always used.
            return show_attempt(
                ledger_path=ledger.path,
                state_dir=self._state_dir,
                attempt_id=attempt_id,
            )


def _snapshots_utc_now() -> str:
    """Current UTC time in contract ``Z`` format for fresh offline status."""
    from coding_agent.operator.contract import utc_now_z

    return utc_now_z()
