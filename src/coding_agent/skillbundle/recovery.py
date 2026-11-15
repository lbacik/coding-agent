"""Recovery from the Attempt Archive: pinned identity plus archived blobs.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §16 (a restart
never enters the Workflow graph and never runs a Workflow Step; recovery uses
pinned values only) and §2.10 (the Attempt records its Effective Configuration
in full).

Matrix row ids: SKV3-29, SKV3-30, SKV3-31.

A restart finishes a recorded outcome from the pinned identity and the
archived blobs, even after the deployment has changed and the former store or
image is gone. It never repairs history from current files and does not
replay the graph or the model:

- ``validate_archive_for_attempt`` checks the record version, the full
  ``version: 2`` schema, the core digest, the reference-index consistency and
  every required blob's digest and size (prompts and activations carry their
  pinned ``size_bytes``; component blobs are digest-checked, their sizes are
  not pinned in the ``version: 2`` core).
- ``ensure_archive_for_recovery`` resolves the Attempt Archive through the
  ledger and validates it, so the Worker-loop seam settles effects and
  finalisation from the archive (SKV3-29) and refuses recovery naming the
  Attempt and the fault on a missing, corrupt, escaping, symlinked or
  incompatible core or blob (SKV3-30).
- No partial success: validation runs before any settlement for the Attempt,
  so a refused Attempt keeps its ledger state, outcome and tracker writes
  untouched. No current-file repair (the current deployment is never read),
  no secret restoration (``not-delivered`` digests stay pinned only, their
  bodies are never loaded), and no skipped integrity gate (both the
  Worker-loop entry and the settlement entry validate).
- An incompatible or incomplete open legacy record is refused, never
  converted or given fabricated archives; closed legacy history stays
  listable, viewable and exportable with existing numbering (SKV3-31): this
  module never creates archives and never converts records, it only reads.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coding_agent.operator.attempts import CONTENT_DIR_NAME

if TYPE_CHECKING:
    from coding_agent.ledger.store import RunLedger


class ArchiveRecoveryRefused(Exception):
    """An unfinished Attempt whose archive cannot be recovered from (§16).

    Names the Attempt and the fault: the message carries
    ``("<attempt-id>", "<fault>")`` so the Worker halt names both (SKV3-30).
    The caller turns this into the startup halt; recovery never guesses from
    the current configuration and never fabricates archives.
    """

    def __init__(self, attempt_id: str, fault: str, detail: str = "") -> None:
        self.attempt_id = attempt_id
        self.fault = fault
        self.detail = detail
        message = f"recovery refused for {attempt_id}: {fault}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def _is_hex_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _normalize_index(index: Mapping[str, object]) -> dict[str, object]:
    """The content index with its three sections present as dicts."""
    normalized: dict[str, object] = {
        "prompts": {},
        "activations": {},
        "components": {},
    }
    for key in ("prompts", "activations", "components"):
        section = index.get(key)
        if isinstance(section, Mapping):
            normalized[key] = dict(section)
    return normalized


def _check_core_paths(record: Mapping[str, object], attempt_id: str) -> None:
    """Refuse an escaping core path (SKV3-30).

    Prompt locators, member names and component paths are archive-relative:
    an absolute path or a ``..`` segment would escape the archive were it
    ever materialised, so such a core is refused rather than used.
    """
    prompts = record.get("prompts")
    if isinstance(prompts, Mapping):
        for locator in prompts.keys():
            if not isinstance(locator, str) or not locator:
                raise ArchiveRecoveryRefused(
                    attempt_id, "incompatible-core", f"bad prompt locator {locator!r}"
                )
            if locator.startswith("/") or locator.startswith("\\"):
                raise ArchiveRecoveryRefused(
                    attempt_id, "escaping-core", f"prompt locator {locator!r} escapes"
                )
            if ".." in locator.split("/"):
                raise ArchiveRecoveryRefused(
                    attempt_id, "escaping-core", f"prompt locator {locator!r} escapes"
                )
    bundle = record.get("skill_bundle")
    if isinstance(bundle, Mapping):
        members = bundle.get("members")
        if isinstance(members, Mapping):
            for name, member in members.items():
                if not isinstance(name, str) or not name:
                    raise ArchiveRecoveryRefused(
                        attempt_id, "incompatible-core", f"bad member name {name!r}"
                    )
                if name.startswith("/") or ".." in name.split("/"):
                    raise ArchiveRecoveryRefused(
                        attempt_id, "escaping-core", f"member {name!r} escapes"
                    )
                if not isinstance(member, Mapping):
                    continue
                files = member.get("files")
                if not isinstance(files, list):
                    continue
                for item in files:
                    if not isinstance(item, Mapping):
                        continue
                    path = item.get("path")
                    if not isinstance(path, str) or not path:
                        raise ArchiveRecoveryRefused(
                            attempt_id, "incompatible-core", f"bad component path {path!r}"
                        )
                    if path.startswith("/") or path.startswith("\\"):
                        raise ArchiveRecoveryRefused(
                            attempt_id, "escaping-core", f"component path {path!r} escapes"
                        )
                    if ".." in path.split("/"):
                        raise ArchiveRecoveryRefused(
                            attempt_id, "escaping-core", f"component path {path!r} escapes"
                        )


def _required_blobs(
    record: Mapping[str, object], index: Mapping[str, object]
) -> list[tuple[str, int | None, str]]:
    """Every required blob: ``(digest, size_or_None, label)`` in order."""
    required: list[tuple[str, int | None, str]] = []

    def _record_size(section: object, key: str) -> int | None:
        if isinstance(section, Mapping):
            entry = section.get(key)
            if isinstance(entry, Mapping):
                size = entry.get("size_bytes")
                if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                    return size
        return None

    prompts_record = record.get("prompts")
    prompts_index = index.get("prompts")
    if isinstance(prompts_index, Mapping):
        for locator in sorted(prompts_index.keys()):
            entry = prompts_index[locator]
            if not isinstance(entry, Mapping):
                continue
            if entry.get("archived") is not True:
                continue
            digest = entry.get("sha256")
            if not isinstance(digest, str):
                continue
            size = _record_size(prompts_record, locator)
            if isinstance(entry.get("size_bytes"), int) and size is None:
                candidate = entry["size_bytes"]
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate >= 0
                ):
                    size = candidate
            required.append((digest, size, f"prompt {locator}"))

    activations_record = record.get("activations")
    activations_index = index.get("activations")
    if isinstance(activations_index, Mapping):
        for name in sorted(activations_index.keys()):
            entry = activations_index[name]
            if not isinstance(entry, Mapping):
                continue
            if entry.get("archived") is not True:
                continue
            digest = entry.get("sha256")
            if not isinstance(digest, str):
                continue
            size = _record_size(activations_record, name)
            if isinstance(entry.get("size_bytes"), int) and size is None:
                candidate = entry["size_bytes"]
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate >= 0
                ):
                    size = candidate
            required.append((digest, size, f"activation {name}"))

    components_index = index.get("components")
    if isinstance(components_index, Mapping):
        for name in sorted(components_index.keys()):
            per_member = components_index[name]
            if not isinstance(per_member, Mapping):
                continue
            for path in sorted(per_member.keys()):
                entry = per_member[path]
                if not isinstance(entry, Mapping):
                    continue
                if entry.get("archived") is not True:
                    continue
                digest = entry.get("sha256")
                if not isinstance(digest, str):
                    continue
                required.append((digest, None, f"component {name}/{path}"))
    # Deduplicate by digest: identical bytes share one blob.
    seen: set[str] = set()
    ordered: list[tuple[str, int | None, str]] = []
    sizes: dict[str, int | None] = {}
    for digest, size, label in required:
        if digest not in seen:
            seen.add(digest)
            ordered.append((digest, size, label))
            sizes[digest] = size
        elif size is not None and sizes.get(digest) is None:
            # A later reference pins a size the first did not; keep it.
            for position, (existing_digest, _, existing_label) in enumerate(ordered):
                if existing_digest == digest:
                    ordered[position] = (digest, size, existing_label)
                    break
            sizes[digest] = size
    return ordered


def validate_archive_for_attempt(
    *,
    archive_dir: Path | None,
    record: Mapping[str, object],
    stored_digest: str,
    stored_refs: Mapping[str, object],
    attempt_id: str,
) -> dict[str, bytes]:
    """Validate the pinned identity and its archived blobs (SKV3-29/30).

    Checks the record version, the full ``version: 2`` schema, the core
    digest, the reference-index consistency and every required blob's digest
    and size. Returns the required blobs by digest, proving the archive
    holds what settlement and finalisation need. Raises
    :class:`ArchiveRecoveryRefused` naming the Attempt and the fault on any
    missing, corrupt, escaping, symlinked or incompatible core or blob; the
    caller settles nothing for the Attempt on refusal (no partial success).
    Never reads the current deployment and never writes blobs (no
    current-file repair, no secret restoration).
    """
    from coding_agent.ledger.store import digest_effective_configuration
    from coding_agent.workflow.effective import (
        EFFECTIVE_CONFIGURATION_VERSION,
        content_index_for_record,
        is_full_effective_configuration,
    )

    version = record.get("version")
    if version != EFFECTIVE_CONFIGURATION_VERSION:
        raise ArchiveRecoveryRefused(
            attempt_id,
            "incompatible-version",
            f"record version {version!r} is not {EFFECTIVE_CONFIGURATION_VERSION}",
        )
    if not is_full_effective_configuration(record):
        raise ArchiveRecoveryRefused(
            attempt_id,
            "incompatible-schema",
            "the pinned Effective Configuration lacks the full version-2 shape",
        )
    computed_digest = digest_effective_configuration(record)
    if computed_digest != stored_digest:
        raise ArchiveRecoveryRefused(
            attempt_id,
            "core-digest-mismatch",
            "the pinned Effective Configuration digest does not match the ledger",
        )
    computed_index = content_index_for_record(record)
    if _normalize_index(computed_index) != _normalize_index(stored_refs):
        raise ArchiveRecoveryRefused(
            attempt_id,
            "reference-index-mismatch",
            "the content-reference index does not match the pinned record",
        )
    _check_core_paths(record, attempt_id)
    normalized = _normalize_index(computed_index)
    required = _required_blobs(record, normalized)
    if not required:
        return {}
    if archive_dir is None:
        raise ArchiveRecoveryRefused(
            attempt_id,
            "missing-archive",
            "the Attempt Archive cannot be resolved but blobs are required",
        )
    content_dir = archive_dir / CONTENT_DIR_NAME
    try:
        if content_dir.is_symlink():
            raise ArchiveRecoveryRefused(
                attempt_id,
                "symlinked-blob",
                f"{CONTENT_DIR_NAME} is a symlink",
            )
    except OSError as exc:
        raise ArchiveRecoveryRefused(
            attempt_id, "unreadable-blob", f"cannot inspect {CONTENT_DIR_NAME}: {exc}"
        ) from exc
    blobs: dict[str, bytes] = {}
    for digest, size, label in required:
        if not _is_hex_digest(digest, 64):
            raise ArchiveRecoveryRefused(
                attempt_id, "incompatible-blob-ref", f"{label} has a bad digest"
            )
        if "/" in digest or "\\" in digest or ".." in digest:
            raise ArchiveRecoveryRefused(
                attempt_id, "escaping-blob", f"{label} escapes the archive"
            )
        candidate = content_dir / digest
        if candidate.parent != content_dir:
            raise ArchiveRecoveryRefused(
                attempt_id, "escaping-blob", f"{label} escapes the archive"
            )
        try:
            if candidate.is_symlink():
                raise ArchiveRecoveryRefused(
                    attempt_id, "symlinked-blob", f"{label} is a symlink"
                )
        except OSError as exc:
            raise ArchiveRecoveryRefused(
                attempt_id, "unreadable-blob", f"{label} cannot be inspected: {exc}"
            ) from exc
        try:
            is_file = candidate.is_file()
        except OSError as exc:
            raise ArchiveRecoveryRefused(
                attempt_id, "unreadable-blob", f"{label} cannot be inspected: {exc}"
            ) from exc
        if not is_file:
            try:
                exists = candidate.exists()
            except OSError:
                exists = False
            if exists:
                raise ArchiveRecoveryRefused(
                    attempt_id, "incompatible-blob", f"{label} is not a file"
                )
            raise ArchiveRecoveryRefused(
                attempt_id, "missing-blob", f"{label} {digest} is missing"
            )
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise ArchiveRecoveryRefused(
                attempt_id, "corrupt-blob", f"{label} cannot be read: {exc}"
            ) from exc
        if size is not None and len(raw) != size:
            raise ArchiveRecoveryRefused(
                attempt_id,
                "size-mismatch",
                f"{label} has {len(raw)} bytes, expected {size}",
            )
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ArchiveRecoveryRefused(
                attempt_id, "digest-mismatch", f"{label} digest does not match"
            )
        blobs[digest] = raw
    return blobs


def ensure_archive_for_recovery(
    *, ledger: RunLedger, attempt_id: str
) -> dict[str, bytes]:
    """Resolve and validate the archive for ``attempt_id`` (SKV3-29/30).

    Loads the Attempt row the Claim pinned, resolves its Attempt Archive
    through the ledger and validates the pinned identity plus every required
    blob. Returns the required blobs by digest. Raises
    :class:`ArchiveRecoveryRefused` naming the Attempt and the fault; the
    caller settles nothing on refusal. Never creates archives and never
    converts records (SKV3-31): an incompatible or incomplete open legacy
    record is refused here through the version/schema gates.
    """
    try:
        stored = ledger.get_attempt(attempt_id)
    except AssertionError as exc:
        raise ArchiveRecoveryRefused(
            attempt_id, "missing-record", "no Attempt row is recorded"
        ) from exc
    except Exception as exc:
        raise ArchiveRecoveryRefused(
            attempt_id, "missing-record", f"the Attempt row cannot be read: {exc}"
        ) from exc
    try:
        archive_dir = ledger.archive_dir_for(attempt_id)
    except Exception:
        archive_dir = None
    record: Mapping[str, object] = stored.effective_configuration
    stored_refs: Mapping[str, object] = stored.content_refs
    if not isinstance(stored_refs, Mapping):
        stored_refs = {}
    return validate_archive_for_attempt(
        archive_dir=archive_dir,
        record=record,
        stored_digest=stored.effective_configuration_digest,
        stored_refs=stored_refs,
        attempt_id=attempt_id,
    )


__all__ = [
    "ArchiveRecoveryRefused",
    "ensure_archive_for_recovery",
    "validate_archive_for_attempt",
]
