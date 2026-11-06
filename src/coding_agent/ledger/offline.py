"""Offline Read of the Run Ledger without touching the state volume.

Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (one SQLite file on
the state volume, in WAL mode) and the operator contract's Offline Read: a
stopped Worker — including one killed without a clean shutdown, whose Run
Ledger still holds an uncheckpointed WAL — stays inspectable without
starting the Worker.

The reader never takes the volume lock and never writes to the volume. It
copies the ledger database file and any ``-wal`` file into a private
temporary directory, then opens the copy: opening replays the committed WAL
frames, so the copy shows the latest committed state. The shared-memory
``-shm`` file is transient and is never copied. The source directory gains
no ``-wal`` or ``-shm`` file and no existing file is modified.

MVP row ids: none yet (adoption step 4 of the operator contract, #245).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.redaction import Redactable


def _sibling_wal(source: Path) -> Path:
    """The WAL file beside the ledger database file, if one exists."""
    return Path(str(source) + "-wal")


@contextmanager
def open_offline_ledger(
    source: Path,
    *,
    ceilings: LoopCeilings | None = None,
    registry: Redactable = None,
) -> Iterator[RunLedger]:
    """Open a private copy of the ledger at ``source`` for Offline Read.

    Copies the database file and any ``-wal`` file into a private temporary
    directory (mode 0700, outside the state volume) and yields a
    :class:`RunLedger` opened on the copy. Opening the copy replays the
    committed WAL frames, so even a ledger left with an uncheckpointed WAL
    by a killed Worker reads back every committed row. Works where no
    ``-wal`` file exists: the database file alone opens as usual.

    The source is only ever read, never opened with SQLite and never
    locked: no volume lock is taken, and the source directory is unchanged
    afterwards. The temporary directory and the copy are removed when the
    context exits; the yielded ledger is closed first.
    """
    if not source.is_file():
        raise FileNotFoundError(f"no Run Ledger at {source}")
    with tempfile.TemporaryDirectory(prefix="run-ledger-offline-") as tmp:
        ledger_copy = Path(tmp) / source.name
        shutil.copyfile(source, ledger_copy)
        wal = _sibling_wal(source)
        if wal.is_file():
            shutil.copyfile(wal, Path(str(ledger_copy) + "-wal"))
        ledger = RunLedger(ledger_copy, ceilings=ceilings, registry=registry)
        try:
            yield ledger
        finally:
            ledger.close()
