"""The prepared live view the control listener reads (adoption step 4, #248, #250).

Contract: the listener reads only this view plus its own store/ledger
reads — never the Worker thread's connection, git, the network or the
provider. For #248 the view held just the process start time; #250 adds the
startup check, the recovering/ready flags and the process start time the
event ring filters on. Thread-safe: the Worker thread writes through the
one lock while listener threads only read. Contract Events themselves live
in the ledger's ``operator_events`` table (see :mod:`operator.events`); this
view holds only the flags that decide ``instance.state``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LiveView:
    """Process-local state the Worker prepares and the listener only reads."""

    process_started_at: datetime
    """When this Worker process started, in UTC."""

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _startup_check: str | None = field(default=None, repr=False)
    _recovering: bool = field(default=False, repr=False)
    _ready: bool = field(default=False, repr=False)

    def process_started_at_iso(self) -> str:
        """The start time in ISO-8601, as ``identity`` and ``status`` report."""
        return self.process_started_at.isoformat()

    def set_startup_check(self, name: str | None) -> None:
        """Name the startup check currently held, or clear it (``None``)."""
        with self._lock:
            self._startup_check = name

    def get_startup_check(self) -> str | None:
        """The startup check currently held, if any."""
        with self._lock:
            return self._startup_check

    def set_recovering(self, recovering: bool) -> None:
        """Whether startup recovery is settling unfinished Attempts."""
        with self._lock:
            self._recovering = recovering

    def is_recovering(self) -> bool:
        """Whether recovery is in progress (status answers ``recovering``)."""
        with self._lock:
            return self._recovering

    def set_ready(self) -> None:
        """Mark readiness: recovery is done, state leaves ``starting``.

        Readiness is reached only after recovery: the Worker calls this once
        the Run Ledger holds no unfinished Attempt (or the first recovery
        pass settled them). Clears any startup check and the recovering flag.
        """
        with self._lock:
            self._ready = True
            self._recovering = False
            self._startup_check = None

    def is_ready(self) -> bool:
        """Whether readiness is reached (state answers other than ``starting``)."""
        with self._lock:
            return self._ready
