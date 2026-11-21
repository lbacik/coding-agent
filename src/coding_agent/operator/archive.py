"""The Attempt Archive's layout and write side (adoption step 4, #255).

Contract: parent #245. An Attempt Archive is the Attempt's per-Attempt
directory on the state volume — ``evidence/<owner>/<repo>/<issue>/<n>``
under the state dir, the same directory the transcript and every evidence
file already live in — created at the Claim boundary. The Contract Event
producer also writes each Attempt-scoped event to that Attempt's
``events.jsonl``.

This module owns everything that defines the archive's layout or writes
into it: the archive directory for an Attempt id, the file and directory
names, the Attempt event types, creating the archive, content blobs,
emitting an Attempt event (database row plus ``events.jsonl`` line), the
execution summary, building, reading and atomically writing the manifest
and the sealed document, sealed-archive repair and its sweep, reading
archive lines, and the sealing operation the Run Ledger calls once an
Attempt is final and fully settled. It is a leaf module: nothing here
imports ``coding_agent.ledger``, ``coding_agent.operator.attempts`` or
``coding_agent.operator.snapshots``. The operator read and export side
(lists, show, export filtering and the tar) lives in
:mod:`coding_agent.operator.attempts`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from coding_agent.runtime.redaction import Redactable

EVIDENCE_DIR_NAME = "evidence"
"""The state-volume directory holding one archive per Attempt."""

EVENTS_FILE_NAME = "events.jsonl"
"""The Attempt's Contract Event log inside its archive."""

TRANSCRIPT_FILE_NAME = "transcript.jsonl"
"""The redacted Transcript's file name inside an archive."""

MANIFEST_FILE_NAME = "manifest.json"
"""The sealed manifest's file name inside an archive (adoption step 4, #256).

An archive is Sealed exactly when this file exists and parses: it carries
``sealed_at`` and the final ``entries``. After it appears nothing in the
archive changes.
"""

MANIFEST_TMP_NAME = "manifest.json.tmp"
"""The atomic-write temporary beside the sealed manifest (write, then rename)."""

EVENT_CLAIM_OPENED = "claim_opened"
EVENT_PHASE_CHANGED = "phase_changed"
EVENT_OUTCOME_RECORDED = "outcome_recorded"
EVENT_ATTEMPT_FINISHED = "attempt_finished"
EVENT_ATTEMPT_RELEASED = "attempt_released"
EVENT_SEALED = "sealed"

NATIVE_HANDOFF = "native.handoff"
"""A Context-Window Handoff recorded its Continuation Snapshot."""

NATIVE_REVIEW_STARTED = "native.review_started"
"""An ``activate_code_review`` call pushed its review snapshot."""

NATIVE_REVIEW_ENDED = "native.review_ended"
"""A ``spawn_subagent`` group owned a pending Review."""

NATIVE_GATE_DIVERSION = "native.gate_diversion"
"""A Gate diverted the Attempt (stop, SIGTERM or a crossed ceiling)."""

NATIVE_STEP_OPENED = "native.step_opened"
"""A Step Execution opened (step-workflow contract §4.5, S9)."""

NATIVE_STEP_ENDED = "native.step_ended"
"""A Step Execution ended (step-workflow contract §4.5, S9)."""

NATIVE_ROUTE_TAKEN = "native.route_taken"
"""A Route was taken (step-workflow contract §4.5, S9)."""

NATIVE_REFUSED_ENTRY = "native.refused_entry"
"""A Route into a step at its Execution Ceiling was refused (§4.5, S9)."""

NATIVE_WORKFLOW_END = "native.workflow_end"
"""A Workflow End was reached (step-workflow contract §4.5, S9)."""

