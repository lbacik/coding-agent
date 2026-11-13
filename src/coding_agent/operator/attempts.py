"""Attempt Archives and Attempt-scoped Contract Events (adoption step 4, #255).

Contract: parent #245. An Attempt Archive is the Attempt's per-Attempt
directory on the state volume — ``evidence/<owner>/<repo>/<issue>/<n>``
under the state dir, the same directory the transcript and every evidence
file already live in — created at the Claim boundary. The Contract Event
producer also writes each Attempt-scoped event to that Attempt's
``events.jsonl``. ``attempts list`` pages AttemptSummaries newest first;
``attempts show`` returns one AttemptSummary plus a manifest of
``{path, kind, media_type, size, sha256}`` entries computed on read.

Archive reads happen directly in the ``agentctl`` process over the volume,
through the functions in this module — the one code path Offline Read
reuses. Nothing here takes the volume lock and nothing here writes to the
volume, except the producer side (``ensure_attempt_archive``,
``emit_attempt_event``) which the Worker calls at the Claim boundary and
on every lifecycle transition.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
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

DEFAULT_LIST_LIMIT = 20
"""``attempts list`` page size without ``--limit``."""

MAX_LIST_LIMIT = 100
"""``attempts list`` never returns more than this per page."""

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
"""Manifest kinds (contract ``v1.0.4``): every Attempt Archive holds an
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


class InvalidCursor(ValueError):
    """An opaque ``--before`` cursor that does not decode."""


def parse_attempt_id(attempt_id: str) -> tuple[int, int]:
    """The ``(issue, attempt)`` halves of ``#<issue>/<n>``.

    Raises :class:`ValueError` where the id is malformed.

    Step-workflow ids (``<workflow>#<N>/<n>``, contract §6.3, S9) resolve
    to their ``(N, n)`` halves here so MVP readers keep working; the
    workflow segment is available from :func:`split_attempt_id`.
    """
    workflow, number, n = split_attempt_id(attempt_id)
    _ = workflow
    return number, n


def split_attempt_id(attempt_id: str) -> tuple[str | None, int, int]:
    """Split any Attempt id into ``(workflow, number, n)`` (S9, §6.3).

    MVP ids (``#<N>/<n>``) yield ``workflow=None``; step-workflow ids
    (``<workflow>#<N>/<n>``) yield the workflow id. Raises
    :class:`ValueError` where the id matches neither shape.
    """
    from coding_agent.inputs.numbering import parse_attempt_id as parse_step_id

    parsed = parse_step_id(attempt_id)
    if parsed is not None:
        workflow, number, n = parsed
        return workflow, number, n
    head, slash, tail = attempt_id.partition("/")
    if not slash or not head.startswith("#"):
        raise ValueError(f"malformed Attempt id {attempt_id!r}")
    try:
        return None, int(head[1:]), int(tail)
    except ValueError as exc:
        raise ValueError(f"malformed Attempt id {attempt_id!r}") from exc


def is_step_attempt_id(attempt_id: str) -> bool:
    """Whether ``attempt_id`` carries a workflow segment (``<wf>#<N>/<n>``)."""
    try:
        workflow, _, _ = split_attempt_id(attempt_id)
    except ValueError:
        return False
    return workflow is not None


