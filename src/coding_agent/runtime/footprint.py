"""The Execution Footprint: what executed project code changed, per tool call.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 / §12 (after every
tool call that executes project code — ``test_targeted`` and ``run_command``
— the runtime records the Attempt's Execution Footprint for that call: the
workspace paths the call changed, with content digests before and after. It
goes to the Run Ledger, to the tool result the model sees, and, bounded, to
the pull-request body. It gates nothing).

Scope: the footprint watches the git-visible tree only — tracked files plus
untracked files that are not gitignored, never ``.git/`` and never ignored
paths. That is the same set the final-checks dirty-tree rule uses (§12,
``dirty_working_tree_files``), so ``__pycache__``, ``.pytest_cache`` and the
like never appear, and a run touching only ignored paths has an empty
footprint. Capture never hashes the whole tree: it digests the dirty set
(from ``git status``, untracked files included) before and after, and treats
clean-before-and-after paths as unchanged. A path that is dirty before and
after with identical content does not count; one already dirty before the
call and rewritten with different content during it does.

Limits (contract §12): no intermediate content, no writes outside the
workspace, no writes to gitignored paths, no change undone within the same
call, and no proof against code that hides its effects.

MVP row ids: C-FP-1, C-FP-2, C-FP-3, C-FP-4, C-FP-5, C-FP-6, C-FP-7.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coding_agent.ledger.store import BudgetSnapshot, LedgerRecord, RunLedger

FOOTPRINT_KIND = "execution_footprint"
"""Run Ledger event kind for one tool call's Execution Footprint (§9, §12)."""

FOOTPRINT_PR_LIMIT = 50
"""At most this many footprint paths are listed in the pull-request body (§15).

Anything beyond that is replaced by an explicit truncation marker giving the
total count, never cut silently.
"""

FootprintChange = Literal["added", "modified", "deleted"]


@dataclass(frozen=True)
class FootprintEntry:
    """One workspace path a tool call changed, with content digests.

    ``change`` is ``added`` (absent before), ``deleted`` (absent after) or
    ``modified`` (present both times with different digests). ``digest_before``
    is absent for an add; ``digest_after`` is absent for a delete. Digests
    are SHA-256 hex over the file bytes.
    """

    path: str
    change: FootprintChange
    digest_before: str | None
    digest_after: str | None

    def as_dict(self) -> dict[str, object]:
        """The JSON-ready record stored in the Run Ledger event payload.

        A digest that is absent (an add's "before", a delete's "after") is
        omitted from the record rather than stored as null.
        """
        record: dict[str, object] = {"path": self.path, "change": self.change}
        if self.digest_before is not None:
            record["digest_before"] = self.digest_before
        if self.digest_after is not None:
            record["digest_after"] = self.digest_after
        return record


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _status_paths(status_output: str) -> list[str]:
    """Parse ``git status --porcelain`` output into workspace-relative paths."""
    paths: list[str] = []
    for line in status_output.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.strip()
        # Handle rename/copy entries of the form ``old -> new``.
        for part in rest.split(" -> "):
            candidate = part.strip().strip('"')
            if candidate:
                paths.append(candidate)
    return paths