ATTEMPT_EVENT_TYPES = frozenset(
    {
        EVENT_CLAIM_OPENED,
        EVENT_PHASE_CHANGED,
        EVENT_OUTCOME_RECORDED,
        EVENT_ATTEMPT_FINISHED,
        EVENT_ATTEMPT_RELEASED,
        EVENT_SEALED,
        NATIVE_HANDOFF,
        NATIVE_REVIEW_STARTED,
        NATIVE_REVIEW_ENDED,
        NATIVE_GATE_DIVERSION,
        NATIVE_STEP_OPENED,
        NATIVE_STEP_ENDED,
        NATIVE_ROUTE_TAKEN,
        NATIVE_REFUSED_ENTRY,
        NATIVE_WORKFLOW_END,
    }
)
"""Every Attempt-scoped Contract Event type the producer appends to ``events.jsonl``."""

ManifestKind = Literal["events", "transcript", "command_output", "record", "other"]
"""Manifest kinds (contract ``v1.1``): every Attempt Archive holds an
``events.jsonl`` with kind ``events`` (the ``attempts-show`` schema
requires it via ``contains``)."""

_COMMAND_OUTPUT_DIRS = frozenset(
    {"targeted", "validation", "readiness", "final-checks", "in-loop-checks"}
)
"""Archive subtrees holding Validation and Criterion Evidence."""

_RECORD_DIRS = frozenset({"tool-results"})
"""Archive subtrees holding Continuation Snapshots and kindred records."""

_COMMAND_OUTPUT_SUFFIXES = (".xml", ".log", ".txt", ".out", ".tap")
"""File suffixes that mark Validation-style command output anywhere."""

EXCLUDED_DIR_PARTS = frozenset(
    {
        "__pycache__",
        ".cache",
        "cache",
        "caches",
        "checkpoint",
        "checkpoints",
        "workspace",
        "workspaces",
        "mirror",
        "mirrors",
        "baseline-base",
    }
)
"""Directory names never listed in a manifest.

The Run Ledger database, checkpoints, workspaces, mirrors and caches live
outside the archive; these names keep a stray copy dropped inside one from
ever appearing in the manifest. ``baseline-base`` is the Baseline's
checkout worktree inside the archive. Nested checkouts are pruned
separately: any directory holding a ``.git`` entry is a workspace, not
evidence.
"""

_EXCLUDED_FILE_EXACT = frozenset(
    {
        ".env",
        ".git-credentials",
        "checkpoint.json",
        "workflow.yml",
        "workflow.yaml",
        "workflow-config.yml",
        "workflow-config.yaml",
    }
)
"""File names never listed: credentials, checkpoints, Workflow Configuration."""

_EXCLUDED_FILE_SUBSTRINGS = ("credential", "secret", "token")
"""Substrings (case-insensitive) that mark a credential file."""

_EXCLUDED_FILE_SUFFIXES = (".pem", ".key")
"""Suffixes that mark a credential file."""

_MEDIA_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".xml": "application/xml",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".out": "text/plain",
    ".tap": "text/plain",
    ".html": "text/html",
}
"""Manifest media types by file suffix; anything else is an octet stream."""


def archive_dir_for(
    state_dir: Path, target_repository: str, attempt_id: str
) -> Path:
    """The Attempt Archive: ``evidence/<owner>/<repo>/<issue>/<n>``.

    Step-workflow Attempts (``<workflow>#<N>/<n>``, contract §6.3, S9)
    live under ``evidence/<owner>/<repo>/<workflow>/<N>/<n>`` so two
    Workflows on the same input (``implement#42/1``, ``review#42/1``)
    never share a directory; MVP ids keep the historical layout.
    """
    from coding_agent.attempt_id import AttemptId

    owner, _, repo = target_repository.partition("/")
    attempt = AttemptId.try_parse(attempt_id)
    if attempt is None:
        raise ValueError(f"malformed Attempt id {attempt_id!r}")
    if attempt.workflow is None:
        return (
            state_dir
            / EVIDENCE_DIR_NAME
            / owner
            / repo
            / str(attempt.input_number)
            / str(attempt.sequence)
        )
    return (
        state_dir
        / EVIDENCE_DIR_NAME
        / owner
        / repo
        / attempt.workflow
        / str(attempt.input_number)
        / str(attempt.sequence)
    )


