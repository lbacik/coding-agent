"""Installer-adapter port for the Skill Bundle deployment path (SKV3).

Contract: the installation/provenance resolution (issue #373) and the
released agent-installer machine interface (``agent-installer#74``,
``agent-installer@0.8.0``). One versioned adapter with two operations:
resolve one symbolic ref to an immutable commit and install one source
group at that commit with exact selectors.

This module ships the subprocess adapter (``git ls-remote`` for
resolution plus ``agent-installer install --json --json-schema 3`` for
installation, one argument per selector, no shell) and an in-process fake
that writes a managed store and its provenance metadata for unit tests. The
fake performs no subprocess, no shell interpolation and no network access.
Diagnostics never echo credential-bearing locators: they name the YAML path
and the rejected component, never the raw value.

Supported formats (declared here, enforced by the adapters and the
deployment validator): installer ``>= 0.8.0`` with install-report
``schemaVersion: 3`` (``kind: agent-installer.install-report``),
state version ``4`` (released installer), and the
``agent-installer-tree-v2`` skill content identity.

Managed-store layout (selected by ``--home``):

- Base Store: ``<home>/.agents/skills/<name>/`` holds one directory per
  managed skill, containing its complete recursive content plus the single
  exempt installer ownership file ``.agent-installer.json``.
- Provenance: ``<home>/.agents/agent-installer/state.json`` (the released
  installer's ``version: 4`` shape, ``entries`` with ``sourceRoot``,
  ``relativeSourcePath``, ``resolvedCommit``, ``sourceHash`` and
  ``installedHash``) records the previously verified per-member provenance
  that offline adoption re-verifies by rehashing current bytes.

Hash algorithm ``agent-installer-tree-v2`` (length-framed SHA-256 tree
digest, see ``installer_tree_v2_for_files``): the domain separator
``agent-installer-tree-v2`` followed by a NUL byte, then per content file
in UTF-8 byte order of its ``/``-separated relative path ``path <len> NUL
<path> NUL mode <x|-> NUL content <len> NUL <content> NUL`` with decimal
ASCII lengths. Only the executable bit is hashed, never the full POSIX
mode. The exempt ownership file is excluded. Symlinks and special files
are refused rather than hashed.
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
"""Installer report schema version the in-process fake emits (legacy buckets)."""

MIN_INSTALLER_VERSION = "0.8.0"
"""Minimum released installer satisfying ``agent-installer#74``.

0.8.0 is the first release with complete Git artifact provenance, freshly
verified installed copies and offline subset verification, so the real
adapter refuses any older binary so the insufficient ``0.7.2`` behaviour
(absent unchanged members, stale provenance, hash-comparing ``list``) can
never slip back in silently. Real compatibility beyond this floor is
enforced by the report format checks on every install and verify (install
``schemaVersion: 3``, verify ``schemaVersion: 3``, state ``version: 4``,
``agent-installer-tree-v2``), not by the version string.

