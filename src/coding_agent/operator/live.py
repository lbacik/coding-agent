"""The prepared live view the control listener reads (adoption step 4, #248, #250).

Contract: the listener reads only this view plus its own store/ledger
reads — never the Worker thread's connection, git, the network or the
provider. For #248 the view held just the process start time; #250 adds the
startup check, the recovering/ready flags and the process start time the
event ring filters on. Issue #253 adds the native phase (the Attempt graph
node name, shown as-is) and the last activity time for ``current_attempt``:
the Worker updates them at Claim, at each graph node transition and at each
usage flush. Thread-safe: the Worker thread writes through the
one lock while listener threads only read. Contract Events themselves live
in the ledger's ``operator_events`` table (see :mod:`operator.events`); this
view holds only the flags that decide ``instance.state`` plus the small
current-Attempt fields the listener joins with its own ledger reads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class LiveView:
    """Process-local state the Worker prepares and the listener only reads."""

    process_started_at: datetime
    """When this Worker process started, in UTC."""

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _startup_check: str | None = field(default=None, repr=False)
    _recovering: bool = field(default=False, repr=False)
    _ready: bool = field(default=False, repr=False)
    _attempt_id: str | None = field(default=None, repr=False)
    _issue_number: int | None = field(default=None, repr=False)
    _issue_title: str | None = field(default=None, repr=False)
    _phase: str | None = field(default=None, repr=False)
    _attempt_started_at: str | None = field(default=None, repr=False)
    _last_activity_at: str | None = field(default=None, repr=False)
    _continuations: int = field(default=0, repr=False)
    _review_seats_running: int = field(default=0, repr=False)

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

    @staticmethod
    def _now_iso() -> str:
        """Now in UTC ISO-8601 for the activity timestamps."""
        return datetime.now(timezone.utc).isoformat()

    def set_claim(
        self,
        attempt_id: str,
        issue_number: int,
        issue_title: str,
        *,
        phase: str = "claim",
    ) -> None:
        """Record a confirmed Claim: identity, native phase and activity (#253).

        Called once per Attempt right after the Claim boundary confirms it.
        ``phase`` is the Attempt graph node name, shown as-is; at Claim time
        that is ``claim``. Both the start and the last activity are now, so
        a fresh ``status`` never reports a stale activity time.
        """
        now = self._now_iso()
        with self._lock:
            self._attempt_id = attempt_id
            self._issue_number = issue_number
            self._issue_title = issue_title
            self._phase = phase
            self._attempt_started_at = now
            self._last_activity_at = now
            self._continuations = 0
            self._review_seats_running = 0

    def set_phase(self, phase: str) -> None:
        """Record a graph node transition: the native phase, as-is (#253).

        Every Attempt graph node entry calls this with its own node name
        (``claim`` … ``terminal_writes``). The activity time moves with the
        phase so ``status`` stays fresh while the Attempt runs.
        """
        now = self._now_iso()
        with self._lock:
            self._phase = phase
            self._last_activity_at = now

    def touch(self) -> None:
        """Record activity without a phase change: each usage flush (#253)."""
        now = self._now_iso()
        with self._lock:
            self._last_activity_at = now

    def note_continuation(self) -> None:
        """Count one Context-Window Handoff (``continuations``, #253)."""
        now = self._now_iso()
        with self._lock:
            self._continuations += 1
            self._last_activity_at = now

    def set_review_seats_running(self, seats: int) -> None:
        """Record how many review Sub-agents run right now (#253)."""
        now = self._now_iso()
        with self._lock:
            self._review_seats_running = max(0, seats)
            self._last_activity_at = now

    def clear_attempt(self) -> None:
        """Forget the Attempt once it finalizes: no stale phase survives."""
        with self._lock:
            self._attempt_id = None
            self._issue_number = None
            self._issue_title = None
            self._phase = None
            self._attempt_started_at = None
            self._last_activity_at = None
            self._continuations = 0
            self._review_seats_running = 0

    def get_current(self) -> dict[str, Any] | None:
        """The live current-Attempt fields, or ``None`` where no Claim holds.

        Returns a copy under the lock: the attempt id, issue number and
        title, the native phase, the start and last-activity times plus the
        handoff and review-seat counters the listener joins with its own
        ledger reads.
        """
        with self._lock:
            if self._attempt_id is None:
                return None
            return {
                "attempt_id": self._attempt_id,
                "issue_number": self._issue_number,
                "issue_title": self._issue_title,
                "phase": self._phase,
                "started_at": self._attempt_started_at,
                "last_activity_at": self._last_activity_at,
                "continuations": self._continuations,
                "review_seats_running": self._review_seats_running,
            }
