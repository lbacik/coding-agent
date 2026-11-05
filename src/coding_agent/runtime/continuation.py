"""Continuation: a new Attempt starting from the previous Attempt's pushed work.

Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (a human starts a
Continuation by re-applying the Selection Label after a Terminal Outcome; the
new Attempt runs on its own new branch, created at the previous head and
rebased onto the current Base Revision during ``readiness``; a rebase
conflict ends ``failed`` / ``continuation-conflict`` with ``ready-for-human``
and no model call; a deleted previous branch is a fresh start from the Base
Revision; the criteria come from the current body and the Effective
Configuration is the current one; the previous Continuation Snapshot is read
back from the previous terminal comment by marker lookup and verified by its
digest, and a tampered snapshot counts as absent; the snapshot is never
committed to the Target Project's tree) and §15 (the snapshot round-trips
through the terminal comment as a versioned block carrying its SHA-256
digest).

MVP row ids: P-HND-1, P-HND-2, P-HND-3, P-HND-4, W-PIN-4, REC-WRK-3.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coding_agent.runtime.redaction import Redactable

CONTINUATION_CONFLICT_REASON = "continuation-conflict"
"""The Terminal Outcome reason when the rebase onto the Base Revision fails.

Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (a rebase conflict
ends the Attempt ``failed`` / ``continuation-conflict`` with
``ready-for-human`` and no model call).

