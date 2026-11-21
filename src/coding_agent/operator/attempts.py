"""Attempt Archives: the operator read and export side (adoption step 4, #255).

Contract: parent #245. ``attempts list`` pages AttemptSummaries newest
first; ``attempts show`` returns one AttemptSummary plus a manifest of
``{path, kind, media_type, size, sha256}`` entries computed on read;
``attempts export`` streams the archived evidence as a tar.

Archive reads happen directly in the ``agentctl`` process over the volume,
through the functions in this module — the one code path Offline Read
reuses. Nothing here takes the volume lock and nothing here writes to the
volume, except through :mod:`coding_agent.operator.archive`, which owns
everything that defines the Attempt Archive's layout or writes into it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.operator.archive import (
    MANIFEST_FILE_NAME,
    archive_dir_for,
    build_manifest,
    read_sealed_manifest,
)
from coding_agent.operator.store import read_instance_repository

DEFAULT_LIST_LIMIT = 20
"""``attempts list`` page size without ``--limit``."""


def list_attempts_page(
    ledger_path: Path, *, limit: int = DEFAULT_LIST_LIMIT, before: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of AttemptSummaries, newest first.

    An Attempt is listed as soon as its Claim opens: the rows come straight
    from the ledger's ``attempts`` table in reverse claim order. ``before``
    is the opaque cursor of the previous page's last row; the page holds
    attempts older than it. Returns ``(attempts, next_cursor)`` where
    ``next_cursor`` is the cursor for the following page, or ``None`` where
    this page is the last (contract ``v1.1`` ``attempts-list``). A missing
    ledger file means no Attempt ever opened: the page is empty. Raises
    :class:`InvalidCursor <coding_agent.operator.snapshots.InvalidCursor>`
    for a cursor that does not decode and ``ValueError`` for a ``limit``
    outside ``1..100``.

    One :class:`RunLedgerReader` per call; assembly lives in
    :mod:`coding_agent.operator.snapshots`.
    """
    from coding_agent.operator import snapshots as _snapshots

    if not ledger_path.is_file():
        return ([], None)
    try:
        with RunLedgerReader(ledger_path) as reader:
            return _snapshots.attempts_page(
                reader, limit=limit, before=before
            )
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {ledger_path}") from exc


def normalize_export_path(raw: str) -> str | None:
    """The archive-relative POSIX path ``--path`` names, or ``None`` invalid.

    Absolute paths and any ``..`` segment escape the archive and are
    invalid; the caller answers ``not_found`` for them like any unknown
    path. A trailing slash is stripped so a directory filters by prefix.
    Empty and ``.`` name nothing.
    """

    text = raw.strip()
    if not text or text == ".":
        return None
    if text.startswith("/"):
        return None
    parts = text.split("/")
    cleaned: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            return None
        cleaned.append(part)
    if not cleaned:
        return None
    return "/".join(cleaned)


def _entry_matches(entry_path: str, requested: str) -> bool:
    """Whether manifest ``entry_path`` is ``requested`` or under it."""

    return entry_path == requested or entry_path.startswith(requested + "/")


def filter_manifest_entries(
    manifest: list[dict[str, Any]], requested: list[str]
) -> list[dict[str, Any]] | None:
    """The manifest subset ``--path`` selects, or ``None`` where unknown.

    Every requested path must match at least one entry (itself or a path
    under it); where any request matches nothing the whole export answers
    ``not_found``. An empty request list selects everything.
    """

    if not requested:
        return list(manifest)
    normalized: list[str] = []
    for raw in requested:
        value = normalize_export_path(raw)
        if value is None:
            return None
        normalized.append(value)
    for want in normalized:
        if not any(_entry_matches(str(entry.get("path", "")), want) for entry in manifest):
            return None
    seen: set[str] = set()
    filtered: list[dict[str, Any]] = []
    for entry in manifest:
        path = str(entry.get("path", ""))
        if any(_entry_matches(path, want) for want in normalized):
            if path not in seen:
                seen.add(path)
                filtered.append(entry)
    filtered.sort(key=lambda entry: str(entry.get("path", "")))
    return filtered