def ensure_attempt_archive(archive_dir: Path) -> Path:
    """Create the archive directory (and an empty ``events.jsonl``) at the Claim boundary.

    Idempotent: a restart or a second Claim for the same Attempt keeps the
    existing directory and its event log.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    events_path = archive_dir / EVENTS_FILE_NAME
    if not events_path.is_file():
        events_path.write_text("", encoding="utf-8")
    return archive_dir


CONTENT_DIR_NAME = "content"
"""The Attempt Archive subtree holding digest-addressed content blobs.

Every prompt the Workflow needs and every available activation's components
and delivery bytes live here as ``content/<sha256>`` before the ledger
transaction the Claim depends on. ``not-delivered`` bodies have no blob;
their digests stay pinned in the content-reference index only. Orphaned
unreferenced files are permitted (SKV3-28): a crash between blob durability
and the ledger commit leaves blobs no confirmed Claim references.
"""


def content_blob_path(archive_dir: Path, digest: str) -> Path:
    """The digest-addressed blob path for ``digest`` inside ``archive_dir``."""
    return archive_dir / CONTENT_DIR_NAME / digest


def _fsync_path(path: Path) -> None:
    """Flush one file or directory to durable storage, best-effort per handle."""
    import os

    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_content_blobs(
    archive_dir: Path, blobs: dict[str, bytes] | Mapping[str, bytes]
) -> list[Path]:
    """Store digest-addressed blobs, flushed before the caller commits.

    Each blob is written atomically (temporary file, ``fsync``, rename) and
    the containing directory is fsynced, so a return means the bytes are
    durable: the Claim's ledger transaction follows and a crash before or
    after that transaction never leaves a confirmed Claim referencing
    missing or unflushed content. Identical bytes are stored once; an
    existing blob with identical bytes is left in place. Returns the blob
    paths in digest order.
    """
    import os
    import tempfile

    content_dir = archive_dir / CONTENT_DIR_NAME
    content_dir.mkdir(parents=True, exist_ok=True)
    stored: list[Path] = []
    for digest in sorted(blobs.keys()):
        raw = bytes(blobs[digest])
        target = content_dir / digest
        if target.is_file():
            try:
                if target.read_bytes() == raw:
                    stored.append(target)
                    continue
            except OSError:
                pass
        fd, tmp_name = tempfile.mkstemp(
            dir=str(content_dir), prefix=f"{digest}.tmp."
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(target)
        finally:
            try:
                if Path(tmp_name).exists():
                    Path(tmp_name).unlink()
            except OSError:
                pass
        stored.append(target)
    try:
        _fsync_path(content_dir)
    except OSError:
        pass
    return stored


def process_started_for_archive(connection: sqlite3.Connection) -> str:
    """The current process start tagging archive Contract Events.

    The instance row the operator store writes at startup; a ledger without
    one (unit tests) falls back to now, like the command-event producer.
    """
    from coding_agent.operator.store import read_process_started_at

    started = read_process_started_at(connection)
    if started is not None:
        return started
    return datetime.now(timezone.utc).isoformat()


def emit_attempt_event(
    connection: sqlite3.Connection,
    *,
    archive_dir: Path | None,
    attempt_id: str,
    event_type: str,
    text: str,
    registry: Redactable = None,
    created_at: str | None = None,
    phase: str | None = None,
    issue: int | None = None,
    outcome: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record one Attempt-scoped Contract Event, in the DB and in the archive.

    The single producer path for Attempt archives: the row goes through
    :func:`operator.events.emit_contract_event` (redacted through the
    existing registry, capped at 300 characters) in the caller's transaction
    — the caller commits — and the same record is appended to the Attempt's
    ``events.jsonl``. Every line of ``events.jsonl`` is therefore a redacted
    Contract Event capped at 300 characters. The archive directory is
    created where missing so a pre-existing Attempt gains its log lazily; a
    ``None`` archive records the database row alone (a ledger without its
    Target Repository yet).

    A Sealed archive never changes: where ``archive_dir`` holds a sealed
    manifest the database row is still recorded but nothing is appended to
    ``events.jsonl``. The seal path writes the ``sealed`` line before the
    manifest, so a legitimate seal still lands; anything reaching the
    producer afterwards is evidence of a repeated transition and stays out
    of the sealed bytes.
    """
    from coding_agent.operator.events import emit_contract_event

    record = emit_contract_event(
        connection,
        process_started_at=process_started_for_archive(connection),
        event_type=event_type,
        text=text,
        registry=registry,
        created_at=created_at,
        attempt_id=attempt_id,
        phase=phase,
        issue=issue,
        outcome=outcome,
        reason=reason,
    )
    record = {"attempt_id": attempt_id, **record}
    if archive_dir is None:
        return record
    if read_sealed_manifest(archive_dir) is not None:
        return record
    archive_dir.mkdir(parents=True, exist_ok=True)
    events_path = archive_dir / EVENTS_FILE_NAME
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def repair_sealed_archive(archive_dir: Path) -> bool:
    """Truncate a damaged Sealed archive's ``events.jsonl`` back to its seal.

    A Sealed archive whose ``events.jsonl`` on disk is longer than the size
    recorded for it in the sealed manifest — and whose first ``size`` bytes
    hash to the manifest's ``sha256`` — carries bytes appended after the
    seal (a repeated terminal transition). Truncating the file back to
    exactly ``size`` bytes restores the sealed bytes; the manifest and
    ``sealed_at`` are never rewritten. The repair is strictly conditional:
    every other manifest entry must still match its file on disk, so an
    archive damaged beyond the known trailing-line shape is left untouched
    and never resealed. Returns ``True`` where the file was truncated,
    ``False`` where it was left untouched: a healthy Sealed archive, an
    unsealed archive, a file shorter than (or equal to) the manifest size,
    a prefix that does not hash-match, a manifest entry that mismatches
    its file, or a malformed manifest entry. Idempotent: a repaired
    archive repairs to ``False`` on the next run. Never raises: an
    unreadable file or a failed truncate reads as untouched, so one bad
    archive never fails the sweep.
    """
    try:
        document = read_sealed_manifest(archive_dir)
    except Exception:
        return False
    if document is None:
        return False
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        return False
    expected_size: int | None = None
    expected_sha256: str | None = None
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            return False
        if raw_entry.get("path") != EVENTS_FILE_NAME:
            continue
        size = raw_entry.get("size")
        sha256 = raw_entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return False
        if not isinstance(sha256, str) or not sha256:
            return False
        expected_size = size
        expected_sha256 = sha256
        break
    if expected_size is None or expected_sha256 is None:
        return False
    events_path = archive_dir / EVENTS_FILE_NAME
    try:
        data = events_path.read_bytes()
    except OSError:
        return False
    if len(data) <= expected_size:
        return False
    if hashlib.sha256(data[:expected_size]).hexdigest() != expected_sha256:
        return False
    if not _other_manifest_entries_match(archive_dir, raw_entries):
        return False
    try:
        with events_path.open("r+b") as handle:
            handle.truncate(expected_size)
    except OSError:
        return False
    return True


