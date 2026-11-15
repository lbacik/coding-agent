"""Offline draft generator for ``skill-policy.json`` (operator review input).

Contract: the Skill Bundle Policy is an explicit trusted operator input
(issues #374 and #385, user story 24). This helper drafts a proposed policy
for the operator to review: it carries forward every existing review whose
identity still matches exactly, and drafts placeholder-only reviews for new
or changed members. It never approves anything: every field that needs a
human decision is written as JSON ``null``, and the strict policy parser
rejects nulls, so an unedited draft always fails validation.

Static except for reading: only the Workflow Configuration file, the
explicitly selected Base Store and the optional current policy file are
read. No network access, no GitHub calls, no writes except the ``--out``
file the caller chooses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.skillbundle.canonical import (
    dumps_canonical,
    loads_strict,
)
from coding_agent.skillbundle.deployment import (
    REVIEW_AID_REF_RE,
    _check_inject_file,
    _matching_reviews,
    _ordered_membership,
)
from coding_agent.skillbundle.installer import (
    base_store_for,
    collect_inventory,
    is_full_commit_ref,
    load_provenance,
    provenance_source,
)
from coding_agent.skillbundle.policy import (
    POLICY_KIND,
    SkillPolicy,
    validate_policy_object,
)
from coding_agent.workflow.v3_validate import validate_v3_file


@dataclass(frozen=True)
class DraftMemberInput:
    """One declared member read from the installed store, before matching."""

    name: str
    source: str
    skill_sha256: str
    records: tuple[dict[str, object], ...]
    contents: dict[str, bytes]


@dataclass(frozen=True)
class DraftOutcome:
    """The result of building a policy draft (no writes performed)."""

    ok: bool
    faults: tuple[str, ...]
    document: dict[str, object] | None
    report: str
    placeholders: int


def _same_file(first: Path, second: Path) -> bool:
    """Whether two paths resolve to the same filesystem entry."""
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return False


def _content_lines(raw: bytes) -> list[str] | None:
    """Split ``raw`` into lines, or ``None`` when it is not plain text."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if raw.startswith(b"#!") or "\x00" in text:
        return None
    return text.splitlines()


def _excerpt_for(lines: list[str], candidate: str) -> str:
    """The first stripped line containing ``candidate`` (never empty)."""
    for line in lines:
        if candidate in line:
            stripped = line.strip()
            if stripped:
                return stripped
    return candidate


