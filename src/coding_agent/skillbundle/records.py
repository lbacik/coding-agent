"""Generated Skill Bundle records: requirements, manifest, validation.

Contract: the installation resolution (issue #373, ``requirements.json`` and
``manifest.json``) and the phased-validation resolution (issue #375,
``validation.json``). All three use canonical JSON at ``schemaVersion: 1`` and
are published as one completed generation only after every check passes.

This module builds the records for an empty Skill Bundle and strictly
validates stored records (duplicate keys, unknown fields, nulls, malformed
digests, unknown versions and inconsistent cross-references are refused).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.skillbundle.canonical import (
    HEX64_RE,
    contains_null,
    dumps_canonical,
    is_hex40,
    is_hex64,
    loads_strict,
    sha256_bytes,
)

REQUIREMENTS_KIND = "coding-agent.skill-requirements"
MANIFEST_KIND = "coding-agent.skill-bundle-manifest"
VALIDATION_KIND = "coding-agent.prepared-validation"
SCHEMA_VERSION = 1
WORKFLOW_CONFIG_VERSION = 3

REQUIREMENTS_FIELDS = frozenset({"schemaVersion", "kind", "workflowConfigVersion", "sources"})
SOURCE_FIELDS = frozenset({"sourceIdentity", "include", "ref"})
MANIFEST_FIELDS = frozenset(
    {"schemaVersion", "kind", "requirementsSha256", "producer", "resolutions", "artifacts"}
)
PRODUCER_FIELDS = frozenset({"codingAgentVersion", "installerVersion", "installerReportSchemaVersion"})
RESOLUTION_FIELDS = frozenset({"sourceIdentity", "ref", "resolvedCommit"})
ARTIFACT_FIELDS = frozenset(
    {
        "id",
        "name",
        "sourceIdentity",
        "resolvedCommit",
        "relativeSourcePath",
        "storeRelativePath",
        "verification",
        "installerContent",
        "overlays",
        "files",
        "treeSha256",
    }
)
INSTALLER_CONTENT_FIELDS = frozenset({"algorithm", "sourceHash", "installedHash"})
OVERLAY_FIELDS = frozenset({"id", "version", "paths"})
FILE_FIELDS = frozenset({"path", "sha256", "executable"})
VALIDATION_FIELDS = frozenset(
    {"schemaVersion", "kind", "workflowSha256", "requirementsSha256", "manifestSha256", "policySha256", "prompts"}
)
PROMPT_FIELDS = frozenset({"reference", "sha256"})

_VALID_VERIFICATIONS = frozenset({"source", "recorded-installation"})


@dataclass(frozen=True)
class RecordError:
    """One strict record refusal naming the JSON path of the fault."""

    path: str
    detail: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.detail}"
        return self.detail


def _is_source_identity(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("git+https://"):
        return False
    https_part = value[len("git+") :]
    if "#" in https_part or "?" in https_part:
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(https_part)
    except ValueError:
        return False
    if "@" in parts.netloc:
        return False
    host = parts.hostname or ""
    if not host:
        return False
    if parts.path in ("", "/"):
        return False
    return True


def _is_rel_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("/") or value.startswith("\\"):
        return False
    if ".." in value.split("/"):
        return False
    if "\x00" in value:
        return False
    return True


def build_requirements(sources: list[dict[str, object]]) -> dict[str, object]:
    """Build the ``requirements.json`` document for the given sources.

    ``sources`` are already-normalized ``{sourceIdentity, include, ref?}``
    entries in declaration order. Callers pass ``[]`` for an empty bundle.
    """
    return {
        "kind": REQUIREMENTS_KIND,
        "schemaVersion": SCHEMA_VERSION,
        "sources": sources,
        "workflowConfigVersion": WORKFLOW_CONFIG_VERSION,
    }


def requirements_sources_from_skills(skills: tuple[object, ...]) -> list[dict[str, object]]:
    """Build requirements ``sources`` from validated v3 ``skills`` entries."""
    sources: list[dict[str, object]] = []
    for entry in skills:
        source = getattr(entry, "source", None)
        include = tuple(getattr(entry, "include", ()))
        ref = getattr(entry, "ref", None)
        record: dict[str, object] = {"sourceIdentity": source, "include": list(include)}
        if ref is not None:
            record["ref"] = ref
        sources.append(record)
    return sources


def validate_requirements_object(obj: object) -> tuple[dict[str, Any] | None, list[RecordError]]:
    """Strictly validate a parsed ``requirements.json`` document."""
    errors: list[RecordError] = []

    def err(path: str, detail: str) -> None:
        errors.append(RecordError(path=path, detail=detail))

    if not isinstance(obj, dict):
        err("(root)", "must be a JSON object")
        return None, errors
    if contains_null(obj):
        err("(root)", "null values are not allowed")
        return None, errors
    for key in sorted(obj.keys()):
        if key not in REQUIREMENTS_FIELDS:
            err(key, f"unknown field {key!r}")
    for key in ("schemaVersion", "kind", "workflowConfigVersion", "sources"):
        if key not in obj:
            err(key, f"missing required field {key!r}")
    if errors:
        return None, errors
    assert isinstance(obj, dict)
    version = obj["schemaVersion"]
    if type(version) is not int or version != 1:
        err("schemaVersion", f"unsupported version {version!r}: must be 1")
    if obj["kind"] != REQUIREMENTS_KIND:
        err("kind", f"unsupported kind {obj['kind']!r}")
    wcv = obj["workflowConfigVersion"]
    if type(wcv) is not int or wcv != 3:
        err("workflowConfigVersion", f"unsupported version {wcv!r}: must be 3")
    raw_sources = obj["sources"]
    if not isinstance(raw_sources, list):
        err("sources", "'sources' must be an array")
        return None, errors
    seen_identities: set[str] = set()
    seen_names: dict[str, str] = {}
    for index, entry in enumerate(raw_sources):
        where = f"sources[{index}]"
        if not isinstance(entry, dict):
            err(where, "must be an object")
            continue
        for key in sorted(entry.keys()):
            if key not in SOURCE_FIELDS:
                err(f"{where}.{key}", f"unknown field {key!r}")
        for key in ("sourceIdentity", "include"):
            if key not in entry:
                err(f"{where}.{key}", f"missing required field {key!r}")
        source = entry.get("sourceIdentity") if isinstance(entry, dict) else None
        if not isinstance(source, str) or not _is_source_identity(source):
            err(f"{where}.sourceIdentity", "'sourceIdentity' must be a normalized 'git+https://' identity")
        elif source in seen_identities:
            err(f"{where}.sourceIdentity", f"duplicate source identity {source!r}")
        elif isinstance(source, str):
            seen_identities.add(source)
        include = entry.get("include") if isinstance(entry, dict) else None
        if not isinstance(include, list) or not include:
            err(f"{where}.include", "'include' must be a non-empty array of skill names")
        else:
            for pos, name in enumerate(include):
                item_where = f"{where}.include[{pos}]"
                if not isinstance(name, str) or not name:
                    err(item_where, "skill name must be a non-empty string")
                    continue
                first = seen_names.get(name)
                if first is not None:
                    err(item_where, f"skill {name!r} is already included at {first}")
                    continue
                seen_names[name] = item_where
        if isinstance(entry, dict) and "ref" in entry:
            ref = entry["ref"]
            if not isinstance(ref, str) or not ref:
                err(f"{where}.ref", "'ref' must be a non-empty string when present")
    if errors:
        return None, errors
    return dict(obj), []


def build_manifest_empty(
    requirements_sha256: str,
    coding_agent_version: str,
    installer_version: str,
    installer_report_schema_version: int,
) -> dict[str, object]:
    """Build the ``manifest.json`` document for an empty Skill Bundle."""
    return {
        "artifacts": [],
        "kind": MANIFEST_KIND,
        "producer": {
            "codingAgentVersion": coding_agent_version,
            "installerReportSchemaVersion": installer_report_schema_version,
            "installerVersion": installer_version,
        },
        "requirementsSha256": requirements_sha256,
        "resolutions": [],
        "schemaVersion": SCHEMA_VERSION,
    }


def build_manifest(
    requirements_sha256: str,
    coding_agent_version: str,
    installer_version: str,
    installer_report_schema_version: int,
    resolutions: list[dict[str, object]],
    artifacts: list[dict[str, object]],
) -> dict[str, object]:
    """Build the ``manifest.json`` document for a non-empty Skill Bundle.

    ``artifacts`` are in source/include declaration order with exactly one
    record per declared name; ``resolutions`` hold one entry per declared
    symbolic-ref source that was resolved during preparation.
    """
    return {
        "artifacts": artifacts,
        "kind": MANIFEST_KIND,
        "producer": {
            "codingAgentVersion": coding_agent_version,
            "installerReportSchemaVersion": installer_report_schema_version,
            "installerVersion": installer_version,
        },
        "requirementsSha256": requirements_sha256,
        "resolutions": resolutions,
        "schemaVersion": SCHEMA_VERSION,
    }


def validate_manifest_object(obj: object) -> tuple[dict[str, Any] | None, list[RecordError]]:
    """Strictly validate a parsed ``manifest.json`` document (structure only).

    Cross-record binding (``requirementsSha256`` vs. canonical requirements,
    artifact order vs. requirements) is checked by the deployment verifier,
    which has both documents; this reports structural faults collected
    independently within the manifest stage.
    """
    errors: list[RecordError] = []

    def err(path: str, detail: str) -> None:
        errors.append(RecordError(path=path, detail=detail))

    if not isinstance(obj, dict):
        err("(root)", "must be a JSON object")
        return None, errors
    if contains_null(obj):
        err("(root)", "null values are not allowed")
        return None, errors
    for key in sorted(obj.keys()):
        if key not in MANIFEST_FIELDS:
            err(key, f"unknown field {key!r}")
    for key in ("schemaVersion", "kind", "requirementsSha256", "producer", "resolutions", "artifacts"):
        if key not in obj:
            err(key, f"missing required field {key!r}")
    if errors:
        return None, errors
    assert isinstance(obj, dict)
    version = obj["schemaVersion"]
    if type(version) is not int or version != 1:
        err("schemaVersion", f"unsupported version {version!r}: must be 1")
    if obj["kind"] != MANIFEST_KIND:
        err("kind", f"unsupported kind {obj['kind']!r}")
    req_digest = obj["requirementsSha256"]
    if not is_hex64(req_digest):
        err("requirementsSha256", "'requirementsSha256' must be a lowercase 64-hex SHA-256")
    producer = obj["producer"]
    if not isinstance(producer, dict):
        err("producer", "'producer' must be an object")
    else:
        for key in sorted(producer.keys()):
            if key not in PRODUCER_FIELDS:
                err(f"producer.{key}", f"unknown field {key!r}")
        for key in ("codingAgentVersion", "installerVersion", "installerReportSchemaVersion"):
            if key not in producer:
                err(f"producer.{key}", f"missing required field {key!r}")
        cav = producer.get("codingAgentVersion")
        if not isinstance(cav, str) or not cav:
            err("producer.codingAgentVersion", "must be a non-empty string")
        iv = producer.get("installerVersion")
        if not isinstance(iv, str) or not iv:
            err("producer.installerVersion", "must be a non-empty string")
        irsv = producer.get("installerReportSchemaVersion")
        if type(irsv) is not int or irsv != 1:
            err("producer.installerReportSchemaVersion", "unsupported version: must be 1")
    resolutions = obj["resolutions"]
    if not isinstance(resolutions, list):
        err("resolutions", "'resolutions' must be an array")
    else:
        seen_res: set[str] = set()
        for index, entry in enumerate(resolutions):
            where = f"resolutions[{index}]"
            if not isinstance(entry, dict):
                err(where, "must be an object")
                continue
            for key in sorted(entry.keys()):
                if key not in RESOLUTION_FIELDS:
                    err(f"{where}.{key}", f"unknown field {key!r}")
            for key in ("sourceIdentity", "ref", "resolvedCommit"):
                if key not in entry:
                    err(f"{where}.{key}", f"missing required field {key!r}")
            source = entry.get("sourceIdentity")
            if not isinstance(source, str) or not _is_source_identity(source):
                err(f"{where}.sourceIdentity", "must be a normalized 'git+https://' identity")
            ref = entry.get("ref")
            if not isinstance(ref, str) or not ref:
                err(f"{where}.ref", "'ref' must be a non-empty string")
            commit = entry.get("resolvedCommit")
            if not is_hex40(commit):
                err(f"{where}.resolvedCommit", "'resolvedCommit' must be a lowercase 40-hex SHA")
            if isinstance(source, str) and isinstance(ref, str):
                key_pair = f"{source}\x00{ref}"
                if key_pair in seen_res:
                    err(where, "duplicate resolution for source and ref")
                else:
                    seen_res.add(key_pair)
    artifacts = obj["artifacts"]
    if not isinstance(artifacts, list):
        err("artifacts", "'artifacts' must be an array")
    else:
        seen_names: set[str] = set()
        for index, entry in enumerate(artifacts):
            where = f"artifacts[{index}]"
            if not isinstance(entry, dict):
                err(where, "must be an object")
                continue
            for key in sorted(entry.keys()):
                if key not in ARTIFACT_FIELDS:
                    err(f"{where}.{key}", f"unknown field {key!r}")
            for key in sorted(ARTIFACT_FIELDS):
                if key not in entry:
                    err(f"{where}.{key}", f"missing required field {key!r}")
            name = entry.get("name")
            ident = entry.get("id")
            if not isinstance(name, str) or not name:
                err(f"{where}.name", "'name' must be a non-empty string")
            else:
                if name in seen_names:
                    err(f"{where}.name", f"duplicate artifact name {name!r}")
                else:
                    seen_names.add(name)
                if ident != f"skill:{name}":
                    err(f"{where}.id", "'id' must be 'skill:<name>'")
            source = entry.get("sourceIdentity")
            if not isinstance(source, str) or not _is_source_identity(source):
                err(f"{where}.sourceIdentity", "must be a normalized 'git+https://' identity")
            commit = entry.get("resolvedCommit")
            if not is_hex40(commit):
                err(f"{where}.resolvedCommit", "'resolvedCommit' must be a lowercase 40-hex SHA")
            for pkey in ("relativeSourcePath", "storeRelativePath"):
                pval = entry.get(pkey)
                if not _is_rel_path(pval):
                    err(f"{where}.{pkey}", f"'{pkey}' must be a relative path without '..'")
            verification = entry.get("verification")
            if verification not in _VALID_VERIFICATIONS:
                err(f"{where}.verification", "'verification' must be 'source' or 'recorded-installation'")
            content = entry.get("installerContent")
            if not isinstance(content, dict):
                err(f"{where}.installerContent", "'installerContent' must be an object")
            else:
                for key in sorted(content.keys()):
                    if key not in INSTALLER_CONTENT_FIELDS:
                        err(f"{where}.installerContent.{key}", f"unknown field {key!r}")
                for key in ("algorithm", "sourceHash", "installedHash"):
                    if key not in content:
                        err(f"{where}.installerContent.{key}", f"missing required field {key!r}")
                algo = content.get("algorithm")
                if not isinstance(algo, str) or not algo:
                    err(f"{where}.installerContent.algorithm", "must be a non-empty string")
                for hkey in ("sourceHash", "installedHash"):
                    hval = content.get(hkey)
                    if not is_hex64(hval):
                        err(f"{where}.installerContent.{hkey}", "must be a lowercase 64-hex SHA-256")
            overlays = entry.get("overlays")
            if not isinstance(overlays, list):
                err(f"{where}.overlays", "'overlays' must be an array")
            else:
                for opos, oentry in enumerate(overlays):
                    owhere = f"{where}.overlays[{opos}]"
                    if not isinstance(oentry, dict):
                        err(owhere, "must be an object")
                        continue
                    for key in sorted(oentry.keys()):
                        if key not in OVERLAY_FIELDS:
                            err(f"{owhere}.{key}", f"unknown field {key!r}")
                    for key in ("id", "version", "paths"):
                        if key not in oentry:
                            err(f"{owhere}.{key}", f"missing required field {key!r}")
                    oid = oentry.get("id")
                    if not isinstance(oid, str) or not oid:
                        err(f"{owhere}.id", "must be a non-empty string")
                    over = oentry.get("version")
                    if not isinstance(over, str) or not over:
                        err(f"{owhere}.version", "must be a non-empty string")
                    opaths = oentry.get("paths")
                    if not isinstance(opaths, list):
                        err(f"{owhere}.paths", "must be an array")
                    else:
                        for ppos, pval in enumerate(opaths):
                            if not _is_rel_path(pval):
                                err(f"{owhere}.paths[{ppos}]", "must be a relative path without '..'")
            files = entry.get("files")
            if not isinstance(files, list):
                err(f"{where}.files", "'files' must be an array")
            else:
                seen_paths: set[bytes] = set()
                prev_key: bytes | None = None
                for fpos, fentry in enumerate(files):
                    fwhere = f"{where}.files[{fpos}]"
                    if not isinstance(fentry, dict):
                        err(fwhere, "must be an object")
                        continue
                    for key in sorted(fentry.keys()):
                        if key not in FILE_FIELDS:
                            err(f"{fwhere}.{key}", f"unknown field {key!r}")
                    for key in ("path", "sha256", "executable"):
                        if key not in fentry:
                            err(f"{fwhere}.{key}", f"missing required field {key!r}")
                    fpath = fentry.get("path")
                    if not _is_rel_path(fpath):
                        err(f"{fwhere}.path", "'path' must be a relative path without '..'")
                    fhash = fentry.get("sha256")
                    if not is_hex64(fhash):
                        err(f"{fwhere}.sha256", "'sha256' must be a lowercase 64-hex SHA-256")
                    fexec = fentry.get("executable")
                    if type(fexec) is not bool:
                        err(f"{fwhere}.executable", "'executable' must be a boolean")
                    if isinstance(fpath, str):
                        key_bytes = fpath.encode("utf-8")
                        if key_bytes in seen_paths:
                            err(fwhere, f"duplicate file path {fpath!r}")
                        else:
                            seen_paths.add(key_bytes)
                        if prev_key is not None and key_bytes < prev_key:
                            err(fwhere, f"file records must be sorted by UTF-8 path bytes")
                        prev_key = key_bytes if prev_key is None or key_bytes > prev_key else prev_key
            tree = entry.get("treeSha256")
            if not is_hex64(tree):
                err(f"{where}.treeSha256", "'treeSha256' must be a lowercase 64-hex SHA-256")
            else:
                files_val = entry.get("files")
                if isinstance(files_val, list) and all(isinstance(f, dict) for f in files_val):
                    try:
                        expected = sha256_bytes(dumps_canonical(files_val))
                    except ValueError:
                        expected = ""
                    if expected and tree != expected:
                        err(f"{where}.treeSha256", "does not match the canonical 'files' digest")
    if errors:
        return None, errors
    return dict(obj), []


def build_validation(
    workflow_sha256: str,
    requirements_sha256: str,
    manifest_sha256: str,
    policy_sha256: str,
    prompts: list[dict[str, str]],
) -> dict[str, object]:
    """Build the ``validation.json`` document."""
    return {
        "kind": VALIDATION_KIND,
        "manifestSha256": manifest_sha256,
        "policySha256": policy_sha256,
        "prompts": prompts,
        "requirementsSha256": requirements_sha256,
        "schemaVersion": SCHEMA_VERSION,
        "workflowSha256": workflow_sha256,
    }


def validate_validation_object(obj: object) -> tuple[dict[str, Any] | None, list[RecordError]]:
    """Strictly validate a parsed ``validation.json`` document."""
    errors: list[RecordError] = []

    def err(path: str, detail: str) -> None:
        errors.append(RecordError(path=path, detail=detail))

    if not isinstance(obj, dict):
        err("(root)", "must be a JSON object")
        return None, errors
    if contains_null(obj):
        err("(root)", "null values are not allowed")
        return None, errors
    for key in sorted(obj.keys()):
        if key not in VALIDATION_FIELDS:
            err(key, f"unknown field {key!r}")
    for key in ("schemaVersion", "kind", "workflowSha256", "requirementsSha256", "manifestSha256", "policySha256", "prompts"):
        if key not in obj:
            err(key, f"missing required field {key!r}")
    if errors:
        return None, errors
    assert isinstance(obj, dict)
    version = obj["schemaVersion"]
    if type(version) is not int or version != 1:
        err("schemaVersion", f"unsupported version {version!r}: must be 1")
    if obj["kind"] != VALIDATION_KIND:
        err("kind", f"unsupported kind {obj['kind']!r}")
    for key in ("workflowSha256", "requirementsSha256", "manifestSha256", "policySha256"):
        if not is_hex64(obj[key]):
            err(key, f"'{key}' must be a lowercase 64-hex SHA-256")
    prompts = obj["prompts"]
    if not isinstance(prompts, list):
        err("prompts", "'prompts' must be an array")
        return None, errors
    seen: set[bytes] = set()
    prev_key: bytes | None = None
    for index, entry in enumerate(prompts):
        where = f"prompts[{index}]"
        if not isinstance(entry, dict):
            err(where, "must be an object")
            continue
        for key in sorted(entry.keys()):
            if key not in PROMPT_FIELDS:
                err(f"{where}.{key}", f"unknown field {key!r}")
        for key in ("reference", "sha256"):
            if key not in entry:
                err(f"{where}.{key}", f"missing required field {key!r}")
        ref = entry.get("reference")
        if not isinstance(ref, str) or not ref:
            err(f"{where}.reference", "'reference' must be a non-empty string")
        elif ref.startswith("/") or ".." in ref.split("/"):
            err(f"{where}.reference", "'reference' must be a relative locator without '..'")
        digest = entry.get("sha256")
        if not is_hex64(digest):
            err(f"{where}.sha256", "'sha256' must be a lowercase 64-hex SHA-256")
        if isinstance(ref, str) and ref:
            key_bytes = ref.encode("utf-8")
            if key_bytes in seen:
                err(where, f"duplicate prompt reference {ref!r}")
            else:
                seen.add(key_bytes)
            if prev_key is not None and key_bytes < prev_key:
                err(where, "prompt records must be sorted by UTF-8 reference bytes")
            if prev_key is None or key_bytes > prev_key:
                prev_key = key_bytes
    if errors:
        return None, errors
    return dict(obj), []


def parse_record_file(path: Path) -> tuple[bytes, Any | None, list[RecordError]]:
    """Read and strictly parse a generated record file.

    Returns raw bytes, parsed value (or ``None``) and faults. Duplicate keys,
    unknown JSON shapes and floats are parse faults here; unknown fields and
    nulls are reported by the per-record validators.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return b"", None, [RecordError(path=str(path), detail=f"cannot read file: {exc}")]
    value, problem = loads_strict(raw)
    if problem is not None:
        return raw, None, [RecordError(path=str(path), detail=problem)]
    return raw, value, []


def check_canonical_bytes(raw: bytes, value: object, path: Path) -> RecordError | None:
    """Refuse stored records whose bytes are not the canonical encoding."""
    try:
        expected = dumps_canonical(value)
    except ValueError as exc:
        return RecordError(path=str(path), detail=str(exc))
    if raw != expected:
        return RecordError(path=str(path), detail="file bytes are not canonical JSON")
    return None