def _other_manifest_entries_match(
    archive_dir: Path, raw_entries: list[Any]
) -> bool:
    """Whether every non-``events.jsonl`` manifest entry matches its file.

    The strictly-conditional half of :func:`repair_sealed_archive`: an
    archive whose damage reaches beyond the known trailing line is not
    the shape the repair restores, so it stays untouched. Never raises:
    any unreadable file, malformed entry or hash mismatch reads as a
    mismatch.
    """
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            return False
        if raw_entry.get("path") == EVENTS_FILE_NAME:
            continue
        path = raw_entry.get("path")
        size = raw_entry.get("size")
        sha256 = raw_entry.get("sha256")
        if not isinstance(path, str) or not path:
            return False
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return False
        if not isinstance(sha256, str) or not sha256:
            return False
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        target = archive_dir / relative
        try:
            if not target.is_file():
                return False
            if target.stat().st_size != size:
                return False
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        except OSError:
            return False
        if digest.hexdigest() != sha256:
            return False
    return True


def sweep_sealed_archive_repairs(state_dir: Path) -> dict[str, int]:
    """Repair every damaged Sealed archive under ``state_dir``, best-effort.

    Walks the ``evidence`` subtree for sealed manifests and runs
    :func:`repair_sealed_archive` on each archive found. A failure on one
    archive never stops the sweep: the archive counts as scanned but not
    repaired and the walk continues. Returns ``{"scanned", "repaired"}``
    counts. Never raises: a missing or unreadable ``evidence`` directory
    reads as nothing to repair.
    """
    scanned = 0
    repaired = 0
    try:
        candidates = sorted((state_dir / EVIDENCE_DIR_NAME).rglob(MANIFEST_FILE_NAME))
    except OSError:
        return {"scanned": 0, "repaired": 0}
    for manifest_path in candidates:
        try:
            if not manifest_path.is_file() or manifest_path.name != MANIFEST_FILE_NAME:
                continue
            archive_dir = manifest_path.parent
            scanned += 1
            try:
                if repair_sealed_archive(archive_dir):
                    repaired += 1
            except Exception:
                continue
        except Exception:
            continue
    return {"scanned": scanned, "repaired": repaired}