Mirrors ``docker/agent-installer/package.json``: the image dependency is the
``>=0.8.0`` range and the lockfile pins the exact resolved release, so an
upgrade is a lockfile refresh, not a code change.
"""

_MAX_ECHOED_VERSION_LENGTH = 32
"""Longest reported version echoed back in a refusal diagnostic."""


def _parse_released_version(text: str) -> tuple[int, int, int] | None:
    """Parse a released ``MAJOR.MINOR.PATCH`` version, or ``None``.

    Strict: three dot-separated plain-integer components (no leading zeros)
    with optional surrounding whitespace and nothing else. Pre-release and
    build suffixes (``0.9.0-beta.1``, ``0.9.0+x``), a ``v`` prefix, empty
    output, extra tokens and multi-line output all return ``None``.
    """
    import re

    match = re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", text.strip()
    )
    if match is None:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


_parsed_minimum = _parse_released_version(MIN_INSTALLER_VERSION)
assert _parsed_minimum is not None  # MIN_INSTALLER_VERSION parses by construction.
_MINIMUM_RELEASE: tuple[int, int, int] = _parsed_minimum
"""Parsed ``MIN_INSTALLER_VERSION`` for numeric per-component comparison."""


def _short_version_for_diagnostic(reported: str) -> str:
    """The reported version safe to echo: bounded single-line text."""
    stripped = reported.strip()
    shown = stripped.splitlines()[0] if stripped else stripped
    if len(shown) > _MAX_ECHOED_VERSION_LENGTH:
        return shown[:_MAX_ECHOED_VERSION_LENGTH] + "..."
    return shown


def query_installer_version() -> str:
    """Run ``agent-installer --version`` and enforce the minimum release.

    Returns the version string the binary reported. Raises ``InstallerFault``
    when the binary is missing, exits non-zero, or reports anything other
    than a released ``MAJOR.MINOR.PATCH`` that is ``>= MIN_INSTALLER_VERSION``
    (compared numerically per component, so ``0.10.0`` passes). Diagnostics
    name the minimum — and the reported version where one parsed — but never
    echo unbounded or multi-line stdout. Local and offline: no install, no
    network, only the ``--version`` invocation.
    """
    import subprocess

    try:
        completed = subprocess.run(
            ["agent-installer", "--version"],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=60,
        )
    except OSError as exc:
        raise InstallerFault(
            path="skills",
            detail=(
                f"installer invocation failed (minimum {MIN_INSTALLER_VERSION}): {exc}"
            ),
        ) from exc
    if completed.returncode != 0:
        raise InstallerFault(
            path="skills",
            detail=(
                "installer version output is not a released MAJOR.MINOR.PATCH "
                f"(minimum {MIN_INSTALLER_VERSION}): refusing to install"
            ),
        )
    parsed = _parse_released_version(completed.stdout)
    if parsed is None:
        raise InstallerFault(
            path="skills",
            detail=(
                "installer version output is not a released MAJOR.MINOR.PATCH "
                f"(minimum {MIN_INSTALLER_VERSION}): refusing to install"
            ),
        )
    if parsed < _MINIMUM_RELEASE:
        raise InstallerFault(
            path="skills",
            detail=(
                f"installer version {_short_version_for_diagnostic(completed.stdout)!r} "
                f"is below the minimum {MIN_INSTALLER_VERSION}: refusing to install"
            ),
        )
    return completed.stdout.strip()

REAL_INSTALL_REPORT_SCHEMA_VERSION = 3
"""Installer report schema version the real adapter requests and accepts."""

REAL_INSTALL_REPORT_KIND = "agent-installer.install-report"
"""``kind`` discriminator of the v3 install report."""

SUPPORTED_INSTALL_REPORT_SCHEMA_VERSIONS = (3,)
"""Install-report schema versions the deployment validator accepts."""

SUPPORTED_STATE_VERSIONS = (4,)
"""Installer state versions adoption accepts (4: released installer)."""

SKILL_TREE_ALGORITHM_V2 = "agent-installer-tree-v2"
"""Released content-identity algorithm for skills (length-framed SHA-256)."""

SUPPORTED_SKILL_HASH_ALGORITHMS = (SKILL_TREE_ALGORITHM_V2,)
"""Skill hash algorithms the released installer may record."""

OVERLAY_ID = "codex-invocation-policy"
OVERLAY_VERSION = "1"
OVERLAY_PATH = "agents/openai.yaml"

EXEMPT_METADATA_NAMES = frozenset({".agent-installer.json"})
"""Only versioned, documented installer metadata exempt from the inventory."""


@dataclass
class InstallerFault(Exception):
    """One installer-port refusal naming its location.

    Deliberately unfrozen: frozen dataclasses block assignment to
    ``__traceback__``, so raising one through any ``with`` block (including
    ``pytest.raises``) fails with ``FrozenInstanceError`` instead of the
    refusal itself.
    """

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


@dataclass(frozen=True)
class MemberInstall:
    """One per-member install fact translated by an adapter."""

    id: str
    disposition: str
    source_identity: str
    resolved_commit: str
    content_algorithm: str | None


@dataclass(frozen=True)
class GroupInstall:
    """One translated install-group result, never a raw report dict."""

    complete: bool
    unmatched_selectors: tuple[str, ...]
    pruned: tuple[str, ...]
    error: str | None
    members: tuple[MemberInstall, ...]


@dataclass(frozen=True)
class ProducerIdentity:
    """The producer identity one adapter run records in the manifest."""

    installer_version: str
    report_schema_version: int


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


def installer_tree_v2_for_files(entries: list[tuple[str, bytes, bool]]) -> str:
    """Compute ``SKILL_TREE_ALGORITHM_V2`` over sorted path/hash inputs.

    Length-framed tree encoding (see the installer's content-identity
    documentation): the domain separator ``agent-installer-tree-v2``
    followed by a NUL byte, then per content file in UTF-8 byte order of
    its ``/``-separated relative path ``path <len> NUL <path> NUL mode
    <x|-> NUL content <len> NUL <content> NUL`` with decimal ASCII lengths.
    Only the executable bit is hashed, never the full POSIX mode. The
    exempt ownership marker must already be excluded by the caller.
    """
    digest = hashlib.sha256()
    digest.update(b"agent-installer-tree-v2\x00")
    for rel, raw, executable in sorted(entries, key=lambda item: item[0].encode("utf-8")):
        path_bytes = rel.encode("utf-8")
        digest.update(f"path {len(path_bytes)}\x00".encode("utf-8"))
        digest.update(path_bytes)
        digest.update(f"\x00mode {'x' if executable else '-'}\x00content {len(raw)}\x00".encode("utf-8"))
        digest.update(raw)
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass(frozen=True)
class ProvenanceRecord:
    """One skill's typed provenance from the managed store (state version 4).

    ``source_identity`` comes from the entry's ``sourceRoot``;
    ``resolved_commit`` is ``None`` for local-path sources. ``hash_algorithm``
    is the entry's recorded ``hashAlgorithm`` (``None`` where unmarked);
    only ``SKILL_TREE_ALGORITHM_V2`` rehashes, anything else is refused per
    member. ``source_hash``/``installed_hash`` are the recorded digests
    (``None`` where absent); callers refuse when either differs from the
    rehashed inventory.
    """

    name: str
    source_identity: str | None
    resolved_commit: str | None
    relative_source_path: str | None
    hash_algorithm: str | None
    source_hash: str | None
    installed_hash: str | None


def recorded_content_identity(
    hash_algorithm: str | None,
    name: str,
    hash_inputs: list[tuple[str, bytes, bool]],
) -> tuple[str, str, InstallerFault | None]:
    """The ``(algorithm, current_digest)`` pair a provenance record needs.

    Only ``SKILL_TREE_ALGORITHM_V2`` is supported: it rehashes ``hash_inputs``
    with the length-framed tree digest. Any other recorded algorithm
    (including a missing one, ``agent-installer-tree-v1`` or any legacy
    digest) yields a fault naming ``skills:<name>`` with the reinstall remedy
    instead of a digest, so callers refuse rather than compare across
    algorithms.
    """
    if hash_algorithm == SKILL_TREE_ALGORITHM_V2:
        return (SKILL_TREE_ALGORITHM_V2, installer_tree_v2_for_files(hash_inputs), None)
    return (
        "",
        "",
        InstallerFault(
            path=f"skills:{name}",
            detail=(
                "installer provenance records an unsupported content algorithm "
                f"{hash_algorithm!r}: reinstall the managed store with "
                f"agent-installer >= {MIN_INSTALLER_VERSION}"
            ),
        ),
    )


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


def installer_state_version(home: Path) -> int | None:
    """The installer state version recorded for ``--home``, if readable.

    Returns the ``version`` field of ``state.json`` (``4`` for the released
    installer), or ``None`` when the file is missing, unparsable, or carries
    no integer version. Never raises; diagnostics belong to
    ``load_provenance``.
    """
    try:
        value = json.loads(state_path_for(home).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    version = value.get("version")
    return version if type(version) is int else None


def load_provenance(home: Path) -> tuple[dict[str, ProvenanceRecord], InstallerFault | None]:
    """Load managed-store provenance for ``--home`` (SKV3-11).

    Reads only the released installer's ``state.json`` shape (state version
    4 with an ``entries`` array) without trusting it for membership: the
    declaration remains authoritative. Returns a map of skill name to its
    frozen provenance record plus an optional fault. Any other version, a
    missing version, or a non-array ``entries`` yields one fault naming the
    ``state.json`` path with the reinstall remedy; the store is never
    migrated. Only ``kind: "skill"`` entries become records (other kinds are
    ignored); the first entry for a name wins. A missing or unreadable file
    yields a fault; callers report per-member missing provenance from the
    empty map.
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

    def _refusal(detail: str) -> tuple[dict[str, ProvenanceRecord], InstallerFault | None]:
        return {}, InstallerFault(path=str(path), detail=detail)

    def _text(record: dict[str, Any], key: str) -> str | None:
        found = record.get(key)
        return found if isinstance(found, str) and found else None

    remedy = f"reinstall the managed store with agent-installer >= {MIN_INSTALLER_VERSION}"
    version = value.get("version")
    if type(version) is not int or version not in SUPPORTED_STATE_VERSIONS:
        return _refusal(
            f"installer provenance has an unsupported version {version!r}: {remedy}"
        )
    entries = value.get("entries")
    if not isinstance(entries, list):
        return _refusal(f"installer provenance entries must be an array: {remedy}")
    by_name: dict[str, ProvenanceRecord] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in by_name:
            continue
        if entry.get("kind") != "skill":
            continue
        source_identity = _text(entry, "sourceRoot")
        resolved_commit = _text(entry, "resolvedCommit")
        relative_source_path = _text(entry, "relativeSourcePath")
        hash_algorithm = _text(entry, "hashAlgorithm")
        source_hash = _text(entry, "sourceHash")
        installed_hash = _text(entry, "installedHash")
        by_name[name] = ProvenanceRecord(
            name=name,
            source_identity=source_identity,
            resolved_commit=resolved_commit,
            relative_source_path=relative_source_path,
            hash_algorithm=hash_algorithm,
            source_hash=source_hash,
            installed_hash=installed_hash,
        )
    return by_name, None


