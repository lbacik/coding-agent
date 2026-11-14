"""Installer-adapter port for the Skill Bundle deployment path (SKV3).

Contract: the installation/provenance resolution (issue #373) and the
extended agent-installer machine interface (agent-installer#74). One
versioned adapter with three operations: install one source group at an
immutable commit with exact selectors, produce complete per-member
outcomes, and verify a declared subset offline without network access.

This module ships both the subprocess adapter (``git ls-remote`` for
resolution plus ``agent-installer install --json`` for installation, one
argument per selector, no shell) and an in-process fake that writes a
managed store and its provenance metadata for unit tests. The fake performs
no subprocess, no shell interpolation and no network access. Diagnostics
never echo credential-bearing locators: they name the YAML path and the
rejected component, never the raw value. Binding the released real
installer version is a later ticket; this one is exercised against the fake.

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

Matrix row ids: SKV3-07 (part), SKV3-08, SKV3-09, SKV3-10, SKV3-11,
SKV3-12 (part).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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
class InstallerFault(Exception):
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
    if not resolved_dir.is_relative_to(resolved_base):
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


def https_locator_from_identity(source_identity: str) -> str:
    """The HTTPS locator the installer receives for a Source Identity.

    The normalised identity always carries the ``git+`` transport marker;
    the installer invocation receives the plain ``https://`` locator. One
    plain string, never interpolated into a shell.
    """
    if source_identity.startswith("git+"):
        return source_identity[len("git+") :]
    return source_identity


class InstallerAdapter(Protocol):
    """Installer-adapter port for ``prepare --mode install`` (SKV3-08/09)."""

    def resolve_ref(self, source_identity: str, ref: str) -> str:
        """Resolve ``ref`` (or ``HEAD``) to an immutable 40-hex commit."""
        ...

    def install_group(
        self,
        *,
        source_identity: str,
        https_locator: str,
        resolved_commit: str,
        selectors: tuple[str, ...],
        home: Path,
    ) -> dict[str, Any]:
        """Install one source group at ``resolved_commit``; returns the report."""
        ...


class SubprocessInstallerAdapter(InstallerAdapter):
    """Real adapter invoking ``git`` and ``agent-installer`` via subprocess.

    Every invocation passes one argument per selector with ``shell=False``;
    diagnostics name the YAML path and the rejected component, never the raw
    locator, so credential-bearing Git arguments are never echoed.
    """

    def resolve_ref(self, source_identity: str, ref: str) -> str:
        import subprocess

        locator = https_locator_from_identity(source_identity)
        argv: list[str] = ["git", "ls-remote", locator, ref]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=120,
            )
        except OSError as exc:
            raise InstallerFault(
                path="skills",
                detail=f"cannot resolve ref {ref!r}: {exc}",
            ) from exc
        if completed.returncode != 0:
            raise InstallerFault(
                path="skills",
                detail=f"cannot resolve ref {ref!r} for the declared source",
            )
        commit: str | None = None
        for line in completed.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            candidate = parts[0].strip()
            if is_full_commit_ref(candidate):
                commit = candidate
                break
        if commit is None:
            raise InstallerFault(
                path="skills",
                detail=f"cannot resolve ref {ref!r} for the declared source",
            )
        return commit

    def install_group(
        self,
        *,
        source_identity: str,
        https_locator: str,
        resolved_commit: str,
        selectors: tuple[str, ...],
        home: Path,
    ) -> dict[str, Any]:
        import subprocess

        _ = source_identity
        _ = home
        argv: list[str] = build_install_argv(https_locator, resolved_commit, selectors)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=600,
            )
        except OSError as exc:
            raise InstallerFault(
                path="skills",
                detail=f"installer invocation failed: {exc}",
            ) from exc
        if completed.returncode != 0:
            raise InstallerFault(
                path="skills",
                detail="installer reported failure for the declared subset",
            )
        try:
            value = json.loads(completed.stdout)
        except ValueError as exc:
            raise InstallerFault(
                path="skills",
                detail=f"installer report does not parse: {exc}",
            ) from exc
        if not isinstance(value, dict):
            raise InstallerFault(
                path="skills",
                detail="installer report must be a JSON object",
            )
        return value


def build_install_argv(
    https_locator: str, resolved_commit: str, selectors: tuple[str, ...]
) -> list[str]:
    """The installer argv for one source group (one argument per selector).

    No shell interpolation: callers pass the returned list directly to
    ``subprocess.run`` with ``shell=False``.
    """
    argv: list[str] = [
        "agent-installer",
        "install",
        https_locator,
        "--ref",
        resolved_commit,
    ]
    for selector in selectors:
        argv.extend(["--only", selector])
    argv.append("--json")
    return argv


@dataclass
class FakeInstallerAdapter(InstallerAdapter):
    """In-process fake for ``prepare --mode install`` unit tests (SKV3-08/09).

    No subprocess, no network, no shell. Tests seed remote resolutions and
    per-member file content; the fake writes the managed store via
    ``write_fake_store`` and returns a complete installer report. Call counts
    prove once-per-group resolution; ``report_mode`` exercises empty,
    partial, refused, skipped, failed and stale-provenance installer
    behaviours.
    """

    resolutions: dict[tuple[str, str], str] = field(default_factory=dict)
    """(sourceIdentity, ref) -> commit, where ``ref`` is symbolic or ``HEAD``."""

    files_by_member: dict[tuple[str, str], dict[str, tuple[bytes, bool]]] = field(
        default_factory=dict
    )
    """(sourceIdentity, name) -> artifact-relative files for the fake remote."""

    relative_paths: dict[tuple[str, str], str] = field(default_factory=dict)
    """(sourceIdentity, name) -> relativeSourcePath override."""

    report_mode: str = "complete"
    """One of complete/empty/partial/skipped/refused/failed/stale."""

    resolve_calls: list[tuple[str, str]] = field(default_factory=list)
    install_calls: list[dict[str, object]] = field(default_factory=list)

    def seed_resolution(self, source_identity: str, ref: str, commit: str) -> None:
        """Seed the commit ``ref`` (or ``HEAD``) resolves to."""
        self.resolutions[(source_identity, ref)] = commit

    def seed_member(
        self,
        source_identity: str,
        name: str,
        files: dict[str, tuple[bytes, bool]],
        relative_source_path: str | None = None,
    ) -> None:
        """Seed the remote file content for one ``(source, name)`` member."""
        self.files_by_member[(source_identity, name)] = dict(files)
        if relative_source_path is not None:
            self.relative_paths[(source_identity, name)] = relative_source_path

    def resolve_ref(self, source_identity: str, ref: str) -> str:
        """Resolve once per call; callers resolve once per source group."""
        self.resolve_calls.append((source_identity, ref))
        commit = self.resolutions.get((source_identity, ref))
        if commit is not None:
            return commit
        raise InstallerFault(
            path="skills",
            detail=f"cannot resolve ref {ref!r} for the declared source",
        )

    def install_group(
        self,
        *,
        source_identity: str,
        https_locator: str,
        resolved_commit: str,
        selectors: tuple[str, ...],
        home: Path,
    ) -> dict[str, Any]:
        _ = https_locator
        names: list[str] = []
        for selector in selectors:
            kind, _, bare = selector.partition(":")
            if kind != "skill" or not bare:
                raise InstallerFault(
                    path="skills",
                    detail="invalid selector for the declared subset",
                )
            names.append(bare)
        self.install_calls.append(
            {
                "source": source_identity,
                "commit": resolved_commit,
                "selectors": tuple(selectors),
            }
        )
        effective_commit = resolved_commit
        if self.report_mode == "stale":
            for (_src, _ref), candidate in self.resolutions.items():
                if candidate != resolved_commit:
                    effective_commit = candidate
                    break
        members: dict[str, dict[str, Any]] = {}
        for name in names:
            files = self.files_by_member.get((source_identity, name))
            if files is None:
                for (seed_source, seed_name), seed_files in self.files_by_member.items():
                    if seed_source == source_identity and seed_name == name:
                        files = seed_files
                        break
            if files is None:
                files = {"SKILL.md": (f"# {name}\n".encode("utf-8"), False)}
            record: dict[str, Any] = {
                "commit": effective_commit,
                "files": dict(files),
                "source": source_identity,
            }
            rel_override = self.relative_paths.get((source_identity, name))
            if rel_override is not None:
                record["relativeSourcePath"] = rel_override
            members[name] = record
        write_fake_store(home, members)
        entries: list[dict[str, object]] = []
        for name in names:
            entries.append(
                {
                    "id": f"skill:{name}",
                    "resolvedCommit": effective_commit,
                    "sourceIdentity": source_identity,
                }
            )
        if self.report_mode == "complete":
            return {
                "failed": [],
                "installed": entries,
                "refused": [],
                "skipped": [],
                "updated": [],
            }
        if self.report_mode == "empty":
            return {
                "failed": [],
                "installed": [],
                "refused": [],
                "skipped": [],
                "updated": [],
            }
        if self.report_mode == "partial":
            return {
                "failed": [],
                "installed": entries[:-1] if len(entries) > 1 else [],
                "refused": [],
                "skipped": [],
                "updated": [],
            }
        if self.report_mode == "skipped":
            return {
                "failed": [],
                "installed": entries[:-1] if entries else [],
                "refused": [],
                "skipped": [entries[-1]] if entries else [],
                "updated": [],
            }
        if self.report_mode == "refused":
            return {
                "failed": [],
                "installed": entries[:-1] if entries else [],
                "refused": [entries[-1]] if entries else [],
                "skipped": [],
                "updated": [],
            }
        if self.report_mode == "failed":
            unknown: dict[str, object] = {"id": "skill:unknown"}
            return {
                "failed": [entries[-1]] if entries else [unknown],
                "installed": entries[:-1] if entries else [],
                "refused": [],
                "skipped": [],
                "updated": [],
            }
        if self.report_mode == "stale":
            return {
                "failed": [],
                "installed": entries,
                "refused": [],
                "skipped": [],
                "updated": [],
            }
        raise AssertionError(f"unknown fake report mode {self.report_mode!r}")