def _is_excluded(relative: Path) -> bool:
    """Whether an archive-relative path is never listed in a manifest."""
    if relative.name == MANIFEST_FILE_NAME or relative.name == MANIFEST_TMP_NAME:
        return True
    if relative.name.startswith(MANIFEST_FILE_NAME + "."):
        return True
    lowered_parts = [part.lower() for part in relative.parts[:-1]]
    if any(part in EXCLUDED_DIR_PARTS or part == ".git" for part in lowered_parts):
        return True
    name = relative.name
    lowered = name.lower()
    if name in _EXCLUDED_FILE_EXACT or lowered in _EXCLUDED_FILE_EXACT:
        return True
    if any(token in lowered for token in _EXCLUDED_FILE_SUBSTRINGS):
        return True
    if lowered.endswith(_EXCLUDED_FILE_SUFFIXES):
        return True
    if name == "run-ledger.sqlite3" or name.startswith("run-ledger.sqlite3-"):
        return True
    return False


def _classify(relative: Path) -> ManifestKind:
    """The manifest kind for an archive-relative path that is not excluded."""
    name = relative.name
    lowered = name.lower()
    if name == EVENTS_FILE_NAME and len(relative.parts) == 1:
        return "events"
    if lowered.endswith(".facts.json"):
        return "record"
    if name == TRANSCRIPT_FILE_NAME:
        return "transcript"
    if name == EVENTS_FILE_NAME or relative.parts[0] in _RECORD_DIRS:
        return "record"
    if relative.parts[0] in _COMMAND_OUTPUT_DIRS:
        return "command_output"
    suffix = relative.suffix.lower()
    if suffix == ".md":
        return "record"
    if suffix in _MEDIA_TYPES and suffix in {
        ".xml",
        ".log",
        ".txt",
        ".out",
        ".tap",
    }:
        return "command_output"
    return "other"


