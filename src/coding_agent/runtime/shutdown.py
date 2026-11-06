"""Cooperative SIGTERM handling: intake stops, the live Attempt finalizes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 ("SIGTERM stops
intake and stops the live Attempt as §11 describes. The process then
exits") and §11 ("SIGTERM: intake stops at once; the live Attempt observes
the signal after every model response and every tool result; the in-flight
model request is cancelled and the running command's whole process group is
killed; before the Delivery Intent the Attempt takes the claim-confirmed
path of the recovery table within ``shutdown_grace_seconds``; after the
Delivery Intent publication completes as ``delivered``; a second signal, or
SIGKILL, is an ordinary process loss").

MVP row ids: P-CAN-3, REC-SIG-1, REC-SIG-2.

In the image ``agent run`` is PID 1, where the kernel ignores a signal with
no installed handler: ``docker compose stop worker`` delivered SIGTERM,
nothing happened, and Docker sent SIGKILL after its timeout (exit 137).
Installing the handler explicitly below is what makes the default action
fire in the container; ``compose.yaml`` additionally sets ``init: true``
so the Worker never relies on PID-1 disposition alone.

A provider call already blocked inside its HTTP request runs to the
provider's own timeout and its result is discarded at the next stop point:
only the runtime's own commands are truly cancelled mid-flight, by killing
their process group (see :mod:`coding_agent.validate.runner`). No new model
call starts once the signal has been observed.
"""

from __future__ import annotations

import os
import signal
import time
from types import FrameType
from typing import Any

FAILED_OUTCOME = "failed"
"""The Terminal Outcome a pre-Intent SIGTERM ends with (§11, §14)."""

INTERRUPTED_REASON = "interrupted"
"""The Terminal Outcome reason for a pre-Intent SIGTERM (§11, §14).

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 with the §14 route
"SIGTERM, or a process loss, before an outcome was recorded" →
``failed`` / ``interrupted``, or ``abandoned`` where the issue was
cancelled meanwhile.
"""

ABANDONED_OUTCOME = "abandoned"
ABANDONED_CANCELLED_REASON = "cancelled"
"""Where the issue was cancelled meanwhile, SIGTERM ends ``abandoned`` (§11)."""

SHUTDOWN_EXIT_CODE = 128 + signal.SIGTERM
"""Exit status of an immediate second-signal exit (143, like death by SIGTERM)."""

_shutdown_requested = False
"""Whether SIGTERM has been observed since the last reset.

Process-wide on purpose: the handler runs in the main thread and every
stop point — the step loop, the Gates, the command runner, the Supervisor
poll loop — reads the same flag.
"""


def is_shutdown_requested() -> bool:
    """Whether SIGTERM has been observed (§11)."""
    return _shutdown_requested


def request_shutdown() -> None:
    """Record that SIGTERM arrived: stop points divert from here on (§11).

    The signal handler's body, extracted so tests drive the stop path
    without sending a real signal.
    """
    global _shutdown_requested
    _shutdown_requested = True


def reset_shutdown() -> None:
    """Clear the flag. Test seam only: production never un-observes SIGTERM."""
    global _shutdown_requested
    _shutdown_requested = False


def _handle_sigterm(signum: int, frame: FrameType | None) -> Any:
    """The SIGTERM handler: first signal cooperates, second exits at once.

    A second signal — or SIGKILL, which cannot be handled — is an ordinary
    process loss (§17): exit immediately with the SIGTERM status instead of
    finalizing, so startup recovery (M8) finishes the Attempt.
    """
    _ = (signum, frame)
    if is_shutdown_requested():
        os._exit(SHUTDOWN_EXIT_CODE)
    request_shutdown()


def install_shutdown_handlers() -> None:
    """Install the explicit SIGTERM handler (§3, §11).

    Called once at Worker startup. An explicitly installed handler fires
    even as PID 1, where the kernel ignores a signal with no handler — the
    observed exit-137 shape from the first real run. A ``ValueError`` from
    ``signal.signal`` outside the main thread (the in-repo operator seam
    runs the Worker in a background thread) leaves the main-thread handler
    in place instead of failing startup: signal disposition is
    process-wide and only the main thread may set it.
    """
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        pass


def shutdown_grace_deadline(grace_seconds: float) -> float:
    """The monotonic deadline the live Attempt finalizes within (§11).

    ``grace_seconds`` is the Effective Configuration's
    ``worker.shutdown_grace_seconds`` (contract §2.1, default 30).
    """
    if grace_seconds <= 0:
        raise ValueError("shutdown_grace_seconds must be positive")
    return time.monotonic() + grace_seconds
