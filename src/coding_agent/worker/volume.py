"""One Worker per state volume, and SQLite never meets a full disk.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup step 2:
take the exclusive lock (``flock``) on the state volume as startup step 2,
right after validating the configuration; a second process using the same
state volume is refused) and §17 ("Storage": one SQLite file on the state
volume, in WAL mode with ``synchronous=FULL``, accessed under the volume
lock; "Liveness is the volume lock: once a Worker holds it, every open
Attempt in the ledger was orphaned by a dead process. There is no Lease").

MVP row ids: W-CFG-14, L3-ERR-13.
"""

from __future__ import annotations

import fcntl
import os
import shutil
from pathlib import Path
from types import TracebackType

VOLUME_LOCK_FILE_NAME = ".worker.lock"
"""The lock file ``flock``ed while the Worker runs (contract §3, W-CFG-14).

It lives directly in the state directory, beside the Run Ledger, so locking
the file locks the volume's use by a second Worker. ``flock`` releases when
the last file description closes — a dead process releases it, which is
what makes the lock the liveness signal with no Lease (§17).
"""

MIN_FREE_BYTES = 256 * 1024 * 1024
"""The volume free-space threshold below which no Attempt starts (§17).

A tuning constant, set against observed runs, not part of the contract:
the mirror, the workspace, the evidence and the ledger's WAL all grow while
an Attempt runs, so the Attempt is refused while hundreds of megabytes are
still free and SQLite never meets a full disk (L3-ERR-13).
"""


class VolumeLockHeld(OSError):
    """The state volume is already held by another Worker (§3, W-CFG-14)."""


class VolumeLock:
    """The exclusive ``flock`` on the state volume, held for the whole run.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (step 2) and §17
    (liveness is the volume lock). The lock releases when the Worker exits —
    normally through :meth:`release`, on a crash when the process dies.
    """

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd
        self._released = False

    @property
    def path(self) -> Path:
        """The lock file the exclusive ``flock`` is held on."""
        return self._path

    def release(self) -> None:
        """Release the lock; idempotent, so every startup path unlocks."""
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> VolumeLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def acquire_volume_lock(state_dir: Path) -> VolumeLock:
    """Take the exclusive lock (``flock``) on the state volume (§3 step 2).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (a second process
    using the same state volume is refused).

    MVP row ids: W-CFG-14.

    Creates the state directory and its lock file, then takes an exclusive
    non-blocking ``flock``. Raises :class:`VolumeLockHeld` where another
    Worker — another process, or this one — already holds it. The returned
    lock must be held until the Worker exits.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / VOLUME_LOCK_FILE_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise VolumeLockHeld(
            f"state directory {state_dir} is locked by another worker: "
            "is another `agent run` using it?"
        ) from None
    return VolumeLock(lock_path, fd)


def volume_free_bytes(path: Path) -> int:
    """Free bytes on the volume holding ``path`` (§17, L3-ERR-13).

    The Attempt-start gate calls this on the state directory, so the
    measurement is of the volume the Run Ledger lives on. Passed as the
    ``free_bytes`` seam of ``run_worker_loop``; tests inject their own.
    """
    return shutil.disk_usage(path).free
