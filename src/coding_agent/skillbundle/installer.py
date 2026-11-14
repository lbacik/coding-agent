"""Installer-adapter port for the Skill Bundle deployment path (SKV3).

Contract: the installation/provenance resolution (issue #373) and the
extended agent-installer machine interface (agent-installer#74). One
versioned adapter with three operations: install one source group at an
immutable commit with exact selectors, produce complete per-member
outcomes, and verify a declared subset offline without network access.

This ticket ships an in-process fake that writes a managed store and its
provenance metadata; binding the real ``agent-installer`` binary is a later
ticket. The fake performs no subprocess, no shell interpolation and no
network access. Diagnostics never echo credential-bearing locators: they
name the YAML path and the rejected component, never the raw value.

Managed-store layout (selected by ``--home``):

- Base Store: ``<home>/.agents/skills/<name>/`` holds one directory per
  managed skill, containing its complete recursive content plus the single
  exempt installer ownership file ``.agent-installer.json``.
- Provenance: ``<home>/.agents/agent-installer/state.json`` (the real
  installer's ``version: 3`` shape, ``entries`` with ``sourceRoot``,
  ``relativeSourcePath``, ``resolvedCommit``, ``sourceHash`` and
  ``installedHash``) records the previously verified per-member provenance
  that offline adoption re-verifies by rehashing current bytes.

Hash algorithm ``agent-installer-sha256-v1`` (versioned, documented here):
sorted artifact-relative paths (UTF-8 bytes order); for each path emit the
path, a newline, ``x`` when any executable bit is set else ``-``, another
newline, then the complete raw file bytes. The exempt ownership file is
excluded. Symlinks and special files are refused rather than hashed.
``agents/openai.yaml`` materialised by the invocation-policy translation is
an Overlay: when ``SKILL.md`` declares ``disable-model-invocation: true``
it is recorded as overlay ``codex-invocation-policy/1`` covering that path;
its resulting bytes remain in the recursive inventory.

Matrix row ids: SKV3-10, SKV3-11, SKV3-12 (part).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ADAPTER_VERSION = 1
"""Version of this installer-adapter port (SKV3-11)."""

FAKE_INSTALLER_VERSION = "fake-installer-1"
"""``producer.installerVersion`` emitted for fake-backed preparations."""

FAKE_REPORT_SCHEMA_VERSION = 1
"""Supported installer report schema version (matches records validator)."""

INSTALLER_HASH_ALGORITHM = "agent-installer-sha256-v1"
"""Versioned installer content-hash identifier recorded in the manifest."""

OVERLAY_ID = "codex-invocation-policy"
OVERLAY_VERSION = "1"
OVERLAY_PATH = "agents/openai.yaml"

EXEMPT_METADATA_NAMES = frozenset({".agent-installer.json"})
"""Only versioned, documented installer metadata exempt from the inventory."""


@dataclass(frozen=True)
class InstallerFault:
    """One installer-port refusal naming its location."""

    path: str
    detail: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.detail}"
        return self.detail


@dataclass(frozen=True)
class MemberOutcome:
    """Complete per-member outcome for one declared skill."""

    id: str
    name: str
    source_identity: str
    resolved_commit: str
    relative_source_path: str
    store_relative_path: str
    verification: str
    installer_algorithm: str
    source_hash: str
    installed_hash: str
    overlays: tuple[dict[str, object], ...]
    files: tuple[dict[str, object], ...]
    tree_sha256: str


def is_full_commit_ref(ref: str) -> bool:
    """Whether ``ref`` is an exact 40-hex commit (SKV3-07)."""
    import re

    return re.match(r"^[0-9a-f]{40}$", ref) is not None


def base_store_for(home: Path) -> Path:
    """The selected skills Base Store for ``--home``."""
    return home / ".agents" / "skills"


def state_path_for(home: Path) -> Path:
    """The managed-store provenance file selected by ``--home``."""
    return home / ".agents" / "agent-installer" / "state.json"


def installer_hash_for_files(entries: list[tuple[str, bytes, bool]]) -> str:
    """Compute ``INSTALLER_HASH_ALGORITHM`` over sorted path/hash inputs.

    ``entries`` are ``(relative_path, raw_bytes, executable)`` triples.
    Callers pass every inventoried file; sorting is by UTF-8 path bytes.
    """
    digest = hashlib.sha256()
    for rel, raw, executable in sorted(entries, key=lambda item: item[0].encode("utf-8")):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\n")
        digest.update(b"x" if executable else b"-")
        digest.update(b"\n")
        digest.update(raw)
    return digest.hexdigest()


def detect_overlays(skill_md_bytes: bytes, inventory_paths: set[str]) -> list[dict[str, object]]:
    """Detect installer Overlays for one artifact (SKV3-12).

    The invocation-policy translation applies when ``SKILL.md`` declares
    ``disable-model-invocation: true`` and materialises
    ``agents/openai.yaml``. Only that documented transformation is reported;
    any other file is ordinary content.
    """
    try:
        text = skill_md_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if "disable-model-invocation: true" not in text:
        return []
    if OVERLAY_PATH not in inventory_paths:
        return []
    return [{"id": OVERLAY_ID, "version": OVERLAY_VERSION, "paths": [OVERLAY_PATH]}]


def _is_executable(mode: int) -> bool:
    return (mode & 0o111) != 0


def collect_inventory(
    artifact_dir: Path, *, base_store: Path
) -> tuple[list[dict[str, object]], list[tuple[str, bytes, bool]], InstallerFault | None]:
    """Collect the complete recursive inventory of one artifact (SKV3-12).

    Returns ``(records, hash_inputs, fault)`` where ``records`` are sorted
    ``{path, sha256, executable}`` dicts, ``hash_inputs`` are the parallel
    ``(path, bytes, executable)`` triples for the installer hash, and
    ``fault`` names the first refusal (symlink, special file, escape,
    unreadable file). Only ``EXEMPT_METADATA_NAMES`` at the artifact root
    is excluded; hidden, nested, Overlay and ``not-delivered`` files are all
    inventoried. Everything must be contained in ``base_store``.
    """
    try:
        resolved_base = base_store.resolve()
    except OSError as exc:
        return [], [], InstallerFault(path=str(artifact_dir), detail=f"cannot resolve Base Store: {exc}")
    try:
        resolved_dir = artifact_dir.resolve()
    except OSError as exc:
        return [], [], InstallerFault(path=str(artifact_dir), detail=f"cannot resolve artifact directory: {exc}")
    try:
        if not resolved_dir.is_relative_to(resolved_base):
            return [], [], InstallerFault(
                path=str(artifact_dir), detail="artifact escapes the selected Base Store"
            )
    except AttributeError:
        # Python < 3.9 fallback (repo requires 3.13, kept for clarity).
        if resolved_base not in resolved_dir.parents and resolved_dir != resolved_base:
            return [], [], InstallerFault(
                path=str(artifact_dir), detail="artifact escapes the selected Base Store"
            )
    records: list[dict[str, object]] = []
    hash_inputs: list[tuple[str, bytes, bool]] = []
    stack: list[Path] = [resolved_dir]
    # Map resolved directory -> artifact-relative prefix for containment.
    rel_prefix: dict[Path, str] = {resolved_dir: ""}
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                dirents = list(iterator)
        except OSError as exc:
            return [], [], InstallerFault(path=str(artifact_dir), detail=f"cannot list artifact content: {exc}")
        for dirent in dirents:
            name = dirent.name
            full = Path(dirent.path)
            try:
                file_stat = os.lstat(full)
            except OSError as exc:
                return [], [], InstallerFault(path=str(artifact_dir), detail=f"cannot inspect artifact content: {exc}")
            if stat_module.S_ISLNK(file_stat.st_mode):
                rel = f"{rel_prefix[current]}{name}" if rel_prefix[current] else name
                return [], [], InstallerFault(
                    path=f"{artifact_dir}/{rel}",
                    detail="symlinks inside skill artifacts are not supported",
                )
            if stat_module.S_ISDIR(file_stat.st_mode):
                prefix = f"{rel_prefix[current]}{name}/" if rel_prefix[current] else f"{name}/"
                # The exempt ownership file is a file, never a directory.
                stack.append(full)
                rel_prefix[full] = prefix
                continue
            if not stat_module.S_ISREG(file_stat.st_mode):
                rel = f"{rel_prefix[current]}{name}" if rel_prefix[current] else name
                return [], [], InstallerFault(
                    path=f"{artifact_dir}/{rel}",
                    detail="special files inside skill artifacts are not supported",
                )
            rel = f"{rel_prefix[current]}{name}" if rel_prefix[current] else name
            if rel in EXEMPT_METADATA_NAMES and "/" not in rel:
                continue
            if rel.startswith("/") or ".." in rel.split("/"):
                return [], [], InstallerFault(
                    path=f"{artifact_dir}/{rel}", detail="artifact path must be relative without '..'"
                )
            try:
                raw = full.read_bytes()
            except OSError as exc:
                return [], [], InstallerFault(
                    path=f"{artifact_dir}/{rel}", detail=f"cannot read artifact file: {exc}"
                )
            executable = _is_executable(file_stat.st_mode)
            file_sha = hashlib.sha256(raw).hexdigest()
            records.append({"executable": executable, "path": rel, "sha256": file_sha})
            hash_inputs.append((rel, raw, executable))
    records.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    return records, hash_inputs, None


def load_provenance(home: Path) -> tuple[dict[str, dict[str, Any]], InstallerFault | None]:
    """Load managed-store provenance for ``--home`` (SKV3-11).

    Reads the real installer's ``version: 3`` ``state.json`` shape without
    trusting it for membership: the declaration remains authoritative.
    Returns a map of skill name to its provenance record. A missing or
    unreadable file yields a fault; callers report per-member missing
    provenance from the empty map.
    """
    path = state_path_for(home)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {}, InstallerFault(path=str(path), detail=f"cannot read installer provenance: {exc}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return {}, InstallerFault(path=str(path), detail=f"installer provenance does not parse: {exc}")
    if not isinstance(value, dict):
        return {}, InstallerFault(path=str(path), detail="installer provenance must be a JSON object")
    entries = value.get("entries", value.get("artifacts", []))
    if not isinstance(entries, list):
        return {}, InstallerFault(path=str(path), detail="installer provenance entries must be an array")
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in by_name:
            continue
        by_name[name] = entry
    return by_name, None


def provenance_source(entry: dict[str, Any]) -> str | None:
    """The entry's Source Identity (``sourceRoot`` or ``sourceIdentity``)."""
    for key in ("sourceRoot", "sourceIdentity"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def write_fake_store(
    home: Path,
    members: dict[str, dict[str, Any]],
) -> Path:
    """Write a managed store plus provenance (in-process fake, SKV3-10).

    ``members`` maps skill name to ``{source, commit, relativeSourcePath?,
    files}`` where ``files`` maps artifact-relative paths to ``(bytes,
    executable)`` pairs. Writes each artifact directory with its files, one
    exempt ``.agent-installer.json`` per artifact, and a ``version: 3``
    ``state.json`` with freshly computed installer hashes. Creates parents
    as needed and leaves unrelated store entries untouched. No subprocess,
    no network, no shell.
    """
    from coding_agent.skillbundle.canonical import dumps_canonical  # noqa: F401

    base = base_store_for(home)
    base.mkdir(parents=True, exist_ok=True)
    state_dir = home / ".agents" / "agent-installer"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Preserve unrelated provenance entries already present.
    existing: dict[str, dict[str, Any]] = {}
    state_path = state_dir / "state.json"
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                raw_entries = previous.get("entries", [])
                if isinstance(raw_entries, list):
                    for item in raw_entries:
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            existing[str(item["name"])] = item
        except (OSError, ValueError):
            existing = {}
    for name in sorted(members.keys()):
        spec = members[name]
        artifact_dir = base / name
        artifact_dir.mkdir(parents=True, exist_ok=True)
        files = spec.get("files", {})
        assert isinstance(files, dict)
        for rel_path, payload in files.items():
            assert isinstance(rel_path, str)
            target = artifact_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, tuple):
                raw_bytes, executable = payload
            else:
                raw_bytes, executable = payload, False
            assert isinstance(raw_bytes, bytes)
            target.write_bytes(raw_bytes)
            mode = 0o755 if executable else 0o644
            try:
                os.chmod(target, mode)
            except OSError:
                pass
        marker = artifact_dir / ".agent-installer.json"
        marker.write_text(
            json.dumps({"id": f"skill:{name}"}, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        records, hash_inputs, fault = collect_inventory(artifact_dir, base_store=base)
        assert fault is None, f"fake wrote an uninventoriable artifact: {fault}"
        _ = records
        content_hash = installer_hash_for_files(hash_inputs)
        source = str(spec.get("source", ""))
        commit = str(spec.get("commit", ""))
        relative_source = str(spec.get("relativeSourcePath", f"skills/{name}"))
        ref_value = spec.get("ref", commit)
        existing[name] = {
            "basePath": str(artifact_dir),
            "id": f"skill:{name}",
            "installedAt": "2026-01-01T00:00:00Z",
            "installedHash": content_hash,
            "kind": "skill",
            "name": name,
            "relativeSourcePath": relative_source,
            "requestedRef": str(ref_value),
            "resolvedCommit": commit,
            "sourceHash": content_hash,
            "sourceRoot": source,
        }
    ordered = [existing[name] for name in sorted(existing.keys())]
    state_path.write_text(
        json.dumps({"entries": ordered, "version": 3}, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return base