MVP row ids: P-HND-2.
"""

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_SNAPSHOT_HEADING = "## Continuation Snapshot"
_DIGEST_LINE_RE = re.compile(r"^- digest: `([0-9a-f]{64})`$")
_VERSION_LINE_RE = re.compile(r"^- version: (\d+)$")
_FENCED_JSON_RE = re.compile(
    r"```json\s*\n(.*?)\n```", re.DOTALL
)


class ContinuationConflict(Exception):
    """The previous head could not be rebased onto the Base Revision (§16)."""


@dataclass(frozen=True)
class ParsedSnapshotBlock:
    """A snapshot block read back from a terminal comment, verified (§15)."""

    snapshot_json: str
    digest: str
    version: int


def parse_snapshot_block(body: str) -> ParsedSnapshotBlock | None:
    """Parse and verify a terminal Continuation Snapshot block (§15, §16).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (the block holds
    the snapshot's SHA-256 digest beside its JSON) and §16 (the previous
    snapshot is read back through the terminal comment by marker lookup and
    its digest is verified; a snapshot that is absent or fails verification
    counts as none).

    MVP row ids: REC-WRK-3.

    Returns ``None`` where the body carries no block, or where the block
    fails verification: a digest line that is missing or malformed, a
    version that is missing or not a positive integer, JSON that does not
    parse as a snapshot, or a digest that does not cover the JSON bytes. A
    tampered snapshot — edited JSON beside a stale digest, or an edited
    digest beside intact JSON — is therefore treated as absent.
    """
    from coding_agent.runtime.context_handoff import ContinuationSnapshot

    heading_at = body.find(_SNAPSHOT_HEADING)
    if heading_at < 0:
        return None
    section = body[heading_at:]
    digest: str | None = None
    version: int | None = None
    for line in section.splitlines():
        digest_match = _DIGEST_LINE_RE.match(line.strip())
        if digest_match is not None:
            digest = digest_match.group(1)
            continue
        version_match = _VERSION_LINE_RE.match(line.strip())
        if version_match is not None:
            try:
                version = int(version_match.group(1))
            except ValueError:
                return None
    if digest is None or not _DIGEST_RE.match(digest):
        return None
    if version is None or version <= 0:
        return None
    fenced = _FENCED_JSON_RE.search(section)
    if fenced is None:
        return None
    snapshot_json = fenced.group(1).strip()
    if not snapshot_json:
        return None
    if ContinuationSnapshot.digest_for(snapshot_json) != digest:
        return None
    try:
        snapshot = ContinuationSnapshot.from_json(snapshot_json)
    except Exception:
        return None
    if snapshot.digest != digest or snapshot.version != version:
        return None
    return ParsedSnapshotBlock(
        snapshot_json=snapshot_json, digest=digest, version=version
    )


def read_previous_snapshot(
    tracker: object,
    issue_number: int,
    previous_attempt_id: str,
) -> ParsedSnapshotBlock | None:
    """Read the previous Attempt's snapshot back through its comment (§16).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (the previous
    Continuation Snapshot is read back from the previous Attempt's terminal
    comment through the paginated, author- and marker-filtered lookup, and
    its digest is verified) and §15 (the block carries its digest).

    MVP row ids: REC-WRK-3, P-HND-1.

    A snapshot that is absent or fails verification counts as none: the
    Attempt continues from the branch and its diff against the base
    (P-HND-4). Never raises for a tampered block — it returns ``None``.
    """
    from coding_agent.runtime.publication import TERMINAL_COMMENT_KIND, attempt_marker

    find = getattr(tracker, "find_terminal_comment", None)
    if find is None:
        return None
    marker = attempt_marker(previous_attempt_id, kind=TERMINAL_COMMENT_KIND)
    try:
        comment = find(issue_number, marker)
    except Exception:
        return None
    if comment is None:
        return None
    body = getattr(comment, "body", "")
    if not isinstance(body, str):
        return None
    return parse_snapshot_block(body)


_PUSH_CANDIDATE_SUFFIXES = ("-push",)

_PUSH_CANDIDATE_PREFIXES = ("review-snapshot-push/",)


def _effect_branch(payload: object) -> str | None:
    """The branch one effect pushed, or ``None`` where it pushed none (§13)."""
    if not isinstance(payload, dict):
        return None
    branch = payload.get("branch")
    if isinstance(branch, str) and branch:
        return branch
    return None


def _is_push_write_kind(write_kind: str) -> bool:
    """Whether an effects write kind records a branch push (§13, §16)."""
    if write_kind.endswith("-push"):
        return True
    return any(
        write_kind.startswith(prefix) for prefix in _PUSH_CANDIDATE_PREFIXES
    )


def previous_pushed_attempt(
    ledger: object,
    issue_number: int,
    attempt_number: int,
) -> tuple[str, str] | None:
    """The most recent previous Attempt that pushed a branch, if any (§16).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (the previous
    work is the head of the branch pushed by the most recent previous
    Attempt that pushed one; if that branch no longer exists on the remote,
    there is no previous work).

    MVP row ids: P-HND-1, P-HND-3.

    Scans ``#<issue>/<n'>`` for ``n'`` below ``attempt_number`` from newest
    to oldest and returns the first ``(attempt_id, branch)`` with a push
    effect carrying a branch. ``None`` where no previous Attempt pushed:
    the new Attempt starts from the Base Revision. The caller checks the
    remote: a branch the remote no longer holds is a fresh start
    (P-HND-3), and it does not fall back to an older branch.
    """
    list_effects = getattr(ledger, "list_effects", None)
    if list_effects is None:
        return None
    for previous_n in range(attempt_number - 1, 0, -1):
        previous_id = f"#{issue_number}/{previous_n}"
        try:
            effects = list_effects(previous_id)
        except Exception:
            continue
        for effect in effects:
            write_kind = getattr(effect, "write_kind", "")
            if not isinstance(write_kind, str) or not _is_push_write_kind(
                write_kind
            ):
                continue
            branch = _effect_branch(getattr(effect, "payload", None))
            if branch is not None:
                return (previous_id, branch)
    return None


@dataclass(frozen=True)
class ContinuationPrep:
    """What ``readiness`` resolved about the previous Attempt's work (§16)."""

    previous_attempt_id: str | None
    """The most recent previous Attempt that pushed, if any."""

    previous_branch: str | None
    """That Attempt's branch, if any."""

    previous_head: str | None
    """That branch's remote head, or ``None`` where it is gone (P-HND-3)."""

    snapshot_json: str | None
    """The verified previous snapshot, or ``None`` where absent (P-HND-4)."""

    snapshot_digest: str | None
    snapshot_version: int | None
    from_base: bool
    """Whether the new Attempt starts from the Base Revision (§16)."""