def show_attempt(
    *, ledger_path: Path, state_dir: Path, attempt_id: str
) -> dict[str, Any] | None:
    """The AttemptSummary plus manifest for ``attempt_id``, or ``None`` unknown.

    The summary is the contract ``v1.1`` AttemptSummary (the Attempt is
    shown as soon as its Claim opens, even while it runs). A Sealed archive
    reports its sealed manifest's entries with ``sealed: True`` and
    ``sealed_at``; an unsealed Attempt reports ``sealed: False`` with a
    manifest computed on read. A missing archive reads as an empty
    manifest. ``None`` means no Attempt is recorded for the id — the caller
    answers ``not_found``.

    One :class:`RunLedgerReader` per call; assembly lives in
    :mod:`coding_agent.operator.snapshots`.
    """
    from coding_agent.attempt_id import AttemptId
    from coding_agent.operator import snapshots as _snapshots
    from coding_agent.operator.contract import to_z, utc_now_z

    if AttemptId.try_parse(attempt_id) is None:
        return None
    if not ledger_path.is_file():
        return None
    try:
        with RunLedgerReader(ledger_path) as reader:
            row = reader.get_attempt_page_row(attempt_id)
            if row is None:
                return None
            lifecycle_state = str(row[2])
            repository = read_instance_repository(reader.connection)
            observed_at = utc_now_z()
            summary = _snapshots.attempt_summary(
                reader,
                attempt_id,
                lifecycle_state,
                observed_at,
                state_dir=state_dir,
            )
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {ledger_path}") from exc
    if summary is None:
        return None
    manifest: list[dict[str, Any]] = []
    sealed = False
    sealed_at: str | None = None
    if repository is not None:
        try:
            archive = archive_dir_for(state_dir, repository, attempt_id)
        except ValueError:
            archive = None
        if archive is not None:
            sealed_document = read_sealed_manifest(archive)
            if sealed_document is not None:
                raw_entries = sealed_document.get("entries")
                if isinstance(raw_entries, list):
                    manifest = [
                        dict(entry)
                        for entry in raw_entries
                        if isinstance(entry, dict)
                    ]
                sealed = True
                maybe_at = sealed_document.get("sealed_at")
                sealed_at = to_z(maybe_at) if isinstance(maybe_at, str) else None
                if sealed and sealed_at is None:
                    sealed = False
            else:
                manifest = [entry.to_dict() for entry in build_manifest(archive)]
    summary["sealed"] = sealed
    summary["sealed_at"] = sealed_at
    return {"attempt": summary, "manifest": manifest}


def export_manifest_document(
    *, attempt: dict[str, Any], files: list[dict[str, Any]]
) -> dict[str, Any]:
    """The ``manifest.json`` document an export streams first.

    The contract export manifest (agent-operator-contract ``v1.1``,
    spec §11.3): ``{"contract", "attempt", "files"}`` where ``attempt``
    is the AttemptSummary the ``attempts show`` payload carries under
    ``attempt`` and ``files`` are the manifest entries for the files
    actually streamed. ``contract`` is the Worker's contract version
    string (the same ``CONTRACT_VERSION`` the agentctl envelope carries).
    """

    from coding_agent.operator.contract import CONTRACT_VERSION

    return {
        "contract": CONTRACT_VERSION,
        "attempt": dict(attempt),
        "files": list(files),
    }


def write_export_tar(
    *,
    archive_dir: Path | None,
    document: dict[str, Any],
    entries: list[dict[str, Any]],
    fileobj: Any,
) -> None:
    """Stream an uncompressed pax tar: ``manifest.json`` first, then evidence.

    Files are streamed from the volume, so a torn read of an unsealed file
    shows up as a ``sha256`` mismatch for the Consumer to detect: the
    exporter never re-hashes. Entries sort by path; the manifest member
    carries the canonical JSON bytes of ``document``.
    """

    import io
    import json
    import tarfile

    manifest_bytes = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with tarfile.open(fileobj=fileobj, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        manifest_info = tarfile.TarInfo(name=MANIFEST_FILE_NAME)
        manifest_info.size = len(manifest_bytes)
        manifest_info.mode = 0o644
        manifest_info.mtime = 0
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for entry in sorted(entries, key=lambda item: str(item.get("path", ""))):
            rel = str(entry.get("path", ""))
            if not rel:
                continue
            if archive_dir is None:
                continue
            source = archive_dir / rel
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise OSError(f"cannot read evidence file {rel!r}") from exc
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mode = 0o644
            try:
                info.mtime = int(source.stat().st_mtime)
            except OSError:
                info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