def _media_type(path: Path) -> str:
    """The manifest media type for a file, by suffix."""
    if path.name == EVENTS_FILE_NAME:
        return "application/x-ndjson"
    if path.name.lower().endswith(".facts.json"):
        return "application/json"
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def write_execution_summary(
    archive_dir: Path, step_id: str, seq: int, summary: dict[str, Any]
) -> Path:
    """Persist one Execution Summary as ``<step>.<seq>.facts.json`` (S9, §9.7).

    The summary is already redacted and size-capped by its composer; this
    writes it verbatim so the Attempt Archive manifest lists Execution
    Summaries alongside Step Artifacts (matrix row OPC-7). Immutable:
    re-writing the same key with identical bytes is a no-op. Best-effort
    like every archive write — an ``OSError`` propagates to the caller,
    which treats archive evidence as never failing the Attempt.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{step_id}.{seq}.facts.json"
    data = (json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if target.is_file():
        try:
            if target.read_bytes() == data:
                return target
        except OSError:
            pass
    target.write_bytes(data)
    return target


def _walk_archive(archive_dir: Path) -> list[Path]:
    """Every listable file under the archive, pruned of workspaces and caches.

    Directories holding a ``.git`` entry are nested checkouts (the Baseline
    worktree, a workspace copy), never evidence: the walk does not descend
    into them. Directory symlinks are never followed; an unreadable file is
    skipped rather than failing the read. Order is filesystem order; callers
    sort the manifest by path.
    """
    found: list[Path] = []
    stack: list[Path] = [archive_dir]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() and entry.is_dir():
                    continue
                if entry.is_dir():
                    if (entry / ".git").exists():
                        continue
                    stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            found.append(entry)
    return found


@dataclass(frozen=True)
class ManifestEntry:
    """One manifest row: where the file is and what it is."""

    path: str
    kind: ManifestKind
    media_type: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        """The contract manifest entry shape."""
        return {
            "path": self.path,
            "kind": self.kind,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
        }


def build_manifest(archive_dir: Path) -> list[ManifestEntry]:
    """The archive manifest, computed on read.

    Every listable file under the archive becomes one
    ``{path, kind, media_type, size, sha256}`` entry with its POSIX path
    relative to the archive, sorted by path. The Run Ledger database,
    checkpoints, workspaces, mirrors, caches, credentials and the Workflow
    Configuration are never listed (see :data:`EXCLUDED_DIR_PARTS`). A
    missing archive reads as an empty manifest, so an Attempt whose files
    predate archives still shows its summary. An unreadable file is skipped
    rather than failing the read.
    """
    entries: list[ManifestEntry] = []
    if not archive_dir.is_dir():
        return entries
    for absolute in _walk_archive(archive_dir):
        try:
            relative = absolute.relative_to(archive_dir)
        except ValueError:
            continue
        if _is_excluded(relative):
            continue
        try:
            digest = hashlib.sha256()
            with absolute.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            size = absolute.stat().st_size
        except OSError:
            continue
        entries.append(
            ManifestEntry(
                path=relative.as_posix(),
                kind=_classify(relative),
                media_type=_media_type(absolute),
                size=size,
                sha256=digest.hexdigest(),
            )
        )
    entries.sort(key=lambda entry: entry.path)
    return entries


def read_sealed_manifest(archive_dir: Path) -> dict[str, Any] | None:
    """The parsed sealed manifest, or ``None`` where the archive is unsealed.

    An archive is Sealed exactly when ``manifest.json`` exists and parses
    with a non-empty ``sealed_at`` string and an ``entries`` list. A missing
    file, an unreadable file or an invalid document reads as unsealed rather
    than failing the read, so a torn seal write never fails observation.
    """

    manifest_path = archive_dir / MANIFEST_FILE_NAME
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    sealed_at = document.get("sealed_at")
    entries = document.get("entries")
    if not isinstance(sealed_at, str) or not sealed_at:
        return None
    if not isinstance(entries, list):
        return None
    return document


def sealed_info(archive_dir: Path) -> tuple[bool, str | None]:
    """Whether ``archive_dir`` is Sealed and its ``sealed_at`` where so."""

    document = read_sealed_manifest(archive_dir)
    if document is None:
        return (False, None)
    sealed_at = document.get("sealed_at")
    if isinstance(sealed_at, str) and sealed_at:
        return (True, sealed_at)
    return (False, None)


def build_sealed_document(
    attempt_id: str, entries: list[ManifestEntry], sealed_at: str
) -> dict[str, Any]:
    """The sealed ``manifest.json`` document for ``attempt_id``."""

    return {
        "attempt_id": attempt_id,
        "sealed": True,
        "sealed_at": sealed_at,
        "entries": [entry.to_dict() for entry in entries],
    }


def build_unsealed_document(
    attempt_id: str, entries: list[ManifestEntry]
) -> dict[str, Any]:
    """The ``manifest.json`` document an unsealed export streams first."""

    return {
        "attempt_id": attempt_id,
        "sealed": False,
        "sealed_at": None,
        "entries": [entry.to_dict() for entry in entries],
    }


def write_sealed_manifest_atomically(
    archive_dir: Path, document: dict[str, Any]
) -> None:
    """Write the final manifest atomically: write, then rename.

    The temporary sits beside ``manifest.json`` so the rename never crosses
    filesystems; after it returns the archive is Sealed and never changes.
    """

    archive_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = archive_dir / MANIFEST_TMP_NAME
    data = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    tmp_path.write_bytes(data)
    tmp_path.replace(archive_dir / MANIFEST_FILE_NAME)


def seal_archive(
    connection: sqlite3.Connection,
    archive_dir: Path,
    attempt_id: str,
    registry: Redactable = None,
) -> dict[str, Any] | None:
    """Seal the archive for ``attempt_id``, best-effort, never raising.

    The sealing mechanics the Run Ledger runs once an Attempt is final and
    fully settled: return ``None`` where a valid sealed manifest already
    exists; remove a torn manifest file that reads as unsealed (a stuck
    file defers sealing to a later finalization pass); emit the ``sealed``
    Attempt event only where the archive does not already hold one for this
    Attempt, committing the caller's transaction like every archive
    producer; then build the manifest and write the sealed document
    atomically. Returns the emitted ``sealed`` record, or ``None`` where
    the event already existed or the archive was already (or stays)
    unsealed.
    """
    manifest_path = archive_dir / MANIFEST_FILE_NAME
    if manifest_path.is_file():
        if read_sealed_manifest(archive_dir) is not None:
            return None
        # A torn seal write leaves a file that reads as unsealed;
        # drop it so this pass can seal cleanly. Best-effort: a
        # stuck file defers sealing to a later finalization path.
        try:
            manifest_path.unlink()
        except OSError:
            return None
    already_sealed = False
    try:
        for record in read_archive_lines(archive_dir):
            if (
                record.get("event", record.get("type")) == EVENT_SEALED
                and record.get("attempt_id") == attempt_id
            ):
                already_sealed = True
                break
    except Exception:
        already_sealed = False
    sealed_record: dict[str, Any] | None = None
    if not already_sealed:
        try:
            sealed_record = emit_attempt_event(
                connection,
                archive_dir=archive_dir,
                attempt_id=attempt_id,
                event_type=EVENT_SEALED,
                text=f"sealed {attempt_id}",
                registry=registry,
            )
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            sealed_record = None
    entries = build_manifest(archive_dir)
    from coding_agent.operator.contract import utc_now_z as _utc_now_z

    sealed_at = _utc_now_z()
    document = build_sealed_document(attempt_id, entries, sealed_at)
    try:
        write_sealed_manifest_atomically(archive_dir, document)
    except Exception:
        pass
    return sealed_record


def read_archive_lines(archive_dir: Path) -> list[dict[str, Any]]:
    """Every parsable ``events.jsonl`` line, oldest first.

    A torn trailing write (the Worker appending while the operator reads)
    is skipped rather than failing the read; each kept line is a Contract
    Event dict as the producer wrote it.
    """
    events_path = archive_dir / EVENTS_FILE_NAME
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