def write_fake_store(
    home: Path,
    members: dict[str, dict[str, Any]],
) -> Path:
    """Write a managed store plus provenance (in-process fake, SKV3-10).

    ``members`` maps skill name to ``{source, commit, relativeSourcePath?,
    files}`` where ``files`` maps artifact-relative paths to ``(bytes,
    executable)`` pairs. Writes each artifact directory with its files, one
    exempt ``.agent-installer.json`` per artifact, and a released-shape
    ``version: 4`` ``state.json`` with freshly computed
    ``SKILL_TREE_ALGORITHM_V2`` hashes. Creates parents as needed and leaves
    unrelated store entries untouched. No subprocess, no network, no shell.
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
        content_hash = installer_tree_v2_for_files(hash_inputs)
        source = str(spec.get("source", ""))
        commit = str(spec.get("commit", ""))
        relative_source = str(spec.get("relativeSourcePath", f"skills/{name}"))
        ref_value = spec.get("ref", commit)
        existing[name] = {
            "basePath": str(artifact_dir),
            "hashAlgorithm": SKILL_TREE_ALGORITHM_V2,
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
        json.dumps({"entries": ordered, "version": 4}, sort_keys=True, separators=(",", ":")),
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
    """Installer-adapter port for ``prepare`` (SKV3-08/09).

    Two operations plus producer identity: resolve one symbolic ref to an
    immutable commit, install one source group at that commit with exact
    selectors, and name the producer identity the deployment records in the
    manifest. Each adapter translates its own report format into typed
    per-member facts; the v3 wire format is known only to the subprocess
    adapter.
    """

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
    ) -> GroupInstall:
        """Install one source group at ``resolved_commit``; returns typed facts."""
        ...

    def producer_for(self, home: Path) -> ProducerIdentity:
        """Name the producer identity recorded for ``home``.

        Raises ``InstallerFault`` on refusal (for example a managed store the
        adapter cannot truthfully describe).
        """
        ...


def _group_install_from_v3(
    report: dict[str, Any], *, force_incomplete: bool = False
) -> GroupInstall:
    """Translate one v3 install report dict into typed install facts.

    Raises ``InstallerFault`` for an unparsable or unsupported shape; a
    refused run that still prints a valid report translates to a
    ``GroupInstall`` with ``complete=False`` so the deployment keeps the
    per-member refusal details.
    """
    artifacts_raw = report.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise InstallerFault(
            path="skills",
            detail="installer report artifacts must be an array for the declared source",
        )
    unmatched_raw = report.get("unmatchedSelectors", [])
    if not isinstance(unmatched_raw, list):
        raise InstallerFault(
            path="skills",
            detail="installer report unmatched selectors must be an array for the declared source",
        )
    pruned_raw = report.get("pruned", [])
    if not isinstance(pruned_raw, list):
        raise InstallerFault(
            path="skills",
            detail="installer report pruned entries must be an array for the declared source",
        )
    error_raw = report.get("error")
    if error_raw is None:
        error: str | None = None
    elif isinstance(error_raw, str):
        error = error_raw
    elif isinstance(error_raw, (dict, list)):
        error = json.dumps(error_raw, sort_keys=True, separators=(",", ":"))
    else:
        error = str(error_raw)
    members: list[MemberInstall] = []
    for item in artifacts_raw:
        if not isinstance(item, dict):
            raise InstallerFault(
                path="skills",
                detail="installer report artifacts must be an array of objects for the declared source",
            )
        item_id = item.get("id")
        if not isinstance(item_id, str):
            raise InstallerFault(
                path="skills",
                detail="installer report artifacts must be an array of objects for the declared source",
            )
        disposition_raw = item.get("disposition")
        disposition = disposition_raw if isinstance(disposition_raw, str) else ""
        source_raw = item.get("sourceIdentity")
        source_identity = source_raw if isinstance(source_raw, str) else ""
        commit_raw = item.get("resolvedCommit")
        resolved_commit = commit_raw if isinstance(commit_raw, str) else ""
        content = item.get("content")
        content_algorithm: str | None = None
        if isinstance(content, dict) and "algorithm" in content:
            algorithm_raw = content.get("algorithm")
            if isinstance(algorithm_raw, str):
                content_algorithm = algorithm_raw
            elif algorithm_raw is not None:
                content_algorithm = str(algorithm_raw)
        members.append(
            MemberInstall(
                id=item_id,
                disposition=disposition,
                source_identity=source_identity,
                resolved_commit=resolved_commit,
                content_algorithm=content_algorithm,
            )
        )
    unmatched: tuple[str, ...] = tuple(str(value) for value in unmatched_raw)
    pruned_list: list[str] = []
    for entry in pruned_raw:
        if isinstance(entry, dict):
            ident = entry.get("id")
            if isinstance(ident, str):
                pruned_list.append(ident)
                continue
            pruned_list.append(
                json.dumps(entry, sort_keys=True, separators=(",", ":"))
            )
        else:
            pruned_list.append(str(entry))
    outcome = report.get("outcome")
    complete = (outcome == "complete") and not force_incomplete
    return GroupInstall(
        complete=complete,
        unmatched_selectors=unmatched,
        pruned=tuple(pruned_list),
        error=error,
        members=tuple(members),
    )


class SubprocessInstallerAdapter(InstallerAdapter):
    """Real adapter invoking ``git`` and ``agent-installer`` via subprocess.

    Every invocation passes one argument per selector with ``shell=False``;
    diagnostics name the YAML path and the rejected component, never the raw
    locator, so credential-bearing Git arguments are never echoed. Installs
    run with ``HOME`` set to the selected home so the managed store lands
    under it no matter which user invokes the command; the installer binary
    itself must report a released version ``>= MIN_INSTALLER_VERSION``,
    checked once per adapter instance before any install (0.8.0 is
    the first release satisfying ``agent-installer#74``; newer compatible
    releases are refused only by the v3 machine-report format checks, never
    by the version string). Only the v3 machine reports are accepted, and
    they are translated to typed facts here so the deployment never sees the
    wire format.
    """

    def __init__(self) -> None:
        self._version_checked = False
        self._reported_version: str | None = None

    def _require_minimum_installer(self) -> None:
        """Refuse to run unless the binary reports a supported release."""
        if self._version_checked:
            return
        self._reported_version = query_installer_version()
        self._version_checked = True

    def producer_for(self, home: Path) -> ProducerIdentity:
        """Record the released installer's own producer identity for ``home``.

        Refuses a managed store whose ``state.json`` is not state version 4:
        state v3 is written only by the fake, so recording a real installer
        version for it would be false provenance. Then runs the
        ``>= MIN_INSTALLER_VERSION`` gate and returns the version the binary
        reported, with report schema 3.
        """
        if installer_state_version(home) != 4:
            raise InstallerFault(
                path="skills",
                detail=(
                    "installer state is not a released-installer store "
                    "(state version 4): refusing to record producer identity"
                ),
            )
        self._require_minimum_installer()
        assert self._reported_version is not None
        return ProducerIdentity(
            installer_version=self._reported_version,
            report_schema_version=REAL_INSTALL_REPORT_SCHEMA_VERSION,
        )

    @staticmethod
    def _report_from_stdout(stdout: str) -> dict[str, Any]:
        try:
            value = json.loads(stdout)
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
    ) -> GroupInstall:
        import os
        import subprocess

        _ = source_identity
        self._require_minimum_installer()
        argv: list[str] = build_install_argv(https_locator, resolved_commit, selectors)
        env = dict(os.environ)
        env["HOME"] = str(home)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=600,
                env=env,
            )
        except OSError as exc:
            raise InstallerFault(
                path="skills",
                detail=f"installer invocation failed: {exc}",
            ) from exc
        if completed.returncode != 0:
            # A refused run (conflicts, unmatched selectors) still prints the
            # versioned machine report on stdout: translate it with
            # ``complete=False`` so the deployment keeps the per-member
            # refusal details instead of a generic failure.
            try:
                failed_value = self._report_from_stdout(completed.stdout)
            except InstallerFault:
                failed_value = None
            if (
                isinstance(failed_value, dict)
                and failed_value.get("schemaVersion") == REAL_INSTALL_REPORT_SCHEMA_VERSION
                and failed_value.get("kind") == REAL_INSTALL_REPORT_KIND
            ):
                return _group_install_from_v3(failed_value, force_incomplete=True)
            raise InstallerFault(
                path="skills",
                detail="installer reported failure for the declared subset",
            )
        value = self._report_from_stdout(completed.stdout)
        if (
            value.get("schemaVersion") != REAL_INSTALL_REPORT_SCHEMA_VERSION
            or value.get("kind") != REAL_INSTALL_REPORT_KIND
        ):
            raise InstallerFault(
                path="skills",
                detail="installer report has an unsupported schema version",
            )
        return _group_install_from_v3(value)


def build_install_argv(
    https_locator: str, resolved_commit: str, selectors: tuple[str, ...]
) -> list[str]:
    """The installer argv for one source group (one argument per selector).

    Requests the versioned v3 machine report (``--json --json-schema 3``):
    one verified outcome per requested artifact. No shell interpolation:
    callers pass the returned list directly to ``subprocess.run`` with
    ``shell=False``.
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
    argv.extend(["--json", "--json-schema", "3"])
    return argv