def archive_dir_for(
    state_dir: Path, target_repository: str, attempt_id: str
) -> Path:
    """The Attempt Archive: ``evidence/<owner>/<repo>/<issue>/<n>``.

    Step-workflow Attempts (``<workflow>#<N>/<n>``, contract §6.3, S9)
    live under ``evidence/<owner>/<repo>/<workflow>/<N>/<n>`` so two
    Workflows on the same input (``implement#42/1``, ``review#42/1``)
    never share a directory; MVP ids keep the historical layout.
    """
    owner, _, repo = target_repository.partition("/")
    workflow, issue_number, attempt_number = split_attempt_id(attempt_id)
    if workflow is None:
        return (
            state_dir
            / EVIDENCE_DIR_NAME
            / owner
            / repo
            / str(issue_number)
            / str(attempt_number)
        )
    return (
        state_dir
        / EVIDENCE_DIR_NAME
        / owner
        / repo
        / workflow
        / str(issue_number)
        / str(attempt_number)
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


def process_started_for_archive(connection: sqlite3.Connection) -> str:
    """The current process start tagging archive Contract Events.

    The instance row the operator store writes at startup; a ledger without
    one (unit tests) falls back to now, like the command-event producer.
    """
    try:
        row = connection.execute(
            "SELECT process_started_at FROM operator_instance WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row[0]:
        return str(row[0])
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
    archive_dir.mkdir(parents=True, exist_ok=True)
    events_path = archive_dir / EVENTS_FILE_NAME
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def encode_before_cursor(last_rowid: int) -> str:
    """The opaque ``--before`` cursor for the page ending at ``last_rowid``."""
    raw = json.dumps({"v": 1, "last": last_rowid}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_before_cursor(cursor: str) -> int:
    """The rowid bound a ``--before`` cursor carries.

    Raises :class:`InvalidCursor` where the cursor does not decode: cursors
    are opaque, so anything unrecognised is rejected rather than guessed.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    if payload.get("v") != 1:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    last = payload.get("last")
    if isinstance(last, bool) or not isinstance(last, int) or last < 1:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    return last


def _check_limit(limit: int) -> None:
    """Reject a page size outside ``1..100``."""
    if not isinstance(limit, bool) and isinstance(limit, int):
        if 1 <= limit <= MAX_LIST_LIMIT:
            return
    raise ValueError(f"invalid --limit {limit!r}: expected 1..{MAX_LIST_LIMIT}")


def _read_instance_repository(connection: sqlite3.Connection) -> str | None:
    """This start's Target Repository, or ``None`` where not recorded."""
    try:
        row = connection.execute(
            "SELECT target_repository FROM operator_instance WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _summaries_from_rows(
    connection: sqlite3.Connection, rows: list[Any]
) -> list[dict[str, Any]]:
    """AttemptSummaries for ``attempts`` rows, newest first as given."""
    try:
        outcomes = {
            str(row[0]): {"outcome": str(row[1]), "reason": str(row[2])}
            for row in connection.execute(
                "SELECT attempt_id, outcome, reason FROM outcomes"
            ).fetchall()
        }
    except sqlite3.Error:
        outcomes = {}
    summaries: list[dict[str, Any]] = []
    for row in rows:
        attempt_id = str(row[1])
        try:
            workflow, issue_number, attempt_number = split_attempt_id(attempt_id)
        except ValueError:
            continue
        entry: dict[str, Any] = {
            "attempt_id": attempt_id,
            "issue_number": issue_number,
            "attempt_number": attempt_number,
            "state": str(row[2]),
            "created_at": str(row[3]),
            "fingerprint": str(row[4]) if row[4] is not None else None,
            "outcome": outcomes.get(attempt_id),
        }
        if workflow is not None:
            entry["workflow_id"] = workflow
            entry["input_number"] = issue_number
        summaries.append(entry)
    return summaries


def _summaries_v1_for_page(
    ledger_path: Path,
    page_rows: list[Any],
    observed_at: str,
    *,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """``v1.0.4`` AttemptSummaries for one page's ledger rows, newest first.

    Each row contributes its Attempt id and lifecycle state; the summary
    itself (issue ref, outcome, delivery, sealed state) comes from the
    shared producer. Rows with malformed Attempt ids are skipped rather
    than failing the read. ``state_dir`` overrides the archive root for
    the sealed lookup (an Offline Read opens a ledger copy, so the copy's
    parent is not the state directory).
    """
    from coding_agent.operator.listener import _attempt_summary_v1

    summaries: list[dict[str, Any]] = []
    for row in page_rows:
        attempt_id = str(row[1])
        lifecycle_state = str(row[2])
        try:
            split_attempt_id(attempt_id)
        except ValueError:
            continue
        summary = _attempt_summary_v1(
            ledger_path,
            attempt_id,
            lifecycle_state,
            observed_at,
            state_dir=state_dir,
        )
        if summary is None:
            continue
        summaries.append(summary)
    return summaries


def list_attempts_page(
    ledger_path: Path, *, limit: int = DEFAULT_LIST_LIMIT, before: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of AttemptSummaries, newest first.

    An Attempt is listed as soon as its Claim opens: the rows come straight
    from the ledger's ``attempts`` table in reverse claim order. ``before``
    is the opaque cursor of the previous page's last row; the page holds
    attempts older than it. Returns ``(attempts, next_cursor)`` where
    ``next_cursor`` is the cursor for the following page, or ``None`` where
    this page is the last (contract ``v1.0.4`` ``attempts-list``). A missing
    ledger file means no Attempt ever opened: the page is empty. Raises
    :class:`InvalidCursor` for a cursor that does not decode and
    :class:`ValueError` for a ``limit`` outside ``1..100``.
    """
    from coding_agent.operator.contract import utc_now_z

    _check_limit(limit)
    bound: int | None = None
    if before is not None:
        bound = decode_before_cursor(before)
    if not ledger_path.is_file():
        return ([], None)
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {ledger_path}") from exc
    try:
        try:
            if bound is None:
                rows = connection.execute(
                    "SELECT rowid, attempt_id, state, created_at, fingerprint"
                    " FROM attempts ORDER BY rowid DESC LIMIT ?",
                    (limit + 1,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT rowid, attempt_id, state, created_at, fingerprint"
                    " FROM attempts WHERE rowid < ? ORDER BY rowid DESC LIMIT ?",
                    (bound, limit + 1),
                ).fetchall()
        except sqlite3.Error:
            return ([], None)
        page_rows = list(rows[:limit])
        observed_at = utc_now_z()
        summaries = _summaries_v1_for_page(
            ledger_path, page_rows, observed_at
        )
        if len(rows) > limit and page_rows:
            next_cursor: str | None = encode_before_cursor(int(page_rows[-1][0]))
        else:
            next_cursor = None
        return (summaries, next_cursor)
    finally:
        connection.close()


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


def _nullable_str(value: Any) -> str | None:
    """``value`` as text, or ``None`` where the column is NULL."""
    return str(value) if value is not None else None


def _parse_step_json_object(value: Any) -> dict[str, Any] | None:
    """``value`` parsed as a JSON object, or ``None`` where it is not one."""
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _step_projection_for(
    ledger_path: Path, attempt_id: str
) -> dict[str, Any] | None:
    """The §19 projection for a step-workflow Attempt, or ``None`` unknown.

    Reads the Attempt row, its outcome, Step Executions, step effects,
    transitions and pinned Effective Configuration through one fresh
    connection — the reader never touches the Worker's handles — and
    projects them through :mod:`coding_agent.operator.step_projection`.
    ``None`` where the Attempt row itself is missing. A released Claim
    carries no outcome and never requires attention.
    """
    from coding_agent.operator import step_projection as projection

    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT state, effective_configuration FROM attempts"
                " WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        state = str(row[0])
        effective = _parse_step_json_object(row[1]) or {}
        try:
            outcome_row = connection.execute(
                "SELECT outcome, reason FROM outcomes WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            outcome_row = None
        try:
            exec_rows = connection.execute(
                "SELECT step_id, seq, state, result, payload, facts, usage,"
                " limit_fact, entry_from_step, entry_result"
                " FROM step_executions WHERE attempt_id = ? ORDER BY rowid",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            exec_rows = []
        try:
            effect_rows = connection.execute(
                "SELECT step_id, execution_number, ordinal, status, remote_id"
                " FROM step_effects WHERE attempt_id = ?"
                " ORDER BY step_id, execution_number, ordinal",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            effect_rows = []
        try:
            transition_rows = connection.execute(
                "SELECT kind, from_step, result, to_step, to_end, reason"
                " FROM step_transitions WHERE attempt_id = ? ORDER BY id",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            transition_rows = []
    finally:
        connection.close()
    executions: list[dict[str, Any]] = [
        {
            "step_id": str(exec_row[0]),
            "seq": int(exec_row[1]),
            "state": str(exec_row[2]),
            "result": _nullable_str(exec_row[3]),
            "payload": _parse_step_json_object(exec_row[4]),
            "facts": _parse_step_json_object(exec_row[5]),
            "usage": _parse_step_json_object(exec_row[6]),
            "limit_fact": _parse_step_json_object(exec_row[7]),
            "entry_from_step": _nullable_str(exec_row[8]),
            "entry_result": _nullable_str(exec_row[9]),
        }
        for exec_row in exec_rows
    ]
    effects: list[dict[str, Any]] = [
        {
            "step_id": str(effect_row[0]),
            "execution_number": int(effect_row[1]),
            "ordinal": int(effect_row[2]),
            "status": str(effect_row[3]),
            "remote_id": _nullable_str(effect_row[4]),
        }
        for effect_row in effect_rows
    ]
    transitions: list[dict[str, Any]] = [
        {
            "kind": str(transition_row[0]),
            "from_step": _nullable_str(transition_row[1]),
            "result": _nullable_str(transition_row[2]),
            "to_step": _nullable_str(transition_row[3]),
            "to_end": _nullable_str(transition_row[4]),
            "reason": _nullable_str(transition_row[5]),
        }
        for transition_row in transition_rows
    ]
    recorded: tuple[str, str] | None = None
    if outcome_row is not None:
        recorded = (str(outcome_row[0]), str(outcome_row[1]))
    return projection.project_attempt(
        state=state,
        recorded=recorded,
        executions=executions,
        step_effects=effects,
        transitions=transitions,
        effective_configuration=effective,
    )


def show_attempt(
    *, ledger_path: Path, state_dir: Path, attempt_id: str
) -> dict[str, Any] | None:
    """The AttemptSummary plus manifest for ``attempt_id``, or ``None`` unknown.

    The summary is the contract ``v1.0.4`` AttemptSummary (the Attempt is
    shown as soon as its Claim opens, even while it runs). A Sealed archive
    reports its sealed manifest's entries with ``sealed: True`` and
    ``sealed_at``; an unsealed Attempt reports ``sealed: False`` with a
    manifest computed on read. A missing archive reads as an empty
    manifest. ``None`` means no Attempt is recorded for the id — the caller
    answers ``not_found``.
    """
    from coding_agent.operator.contract import to_z, utc_now_z
    from coding_agent.operator.listener import _attempt_summary_v1

    try:
        split_attempt_id(attempt_id)
    except ValueError:
        return None
    if not ledger_path.is_file():
        return None
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the Run Ledger at {ledger_path}") from exc
    try:
        try:
            row = connection.execute(
                "SELECT rowid, attempt_id, state, created_at, fingerprint"
                " FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        lifecycle_state = str(row[2])
        repository = _read_instance_repository(connection)
    finally:
        connection.close()
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
    observed_at = utc_now_z()
    summary = _attempt_summary_v1(
        ledger_path,
        attempt_id,
        lifecycle_state,
        observed_at,
        state_dir=state_dir,
    )
    if summary is None:
        return None
    summary["sealed"] = sealed
    summary["sealed_at"] = sealed_at
    return {"attempt": summary, "manifest": manifest}


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


def export_manifest_document(
    *, attempt_id: str, sealed: bool, sealed_at: str | None, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """The ``manifest.json`` document an export streams first."""

    return {
        "attempt_id": attempt_id,
        "sealed": sealed,
        "sealed_at": sealed_at,
        "entries": list(entries),
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
    import tarfile

    manifest_bytes = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tar:
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