def dirty_paths(workspace: Path) -> tuple[str, ...]:
    """The git-visible dirty paths: modified, added or deleted (§12 scope).

    This is the same set the final-checks dirty-tree rule uses: ``git status
    --porcelain --untracked-files=all`` reports tracked changes plus
    untracked, non-ignored files, never ``.git/`` and never ignored paths.
    An unreadable tree reports no paths rather than failing the tool call.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(sorted(set(_status_paths(completed.stdout))))


def _digest_working_file(workspace: Path, path: str) -> str | None:
    """The SHA-256 of one working-tree file, or ``None`` where absent."""
    target = workspace / path
    try:
        if not target.is_file():
            return None
        return _sha256_bytes(target.read_bytes())
    except (OSError, ValueError):
        return None


def _digest_head_blob(workspace: Path, path: str) -> str | None:
    """The SHA-256 of ``path`` as committed at ``HEAD``, or ``None``.

    ``None`` means the path is not tracked at ``HEAD``: a clean path that is
    absent from ``HEAD`` is a path that did not exist (an add's "before" or
    a removal that stays removed).
    """
    try:
        completed = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            cwd=workspace,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return _sha256_bytes(completed.stdout)


def collect_dirty_digests(workspace: Path) -> dict[str, str | None]:
    """Digest every git-visible dirty path, without hashing the whole tree.

    Only paths ``dirty_paths`` reports are read. A dirty path that names a
    directory (a wholly untracked directory in collapsed form) is expanded
    to its files. A path whose content cannot be read maps to ``None``,
    which compares unequal to any digest and therefore counts as changed —
    an unreadable state never silently reports a clean call.
    """
    digests: dict[str, str | None] = {}
    for path in dirty_paths(workspace):
        if path == ".git" or path.startswith(".git/"):
            continue
        target = workspace / path
        try:
            is_dir = target.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            try:
                members = sorted(
                    member.relative_to(workspace).as_posix()
                    for member in target.rglob("*")
                    if member.is_file()
                )
            except OSError:
                members = []
            for member in members:
                if member == ".git" or member.startswith(".git/"):
                    continue
                digests[member] = _digest_working_file(workspace, member)
        else:
            digests[path] = _digest_working_file(workspace, path)
    return digests


def compute_footprint(
    before: dict[str, str | None],
    after: dict[str, str | None],
    workspace: Path,
) -> tuple[FootprintEntry, ...]:
    """Diff two dirty-digest snapshots into footprint entries, sorted by path.

    A path absent from one snapshot was clean on that side: its digest is
    resolved from ``HEAD`` (tracked content) or ``None`` (did not exist), so
    a clean-before file rewritten during the call still carries its "before"
    digest. A path clean on both sides never appears in either snapshot and
    counts as unchanged. Identical digests on both sides do not count,
    including a path dirty before and after with identical content.
    """
    entries: list[FootprintEntry] = []
    for path in sorted(set(before) | set(after)):
        digest_before = before[path] if path in before else _digest_head_blob(workspace, path)
        digest_after = after[path] if path in after else _digest_head_blob(workspace, path)
        if digest_before == digest_after:
            continue
        if digest_before is None:
            change: FootprintChange = "added"
        elif digest_after is None:
            change = "deleted"
        else:
            change = "modified"
        entries.append(
            FootprintEntry(
                path=path,
                change=change,
                digest_before=digest_before,
                digest_after=digest_after,
            )
        )
    return tuple(entries)


def capture_footprint(
    workspace: Path,
    before: dict[str, str | None],
) -> tuple[FootprintEntry, ...]:
    """Digest the after state and diff it against ``before`` (one tool call).

    ``before`` comes from ``collect_dirty_digests`` taken just before the
    call. Never raises: an unreadable after state yields an empty footprint
    rather than failing the tool call it observes.
    """
    try:
        after = collect_dirty_digests(workspace)
    except Exception:
        return ()
    try:
        return compute_footprint(before, after, workspace)
    except Exception:
        return ()


def format_footprint_suffix(entries: tuple[FootprintEntry, ...]) -> str:
    """The extra tool-result part naming what the call changed (§9, §12).

    One part naming the changed paths and their change kinds. Empty input
    yields an empty string, so a call that changed nothing leaves the tool
    result byte-for-byte what it is without a footprint.
    """
    if not entries:
        return ""
    noun = "path" if len(entries) == 1 else "paths"
    lines = [
        "",
        "",
        f"Execution Footprint: this call changed {len(entries)} workspace {noun}:",
    ]
    lines.extend(f"- {entry.path} ({entry.change})" for entry in entries)
    return "\n".join(lines)


def record_footprint_event(
    ledger: RunLedger,
    attempt_id: str,
    snapshot: BudgetSnapshot,
    *,
    tool: str = "test_targeted",
    arg: str = "",
    test_path: str | None = None,
    entries: tuple[FootprintEntry, ...],
) -> LedgerRecord:
    """Record one tool call's footprint as a Run Ledger event (§9, §12).

    Calls with an empty footprint are recorded too, with an empty list. The
    payload carries the tool name, its argument and the ``{path, change,
    digest_before, digest_after}`` entries, with an absent digest omitted
    rather than null. ``test_path`` is the retired ``test_targeted``-only
    spelling of the argument: where given it is the argument, and the payload
    keeps the ``test_path`` key so existing ``test_targeted`` events keep
    their meaning. Redaction is the ledger's own
    (§18): this function passes the payload through unredacted.
    """
    if test_path is not None:
        tool = "test_targeted"
        arg = test_path
    count = len(entries)
    noun = "path" if count == 1 else "paths"
    payload: dict[str, object] = {
        "tool": tool,
        "arg": arg,
        "footprint": [entry.as_dict() for entry in entries],
    }
    if tool == "test_targeted":
        payload["test_path"] = arg
    return ledger.record(
        attempt_id,
        FOOTPRINT_KIND,
        snapshot,
        detail=(
            f"execution footprint for {tool} {arg!r}: "
            f"{count} {noun} changed" if count else
            f"execution footprint for {tool} {arg!r}: no paths changed"
        ),
        payload=payload,
    )


def footprint_paths_from_records(records: tuple[LedgerRecord, ...]) -> list[str]:
    """The union of footprint paths over footprint events, deduplicated.

    Reads ``FOOTPRINT_KIND`` events in ledger order and returns each path
    once, sorted, for the pull-request body (§15).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for record in records:
        if record.kind != FOOTPRINT_KIND:
            continue
        raw = record.payload.get("footprint")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path or path in seen:
                continue
            seen.add(path)
            ordered.append(path)
    return sorted(ordered)


def footprint_paths_for_attempt(ledger: RunLedger, attempt_id: str) -> list[str]:
    """The deduplicated footprint paths of one Attempt, read from its ledger."""
    return footprint_paths_from_records(ledger.records(attempt_id))


def render_footprint_section(paths: list[str], *, limit: int = FOOTPRINT_PR_LIMIT) -> str:
    """The §15 pull-request section listing paths changed by executed code.

    Lists at most ``limit`` paths; anything beyond that is replaced by an
    explicit truncation marker giving the total count, never cut silently.
    Where no call had a footprint the section says so.
    """
    lines = ["## Execution Footprint"]
    if not paths:
        lines.append("(no workspace paths changed by executed project code)")
        return "\n".join(lines)
    for path in paths[:limit]:
        lines.append(f"- {path}")
    if len(paths) > limit:
        lines.append(f"... and {len(paths) - limit} more ({len(paths)} total)")
    return "\n".join(lines)