@dataclass
class FakeInstallerAdapter(InstallerAdapter):
    """In-process fake for ``prepare --mode install`` unit tests (SKV3-08/09).

    No subprocess, no network, no shell. Tests seed remote resolutions and
    per-member file content; the fake writes the managed store via
    ``write_fake_store`` and returns typed install facts directly. Call counts
    prove once-per-group resolution; ``report_mode`` exercises empty,
    partial, refused, skipped, failed and stale-provenance installer
    behaviours. The legacy bucket-report format is gone: every mode maps to
    typed ``GroupInstall`` facts.
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

    def producer_for(self, home: Path) -> ProducerIdentity:
        """The fake's own producer identity, independent of the store."""
        _ = home
        return ProducerIdentity(
            installer_version=FAKE_INSTALLER_VERSION,
            report_schema_version=FAKE_REPORT_SCHEMA_VERSION,
        )

    def install_group(
        self,
        *,
        source_identity: str,
        https_locator: str,
        resolved_commit: str,
        selectors: tuple[str, ...],
        home: Path,
    ) -> GroupInstall:
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

        def _member(name: str, disposition: str, commit: str) -> MemberInstall:
            return MemberInstall(
                id=f"skill:{name}",
                disposition=disposition,
                source_identity=source_identity,
                resolved_commit=commit,
                content_algorithm=None,
            )

        if self.report_mode == "complete":
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=tuple(_member(name, "installed", effective_commit) for name in names),
            )
        if self.report_mode == "empty":
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=(),
            )
        if self.report_mode == "partial":
            kept = names[:-1] if len(names) > 1 else []
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=tuple(_member(name, "installed", effective_commit) for name in kept),
            )
        if self.report_mode == "skipped":
            leading = names[:-1] if names else []
            trailing = [names[-1]] if names else []
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=tuple(
                    [_member(name, "installed", effective_commit) for name in leading]
                    + [_member(name, "skipped", effective_commit) for name in trailing]
                ),
            )
        if self.report_mode == "refused":
            leading = names[:-1] if names else []
            trailing = [names[-1]] if names else []
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=tuple(
                    [_member(name, "installed", effective_commit) for name in leading]
                    + [_member(name, "refused", effective_commit) for name in trailing]
                ),
            )
        if self.report_mode == "failed":
            leading = names[:-1] if names else []
            trailing = [names[-1]] if names else []
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=tuple(
                    [_member(name, "installed", effective_commit) for name in leading]
                    + [_member(name, "failed", effective_commit) for name in trailing]
                ),
            )
        if self.report_mode == "stale":
            return GroupInstall(
                complete=True,
                unmatched_selectors=(),
                pruned=(),
                error=None,
                members=tuple(_member(name, "installed", effective_commit) for name in names),
            )
        raise AssertionError(f"unknown fake report mode {self.report_mode!r}")
