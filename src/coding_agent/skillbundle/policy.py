"""Strict parsing for ``skill-policy.json`` (SKV3, phased validation).

Contract: the reviewed closure/Companion File resolution (issue #374, amended
by issue #383) and the phased-validation resolution (issue #375): the policy
is an explicit trusted operator input, parsed strictly from ``prepare``
onwards; ``config validate`` neither requires nor creates it.

Schema (``kind: coding-agent.skill-bundle-policy``, ``schemaVersion: 1``)::

    {schemaVersion, kind, reviews: [
      {sourceIdentity, name, skillSha256, requires, files, references, reason}
    ]}

``files[].disposition`` is ``inject`` or ``not-delivered``; ``references``
entries are ``not-a-skill-reference`` (no ``target``/``notice``) or
``unavailable-nonrequired-reference`` (``target`` and ``notice`` required).
Rejects duplicate keys, unknown fields, nulls, malformed digests, unknown
versions and ambiguous duplicate reviews. Diagnostics never echo file
contents or credential-bearing locators.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.skillbundle.canonical import (
    HEX64_RE,
    loads_strict,
)

POLICY_KIND = "coding-agent.skill-bundle-policy"
POLICY_SCHEMA_VERSION = 1

_TOP_FIELDS = frozenset({"schemaVersion", "kind", "reviews"})
_REVIEW_FIELDS = frozenset(
    {"sourceIdentity", "name", "skillSha256", "requires", "files", "references", "reason"}
)
_FILE_FIELDS = frozenset({"path", "sha256", "executable", "disposition", "reason"})
_REF_FIELDS = frozenset({"origin", "text", "classification", "target", "reason", "notice"})

_VALID_DISPOSITIONS = frozenset({"inject", "not-delivered"})
_VALID_CLASSIFICATIONS = frozenset({"not-a-skill-reference", "unavailable-nonrequired-reference"})


@dataclass(frozen=True)
class PolicyError:
    """One strict policy refusal naming the JSON path of the fault."""

    path: str
    detail: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.detail}"
        return self.detail


@dataclass(frozen=True)
class PolicyFile:
    """One reviewed content-file record (every non-``SKILL.md`` file)."""

    path: str
    sha256: str
    executable: bool
    disposition: str
    reason: str


@dataclass(frozen=True)
class PolicyReference:
    """One reviewed reference/exclusion record."""

    origin: str
    text: str
    classification: str
    target: str | None
    reason: str
    notice: str | None


@dataclass(frozen=True)
class PolicyReview:
    """One operator-approved review record."""

    source_identity: str
    name: str
    skill_sha256: str
    requires: tuple[str, ...]
    files: tuple[PolicyFile, ...]
    references: tuple[PolicyReference, ...]
    reason: str


@dataclass(frozen=True)
class SkillPolicy:
    """A validated ``skill-policy.json`` document."""

    schema_version: int
    kind: str
    reviews: tuple[PolicyReview, ...]


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


def _is_valid_rel_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("/") or value.startswith("\\"):
        return False
    if ".." in value.split("/"):
        return False
    if value in (".", ""):
        return False
    # Path-shaped names (containing a slash) are allowed for file records
    # (nested companions); bare skill-name checks happen elsewhere.
    if "\x00" in value:
        return False
    return True


def validate_policy_object(obj: object) -> tuple[SkillPolicy | None, list[PolicyError]]:
    """Validate a strictly-parsed policy document; returns policy plus faults."""
    errors: list[PolicyError] = []

    def err(path: str, detail: str) -> None:
        errors.append(PolicyError(path=path, detail=detail))

    if not isinstance(obj, dict):
        err("(root)", "must be a JSON object")
        return None, errors
    for key in sorted(obj.keys()):
        if key not in _TOP_FIELDS:
            err(key, f"unknown field {key!r}")
    for key in ("schemaVersion", "kind", "reviews"):
        if key not in obj:
            err(key, f"missing required field {key!r}")
    if errors:
        return None, errors
    version = obj["schemaVersion"]
    if type(version) is not int or version != POLICY_SCHEMA_VERSION:
        err("schemaVersion", f"unsupported version {version!r}: must be 1")
    kind = obj["kind"]
    if kind != POLICY_KIND:
        err("kind", f"unsupported kind {kind!r}: must be {POLICY_KIND!r}")
    raw_reviews = obj["reviews"]
    if not isinstance(raw_reviews, list):
        err("reviews", "'reviews' must be an array")
        return None, errors
    if version != POLICY_SCHEMA_VERSION or kind != POLICY_KIND or not isinstance(raw_reviews, list):
        return None, errors

    reviews: list[PolicyReview] = []
    seen_content_keys: dict[tuple[str, str, str, tuple[str, ...]], str] = {}
    for index, entry in enumerate(raw_reviews):
        where = f"reviews[{index}]"
        review = _validate_review(entry, where, errors)
        if review is None:
            continue
        # Ambiguous duplicate reviews: same source/name/skill-bytes/file-set
        # matching more than one review is invalid.
        file_key = tuple(sorted(f"{f.path}={f.sha256}={f.executable}" for f in review.files))
        content_key = (review.source_identity, review.name, review.skill_sha256, file_key)
        first = seen_content_keys.get(content_key)
        if first is not None:
            err(where, f"ambiguous duplicate review: already reviewed at {first}")
            continue
        seen_content_keys[content_key] = where
        reviews.append(review)
    if errors:
        return None, errors
    return SkillPolicy(schema_version=1, kind=POLICY_KIND, reviews=tuple(reviews)), []


def _validate_review(entry: object, where: str, errors: list[PolicyError]) -> PolicyReview | None:
    def err(path: str, detail: str) -> None:
        errors.append(PolicyError(path=path, detail=detail))

    if not isinstance(entry, dict):
        err(where, "must be an object")
        return None
    for key in sorted(entry.keys()):
        if key not in _REVIEW_FIELDS:
            err(f"{where}.{key}", f"unknown field {key!r}")
    for key in sorted(_REVIEW_FIELDS):
        if key not in entry:
            err(f"{where}.{key}", f"missing required field {key!r}")
    if any(e.path.startswith(where) for e in errors):
        # Only consider faults anchored at this review; other reviews may
        # still validate independently.
        pass
    before = len(errors)
    if not isinstance(entry, dict):
        return None
    ok = True
    source = entry.get("sourceIdentity")
    if not isinstance(source, str) or not source:
        err(f"{where}.sourceIdentity", "'sourceIdentity' must be a non-empty string")
        ok = False
    elif not _is_source_identity(source):
        err(f"{where}.sourceIdentity", "'sourceIdentity' must be a normalized 'git+https://' identity")
        ok = False
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        err(f"{where}.name", "'name' must be a non-empty string")
        ok = False
    elif "/" in name or name.startswith(".") or ".." in name:
        err(f"{where}.name", "'name' must be an exact canonical skill name")
        ok = False
    digest = entry.get("skillSha256")
    if not isinstance(digest, str) or HEX64_RE.match(digest) is None:
        err(f"{where}.skillSha256", "'skillSha256' must be a lowercase 64-hex SHA-256")
        ok = False
    requires_raw = entry.get("requires")
    requires: list[str] = []
    if not isinstance(requires_raw, list):
        err(f"{where}.requires", "'requires' must be an array of unique skill names")
        ok = False
    else:
        seen_req: set[str] = set()
        for pos, item in enumerate(requires_raw):
            item_where = f"{where}.requires[{pos}]"
            if not isinstance(item, str) or not item:
                err(item_where, "skill name must be a non-empty string")
                ok = False
                continue
            if item in seen_req:
                err(item_where, f"duplicate required skill {item!r}")
                ok = False
                continue
            seen_req.add(item)
            requires.append(item)
    files_raw = entry.get("files")
    files: list[PolicyFile] = []
    if not isinstance(files_raw, list):
        err(f"{where}.files", "'files' must be an array")
        ok = False
    else:
        seen_paths: set[str] = set()
        for pos, fentry in enumerate(files_raw):
            fwhere = f"{where}.files[{pos}]"
            record = _validate_file(fentry, fwhere, errors)
            if record is None:
                ok = False
                continue
            if record.path in seen_paths:
                err(fwhere, f"duplicate file path {record.path!r}")
                ok = False
                continue
            seen_paths.add(record.path)
            files.append(record)
    refs_raw = entry.get("references")
    references: list[PolicyReference] = []
    if not isinstance(refs_raw, list):
        err(f"{where}.references", "'references' must be an array")
        ok = False
    else:
        seen_refs: set[str] = set()
        for pos, rentry in enumerate(refs_raw):
            rwhere = f"{where}.references[{pos}]"
            ref_record = _validate_reference(rentry, rwhere, errors)
            if ref_record is None:
                ok = False
                continue
            key = f"{ref_record.origin}\x00{ref_record.text}\x00{ref_record.classification}\x00{ref_record.target}"
            if key in seen_refs:
                err(rwhere, "duplicate reference record")
                ok = False
                continue
            seen_refs.add(key)
            references.append(ref_record)
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason:
        err(f"{where}.reason", "'reason' must be a non-empty string")
        ok = False
    # Representable contradiction: a required dependency cannot also be
    # waived as an unavailable-nonrequired reference target.
    if ok:
        required_set = set(requires)
        for reference in references:
            if reference.classification == "unavailable-nonrequired-reference" and reference.target in required_set:
                err(where, f"reference target {reference.target!r} is also a required dependency")
                ok = False
                break
        if isinstance(name, str) and name in required_set:
            err(f"{where}.requires", "a skill cannot require itself")
            ok = False
    if len(errors) != before:
        return None
    if not ok:
        return None
    assert isinstance(source, str) and isinstance(name, str)
    assert isinstance(digest, str) and isinstance(reason, str)
    return PolicyReview(
        source_identity=source,
        name=name,
        skill_sha256=digest,
        requires=tuple(requires),
        files=tuple(files),
        references=tuple(references),
        reason=reason,
    )


def _validate_file(entry: object, where: str, errors: list[PolicyError]) -> PolicyFile | None:
    def err(path: str, detail: str) -> None:
        errors.append(PolicyError(path=path, detail=detail))

    if not isinstance(entry, dict):
        err(where, "must be an object")
        return None
    for key in sorted(entry.keys()):
        if key not in _FILE_FIELDS:
            err(f"{where}.{key}", f"unknown field {key!r}")
    for key in sorted(_FILE_FIELDS):
        if key not in entry:
            err(f"{where}.{key}", f"missing required field {key!r}")
    if any(e.path == where or e.path.startswith(where + ".") for e in errors[len(errors):]):
        pass
    before = len(errors)
    path = entry.get("path")
    if not _is_valid_rel_path(path):
        err(f"{where}.path", "'path' must be a relative artifact path without '..'")
    sha = entry.get("sha256")
    if not isinstance(sha, str) or HEX64_RE.match(sha) is None:
        err(f"{where}.sha256", "'sha256' must be a lowercase 64-hex SHA-256")
    executable = entry.get("executable")
    if type(executable) is not bool:
        err(f"{where}.executable", "'executable' must be a boolean")
    disposition = entry.get("disposition")
    if disposition not in _VALID_DISPOSITIONS:
        err(f"{where}.disposition", "'disposition' must be 'inject' or 'not-delivered'")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason:
        err(f"{where}.reason", "'reason' must be a non-empty string")
    if len(errors) != before:
        return None
    assert isinstance(path, str) and isinstance(sha, str)
    assert type(executable) is bool and isinstance(disposition, str) and isinstance(reason, str)
    return PolicyFile(path=path, sha256=sha, executable=executable, disposition=disposition, reason=reason)


def _validate_reference(entry: object, where: str, errors: list[PolicyError]) -> PolicyReference | None:
    def err(path: str, detail: str) -> None:
        errors.append(PolicyError(path=path, detail=detail))

    if not isinstance(entry, dict):
        err(where, "must be an object")
        return None
    for key in sorted(entry.keys()):
        if key not in _REF_FIELDS:
            err(f"{where}.{key}", f"unknown field {key!r}")
    for key in ("origin", "text", "classification", "reason"):
        if key not in entry:
            err(f"{where}.{key}", f"missing required field {key!r}")
    before = len(errors)
    origin = entry.get("origin")
    if not isinstance(origin, str) or not origin:
        err(f"{where}.origin", "'origin' must be a non-empty string")
    text = entry.get("text")
    if not isinstance(text, str) or not text:
        err(f"{where}.text", "'text' must be a non-empty excerpt")
    classification = entry.get("classification")
    if classification not in _VALID_CLASSIFICATIONS:
        err(f"{where}.classification", "'classification' must be a known value")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason:
        err(f"{where}.reason", "'reason' must be a non-empty string")
    target = entry.get("target")
    notice = entry.get("notice")
    if classification == "not-a-skill-reference":
        if "target" in entry:
            err(f"{where}.target", "'target' must be absent for 'not-a-skill-reference'")
        if "notice" in entry:
            err(f"{where}.notice", "'notice' must be absent for 'not-a-skill-reference'")
    elif classification == "unavailable-nonrequired-reference":
        if not isinstance(target, str) or not target:
            err(f"{where}.target", "'target' must be a non-empty skill/command name")
        if not isinstance(notice, str) or not notice:
            err(f"{where}.notice", "'notice' must be a non-empty operator explanation")
    if len(errors) != before:
        return None
    assert isinstance(origin, str) and isinstance(text, str)
    assert isinstance(classification, str) and isinstance(reason, str)
    target_value = target if isinstance(target, str) else None
    notice_value = notice if isinstance(notice, str) else None
    return PolicyReference(
        origin=origin,
        text=text,
        classification=classification,
        target=target_value,
        reason=reason,
        notice=notice_value,
    )


def parse_policy_bytes(raw: bytes) -> tuple[SkillPolicy | None, list[PolicyError]]:
    """Parse raw ``skill-policy.json`` bytes strictly.

    Duplicate JSON keys, unknown fields, nulls, malformed digests and
    unknown versions are refusals. Never echoes file contents.
    """
    value, problem = loads_strict(raw)
    if problem is not None:
        return None, [PolicyError(path="(policy)", detail=problem)]
    if value is None or isinstance(value, (list, str, int, float, bool)):
        if value is None:
            return None, [PolicyError(path="(policy)", detail="must not be null")]
        return None, [PolicyError(path="(root)", detail="must be a JSON object")]
    from coding_agent.skillbundle.canonical import contains_null

    if contains_null(value):
        return None, [PolicyError(path="(policy)", detail="null values are not allowed")]
    return validate_policy_object(value)


def load_policy_file(path: Path) -> tuple[bytes, SkillPolicy | None, list[PolicyError]]:
    """Read and strictly parse the policy file; returns raw bytes plus result."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return b"", None, [PolicyError(path=str(path), detail=f"cannot read policy file: {exc}")]
    policy, faults = parse_policy_bytes(raw)
    fixed: list[PolicyError] = []
    for fault in faults:
        if fault.path in ("(policy)", "(root)"):
            fixed.append(PolicyError(path=str(path), detail=fault.detail))
        elif fault.path.startswith(str(path)):
            fixed.append(fault)
        else:
            # Anchor policy-internal paths under the file for CLI diagnostics.
            fixed.append(PolicyError(path=f"{path}: {fault.path}", detail=fault.detail))
    if policy is None:
        return raw, None, fixed
    return raw, policy, []
