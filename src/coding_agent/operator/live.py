"""The prepared live view the control listener reads (adoption step 4, #248).

Contract: the listener reads only this view plus its own store/ledger
reads — never the Worker thread's connection, git, the network or the
provider. For this slice the view holds just the process start time; later
steps add the startup check, the native phase, activity time and the event
ring buffer. Thread-safe by construction: the timestamp is immutable after
creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LiveView:
    """Process-local state the Worker prepares and the listener only reads."""

    process_started_at: datetime
    """When this Worker process started, in UTC."""

    def process_started_at_iso(self) -> str:
        """The start time in ISO-8601, as ``identity`` and ``status`` report."""
        return self.process_started_at.isoformat()