def _reference_rulings(
    contents: dict[str, bytes],
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    """One placeholder ruling per review-aid candidate in scannable text.

    The scannable set is ``SKILL.md`` plus every companion file the
    deployment could ever scan: a file that could never be ``inject``
    (administrative, executable, non-UTF-8, script or binary, by the same
    ``_check_inject_file`` rule preparation enforces) is never delivered
    text, so it contributes no candidates. Rulings are ordered by origin
    path bytes, then candidate, so the output is deterministic.
    """
    scannable: dict[str, list[str]] = {}
    skill_raw = contents.get("SKILL.md")
    if skill_raw is not None:
        lines = _content_lines(skill_raw)
        if lines is not None:
            scannable["SKILL.md"] = lines
    for record in records:
        rel = str(record["path"])
        if rel == "SKILL.md":
            continue
        raw = contents.get(rel)
        if raw is None:
            continue
        if _check_inject_file(rel, raw, bool(record["executable"]), "inject") is not None:
            continue
        lines = _content_lines(raw)
        if lines is not None:
            scannable[rel] = lines
    rulings: list[dict[str, object]] = []
    for origin in sorted(scannable.keys(), key=lambda s: s.encode("utf-8")):
        lines = scannable[origin]
        joined = "\n".join(lines)
        candidates = sorted(
            {match.group(0) for match in REVIEW_AID_REF_RE.finditer(joined)}
        )
        for candidate in candidates:
            rulings.append(
                {
                    "classification": None,
                    "origin": origin,
                    "reason": None,
                    "text": _excerpt_for(lines, candidate),
                }
            )
    return rulings


def _requires_candidates(
    contents: dict[str, bytes],
    records: list[dict[str, object]],
    member_name: str,
    declared_names: set[str],
) -> list[str]:
    """Declared member names mentioned as ``/name`` in scannable text.

    Suggestions only: they are reported for the operator and never written
    into the draft (``requires`` is drafted as an empty array).
    """
    mentioned: set[str] = set()
    texts: list[str] = []
    skill_raw = contents.get("SKILL.md")
    if skill_raw is not None:
        try:
            texts.append(skill_raw.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    for record in records:
        rel = str(record["path"])
        if rel == "SKILL.md":
            continue
        raw = contents.get(rel)
        if raw is None:
            continue
        if _check_inject_file(rel, raw, bool(record["executable"]), "inject") is not None:
            continue
        try:
            texts.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    for text in texts:
        for match in REVIEW_AID_REF_RE.finditer(text):
            bare = match.group(0)[1:]
            if bare in declared_names and bare != member_name:
                mentioned.add(bare)
    return sorted(mentioned)


def _read_member(
    *,
    name: str,
    source: str,
    ref: str | None,
    home: Path,
    provenance: dict[str, dict[str, Any]],
    faults: list[str],
) -> DraftMemberInput | None:
    """Read one declared member from the installed store (offline)."""
    where = f"skills:{name}"
    entry = provenance.get(name)
    if entry is None:
        faults.append(f"{where}: skill {name!r} has no installer provenance")
        return None
    actual_source = provenance_source(entry)
    if actual_source != source:
        faults.append(f"{where}: skill {name!r} has the wrong source")
        return None
    recorded = entry.get("resolvedCommit")
    if ref is not None and is_full_commit_ref(ref) and recorded != ref:
        faults.append(
            f"{where}: skill {name!r} provenance does not match the exact-commit ref"
        )
        return None
    base = base_store_for(home)
    try:
        resolved_base = base.resolve()
    except OSError as exc:
        faults.append(f"{where}: cannot resolve Base Store: {exc}")
        return None
    artifact_dir = base / name
    try:
        if artifact_dir.is_symlink():
            faults.append(f"{where}: artifact path must not be a symlink")
            return None
        if not artifact_dir.is_dir():
            faults.append(f"{where}: skill {name!r} is not installed in the Base Store")
            return None
    except OSError as exc:
        faults.append(f"{where}: cannot inspect artifact: {exc}")
        return None
    records, _, inv_fault = collect_inventory(artifact_dir, base_store=resolved_base)
    if inv_fault is not None:
        faults.append(f"{where}: {inv_fault.path}: {inv_fault.detail}")
        return None
    if not any(str(item["path"]) == "SKILL.md" for item in records):
        faults.append(f"{where}: skill {name!r} is missing SKILL.md")
        return None
    contents: dict[str, bytes] = {}
    for item in records:
        rel = str(item["path"])
        try:
            contents[rel] = (artifact_dir / rel).read_bytes()
        except OSError as exc:
            faults.append(f"{where}: cannot read artifact file {rel!r}: {exc}")
            return None
    skill_sha = hashlib.sha256(contents["SKILL.md"]).hexdigest()
    return DraftMemberInput(
        name=name,
        source=source,
        skill_sha256=skill_sha,
        records=tuple(records),
        contents=contents,
    )


def build_draft(
    *,
    config: Path,
    home: Path,
    policy: Path | None,
) -> DraftOutcome:
    """Build a draft policy document plus the operator review report.

    Reads the Workflow Configuration, the installed store under ``home``
    and the optional current policy file. Performs no writes. The returned
    document uses JSON ``null`` for every undecided field, so it always
    fails strict policy validation until the operator resolves each one.
    """
    faults: list[str] = []

    try:
        config.read_bytes()
    except OSError as exc:
        return DraftOutcome(
            ok=False,
            faults=(f"configuration: cannot read workflow configuration: {exc}",),
            document=None,
            report="",
            placeholders=0,
        )
    cfg, config_faults = validate_v3_file(config)
    if cfg is None:
        details = "; ".join(str(fault) for fault in config_faults) or "invalid"
        return DraftOutcome(
            ok=False,
            faults=(f"configuration: {details}",),
            document=None,
            report="",
            placeholders=0,
        )
    for config_fault in config_faults:
        faults.append(f"configuration: {config_fault}")
    if faults:
        return DraftOutcome(
            ok=False, faults=tuple(faults), document=None, report="", placeholders=0
        )
    membership = _ordered_membership(cfg)

    raw_reviews: list[dict[str, object]] = []
    policy_obj: SkillPolicy | None = None
    if policy is not None:
        try:
            policy_raw = policy.read_bytes()
        except OSError as exc:
            return DraftOutcome(
                ok=False,
                faults=(f"policy: cannot read policy file: {exc}",),
                document=None,
                report="",
                placeholders=0,
            )
        value, problem = loads_strict(policy_raw)
        if problem is not None or not isinstance(value, dict):
            return DraftOutcome(
                ok=False,
                faults=(f"policy: {problem or 'must be a JSON object'}",),
                document=None,
                report="",
                placeholders=0,
            )
        parsed, policy_faults = validate_policy_object(value)
        if parsed is None:
            details = "; ".join(str(fault) for fault in policy_faults) or "invalid"
            return DraftOutcome(
                ok=False,
                faults=(f"policy: {details}",),
                document=None,
                report="",
                placeholders=0,
            )
        policy_obj = parsed
        raw_value = value.get("reviews")
        assert isinstance(raw_value, list)
        for item in raw_value:
            assert isinstance(item, dict)
            raw_reviews.append(item)

    provenance, prov_fault = load_provenance(home)
    if prov_fault is not None:
        return DraftOutcome(
            ok=False,
            faults=(f"bundle: {prov_fault.path}: {prov_fault.detail}",),
            document=None,
            report="",
            placeholders=0,
        )

    inputs: list[DraftMemberInput | None] = []
    for name, source, ref in membership:
        inputs.append(
            _read_member(
                name=name,
                source=source,
                ref=ref,
                home=home,
                provenance=provenance,
                faults=faults,
            )
        )
    if faults:
        return DraftOutcome(
            ok=False, faults=tuple(faults), document=None, report="", placeholders=0
        )

    review_by_id: dict[int, dict[str, object]] = {}
    if policy_obj is not None:
        for raw, review in zip(raw_reviews, policy_obj.reviews, strict=True):
            review_by_id[id(review)] = raw

    declared_names = {name for name, _, _ in membership}
    matched_ids: set[int] = set()
    carried: list[dict[str, object]] = []
    drafted: list[dict[str, object]] = []
    report_lines: list[str] = []
    unchanged = 0
    changed = 0
    new = 0
    placeholders = 0

    old_by_name: dict[tuple[str, str], list[dict[str, object]]] = {}
    for raw in raw_reviews:
        key = (str(raw["sourceIdentity"]), str(raw["name"]))
        old_by_name.setdefault(key, []).append(raw)

    for member_input, (name, source, _ref) in zip(inputs, membership, strict=True):
        assert member_input is not None
        files_key = frozenset(
            (str(item["path"]), str(item["sha256"]), bool(item["executable"]))
            for item in member_input.records
            if str(item["path"]) != "SKILL.md"
        )
        matched = (
            _matching_reviews(
                policy_obj, source, name, member_input.skill_sha256, files_key
            )
            if policy_obj is not None
            else []
        )
        if matched:
            for review in matched:
                matched_ids.add(id(review))
                carried.append(review_by_id[id(review)])
            unchanged += 1
            report_lines.append(f"member {name!r}: unchanged")
            continue
        old_same = old_by_name.get((source, name), [])
        old = old_same[0] if old_same else None
        if old is None:
            new += 1
            report_lines.append(f"member {name!r}: new (no earlier review)")
        else:
            changed += 1
            report_lines.append(f"member {name!r}: changed (no review still matches)")
            old_skill = str(old["skillSha256"])
            if old_skill != member_input.skill_sha256:
                report_lines.append(f"  SKILL.md changed for {name!r}")
            old_files = old["files"]
            assert isinstance(old_files, list)
            old_by_path = {
                str(item["path"]): item for item in old_files if isinstance(item, dict)
            }
            new_records = [
                item for item in member_input.records if str(item["path"]) != "SKILL.md"
            ]
            new_paths = {str(item["path"]) for item in new_records}
            for item in new_records:
                rel = str(item["path"])
                previous = old_by_path.get(rel)
                if previous is None:
                    report_lines.append(f"  file {rel!r}: added")
                else:
                    if str(previous["sha256"]) != str(item["sha256"]):
                        report_lines.append(f"  file {rel!r}: content changed")
                    if bool(previous["executable"]) != bool(item["executable"]):
                        report_lines.append(
                            f"  file {rel!r}: executable bit changed "
                            f"(was {bool(previous['executable'])}, "
                            f"now {bool(item['executable'])})"
                        )
                    report_lines.append(
                        f"  suggestion (unconfirmed): file {rel!r} was previously "
                        f"ruled {str(previous['disposition'])!r}"
                    )
            for rel in sorted(
                set(old_by_path) - new_paths, key=lambda s: s.encode("utf-8")
            ):
                report_lines.append(f"  file {rel!r}: removed")
        file_entries: list[dict[str, object]] = []
        for item in member_input.records:
            rel = str(item["path"])
            if rel == "SKILL.md":
                continue
            file_entries.append(
                {
                    "disposition": None,
                    "executable": bool(item["executable"]),
                    "path": rel,
                    "reason": None,
                    "sha256": str(item["sha256"]),
                }
            )
            placeholders += 2
        non_skill = [
            item for item in member_input.records if str(item["path"]) != "SKILL.md"
        ]
        rulings = _reference_rulings(member_input.contents, non_skill)
        placeholders += 2 * len(rulings)
        placeholders += 1
        old_texts: dict[str, list[dict[str, object]]] = {}
        if old is not None:
            old_refs = old["references"]
            assert isinstance(old_refs, list)
            for ruling in old_refs:
                assert isinstance(ruling, dict)
                old_texts.setdefault(str(ruling["text"]), []).append(ruling)
        for ruling in rulings:
            text = str(ruling["text"])
            candidates = {match.group(0) for match in REVIEW_AID_REF_RE.finditer(text)}
            report_lines.append(
                f"  reference candidate in {str(ruling['origin'])!r} "
                f"(member {name!r}): {text}"
            )
            for previous in old_texts.get(text, []):
                report_lines.append(
                    f"  suggestion (unconfirmed): identical reference text was "
                    f"previously ruled {str(previous['classification'])!r}"
                )
            if text not in old_texts and old is not None:
                old_refs = old["references"]
                assert isinstance(old_refs, list)
                for previous in old_refs:
                    assert isinstance(previous, dict)
                    previous_text = str(previous["text"])
                    if any(c in previous_text for c in candidates):
                        report_lines.append(
                            f"  suggestion (unconfirmed): reference candidate "
                            f"previously ruled {str(previous['classification'])!r} "
                            f"in overlapping text"
                        )
                        break
        for suggestion in _requires_candidates(
            member_input.contents, non_skill, name, declared_names
        ):
            report_lines.append(
                f"  suggestion (unconfirmed): member {name!r} mentions "
                f"'/{suggestion}' matching declared member {suggestion!r} "
                f"(consider requires)"
            )
        drafted.append(
            {
                "files": file_entries,
                "name": name,
                "reason": None,
                "references": rulings,
                "requires": [],
                "skillSha256": member_input.skill_sha256,
                "sourceIdentity": source,
            }
        )

    stale = 0
    stale_lines: list[str] = []
    if policy_obj is not None:
        for raw, review in zip(raw_reviews, policy_obj.reviews, strict=True):
            if id(review) not in matched_ids:
                stale += 1
                stale_lines.append(
                    f"stale review: source {review.source_identity!r} "
                    f"name {review.name!r} (kept; no declared member matches)"
                )

    header = (
        f"draft-skill-policy: {unchanged} unchanged, {changed} changed, "
        f"{new} new, {stale} stale; {placeholders} placeholders need operator review"
    )
    full_report = "\n".join([header, *report_lines, *stale_lines])
    document: dict[str, object] = {
        "kind": POLICY_KIND,
        "reviews": [*raw_reviews, *drafted],
        "schemaVersion": 1,
    }
    return DraftOutcome(
        ok=True,
        faults=(),
        document=document,
        report=full_report,
        placeholders=placeholders,
    )


def draft_document_bytes(document: dict[str, object]) -> bytes:
    """Canonical JSON bytes for a draft document (same convention as policy)."""
    return dumps_canonical(document)


def draft_to(
    *,
    config: Path,
    home: Path,
    policy: Path | None,
    out: Path,
) -> DraftOutcome:
    """Build the draft and write it to ``out`` (the only write performed)."""
    if policy is not None and _same_file(out, policy):
        return DraftOutcome(
            ok=False,
            faults=("refuses to overwrite the current policy file",),
            document=None,
            report="",
            placeholders=0,
        )
    outcome = build_draft(config=config, home=home, policy=policy)
    if not outcome.ok or outcome.document is None:
        return outcome
    try:
        out.write_bytes(draft_document_bytes(outcome.document))
    except OSError as exc:
        return DraftOutcome(
            ok=False,
            faults=(f"cannot write draft file: {exc}",),
            document=None,
            report="",
            placeholders=0,
        )
    return outcome