def prepare_continuation(
    *,
    ledger: object,
    tracker: object,
    workspace_path: Path,
    issue_number: int,
    attempt_number: int,
    new_branch: str,
    base_revision: str,
    remote_ref_sha: Callable[[str], str | None] | None = None,
    redact: Redactable = None,
    helper: object = None,
) -> ContinuationPrep:
    """Create the new branch from the previous head, rebased onto base (§16).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (the new Attempt
    runs on its own new branch, created at the previous head and rebased
    onto the current Base Revision during ``readiness``; a rebase conflict
    ends the Attempt ``failed`` / ``continuation-conflict`` with
    ``ready-for-human`` and no model call; a deleted previous branch starts
    the new Attempt from the Base Revision).

    MVP row ids: P-HND-1, P-HND-2, P-HND-3, P-HND-4, REC-WRK-3.

    - No previous push → ``from_base`` with no snapshot and no git work:
      the Attempt starts from the Base Revision.
    - The most recent previous branch is gone on the remote → ``from_base``
      (P-HND-3): a deleted branch is a fresh start, and the stale snapshot
      is not carried.
    - Otherwise the new branch is created at the previous head and rebased
      onto the Base Revision; a rebase failure aborts the rebase and raises
      :class:`ContinuationConflict` (P-HND-2). The verified snapshot rides
      along where one exists, and ``None`` where the comment carries none
      or a tampered one (P-HND-4, REC-WRK-3).
    """
    from coding_agent.runtime import git as git_ops

    found = previous_pushed_attempt(ledger, issue_number, attempt_number)
    if found is None:
        return ContinuationPrep(
            previous_attempt_id=None,
            previous_branch=None,
            previous_head=None,
            snapshot_json=None,
            snapshot_digest=None,
            snapshot_version=None,
            from_base=True,
        )
    previous_attempt_id, previous_branch = found
    previous_head: str | None = None
    if remote_ref_sha is not None:
        try:
            previous_head = remote_ref_sha(previous_branch)
        except Exception:
            previous_head = None
    if previous_head is None:
        return ContinuationPrep(
            previous_attempt_id=previous_attempt_id,
            previous_branch=previous_branch,
            previous_head=None,
            snapshot_json=None,
            snapshot_digest=None,
            snapshot_version=None,
            from_base=True,
        )
    parsed = read_previous_snapshot(tracker, issue_number, previous_attempt_id)
    try:
        git_ops.create_branch_at(workspace_path, new_branch, previous_head)
    except Exception as exc:
        raise ContinuationConflict(
            f"could not create branch {new_branch} at {previous_head}: {exc}"
        ) from exc
    try:
        git_ops.rebase_onto(workspace_path, base_revision)
    except Exception as exc:
        raise ContinuationConflict(
            f"rebase of {previous_branch} onto {base_revision} failed: {exc}"
        ) from exc
    _ = (redact, helper)
    if parsed is None:
        return ContinuationPrep(
            previous_attempt_id=previous_attempt_id,
            previous_branch=previous_branch,
            previous_head=previous_head,
            snapshot_json=None,
            snapshot_digest=None,
            snapshot_version=None,
            from_base=False,
        )
    return ContinuationPrep(
        previous_attempt_id=previous_attempt_id,
        previous_branch=previous_branch,
        previous_head=previous_head,
        snapshot_json=parsed.snapshot_json,
        snapshot_digest=parsed.digest,
        snapshot_version=parsed.version,
        from_base=False,
    )


def continuation_absent_note(
    *,
    previous_attempt_id: str | None,
    previous_branch: str | None,
    new_branch: str,
    diff_stat: str,
) -> str:
    """The first-step note where no verified snapshot exists (§16, P-HND-4).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (a snapshot
    that is absent or fails verification counts as none: the Attempt
    continues from the branch and its diff against the base, and the Attempt
    Header says so).

    MVP row ids: P-HND-4.
    """
    lines = [
        "# Continuation (no verified snapshot)",
        "",
        "No verified Continuation Snapshot exists for this Attempt: "
        "continue from the branch and its diff against the base.",
    ]
    if previous_attempt_id is not None:
        lines.append(f"Previous Attempt: {previous_attempt_id}")
    if previous_branch is not None:
        lines.append(f"Previous branch: `{previous_branch}`")
    lines.append(f"Attempt branch: `{new_branch}`")
    if diff_stat.strip():
        lines.append("")
        lines.append("Diff against the base (`git diff --stat <base>...HEAD`):")
        lines.append("")
        lines.append("```")
        lines.append(diff_stat.strip())
        lines.append("```")
    else:
        lines.append("Diff against the base: none (the tree matches the base).")
    return "\n".join(lines)
