"""Where the operator control socket lives.

Contract: adoption step 4 of the operator contract (#245, #248) — the
control socket lives in a 0700 directory on a writable tmpfs, with the
socket itself at 0600, reachable only by ``docker exec --user agent``. No
network listener and no published port exist.

In production the directory is ``<state-dir>/operator`` with
``<state-dir>`` defaulting to ``/var/lib/coding-agent``; the Compose
``worker`` service mounts a tmpfs over that path so the socket never
touches the named volume. In tests ``state_dir`` is a temporary directory
and the Worker and ``agentctl`` meet through the in-process current socket
(see below) or through ``CODING_AGENT_OPERATOR_SOCKET``.
"""

from __future__ import annotations

import os
from pathlib import Path

OPERATOR_DIR_NAME = "operator"
"""The socket directory's name, both under a state dir and on tmpfs."""

OPERATOR_SOCKET_NAME = "agent.sock"
"""The Unix socket's file name inside the operator directory."""

OPERATOR_SOCKET_ENV_VAR = "CODING_AGENT_OPERATOR_SOCKET"
"""Explicit socket path for both the Worker and ``agentctl``.

Set in tests to point both ends at the same temporary socket; unset in
production, where both ends derive the same default from the state dir.
"""

DEFAULT_STATE_DIR = Path("/var/lib/coding-agent")
"""The production state directory ``agent run --state-dir`` defaults to."""

STATE_DIR_ENV_VAR = "CODING_AGENT_STATE_DIR"
"""Explicit state directory for volume-direct ``agentctl`` reads.

``attempts list`` and ``attempts show`` read the Run Ledger and the Attempt
Archives directly in the ``agentctl`` process over the volume (adoption
step 4, #255): this override points them at a test or alternate volume;
unset, they read the production default. A separate ``docker exec`` process
has no in-process listener entry and uses the same default.
"""

RUN_LEDGER_FILE_NAME = "run-ledger.sqlite3"
"""The Run Ledger's file name directly under the state directory (§17)."""

_current_socket: Path | None = None
"""The live listener's socket, set at start for same-process discovery.

``supervisor.run_worker`` records the socket it bound; ``agentctl`` in the
same process (the in-repo seam: Worker in a background thread, ``agentctl``
in-process) prefers it over the production default, so a Worker on a
temporary state directory is reachable without extra configuration. A
separate ``docker exec`` process has no such entry and uses the default or
the environment override. ``None`` where no listener runs here.
"""


def socket_path_for_state(state_dir: Path) -> Path:
    """The socket path a Worker on ``state_dir`` binds."""
    return state_dir / OPERATOR_DIR_NAME / OPERATOR_SOCKET_NAME


def default_socket_path() -> Path:
    """The production socket path ``agentctl`` dials by default."""
    return DEFAULT_STATE_DIR / OPERATOR_DIR_NAME / OPERATOR_SOCKET_NAME


def state_dir_path() -> Path:
    """The state directory volume-direct ``agentctl`` reads use.

    The explicit override first, then the production default: the archive
    reads never dial the control socket, so they work whether the Worker
    runs or not.
    """
    override = os.environ.get(STATE_DIR_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_STATE_DIR


def ledger_path_for_state(state_dir: Path) -> Path:
    """The Run Ledger file on ``state_dir``."""
    return state_dir / RUN_LEDGER_FILE_NAME


def worker_socket_path(state_dir: Path, explicit: Path | None = None) -> Path:
    """Where the Worker binds: explicit, env override, or under ``state_dir``."""
    if explicit is not None:
        return explicit
    override = os.environ.get(OPERATOR_SOCKET_ENV_VAR)
    if override:
        return Path(override)
    return socket_path_for_state(state_dir)


def client_socket_path() -> Path:
    """Where ``agentctl`` dials: env override, in-process listener, default."""
    override = os.environ.get(OPERATOR_SOCKET_ENV_VAR)
    if override:
        return Path(override)
    current = get_current_socket()
    if current is not None:
        return current
    return default_socket_path()


def set_current_socket(path: Path | None) -> None:
    """Record (or clear with ``None``) the live listener's socket."""
    global _current_socket
    _current_socket = path


def get_current_socket() -> Path | None:
    """The live listener's socket in this process, if one runs here."""
    return _current_socket


def clear_current_socket(path: Path | None = None) -> None:
    """Forget the live listener, optionally only where it is ``path``."""
    global _current_socket
    if path is None or _current_socket == path:
        _current_socket = None
