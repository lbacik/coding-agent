"""Phase-API for the Skill Bundle deployment path (SKV3).

Contract: the installation resolution (issue #373) and the phased-validation
resolution (issue #375, as amended by issue #383): ``prepare-skill-bundle``
publishes ``requirements.json``, ``manifest.json`` and ``validation.json``
together as one completed generation only after every check passes;
``verify-skill-bundle`` re-checks the prepared deployment offline and
read-only. The empty bundle was the tracer bullet (issue #391); this module
also adopts a declared non-empty subset from an existing managed store
offline in ``--mode installed`` (issue #392) and installs it through the
installer-adapter port in ``--mode install`` (issue #394).

Static except for install: only the Workflow Configuration file, the prompt
files it names, the explicit policy file and the explicitly selected Base
Store and bundle directory are read. Network access happens only in
``--mode install``; ``--mode installed`` and verification never touch the
network, consult ambient discovery or a package locator, and relative CLI
paths resolve against the invocation directory. No secret value is echoed
in diagnostics.

Matrix row ids: SKV3-04 (records; runtime part via ``verify_deployment_for_run``),
SKV3-07 (part), SKV3-08, SKV3-09, SKV3-10, SKV3-11, SKV3-12, SKV3-13,
SKV3-14, SKV3-15, SKV3-16, SKV3-17, SKV3-19, SKV3-22,
SKV3-23, SKV3-24, SKV3-32 (part).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from coding_agent.skillbundle.canonical import (
    dumps_canonical,
    sha256_bytes,
)
from coding_agent.skillbundle.delivery import delivery_for_review
from coding_agent.skillbundle.installer import (
    FAKE_INSTALLER_VERSION,
    FAKE_REPORT_SCHEMA_VERSION,
    REAL_INSTALLER_VERSION,
    REAL_INSTALL_REPORT_SCHEMA_VERSION,
    SUPPORTED_SKILL_HASH_ALGORITHMS,
    InstallerAdapter,
    InstallerFault,
    MemberOutcome,
    SubprocessInstallerAdapter,
    base_store_for as _base_store_for,
    collect_inventory,
    detect_overlays,
    https_locator_from_identity,
    installer_state_version,
    is_full_commit_ref,
    load_provenance,
    provenance_source,
    recorded_content_identity,
)
from coding_agent.skillbundle.policy import (
    PolicyReview,
    SkillPolicy,
    load_policy_file,
)
from coding_agent.skillbundle.records import (
    build_manifest,
    build_manifest_empty,
    build_requirements,
    build_validation,
    check_canonical_bytes,
    parse_record_file,
    requirements_sources_from_skills,
    validate_manifest_object,
    validate_requirements_object,
    validate_validation_object,
)
from coding_agent.workflow.prompt_files import (
    PromptFileError,
    resolve_prompt_candidate,
)
from coding_agent.workflow.effective import BundleMember, InventoryFile, MemberOverlay
from coding_agent.workflow.v3_validate import validate_v3_file

REQUIREMENTS_NAME = "requirements.json"
MANIFEST_NAME = "manifest.json"
VALIDATION_NAME = "validation.json"

INSTALLER_VERSION_EMPTY = "empty-bundle"
INSTALLER_REPORT_SCHEMA_VERSION = 1

REVIEW_AID_REF_RE = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")
"""Review-aid scanner for delivered instruction text (SKV3-15).

Deliberately naive, like the legacy verifier: any slash followed by a word.
It only flags candidates inside delivered instructions (``SKILL.md`` plus
``inject`` files) so that every flag can demand an explicit content-bound
``references[]`` ruling. It never adds dependencies, never proves closure,
never auto-approves anything, and never consults the bare-name ignore
register: that register does not exist on this path.
"""


@dataclass(frozen=True)
class DeploymentFault:
    """One deployment refusal naming its phase and location."""

    phase: str
    path: str
    detail: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.phase}: {self.path}: {self.detail}"
        return f"{self.phase}: {self.detail}"


@dataclass(frozen=True)
class PrepareResult:
    """The outcome of ``prepare_skill_bundle`` (Phase-API seam)."""

    ok: bool
    faults: tuple[DeploymentFault, ...]
    wrote: tuple[Path, ...]


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of ``verify_skill_bundle_deployment`` (Phase-API seam)."""

    ok: bool
    faults: tuple[DeploymentFault, ...]


DEPLOYMENT_BUNDLE_SOURCE_IDENTITY = "skill-bundle-deployment"
"""Source-identity marker for a prepared-deployment bundle identity (SKV3 run).

A prepared deployment carries one source per declared member, so no single
Source Identity fits the legacy single-source shape; the pinned record marks
the generation as a prepared deployment and binds it by the manifest's
SHA-256 (``resolved_commit``).
"""


@dataclass(frozen=True)
class VerifiedDeployment:
    """A verified deployment plus activation deliveries built from verified bytes.

    The Phase-API seam for ``agent run`` (SKV3-17; SKV3-19; SKV3-04
    runtime; SKV3-26): where ``ok``, ``bodies`` holds every declared
    member's activation delivery keyed by exact skill name (an empty bundle
    holds no members) — the complete ``SKILL.md``, then each reviewed
    ``inject`` companion whole in policy order, with operator-authored
    notices framed outside the upstream bytes
    (``coding_agent.skillbundle.delivery``) — ``member_names`` the declared
    names in declaration order, ``manifest_sha256`` the verified manifest's
    SHA-256, and ``members`` every available member's provenance, inventory
    and selected review for the Effective Configuration identity core. The
    deliveries are decoded from the same store bytes the verification
    hashed and review-matched; no path is reopened after checking and no
    legacy fixed-companion constant is consulted. Where not ``ok``,
    ``faults`` names every refusal and the other fields are ``None``.
    """

    ok: bool
    faults: tuple[DeploymentFault, ...]
    bodies: dict[str, str] | None
    member_names: tuple[str, ...] | None
    manifest_sha256: str | None
    members: tuple[BundleMember, ...] | None = None


@dataclass
class _VerifiedCollection:
    """Activation bodies gathered from verified in-memory bytes (run only)."""

    bodies: dict[str, str] = field(default_factory=dict)
    member_names: list[str] = field(default_factory=list)
    manifest_sha256: str = ""
    members: list[BundleMember] = field(default_factory=list)


def _coding_agent_version() -> str:
    try:
        return metadata.version("coding-agent")
    except Exception:
        return "0.3.0"


def _prompt_locators(cfg: object) -> list[str]:
    """Every distinct prompt locator in declaration order of first use."""
    seen: list[str] = []
    seen_set: set[str] = set()
    workflows = getattr(cfg, "workflows", {})
    if not isinstance(workflows, dict):
        return []
    for workflow in workflows.values():
        steps = getattr(workflow, "steps", {})
        if not isinstance(steps, dict):
            continue
        for step in steps.values():
            agent = getattr(step, "agent", None)
            if agent is None:
                continue
            instructions = getattr(agent, "instructions", ())
            for instruction in instructions:
                locator = getattr(instruction, "prompt", None)
                if locator is None:
                    continue
                if locator not in seen_set:
                    seen_set.add(locator)
                    seen.append(locator)
    return seen


def _read_prompt_raw_bytes(base: Path, locator: str) -> tuple[bytes | None, str | None]:
    """Read one prompt locator as raw bytes, rechecking containment."""
    try:
        candidate = resolve_prompt_candidate(base, locator)
    except PromptFileError as exc:
        return None, str(exc)
    try:
        if not candidate.is_file():
            return None, f"prompt file {locator!r} does not exist"
    except OSError as exc:
        return None, f"prompt file {locator!r} cannot be read: {exc}"
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        return None, f"prompt file {locator!r} cannot be read: {exc}"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"prompt file {locator!r} is not valid UTF-8"
    return raw, None


def _ordered_membership(cfg: object) -> list[tuple[str, str, str | None]]:
    """Declared membership in source/include order: (name, source, ref)."""
    ordered: list[tuple[str, str, str | None]] = []
    skills = getattr(cfg, "skills", ())
    for entry in skills or ():
        source = getattr(entry, "source", "")
        ref = getattr(entry, "ref", None)
        include = getattr(entry, "include", ())
        for name in include or ():
            ordered.append((str(name), str(source), ref if ref is None else str(ref)))
    return ordered


def _load_resolution_map(
    resolution_manifest: Path | None,
    faults: list[DeploymentFault],
) -> dict[tuple[str, str], str] | None:
    """Load saved symbolic-ref resolutions; ``None`` on structural refusal."""
    if resolution_manifest is None:
        return {}
    res_raw, res_value, res_parse_faults = parse_record_file(resolution_manifest)
    if res_parse_faults:
        for fault in res_parse_faults:
            faults.append(DeploymentFault(phase="bundle", path=fault.path, detail=fault.detail))
        return None
    res_doc, res_faults = validate_manifest_object(res_value)
    if res_faults:
        for fault in res_faults:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=f"{resolution_manifest}: {fault.path}",
                    detail=fault.detail,
                )
            )
        return None
    assert res_doc is not None
    _ = res_raw
    saved: dict[tuple[str, str], str] = {}
    raw_resolutions = res_doc.get("resolutions", [])
    assert isinstance(raw_resolutions, list)
    for entry in raw_resolutions:
        assert isinstance(entry, dict)
        source = entry.get("sourceIdentity")
        ref = entry.get("ref")
        commit = entry.get("resolvedCommit")
        if isinstance(source, str) and isinstance(ref, str) and isinstance(commit, str):
            saved[(source, ref)] = commit
    return saved


def _matching_reviews(
    policy_obj: SkillPolicy,
    source: str,
    name: str,
    skill_sha: str,
    files_key: frozenset[tuple[str, str, bool]],
) -> list[PolicyReview]:
    """Policy reviews matching source, name, SKILL.md digest and file set."""
    matched: list[PolicyReview] = []
    for review in policy_obj.reviews:
        if review.source_identity != source:
            continue
        if review.name != name:
            continue
        if review.skill_sha256 != skill_sha:
            continue
        review_key = frozenset(
            (item.path, item.sha256, item.executable) for item in review.files
        )
        if review_key != files_key:
            continue
        matched.append(review)
    return matched


def _is_administrative_path(path: str) -> bool:
    return path == "agents/openai.yaml" or path.startswith("agents/")


def _check_inject_file(
    rel: str, raw: bytes, executable: bool, disposition: str
) -> str | None:
    """Refuse unsupported ``inject`` content; ``None`` when acceptable."""
    if disposition != "inject":
        return None
    if executable:
        return f"file {rel!r} is executable and cannot be 'inject'"
    if _is_administrative_path(rel):
        return f"file {rel!r} is administrative and cannot be 'inject'"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"file {rel!r} is not UTF-8 text and cannot be 'inject'"
    if raw.startswith(b"#!"):
        return f"file {rel!r} is a script and cannot be 'inject'"
    if "\x00" in text:
        return f"file {rel!r} is binary and cannot be 'inject'"
    return None


@dataclass
class _AdoptedMember:
    """One adopted skill: the adapter's per-member outcome plus check inputs.

    ``outcome`` is the ``MemberOutcome`` the installer-adapter port produces
    for this member (SKV3-11); the remaining fields are the in-memory inputs
    the second pass (closure, dispositions, references) checks against.
    """

    outcome: MemberOutcome
    skill_md_bytes: bytes
    content_by_path: dict[str, bytes]
    review: PolicyReview


def _adopt_declared_subset(
    *,
    membership: list[tuple[str, str, str | None]],
    policy_obj: SkillPolicy,
    home: Path,
    config_path: Path,
    resolution_manifest: Path | None,
    faults: list[DeploymentFault],
) -> tuple[list[_AdoptedMember], list[dict[str, object]]] | None:
    """Adopt the declared subset offline from the managed store (SKV3-10/11/12/14).

    Returns ``(adopted, resolutions)`` in declaration order, or ``None``
    after appending bundle-phase faults. Never touches the network, never
    prunes or overwrites unmanaged artifacts, and never consults ambient
    discovery: expected membership comes only from the declaration.
    """
    base = _base_store_for(home)
    try:
        resolved_base = base.resolve()
    except OSError as exc:
        faults.append(
            DeploymentFault(phase="bundle", path=str(base), detail=f"cannot resolve Base Store: {exc}")
        )
        return None

    provenance, prov_fault = load_provenance(home)
    if prov_fault is not None:
        faults.append(DeploymentFault(phase="bundle", path=prov_fault.path, detail=prov_fault.detail))

    saved_resolutions = _load_resolution_map(resolution_manifest, faults)
    if saved_resolutions is None:
        return None

    needs_symbolic = any(
        ref is not None and not is_full_commit_ref(ref) for _, _, ref in membership
    )
    if needs_symbolic and resolution_manifest is None:
        faults.append(
            DeploymentFault(
                phase="bundle",
                path=str(config_path),
                detail="preparation required: a symbolic ref needs --resolution-manifest with the saved resolution",
            )
        )
        return None

    adopted: list[_AdoptedMember] = []
    # First pass: inventory, provenance and review identity per member.
    for name, source, ref in membership:
        where = f"skills:{name}"
        entry = provenance.get(name)
        if entry is None:
            faults.append(
                DeploymentFault(
                    phase="bundle", path=where, detail=f"skill {name!r} has no installer provenance"
                )
            )
            continue
        actual_source = provenance_source(entry)
        if actual_source != source:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=f"skill {name!r} has the wrong source",
                )
            )
            continue
        recorded = entry.get("resolvedCommit")
        if not isinstance(recorded, str) or not is_full_commit_ref(recorded):
            faults.append(
                DeploymentFault(
                    phase="bundle", path=where, detail=f"skill {name!r} has no recorded commit"
                )
            )
            continue
        required: str
        if ref is None:
            required = recorded
        elif is_full_commit_ref(ref):
            if recorded != ref:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=where,
                        detail=f"skill {name!r} provenance does not match the exact-commit ref",
                    )
                )
                continue
            required = ref
        else:
            saved = saved_resolutions.get((source, ref))
            if saved is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=str(config_path),
                        detail=(
                            f"preparation required for skill {name!r}: "
                            "supply --resolution-manifest with the saved resolution"
                        ),
                    )
                )
                continue
            if recorded != saved:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=where,
                        detail=f"skill {name!r} provenance does not match the saved resolution",
                    )
                )
                continue
            required = saved

        artifact_dir = base / name
        try:
            if artifact_dir.is_symlink():
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=str(artifact_dir),
                        detail="artifact path must not be a symlink",
                    )
                )
                continue
            if not artifact_dir.is_dir():
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=str(artifact_dir),
                        detail=f"skill {name!r} is not installed in the Base Store",
                    )
                )
                continue
        except OSError as exc:
            faults.append(
                DeploymentFault(phase="bundle", path=str(artifact_dir), detail=f"cannot inspect artifact: {exc}")
            )
            continue

        records, hash_inputs, inv_fault = collect_inventory(artifact_dir, base_store=resolved_base)
        if inv_fault is not None:
            faults.append(DeploymentFault(phase="bundle", path=inv_fault.path, detail=inv_fault.detail))
            continue
        skill_entry = next((item for item in records if str(item["path"]) == "SKILL.md"), None)
        if skill_entry is None:
            faults.append(
                DeploymentFault(
                    phase="bundle", path=str(artifact_dir), detail=f"skill {name!r} is missing SKILL.md"
                )
            )
            continue
        identity = _recorded_content_identity(
            entry=entry, hash_inputs=hash_inputs, faults=faults
        )
        if identity is None:
            continue
        algorithm, current_hash = identity
        recorded_source_hash = entry.get("sourceHash")
        recorded_installed_hash = entry.get("installedHash")
        if recorded_source_hash != current_hash or recorded_installed_hash != current_hash:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=str(artifact_dir),
                    detail=f"skill {name!r} content does not match its recorded installation",
                )
            )
            continue
        relative_source = entry.get("relativeSourcePath")
        if not isinstance(relative_source, str) or not relative_source or relative_source.startswith("/") or ".." in relative_source.split("/"):
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=str(artifact_dir),
                    detail=f"skill {name!r} has an invalid source-relative path",
                )
            )
            continue
        # Read raw bytes for review matching and disposition checks.
        content_by_path: dict[str, bytes] = {}
        read_failed = False
        for item in records:
            rel = str(item["path"])
            try:
                content_by_path[rel] = (artifact_dir / rel).read_bytes()
            except OSError as exc:
                faults.append(
                    DeploymentFault(
                        phase="bundle", path=f"{artifact_dir}/{rel}", detail=f"cannot read artifact file: {exc}"
                    )
                )
                read_failed = True
                break
        if read_failed:
            continue
        skill_md_bytes = content_by_path["SKILL.md"]
        skill_sha = hashlib.sha256(skill_md_bytes).hexdigest()
        files_key = frozenset(
            (str(item["path"]), str(item["sha256"]), bool(item["executable"]))
            for item in records
            if str(item["path"]) != "SKILL.md"
        )
        matched = _matching_reviews(policy_obj, source, name, skill_sha, files_key)
        if not matched:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=f"skill {name!r} has no matching approved review",
                )
            )
            continue
        if len(matched) > 1:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=f"skill {name!r} matches several approved reviews",
                )
            )
            continue
        review = matched[0]
        inventory_paths = {str(item["path"]) for item in records}
        overlays = detect_overlays(skill_md_bytes, inventory_paths)
        try:
            tree_sha = sha256_bytes(dumps_canonical(records))
        except ValueError as exc:
            faults.append(DeploymentFault(phase="bundle", path=str(artifact_dir), detail=str(exc)))
            continue
        adopted.append(
            _AdoptedMember(
                outcome=MemberOutcome(
                    id=f"skill:{name}",
                    name=name,
                    source_identity=source,
                    resolved_commit=required,
                    relative_source_path=relative_source,
                    store_relative_path=name,
                    verification="recorded-installation",
                    installer_algorithm=algorithm,
                    source_hash=current_hash,
                    installed_hash=current_hash,
                    overlays=tuple(dict(item) for item in overlays),
                    files=tuple(dict(item) for item in records),
                    tree_sha256=tree_sha,
                ),
                skill_md_bytes=skill_md_bytes,
                content_by_path=content_by_path,
                review=review,
            )
        )

    if faults:
        return None
    if len(adopted) != len(membership):
        # A member was skipped without a fault above; refuse rather than
        # publish a partial generation.
        faults.append(
            DeploymentFault(phase="bundle", path=str(config_path), detail="declared subset is incomplete")
        )
        return None

    # Second pass: explicit closure, dispositions and reference rulings.
    declared_names = {name for name, _, _ in membership}
    by_name = {member.outcome.name: member for member in adopted}
    # SKV3-13: explicit closure over every declared member, whether or not a
    # static instruction names it. Finite closed cycles pass here with no
    # recursive installation or activation: ``requires`` only creates
    # availability, and activation never follows it automatically.
    for member in adopted:
        for required_name in member.review.requires:
            if required_name not in declared_names:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"skill {member.outcome.name!r} requires {required_name!r} which is not declared",
                    )
                )
            elif required_name not in by_name:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=(
                            f"skill {member.outcome.name!r} requires {required_name!r} "
                            "which has no matching approved review"
                        ),
                    )
                )
    if faults:
        return None
    for member in adopted:
        record_by_path = {str(record["path"]): record for record in member.outcome.files}
        inject_paths = {
            review_file.path for review_file in member.review.files if review_file.disposition == "inject"
        }
        not_delivered_paths = {
            review_file.path
            for review_file in member.review.files
            if review_file.disposition == "not-delivered"
        }
        for review_file in member.review.files:
            inv = record_by_path.get(review_file.path)
            if inv is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reviewed file {review_file.path!r} is not installed",
                    )
                )
                continue
            raw = member.content_by_path.get(review_file.path, b"")
            problem = _check_inject_file(
                review_file.path, raw, bool(inv["executable"]), review_file.disposition
            )
            if problem is not None:
                faults.append(
                    DeploymentFault(phase="bundle", path=f"skills:{member.outcome.name}", detail=problem)
                )
        if faults:
            return None
        # SKV3-15: every ruling is bound to its delivered origin and exact
        # excerpt. Stale (excerpt absent, including an empty excerpt which is
        # trivially "contained") or unbound (origin not delivered) rulings
        # refuse. Classification/target/reason/notice contradictions that
        # would waive a required dependency refuse here as well as in policy
        # parsing.
        for reference in member.review.references:
            origin = reference.origin
            if origin != "SKILL.md" and origin not in inject_paths:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference origin {origin!r} is not delivered content",
                    )
                )
                continue
            origin_raw = member.content_by_path.get(origin)
            if origin_raw is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference origin {origin!r} is not installed",
                    )
                )
                continue
            try:
                origin_text = origin_raw.decode("utf-8")
            except UnicodeDecodeError:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference origin {origin!r} is not UTF-8 text",
                    )
                )
                continue
            if not reference.text or reference.text not in origin_text:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference excerpt does not occur in {origin!r}",
                    )
                )
                continue
            if not reference.reason:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference ruling in {origin!r} has no reason",
                    )
                )
                continue
            if reference.classification == "not-a-skill-reference":
                if reference.target is not None:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail="'target' must be absent for 'not-a-skill-reference'",
                        )
                    )
                if reference.notice is not None:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail="'notice' must be absent for 'not-a-skill-reference'",
                        )
                    )
            elif reference.classification == "unavailable-nonrequired-reference":
                target = reference.target
                # Scanner flags slash-form candidates ("/beta"); the ruling
                # target may use either form, so compare the bare name.
                bare_target = target.lstrip("/") if target else ""
                if not target:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail="'target' must be a non-empty skill/command name",
                        )
                    )
                elif bare_target in member.review.requires:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail=(
                                f"reference target {reference.target!r} is also a required dependency"
                            ),
                        )
                    )
                elif bare_target in declared_names:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail=(
                                f"reference target {reference.target!r} is a declared bundle member "
                                "and cannot be waived as unavailable"
                            ),
                        )
                    )
                if not reference.notice:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail="'notice' must be a non-empty operator explanation",
                        )
                    )
            else:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"unknown reference classification {reference.classification!r}",
                    )
                )
        if faults:
            return None
        # Delivered instruction bytes for the review-aid scan and the
        # unsupported-use check: SKILL.md plus every inject file. Unavailable
        # files never contribute dependencies and are never scanned.
        delivered_origins = {"SKILL.md"} | set(inject_paths)
        delivered_texts: dict[str, str] = {}
        for delivered in sorted(delivered_origins):
            delivered_raw = member.content_by_path.get(delivered)
            if delivered_raw is None:
                # SKILL.md presence was checked in the first pass; an inject
                # path missing here was already reported as not installed.
                if delivered == "SKILL.md":
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail="delivered file 'SKILL.md' is not installed",
                        )
                    )
                continue
            try:
                delivered_texts[delivered] = delivered_raw.decode("utf-8")
            except UnicodeDecodeError:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"delivered file {delivered!r} is not UTF-8 text",
                    )
                )
        if faults:
            return None
        # The scanner is a review aid only: each slash-form candidate in
        # delivered instructions must be covered by an explicit ruling whose
        # excerpt contains that candidate. It never adds a dependency, never
        # proves closure, never auto-approves, and never consults a global
        # bare-name ignore list.
        delivered_keys = sorted(delivered_texts)
        for delivered in delivered_keys:
            text = delivered_texts[delivered]
            flagged: set[str] = {match.group(0) for match in REVIEW_AID_REF_RE.finditer(text)}
            for candidate in sorted(flagged):
                covered = any(
                    ruling.origin == delivered
                    and candidate in ruling.text
                    and ruling.text in text
                    for ruling in member.review.references
                )
                if not covered:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail=(
                                f"unruled reference {candidate!r} in {delivered!r} "
                                "requires an explicit references[] ruling"
                            ),
                        )
                    )
        if faults:
            return None
        # SKV3-16: supported use that requires reading a not-delivered file
        # (including executing a packaged script left not-delivered) refuses.
        # The representable check is an exact relative-path mention in
        # delivered instructions; prose semantics beyond that are not inferred.
        # Incidental packaging of scripts/binaries/admin files that delivered
        # instructions never mention remains allowed.
        for not_delivered in sorted(not_delivered_paths):
            for delivered in delivered_keys:
                if not_delivered in delivered_texts[delivered]:
                    faults.append(
                        DeploymentFault(
                            phase="bundle",
                            path=f"skills:{member.outcome.name}",
                            detail=(
                                f"skill {member.outcome.name!r} requires not-delivered file "
                                f"{not_delivered!r} referenced in {delivered!r}"
                            ),
                        )
                    )
    if faults:
        return None

    resolutions: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for _, source, ref in membership:
        if ref is None or is_full_commit_ref(ref):
            continue
        if source in seen_sources:
            continue
        seen_sources.add(source)
        commit = saved_resolutions.get((source, ref))
        if commit is None:
            continue
        resolutions.append({"ref": ref, "resolvedCommit": commit, "sourceIdentity": source})
    return adopted, resolutions


def _report_entries(report: dict[str, object], key: str) -> list[dict[str, object]] | None:
    """Typed access to one installer-report bucket; ``None`` when malformed."""
    raw = report.get(key, [])
    if not isinstance(raw, list):
        return None
    entries: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        entries.append(dict(item))
    return entries


_V3_SUCCESS_DISPOSITIONS = frozenset({"installed", "updated", "unchanged"})
"""Install dispositions that positively verify a requested member (v3)."""


def _recorded_content_identity(
    *,
    entry: dict[str, Any],
    hash_inputs: list[tuple[str, bytes, bool]],
    faults: list[DeploymentFault],
) -> tuple[str, str] | None:
    """Rehash inventory inputs with the provenance entry's own algorithm.

    Returns ``(algorithm, digest)`` or ``None`` after appending a bundle-phase
    fault for an unsupported recorded algorithm, so callers never compare
    digests across algorithms.
    """
    algorithm, digest, fault = recorded_content_identity(entry, hash_inputs)
    if fault is not None:
        faults.append(DeploymentFault(phase="bundle", path=fault.path, detail=fault.detail))
        return None
    return (algorithm, digest)


def _validate_v3_install_report_for_group(
    *,
    source_identity: str,
    resolved_commit: str,
    selectors: tuple[str, ...],
    report: dict[str, Any],
    group_where: str,
    faults: list[DeploymentFault],
) -> bool:
    """Validate one released-installer v3 report (SKV3-09).

    Every requested member must be positively verified with disposition
    ``installed``/``updated``/``unchanged`` at the immutable commit and a
    supported content algorithm; any other outcome, unmatched selector,
    reported error, pruned entry or non-success disposition refuses the
    generation while disclosing what changed. Host-installed conflicts are
    never auto-approved: a ``conflict`` disposition refuses here and the
    member is re-verified from the store afterwards regardless.
    """
    ok = True

    def refuse(detail: str) -> None:
        nonlocal ok
        ok = False
        faults.append(
            DeploymentFault(phase="bundle", path=group_where, detail=detail)
        )

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        refuse(
            "installer report artifacts must be an array "
            "for the declared source"
        )
        return False
    if report.get("outcome") != "complete":
        refuse(
            f"installer outcome {report.get('outcome')!r} is not complete "
            "for the declared source; no new successful generation"
        )
    unmatched = report.get("unmatchedSelectors", [])
    if not isinstance(unmatched, list) or unmatched:
        refuse(
            f"installer reported unmatched selectors {unmatched!r}: "
            "no new successful generation"
        )
    if report.get("error") is not None:
        refuse(
            f"installer reported an error {report.get('error')!r}: "
            "no new successful generation"
        )
    pruned = report.get("pruned", [])
    if not isinstance(pruned, list) or pruned:
        refuse(
            f"installer pruned unexpected entries {pruned!r}: "
            "no new successful generation"
        )
    by_id: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            refuse(
                "installer report artifacts must be an array of objects "
                "for the declared source"
            )
            return False
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id not in by_id:
            by_id[item_id] = item
    expected_ids = set(selectors)
    missing = sorted(expected_ids - set(by_id.keys()))
    if missing:
        refuse(
            f"installer verified {len(set(by_id.keys()) & expected_ids)} "
            f"of {len(expected_ids)} requested members: "
            f"missing {missing}; no new successful generation"
        )
    for entry_id in sorted(expected_ids & set(by_id.keys())):
        entry = by_id[entry_id]
        disposition = entry.get("disposition")
        if disposition not in _V3_SUCCESS_DISPOSITIONS:
            refuse(
                f"installer record {entry_id!r} disposition {disposition!r} "
                "is not a successful installation; "
                "no new successful generation"
            )
            continue
        if entry.get("sourceIdentity") != source_identity:
            refuse(
                f"installer record {entry_id!r} has the wrong source"
            )
        if entry.get("resolvedCommit") != resolved_commit:
            refuse(
                f"installer record {entry_id!r} does not match "
                f"the immutable commit"
            )
        content = entry.get("content", {})
        if isinstance(content, dict) and "algorithm" in content:
            if content.get("algorithm") not in SUPPORTED_SKILL_HASH_ALGORITHMS:
                refuse(
                    f"installer record {entry_id!r} uses an unsupported "
                    f"content algorithm {content.get('algorithm')!r}"
                )
    return ok


def _validate_verify_report_for_subset(
    *,
    selectors: tuple[str, ...],
    report: dict[str, Any],
    group_where: str,
    faults: list[DeploymentFault],
) -> bool:
    """Validate one offline verify report for the declared subset (SKV3-11).

    Every requested member must be ``verified`` with
    ``verification: recorded-installation``; any refused member refuses the
    generation while disclosing which members failed.
    """
    ok = True

    def refuse(detail: str) -> None:
        nonlocal ok
        ok = False
        faults.append(
            DeploymentFault(phase="bundle", path=group_where, detail=detail)
        )

    if not isinstance(report, dict):
        refuse("installer verify report must be a JSON object")
        return False
    if report.get("outcome") != "verified":
        refuse(
            f"installer verification outcome {report.get('outcome')!r} "
            "is not verified for the declared subset"
        )
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        refuse("installer verify report artifacts must be an array")
        return False
    by_id: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            refuse("installer verify report artifacts must be an array of objects")
            return False
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id not in by_id:
            by_id[item_id] = item
    for selector in selectors:
        entry = by_id.get(selector)
        if entry is None:
            refuse(
                f"installer verification is missing member {selector!r}"
            )
            continue
        if entry.get("result") != "verified":
            refuse(
                f"installer verification refused member {selector!r}: "
                f"{entry.get('refusals')!r}"
            )
        if entry.get("verification") != "recorded-installation":
            refuse(
                f"installer verification for member {selector!r} is not "
                "a recorded installation"
            )
    return ok


def _validate_install_report_for_group(
    *,
    source_identity: str,
    resolved_commit: str,
    selectors: tuple[str, ...],
    report: dict[str, Any],
    group_where: str,
    faults: list[DeploymentFault],
) -> bool:
    """Validate one installer's report bucket set (SKV3-09).

    Released-installer v3 reports (``artifacts``) take the v3 branch above;
    the in-process fake's legacy bucket reports keep the bucket validation
    below. Every requested member must be positively verified in
    ``installed``/``updated`` with the immutable commit; empty mutation
    buckets never count as success; any failed, skipped or refused member
    refuses the generation while disclosing what changed.
    """
    if isinstance(report, dict) and "artifacts" in report:
        return _validate_v3_install_report_for_group(
            source_identity=source_identity,
            resolved_commit=resolved_commit,
            selectors=selectors,
            report=report,
            group_where=group_where,
            faults=faults,
        )
    ok = True

    def refuse(detail: str) -> None:
        nonlocal ok
        ok = False
        faults.append(
            DeploymentFault(phase="bundle", path=group_where, detail=detail)
        )

    installed = _report_entries(report, "installed")
    updated = _report_entries(report, "updated")
    skipped = _report_entries(report, "skipped")
    refused = _report_entries(report, "refused")
    failed = _report_entries(report, "failed")
    if (
        installed is None
        or updated is None
        or skipped is None
        or refused is None
        or failed is None
    ):
        refuse(
            "installer report buckets must be arrays of objects "
            "for the declared source"
        )
        return False
    attempted = [*installed, *updated]
    attempted_ids = {
        str(entry.get("id")) for entry in attempted if isinstance(entry.get("id"), str)
    }
    expected_ids = set(selectors)
    missing = sorted(expected_ids - attempted_ids)
    if missing:
        refuse(
            f"installer installed {len(attempted_ids & expected_ids)} "
            f"of {len(expected_ids)} requested members: "
            f"missing {missing}; "
            f"installer changed {sorted(attempted_ids & expected_ids)}; "
            f"no new successful generation"
        )
    if not attempted:
        refuse(
            "installer reported no installed or updated members "
            "for the declared source; no new successful generation"
        )
    for bucket_name, bucket in (
        ("skipped", skipped),
        ("refused", refused),
        ("failed", failed),
    ):
        if bucket:
            ids = sorted(
                str(entry.get("id", "<unknown>")) for entry in bucket
            )
            refuse(
                f"installer reported {bucket_name} members {ids}: "
                f"no new successful generation; "
                f"installer changed {[str(e.get('id')) for e in attempted]}"
            )
    for entry in attempted:
        entry_id = str(entry.get("id", ""))
        if entry_id not in expected_ids:
            continue
        if entry.get("sourceIdentity") != source_identity:
            refuse(
                f"installer record {entry_id!r} has the wrong source"
            )
        if entry.get("resolvedCommit") != resolved_commit:
            refuse(
                f"installer record {entry_id!r} does not match "
                f"the immutable commit"
            )
    return ok


def _adopt_after_install(
    *,
    membership: list[tuple[str, str, str | None]],
    required_commits: dict[str, str],
    policy_obj: SkillPolicy,
    home: Path,
    config_path: Path,
    faults: list[DeploymentFault],
) -> tuple[list[_AdoptedMember], list[dict[str, object]]] | None:
    """Re-verify the freshly installed store (SKV3-08/09).

    Provenance must freshly record the requested immutable commit even when
    bytes are identical to a previous commit; every member is positively
    re-verified via inventory and policy, never via empty installer buckets.
    Returns ``(adopted, resolutions)`` with ``verification="source"``.

    The content identity is algorithm-aware: released-installer provenance
    (``hashAlgorithm: agent-installer-tree-v2``) is rehashed with the tree
    digest, legacy provenance without one with ``INSTALLER_HASH_ALGORITHM``.
    """
    base = _base_store_for(home)
    try:
        resolved_base = base.resolve()
    except OSError as exc:
        faults.append(
            DeploymentFault(phase="bundle", path=str(base), detail=f"cannot resolve Base Store: {exc}")
        )
        return None
    provenance, prov_fault = load_provenance(home)
    if prov_fault is not None:
        faults.append(DeploymentFault(phase="bundle", path=prov_fault.path, detail=prov_fault.detail))
    adopted: list[_AdoptedMember] = []
    for name, source, _ref in membership:
        where = f"skills:{name}"
        required = required_commits.get(name)
        if required is None:
            faults.append(
                DeploymentFault(phase="bundle", path=where, detail=f"skill {name!r} has no resolved commit")
            )
            continue
        entry = provenance.get(name)
        if entry is None:
            faults.append(
                DeploymentFault(
                    phase="bundle", path=where, detail=f"skill {name!r} has no installer provenance"
                )
            )
            continue
        actual_source = provenance_source(entry)
        if actual_source != source:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=f"skill {name!r} has the wrong source",
                )
            )
            continue
        recorded = entry.get("resolvedCommit")
        if recorded != required:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=(
                        f"skill {name!r} provenance does not match "
                        f"the immutable commit"
                    ),
                )
            )
            continue
        artifact_dir = base / name
        try:
            if artifact_dir.is_symlink():
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=str(artifact_dir),
                        detail="artifact path must not be a symlink",
                    )
                )
                continue
            if not artifact_dir.is_dir():
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=str(artifact_dir),
                        detail=f"skill {name!r} is not installed in the Base Store",
                    )
                )
                continue
        except OSError as exc:
            faults.append(
                DeploymentFault(phase="bundle", path=str(artifact_dir), detail=f"cannot inspect artifact: {exc}")
            )
            continue
        records, hash_inputs, inv_fault = collect_inventory(artifact_dir, base_store=resolved_base)
        if inv_fault is not None:
            faults.append(DeploymentFault(phase="bundle", path=inv_fault.path, detail=inv_fault.detail))
            continue
        skill_entry = next((item for item in records if str(item["path"]) == "SKILL.md"), None)
        if skill_entry is None:
            faults.append(
                DeploymentFault(
                    phase="bundle", path=str(artifact_dir), detail=f"skill {name!r} is missing SKILL.md"
                )
            )
            continue
        identity = _recorded_content_identity(
            entry=entry, hash_inputs=hash_inputs, faults=faults
        )
        if identity is None:
            continue
        algorithm, current_hash = identity
        recorded_source_hash = entry.get("sourceHash")
        recorded_installed_hash = entry.get("installedHash")
        if recorded_source_hash != current_hash or recorded_installed_hash != current_hash:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=str(artifact_dir),
                    detail=f"skill {name!r} content does not match its recorded installation",
                )
            )
            continue
        relative_source = entry.get("relativeSourcePath")
        if not isinstance(relative_source, str) or not relative_source or relative_source.startswith("/") or ".." in relative_source.split("/"):
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=str(artifact_dir),
                    detail=f"skill {name!r} has an invalid source-relative path",
                )
            )
            continue
        content_by_path: dict[str, bytes] = {}
        read_failed = False
        for item in records:
            rel = str(item["path"])
            try:
                content_by_path[rel] = (artifact_dir / rel).read_bytes()
            except OSError as exc:
                faults.append(
                    DeploymentFault(
                        phase="bundle", path=f"{artifact_dir}/{rel}", detail=f"cannot read artifact file: {exc}"
                    )
                )
                read_failed = True
                break
        if read_failed:
            continue
        skill_md_bytes = content_by_path["SKILL.md"]
        skill_sha = hashlib.sha256(skill_md_bytes).hexdigest()
        files_key = frozenset(
            (str(item["path"]), str(item["sha256"]), bool(item["executable"]))
            for item in records
            if str(item["path"]) != "SKILL.md"
        )
        matched = _matching_reviews(policy_obj, source, name, skill_sha, files_key)
        if not matched:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=f"skill {name!r} has no matching approved review",
                )
            )
            continue
        if len(matched) > 1:
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=where,
                    detail=f"skill {name!r} matches several approved reviews",
                )
            )
            continue
        review = matched[0]
        inventory_paths = {str(item["path"]) for item in records}
        overlays = detect_overlays(skill_md_bytes, inventory_paths)
        try:
            tree_sha = sha256_bytes(dumps_canonical(records))
        except ValueError as exc:
            faults.append(DeploymentFault(phase="bundle", path=str(artifact_dir), detail=str(exc)))
            continue
        adopted.append(
            _AdoptedMember(
                outcome=MemberOutcome(
                    id=f"skill:{name}",
                    name=name,
                    source_identity=source,
                    resolved_commit=required,
                    relative_source_path=relative_source,
                    store_relative_path=name,
                    verification="source",
                    installer_algorithm=algorithm,
                    source_hash=current_hash,
                    installed_hash=current_hash,
                    overlays=tuple(dict(item) for item in overlays),
                    files=tuple(dict(item) for item in records),
                    tree_sha256=tree_sha,
                ),
                skill_md_bytes=skill_md_bytes,
                content_by_path=content_by_path,
                review=review,
            )
        )
    if faults:
        return None
    if len(adopted) != len(membership):
        faults.append(
            DeploymentFault(phase="bundle", path=str(config_path), detail="declared subset is incomplete")
        )
        return None
    declared_names = {name for name, _, _ in membership}
    by_name = {member.outcome.name: member for member in adopted}
    for member in adopted:
        for required_name in member.review.requires:
            if required_name not in declared_names or required_name not in by_name:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"skill {member.outcome.name!r} requires {required_name!r} which is not declared",
                    )
                )
    if faults:
        return None
    for member in adopted:
        record_by_path = {str(record["path"]): record for record in member.outcome.files}
        inject_paths = {
            review_file.path for review_file in member.review.files if review_file.disposition == "inject"
        }
        for review_file in member.review.files:
            inv = record_by_path.get(review_file.path)
            if inv is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reviewed file {review_file.path!r} is not installed",
                    )
                )
                continue
            raw = member.content_by_path.get(review_file.path, b"")
            problem = _check_inject_file(
                review_file.path, raw, bool(inv["executable"]), review_file.disposition
            )
            if problem is not None:
                faults.append(
                    DeploymentFault(phase="bundle", path=f"skills:{member.outcome.name}", detail=problem)
                )
        if faults:
            return None
        for reference in member.review.references:
            origin = reference.origin
            if origin != "SKILL.md" and origin not in inject_paths:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference origin {origin!r} is not delivered content",
                    )
                )
                continue
            origin_raw = member.content_by_path.get(origin)
            if origin_raw is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference origin {origin!r} is not installed",
                    )
                )
                continue
            try:
                origin_text = origin_raw.decode("utf-8")
            except UnicodeDecodeError:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference origin {origin!r} is not UTF-8 text",
                    )
                )
                continue
            if reference.text not in origin_text:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.outcome.name}",
                        detail=f"reference excerpt does not occur in {origin!r}",
                    )
                )
    if faults:
        return None
    return adopted, []


def _resolve_group_ref(
    *,
    installer: InstallerAdapter,
    source: str,
    ref: str,
    group_where: str,
    faults: list[DeploymentFault],
) -> str | None:
    """Resolve one source group's ``ref`` to an immutable commit (SKV3-08).

    Returns the commit, or ``None`` after appending a bundle-phase fault.
    Only the fault detail crosses the adapter port, never credential-bearing
    Git arguments.
    """
    try:
        resolved = installer.resolve_ref(source, ref)
    except InstallerFault as exc:
        faults.append(DeploymentFault(phase="bundle", path=group_where, detail=exc.detail))
        return None
    if not is_full_commit_ref(resolved):
        faults.append(
            DeploymentFault(
                phase="bundle",
                path=group_where,
                detail="installer resolved an invalid commit",
            )
        )
        return None
    return resolved


def _install_declared_subset(
    *,
    cfg: object,
    membership: list[tuple[str, str, str | None]],
    policy_obj: SkillPolicy,
    home: Path,
    config_path: Path,
    installer: InstallerAdapter,
    faults: list[DeploymentFault],
) -> tuple[list[_AdoptedMember], list[dict[str, object]]] | None:
    """Install the declared subset through the installer-adapter port (SKV3-08/09).

    Resolves each source group's symbolic ``ref`` once, installs the group at
    the immutable commit with one argument per selector and no shell
    interpolation, then positively re-verifies every member from the store.
    Network access happens only here. Returns ``(adopted, resolutions)`` in
    declaration order, or ``None`` after appending bundle-phase faults.
    """
    entries: list[tuple[str, tuple[str, ...], str | None]] = []
    raw_skills = getattr(cfg, "skills", ())
    for entry in raw_skills or ():
        source = str(getattr(entry, "source", ""))
        include = tuple(str(n) for n in (getattr(entry, "include", ()) or ()))
        ref_value = getattr(entry, "ref", None)
        ref = str(ref_value) if ref_value is not None else None
        entries.append((source, include, ref))
    required_commits: dict[str, str] = {}
    group_index = 0
    for source, include, ref in entries:
        group_where = f"skills[{group_index}].source"
        selectors = tuple(f"skill:{name}" for name in include)
        resolved: str | None
        if ref is None:
            # A fresh unconstrained group install resolves HEAD once.
            resolved = _resolve_group_ref(
                installer=installer,
                source=source,
                ref="HEAD",
                group_where=group_where,
                faults=faults,
            )
        elif is_full_commit_ref(ref):
            resolved = ref
        else:
            resolved = _resolve_group_ref(
                installer=installer,
                source=source,
                ref=ref,
                group_where=group_where,
                faults=faults,
            )
        if resolved is None:
            group_index += 1
            continue
        for name in include:
            if name in required_commits and required_commits[name] != resolved:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{name}",
                        detail=f"skill {name!r} is included by conflicting groups",
                    )
                )
            required_commits[name] = resolved
        locator = https_locator_from_identity(source)
        try:
            report = installer.install_group(
                source_identity=source,
                https_locator=locator,
                resolved_commit=resolved,
                selectors=selectors,
                home=home,
            )
        except InstallerFault as exc:
            faults.append(
                DeploymentFault(phase="bundle", path=group_where, detail=exc.detail)
            )
            group_index += 1
            continue
        if not isinstance(report, dict):
            faults.append(
                DeploymentFault(
                    phase="bundle",
                    path=group_where,
                    detail="installer report must be a JSON object",
                )
            )
            group_index += 1
            continue
        _validate_install_report_for_group(
            source_identity=source,
            resolved_commit=resolved,
            selectors=selectors,
            report=report,
            group_where=group_where,
            faults=faults,
        )
        group_index += 1
    if faults:
        return None
    adopted_result = _adopt_after_install(
        membership=membership,
        required_commits=required_commits,
        policy_obj=policy_obj,
        home=home,
        config_path=config_path,
        faults=faults,
    )
    if adopted_result is None:
        return None
    adopted, _ = adopted_result
    resolutions: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for _, source, ref in membership:
        if ref is None or is_full_commit_ref(ref):
            continue
        if source in seen_sources:
            continue
        seen_sources.add(source)
        first_name = next((n for n, s, _ in membership if s == source), None)
        if first_name is None:
            continue
        resolved_for_source = required_commits.get(first_name)
        if resolved_for_source is None:
            continue
        resolutions.append(
            {"ref": ref, "resolvedCommit": resolved_for_source, "sourceIdentity": source}
        )
    return adopted, resolutions


def _artifact_record(member: _AdoptedMember) -> dict[str, object]:
    outcome = member.outcome
    return {
        "id": outcome.id,
        "installerContent": {
            "algorithm": outcome.installer_algorithm,
            "installedHash": outcome.installed_hash,
            "sourceHash": outcome.source_hash,
        },
        "name": outcome.name,
        "overlays": [dict(item) for item in outcome.overlays],
        "relativeSourcePath": outcome.relative_source_path,
        "resolvedCommit": outcome.resolved_commit,
        "sourceIdentity": outcome.source_identity,
        "storeRelativePath": outcome.store_relative_path,
        "treeSha256": outcome.tree_sha256,
        "files": [dict(item) for item in outcome.files],
        "verification": outcome.verification,
    }


def prepare_skill_bundle(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    mode: str,
    resolution_manifest: Path | None = None,
    installer: InstallerAdapter | None = None,
) -> PrepareResult:
    """Prepare a Skill Bundle deployment (Phase-API seam).

    Reads real deployment roots under the given paths (tests use ``tmp_path``).
    Publishes the three generated records as one completed generation only
    after every check passes; a failure leaves no new successful generation.
    ``--mode installed`` adopts the declared subset from the existing managed
    store offline (with an ``installer`` supplied, the declared subset is
    first cross-checked through the adapter's offline ``verify_subset``);
    ``--mode install`` installs the declared subset through the
    installer-adapter port (network only here) and records resolutions. The
    manifest's producer identity is the active adapter's own in install mode,
    or the identity inferred from ``installer_state_version`` in installed
    mode (released installer for state version 4, the fake otherwise).
    """
    faults: list[DeploymentFault] = []

    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    if mode not in ("installed", "install"):
        refuse("usage", "--mode", f"unsupported mode {mode!r}: must be 'installed' or 'install'")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    # -- Stage 1: configuration (strict YAML/schema/version, graphs, static
    # skill names, prompt containment/read/UTF-8). Stage 2 policy faults are
    # independent of these, so both sets are collected before returning;
    # bundle/prompts/binding depend on both and are skipped below.
    cfg = None
    try:
        config_raw = config.read_bytes()
    except OSError as exc:
        refuse("configuration", str(config), f"cannot read workflow configuration: {exc}")
        config_raw = b""
    else:
        try:
            config_text_ok = config_raw.decode("utf-8")
        except UnicodeDecodeError:
            refuse("configuration", str(config), "workflow configuration is not valid UTF-8")
        else:
            _ = config_text_ok
            try:
                validated, config_faults = validate_v3_file(config)
            except Exception as exc:
                refuse(
                    "configuration",
                    str(config),
                    f"cannot validate the workflow configuration: {exc}",
                )
            else:
                for config_fault in config_faults:
                    refuse("configuration", config_fault.path or str(config), config_fault.detail)
                if validated is not None:
                    cfg = validated

    # -- Stage 2: policy (schema + representable consistency), independent of
    # the configuration faults above: both are collected before returning.
    policy_obj: SkillPolicy | None = None
    try:
        policy_raw = policy.read_bytes()
    except OSError as exc:
        refuse("policy", str(policy), f"cannot read policy file: {exc}")
        policy_raw = b""
    else:
        from coding_agent.skillbundle.policy import parse_policy_bytes

        parsed_policy, policy_faults = parse_policy_bytes(policy_raw)
        for policy_fault in policy_faults:
            # Anchor policy-internal paths under the file without echoing contents.
            if policy_fault.path in ("(policy)", "(root)"):
                refuse("policy", str(policy), policy_fault.detail)
            else:
                refuse("policy", f"{policy}: {policy_fault.path}", policy_fault.detail)
        if parsed_policy is not None:
            policy_obj = parsed_policy

    if cfg is None or policy_obj is None:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    # -- Stage 3: bundle (membership/provenance/inventory/closure).
    sources = requirements_sources_from_skills(cfg.skills)
    membership = _ordered_membership(cfg)
    if not sources:
        # Empty bundle path (tracer bullet, issue #391).
        _ = membership
        if mode == "install":
            pass  # No store writes for the empty bundle in either mode.
        if resolution_manifest is not None:
            res_raw, res_value, res_parse_faults = parse_record_file(resolution_manifest)
            if res_parse_faults:
                for res_parse_fault in res_parse_faults:
                    refuse("bundle", res_parse_fault.path, res_parse_fault.detail)
                return PrepareResult(ok=False, faults=tuple(faults), wrote=())
            res_doc, res_faults = validate_manifest_object(res_value)
            if res_faults:
                for res_fault in res_faults:
                    refuse("bundle", f"{resolution_manifest}: {res_fault.path}", res_fault.detail)
                return PrepareResult(ok=False, faults=tuple(faults), wrote=())
            assert res_doc is not None
            if list(res_doc.get("resolutions", [])):
                refuse("bundle", str(resolution_manifest), "empty bundle requires no saved resolutions")
                return PrepareResult(ok=False, faults=tuple(faults), wrote=())
            _ = res_raw
        _ = home  # The empty bundle needs no store writes; the root stays explicit.
        return _prepare_publish_empty(
            config=config,
            policy=policy,
            bundle_dir=bundle_dir,
            config_raw=config_raw,
            policy_raw=policy_raw,
            cfg=cfg,
            sources=sources,
            faults=faults,
        )

    # Non-empty declaration: installed adopts offline; install resolves and
    # installs through the installer-adapter port (network only here).
    producer_version: str
    producer_schema: int
    if mode == "installed":
        # Unused policy reviews are permitted; only declared members are adopted.
        if installer is not None:
            verify_selectors = tuple(f"skill:{name}" for name, _, _ in membership)
            try:
                verify_report = installer.verify_subset(
                    selectors=verify_selectors, home=home
                )
            except InstallerFault as exc:
                refuse("bundle", exc.path, exc.detail)
                return PrepareResult(ok=False, faults=tuple(faults), wrote=())
            if not isinstance(verify_report, dict) or not _validate_verify_report_for_subset(
                selectors=verify_selectors,
                report=verify_report,
                group_where="skills",
                faults=faults,
            ):
                if not any(f.phase == "bundle" for f in faults):
                    refuse("bundle", "skills", "installer verification refused the declared subset")
                return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        adopted_result = _adopt_declared_subset(
            membership=membership,
            policy_obj=policy_obj,
            home=home,
            config_path=config,
            resolution_manifest=resolution_manifest,
            faults=faults,
        )
        if adopted_result is None:
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        adopted, resolutions = adopted_result
        if installer_state_version(home) == 4:
            producer_version = REAL_INSTALLER_VERSION
            producer_schema = REAL_INSTALL_REPORT_SCHEMA_VERSION
        else:
            producer_version = FAKE_INSTALLER_VERSION
            producer_schema = FAKE_REPORT_SCHEMA_VERSION
    else:
        active_installer: InstallerAdapter
        if installer is not None:
            active_installer = installer
        else:
            active_installer = SubprocessInstallerAdapter()
        installed_result = _install_declared_subset(
            cfg=cfg,
            membership=membership,
            policy_obj=policy_obj,
            home=home,
            config_path=config,
            installer=active_installer,
            faults=faults,
        )
        if installed_result is None:
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        adopted, resolutions = installed_result
        producer_version = active_installer.installer_version
        producer_schema = active_installer.report_schema_version

    return _prepare_publish_non_empty(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        config_raw=config_raw,
        policy_raw=policy_raw,
        cfg=cfg,
        sources=sources,
        adopted=adopted,
        resolutions=resolutions,
        installer_version=producer_version,
        installer_report_schema_version=producer_schema,
        faults=faults,
    )


def _prepare_publish_empty(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    config_raw: bytes,
    policy_raw: bytes,
    cfg: object,
    sources: list[dict[str, object]],
    faults: list[DeploymentFault],
) -> PrepareResult:
    """Publish the empty-bundle generation (shared prompts/binding steps)."""

    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    # Unused policy reviews are permitted; the selected review set is empty.
    # -- Prompts: distinct locators sorted by UTF-8 bytes, raw-byte digests.
    base = config.parent.resolve()
    locators = _prompt_locators(cfg)
    prompt_entries: list[dict[str, str]] = []
    prompt_raws: dict[str, bytes] = {}
    for locator in locators:
        raw, problem = _read_prompt_raw_bytes(base, locator)
        if raw is None:
            refuse("configuration", str(config), problem or f"cannot read prompt {locator!r}")
        else:
            prompt_raws[locator] = raw
    if any(f.phase == "configuration" for f in faults):
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    for locator in sorted(prompt_raws.keys(), key=lambda s: s.encode("utf-8")):
        prompt_entries.append(
            {"reference": locator, "sha256": sha256_bytes(prompt_raws[locator])}
        )

    # -- Binding: generate the three records in memory first.
    requirements_doc = build_requirements(sources)
    try:
        requirements_bytes = dumps_canonical(requirements_doc)
    except ValueError as exc:
        refuse("binding", REQUIREMENTS_NAME, str(exc))
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    requirements_sha = sha256_bytes(requirements_bytes)
    # Structural self-check before publication.
    _, req_self_faults = validate_requirements_object(requirements_doc)
    for req_self_fault in req_self_faults:
        refuse("binding", f"{REQUIREMENTS_NAME}: {req_self_fault.path}", req_self_fault.detail)
    if faults:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    manifest_doc = build_manifest_empty(
        requirements_sha,
        _coding_agent_version(),
        INSTALLER_VERSION_EMPTY,
        INSTALLER_REPORT_SCHEMA_VERSION,
    )
    try:
        manifest_bytes = dumps_canonical(manifest_doc)
    except ValueError as exc:
        refuse("binding", MANIFEST_NAME, str(exc))
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    manifest_sha = sha256_bytes(manifest_bytes)
    _, manifest_self_faults = validate_manifest_object(manifest_doc)
    for manifest_self_fault in manifest_self_faults:
        refuse("binding", f"{MANIFEST_NAME}: {manifest_self_fault.path}", manifest_self_fault.detail)
    if faults:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    workflow_sha = sha256_bytes(config_raw)
    policy_sha = sha256_bytes(policy_raw)
    validation_doc = build_validation(
        workflow_sha, requirements_sha, manifest_sha, policy_sha, prompt_entries
    )
    try:
        validation_bytes = dumps_canonical(validation_doc)
    except ValueError as exc:
        refuse("binding", VALIDATION_NAME, str(exc))
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    _, validation_self_faults = validate_validation_object(validation_doc)
    for validation_self_fault in validation_self_faults:
        refuse("binding", f"{VALIDATION_NAME}: {validation_self_fault.path}", validation_self_fault.detail)
    if faults:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    # Time-of-check guard: refuse inputs changed during preparation.
    try:
        config_reread = config.read_bytes()
    except OSError as exc:
        refuse("binding", str(config), f"workflow configuration changed during preparation: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    try:
        policy_reread = policy.read_bytes()
    except OSError as exc:
        refuse("binding", str(policy), f"policy file changed during preparation: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    if config_reread != config_raw:
        refuse("binding", str(config), "workflow configuration changed during preparation")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    if policy_reread != policy_raw:
        refuse("binding", str(policy), "policy file changed during preparation")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    for locator, raw in prompt_raws.items():
        current, problem = _read_prompt_raw_bytes(base, locator)
        if current is None or current != raw:
            refuse("binding", str(config), f"prompt file {locator!r} changed during preparation")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    # -- Publication: one completed generation, atomic per file.
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        refuse("publication", str(bundle_dir), f"cannot create bundle directory: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    outputs: list[tuple[str, bytes]] = [
        (REQUIREMENTS_NAME, requirements_bytes),
        (MANIFEST_NAME, manifest_bytes),
        (VALIDATION_NAME, validation_bytes),
    ]
    written: list[Path] = []
    tmp_paths: list[Path] = []
    try:
        for name, data in outputs:
            tmp_path = bundle_dir / f"{name}.tmp"
            tmp_path.write_bytes(data)
            tmp_paths.append(tmp_path)
        for (name, _), tmp_path in zip(outputs, tmp_paths):
            final_path = bundle_dir / name
            os.replace(tmp_path, final_path)
            written.append(final_path)
    except OSError as exc:
        for tmp_path in tmp_paths:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        refuse("publication", str(bundle_dir), f"cannot publish generated records: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    return PrepareResult(ok=True, faults=(), wrote=tuple(written))


def _prepare_publish_non_empty(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    config_raw: bytes,
    policy_raw: bytes,
    cfg: object,
    sources: list[dict[str, object]],
    adopted: list[_AdoptedMember],
    resolutions: list[dict[str, object]],
    installer_version: str,
    installer_report_schema_version: int,
    faults: list[DeploymentFault],
) -> PrepareResult:
    """Publish the non-empty installed-bundle generation.

    ``installer_version``/``installer_report_schema_version`` name the
    producer identity recorded in the manifest: the active adapter's own
    identity in ``--mode install``, or the identity inferred from
    ``installer_state_version`` (4: released installer, otherwise the fake)
    in ``--mode installed``.
    """

    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    base = config.parent.resolve()
    locators = _prompt_locators(cfg)
    prompt_entries: list[dict[str, str]] = []
    prompt_raws: dict[str, bytes] = {}
    for locator in locators:
        raw, problem = _read_prompt_raw_bytes(base, locator)
        if raw is None:
            refuse("configuration", str(config), problem or f"cannot read prompt {locator!r}")
        else:
            prompt_raws[locator] = raw
    if any(f.phase == "configuration" for f in faults):
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    requirements_doc = build_requirements(sources)
    try:
        requirements_bytes = dumps_canonical(requirements_doc)
    except ValueError as exc:
        refuse("binding", REQUIREMENTS_NAME, str(exc))
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    requirements_sha = sha256_bytes(requirements_bytes)
    _, req_self_faults = validate_requirements_object(requirements_doc)
    for req_self_fault in req_self_faults:
        refuse("binding", f"{REQUIREMENTS_NAME}: {req_self_fault.path}", req_self_fault.detail)
    if faults:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    for locator in sorted(prompt_raws.keys(), key=lambda s: s.encode("utf-8")):
        prompt_entries.append(
            {"reference": locator, "sha256": sha256_bytes(prompt_raws[locator])}
        )

    artifacts = [_artifact_record(member) for member in adopted]
    manifest_doc = build_manifest(
        requirements_sha,
        _coding_agent_version(),
        installer_version,
        installer_report_schema_version,
        resolutions,
        artifacts,
    )
    try:
        manifest_bytes = dumps_canonical(manifest_doc)
    except ValueError as exc:
        refuse("binding", MANIFEST_NAME, str(exc))
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    manifest_sha = sha256_bytes(manifest_bytes)
    _, manifest_self_faults = validate_manifest_object(manifest_doc)
    for manifest_self_fault in manifest_self_faults:
        refuse("binding", f"{MANIFEST_NAME}: {manifest_self_fault.path}", manifest_self_fault.detail)
    if faults:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    workflow_sha = sha256_bytes(config_raw)
    policy_sha = sha256_bytes(policy_raw)
    validation_doc = build_validation(
        workflow_sha, requirements_sha, manifest_sha, policy_sha, prompt_entries
    )
    try:
        validation_bytes = dumps_canonical(validation_doc)
    except ValueError as exc:
        refuse("binding", VALIDATION_NAME, str(exc))
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    _, validation_self_faults = validate_validation_object(validation_doc)
    for validation_self_fault in validation_self_faults:
        refuse("binding", f"{VALIDATION_NAME}: {validation_self_fault.path}", validation_self_fault.detail)
    if faults:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    # Time-of-check guard: inputs and adopted store bytes must be unchanged.
    try:
        if config.read_bytes() != config_raw:
            refuse("binding", str(config), "workflow configuration changed during preparation")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        if policy.read_bytes() != policy_raw:
            refuse("binding", str(policy), "policy file changed during preparation")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    except OSError as exc:
        refuse("binding", str(bundle_dir), f"cannot recheck inputs: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    for locator, raw in prompt_raws.items():
        current, _ = _read_prompt_raw_bytes(base, locator)
        if current is None or current != raw:
            refuse("binding", str(config), f"prompt file {locator!r} changed during preparation")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    try:
        resolved_base = _base_store_for(home).resolve()
    except OSError as exc:
        refuse("binding", str(home), f"cannot resolve Base Store: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    for member in adopted:
        artifact_dir = resolved_base / member.outcome.name
        records, hash_inputs, inv_fault = collect_inventory(artifact_dir, base_store=resolved_base)
        if inv_fault is not None:
            refuse("binding", inv_fault.path, f"artifact changed during preparation: {inv_fault.detail}")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        rehashed = _recorded_content_identity(
            entry={"hashAlgorithm": member.outcome.installer_algorithm, "name": member.outcome.name},
            hash_inputs=hash_inputs,
            faults=faults,
        )
        if rehashed is None:
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        _, rehash_digest = rehashed
        if rehash_digest != member.outcome.installed_hash:
            refuse("binding", str(artifact_dir), f"skill {member.outcome.name!r} changed during preparation")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        _ = records

    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        refuse("publication", str(bundle_dir), f"cannot create bundle directory: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    outputs: list[tuple[str, bytes]] = [
        (REQUIREMENTS_NAME, requirements_bytes),
        (MANIFEST_NAME, manifest_bytes),
        (VALIDATION_NAME, validation_bytes),
    ]
    written: list[Path] = []
    tmp_paths: list[Path] = []
    try:
        for name, data in outputs:
            tmp_path = bundle_dir / f"{name}.tmp"
            tmp_path.write_bytes(data)
            tmp_paths.append(tmp_path)
        for (name, _), tmp_path in zip(outputs, tmp_paths):
            final_path = bundle_dir / name
            os.replace(tmp_path, final_path)
            written.append(final_path)
    except OSError as exc:
        for tmp_path in tmp_paths:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        refuse("publication", str(bundle_dir), f"cannot publish generated records: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    return PrepareResult(ok=True, faults=(), wrote=tuple(written))


def _verify_internal(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    collection: _VerifiedCollection | None,
) -> VerifyResult:
    """Verify a prepared bundle deployment offline and read-only.

    Where ``collection`` is not ``None`` (the ``agent run`` seam), the
    verified activation bodies are gathered from the same in-memory bytes
    the verification hashed and review-matched; no path is reopened after
    checking. ``None`` (the ``verify-skill-bundle`` command) collects
    nothing and behaves exactly as before.
    """
    faults: list[DeploymentFault] = []

    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    # -- Stage 1: configuration + policy + records are independent; binding
    # depends on all three.
    try:
        config_raw = config.read_bytes()
    except OSError as exc:
        refuse("configuration", str(config), f"cannot read workflow configuration: {exc}")
        config_raw = b""
    cfg = None
    config_ok = False
    if config_raw != b"" or config.exists():
        try:
            validated, config_faults = validate_v3_file(config)
        except Exception as exc:
            refuse("configuration", str(config), f"cannot validate the workflow configuration: {exc}")
            validated, config_faults = None, []
        for verify_config_fault in config_faults:
            refuse("configuration", verify_config_fault.path or str(config), verify_config_fault.detail)
        if validated is not None and not config_faults:
            cfg = validated
            config_ok = True

    try:
        policy_raw = policy.read_bytes()
    except OSError as exc:
        refuse("policy", str(policy), f"cannot read policy file: {exc}")
        policy_raw = b""
    policy_ok = False
    policy_obj: SkillPolicy | None = None
    if policy_raw != b"" or policy.exists():
        from coding_agent.skillbundle.policy import parse_policy_bytes as _parse_policy

        parsed_policy, policy_faults = _parse_policy(policy_raw)
        for verify_policy_fault in policy_faults:
            if verify_policy_fault.path in ("(policy)", "(root)"):
                refuse("policy", str(policy), verify_policy_fault.detail)
            else:
                refuse("policy", f"{policy}: {verify_policy_fault.path}", verify_policy_fault.detail)
        if parsed_policy is not None and not policy_faults:
            policy_ok = True
            policy_obj = parsed_policy

    requirements_path = bundle_dir / REQUIREMENTS_NAME
    manifest_path = bundle_dir / MANIFEST_NAME
    validation_path = bundle_dir / VALIDATION_NAME

    req_raw, req_value, req_parse_faults = parse_record_file(requirements_path)
    for req_parse_fault in req_parse_faults:
        refuse("records", req_parse_fault.path, req_parse_fault.detail)
    req_doc = None
    if not req_parse_faults:
        req_canonical_problem = check_canonical_bytes(req_raw, req_value, requirements_path)
        if req_canonical_problem is not None:
            refuse("records", req_canonical_problem.path, req_canonical_problem.detail)
        else:
            req_doc, req_faults = validate_requirements_object(req_value)
            for req_fault in req_faults:
                refuse("records", f"{requirements_path}: {req_fault.path}", req_fault.detail)

    man_raw, man_value, man_parse_faults = parse_record_file(manifest_path)
    for man_parse_fault in man_parse_faults:
        refuse("records", man_parse_fault.path, man_parse_fault.detail)
    man_doc = None
    if not man_parse_faults:
        man_canonical_problem = check_canonical_bytes(man_raw, man_value, manifest_path)
        if man_canonical_problem is not None:
            refuse("records", man_canonical_problem.path, man_canonical_problem.detail)
        else:
            man_doc, man_faults = validate_manifest_object(man_value)
            for man_fault in man_faults:
                refuse("records", f"{manifest_path}: {man_fault.path}", man_fault.detail)

    val_raw, val_value, val_parse_faults = parse_record_file(validation_path)
    for val_parse_fault in val_parse_faults:
        refuse("records", val_parse_fault.path, val_parse_fault.detail)
    val_doc = None
    if not val_parse_faults:
        val_canonical_problem = check_canonical_bytes(val_raw, val_value, validation_path)
        if val_canonical_problem is not None:
            refuse("records", val_canonical_problem.path, val_canonical_problem.detail)
        else:
            val_doc, val_faults = validate_validation_object(val_value)
            for val_fault in val_faults:
                refuse("records", f"{validation_path}: {val_fault.path}", val_fault.detail)

    records_ok = (
        req_doc is not None and man_doc is not None and val_doc is not None
        and not any(f.phase == "records" for f in faults)
    )

    if not (config_ok and policy_ok and records_ok):
        # Dependent binding/store checks are skipped, never reported as passing.
        return VerifyResult(ok=False, faults=tuple(faults))

    assert cfg is not None and req_doc is not None and man_doc is not None and val_doc is not None
    assert policy_obj is not None

    # -- Stage 2 (dependent): binding comparison with current files.
    sources = requirements_sources_from_skills(cfg.skills)
    membership = _ordered_membership(cfg)
    if not sources:
        return _verify_empty_binding(
            config=config,
            policy=policy,
            bundle_dir=bundle_dir,
            home=home,
            config_raw=config_raw,
            policy_raw=policy_raw,
            cfg=cfg,
            sources=sources,
            req_raw=req_raw,
            man_raw=man_raw,
            man_doc=man_doc,
            val_doc=val_doc,
            requirements_path=requirements_path,
            manifest_path=manifest_path,
            validation_path=validation_path,
            faults=faults,
            collection=collection,
        )
    return _verify_non_empty_binding(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        config_raw=config_raw,
        policy_raw=policy_raw,
        cfg=cfg,
        policy_obj=policy_obj,
        sources=sources,
        membership=membership,
        req_raw=req_raw,
        man_raw=man_raw,
        man_doc=man_doc,
        val_doc=val_doc,
        requirements_path=requirements_path,
        manifest_path=manifest_path,
        validation_path=validation_path,
        faults=faults,
        collection=collection,
    )


def verify_skill_bundle_deployment(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
) -> VerifyResult:
    """Verify a prepared bundle deployment offline and read-only.

    Re-checks the prepared configuration, prompts, policy and bundle. Exit
    ``ok`` on success; ``faults`` names every refusal. Never refreshes or
    repairs records, and never publishes a generation (the build-only
    ``prepare_skill_bundle`` path is unreachable from here).
    """
    return _verify_internal(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        collection=None,
    )


def verify_deployment_for_run(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
) -> VerifiedDeployment:
    """Verify a prepared deployment and build deliveries from the verified bytes.

    The Phase-API seam for ``agent run`` (SKV3-17; SKV3-19; SKV3-04
    runtime): the full offline deployment verification (configuration,
    prompts, policy, records and store) runs first, exactly as
    ``verify_skill_bundle_deployment`` runs it, and where it passes the
    activation deliveries are composed from the same in-memory store bytes
    the verification hashed and review-matched — no path is reopened after
    checking and no legacy fixed-companion constant is consulted. Read-only
    like the verify command: no generation is published, so the build-only
    ``prepare_skill_bundle`` path is unreachable from here and from ``run``.
    """
    collection = _VerifiedCollection()
    result = _verify_internal(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        collection=collection,
    )
    if not result.ok:
        return VerifiedDeployment(
            ok=False,
            faults=result.faults,
            bodies=None,
            member_names=None,
            manifest_sha256=None,
            members=None,
        )
    return VerifiedDeployment(
        ok=True,
        faults=(),
        bodies=dict(collection.bodies),
        member_names=tuple(collection.member_names),
        manifest_sha256=collection.manifest_sha256,
        members=tuple(collection.members),
    )


def _verify_empty_binding(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    config_raw: bytes,
    policy_raw: bytes,
    cfg: object,
    sources: list[dict[str, object]],
    req_raw: bytes,
    man_raw: bytes,
    man_doc: dict[str, object],
    val_doc: dict[str, object],
    requirements_path: Path,
    manifest_path: Path,
    validation_path: Path,
    faults: list[DeploymentFault],
    collection: _VerifiedCollection | None,
) -> VerifyResult:
    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    expected_requirements = build_requirements(sources)
    try:
        expected_requirements_bytes = dumps_canonical(expected_requirements)
    except ValueError as exc:
        refuse("binding", REQUIREMENTS_NAME, str(exc))
        return VerifyResult(ok=False, faults=tuple(faults), )
    if req_raw != expected_requirements_bytes:
        refuse("binding", str(requirements_path), "requirements do not match the current workflow configuration")
    expected_requirements_sha = sha256_bytes(expected_requirements_bytes)
    if str(man_doc.get("requirementsSha256")) != expected_requirements_sha:
        refuse("binding", str(manifest_path), "manifest does not bind the current requirements")
    if str(val_doc.get("requirementsSha256")) != expected_requirements_sha:
        refuse("binding", str(validation_path), "validation does not bind the current requirements")
    expected_manifest_sha = sha256_bytes(man_raw)
    if str(val_doc.get("manifestSha256")) != expected_manifest_sha:
        refuse("binding", str(validation_path), "validation does not bind the current manifest")
    if str(val_doc.get("workflowSha256")) != sha256_bytes(config_raw):
        refuse("binding", str(config), "validation does not bind the current workflow configuration bytes")
    if str(val_doc.get("policySha256")) != sha256_bytes(policy_raw):
        refuse("binding", str(policy), "validation does not bind the current policy bytes")

    # Stored manifest/validation consistency with the empty declaration.
    empty_resolutions = man_doc.get("resolutions", [])
    empty_artifacts = man_doc.get("artifacts", [])
    if isinstance(empty_resolutions, list) and list(empty_resolutions):
        refuse("binding", str(manifest_path), "empty bundle must carry no resolutions")
    if isinstance(empty_artifacts, list) and list(empty_artifacts):
        refuse("binding", str(manifest_path), "empty bundle must carry no artifacts")

    # Prompts: recompute distinct locators and raw-byte digests.
    base = config.parent.resolve()
    locators = _prompt_locators(cfg)
    recomputed: dict[str, str] = {}
    for locator in locators:
        raw, problem = _read_prompt_raw_bytes(base, locator)
        if raw is None:
            refuse("binding", str(config), problem or f"cannot read prompt {locator!r}")
        else:
            recomputed[locator] = sha256_bytes(raw)
    stored_prompts = val_doc.get("prompts")
    if isinstance(stored_prompts, list):
        expected_prompt_entries = [
            {"reference": ref, "sha256": recomputed[ref]}
            for ref in sorted(recomputed.keys(), key=lambda s: s.encode("utf-8"))
        ]
        if stored_prompts != expected_prompt_entries:
            refuse("binding", str(validation_path), "validation does not bind the current prompt bytes")
    else:
        refuse("binding", str(validation_path), "'prompts' must be an array")

    # Store check for the empty bundle: no members required; unrelated
    # artifacts remain outside the scope. The home root stays explicit so a
    # relocated tree verifies; only an unreadable selected root would refuse.
    _ = home
    _ = policy_raw

    if faults:
        # Re-check: binding faults above already appended; configuration,
        # policy and records faults would have returned earlier.
        return VerifyResult(ok=False, faults=tuple(faults))

    # Time-of-check guard: refuse inputs changed during verification.
    try:
        if config.read_bytes() != config_raw:
            refuse("binding", str(config), "workflow configuration changed during verification")
            return VerifyResult(ok=False, faults=tuple(faults))
        if policy.read_bytes() != policy_raw:
            refuse("binding", str(policy), "policy file changed during verification")
            return VerifyResult(ok=False, faults=tuple(faults))
    except OSError as exc:
        refuse("binding", str(bundle_dir), f"cannot recheck inputs: {exc}")
        return VerifyResult(ok=False, faults=tuple(faults))

    if collection is not None:
        # An empty bundle declares no members, so no static skill reference
        # can exist (SKV3-04, static part) and the registry holds nothing:
        # on-demand activation refuses with a typed outcome (SKV3-04,
        # runtime).
        collection.bodies = {}
        collection.member_names = []
        collection.manifest_sha256 = sha256_bytes(man_raw)
        collection.members = []

    return VerifyResult(ok=True, faults=())


def _verify_non_empty_binding(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    config_raw: bytes,
    policy_raw: bytes,
    cfg: object,
    policy_obj: SkillPolicy,
    sources: list[dict[str, object]],
    membership: list[tuple[str, str, str | None]],
    req_raw: bytes,
    man_raw: bytes,
    man_doc: dict[str, object],
    val_doc: dict[str, object],
    requirements_path: Path,
    manifest_path: Path,
    validation_path: Path,
    faults: list[DeploymentFault],
    collection: _VerifiedCollection | None,
) -> VerifyResult:
    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    expected_requirements = build_requirements(sources)
    try:
        expected_requirements_bytes = dumps_canonical(expected_requirements)
    except ValueError as exc:
        refuse("binding", REQUIREMENTS_NAME, str(exc))
        return VerifyResult(ok=False, faults=tuple(faults))
    if req_raw != expected_requirements_bytes:
        refuse("binding", str(requirements_path), "requirements do not match the current workflow configuration")
        return VerifyResult(ok=False, faults=tuple(faults))
    expected_requirements_sha = sha256_bytes(expected_requirements_bytes)
    if str(man_doc.get("requirementsSha256")) != expected_requirements_sha:
        refuse("binding", str(manifest_path), "manifest does not bind the current requirements")
    if str(val_doc.get("requirementsSha256")) != expected_requirements_sha:
        refuse("binding", str(validation_path), "validation does not bind the current requirements")
    expected_manifest_sha = sha256_bytes(man_raw)
    if str(val_doc.get("manifestSha256")) != expected_manifest_sha:
        refuse("binding", str(validation_path), "validation does not bind the current manifest")
    if str(val_doc.get("workflowSha256")) != sha256_bytes(config_raw):
        refuse("binding", str(config), "validation does not bind the current workflow configuration bytes")
    if str(val_doc.get("policySha256")) != sha256_bytes(policy_raw):
        refuse("binding", str(policy), "validation does not bind the current policy bytes")

    # Stored manifest membership must equal the declaration in order.
    stored_artifacts = man_doc.get("artifacts", [])
    stored_resolutions = man_doc.get("resolutions", [])
    if not isinstance(stored_artifacts, list) or not isinstance(stored_resolutions, list):
        refuse("binding", str(manifest_path), "manifest artifacts and resolutions must be arrays")
        return VerifyResult(ok=False, faults=tuple(faults))
    expected_names = [name for name, _, _ in membership]
    stored_names = [
        str(item.get("name")) if isinstance(item, dict) else "" for item in stored_artifacts
    ]
    if stored_names != expected_names:
        refuse(
            "binding",
            str(manifest_path),
            "manifest membership does not match the declared Skill Bundle",
        )
    # Resolutions: every symbolic source needs exactly one saved entry.
    expected_symbolic: dict[str, str] = {}
    for _, source, ref in membership:
        if ref is not None and not is_full_commit_ref(ref):
            expected_symbolic[source] = ref
    stored_map: dict[tuple[str, str], str] = {}
    for stored_item in stored_resolutions:
        if not isinstance(stored_item, dict):
            continue
        stored_source = stored_item.get("sourceIdentity")
        stored_ref = stored_item.get("ref")
        stored_commit = stored_item.get("resolvedCommit")
        if isinstance(stored_source, str) and isinstance(stored_ref, str) and isinstance(stored_commit, str):
            stored_map[(stored_source, stored_ref)] = stored_commit
    for expected_source, expected_ref in expected_symbolic.items():
        if (expected_source, expected_ref) not in stored_map:
            refuse(
                "binding",
                str(manifest_path),
                "manifest is missing the saved resolution for the symbolic ref",
            )
    for (stored_source_key, stored_ref_key) in list(stored_map.keys()):
        if stored_source_key not in expected_symbolic or expected_symbolic.get(stored_source_key) != stored_ref_key:
            refuse(
                "binding",
                str(manifest_path),
                "manifest carries an unused saved resolution",
            )
    if faults:
        return VerifyResult(ok=False, faults=tuple(faults))

    # Re-verify the store, provenance, inventory and reviews offline.
    adopt_faults: list[DeploymentFault] = []
    readopted = _adopt_declared_subset(
        membership=membership,
        policy_obj=policy_obj,
        home=home,
        config_path=config,
        resolution_manifest=manifest_path,
        faults=adopt_faults,
    )
    # _adopt_declared_subset reports bundle-phase faults; surface them as
    # binding faults here without echoing secrets (it never echoes them).
    for fault in adopt_faults:
        refuse("binding", fault.path, fault.detail)
    if readopted is None:
        return VerifyResult(ok=False, faults=tuple(faults))
    readopted_members, re_resolutions = readopted
    if re_resolutions != stored_resolutions:
        refuse("binding", str(manifest_path), "manifest resolutions do not match the saved resolutions")
    by_stored = {
        str(item.get("name")): item
        for item in stored_artifacts
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for member in readopted_members:
        stored = by_stored.get(member.outcome.name)
        if not isinstance(stored, dict):
            refuse("binding", str(manifest_path), f"manifest is missing skill {member.outcome.name!r}")
            continue
        expected_record = _artifact_record(member)
        # ``verification`` names how the stored generation proved the member
        # (``source`` for a fresh install, ``recorded-installation`` for an
        # adoption); re-verification always proves ``recorded-installation``
        # now, so the content binding compares with that field normalised.
        normalised_stored = dict(stored)
        normalised_stored["verification"] = "recorded-installation"
        normalised_expected = dict(expected_record)
        normalised_expected["verification"] = "recorded-installation"
        if normalised_stored != normalised_expected:
            refuse(
                "binding",
                str(manifest_path),
                f"manifest record for skill {member.outcome.name!r} does not match the installed store",
            )
    if faults:
        return VerifyResult(ok=False, faults=tuple(faults))

    # Prompts binding (same semantics as the empty bundle).
    base = config.parent.resolve()
    locators = _prompt_locators(cfg)
    recomputed: dict[str, str] = {}
    for locator in locators:
        raw, problem = _read_prompt_raw_bytes(base, locator)
        if raw is None:
            refuse("binding", str(config), problem or f"cannot read prompt {locator!r}")
        else:
            recomputed[locator] = sha256_bytes(raw)
    stored_prompts = val_doc.get("prompts")
    if isinstance(stored_prompts, list):
        expected_prompt_entries = [
            {"reference": ref, "sha256": recomputed[ref]}
            for ref in sorted(recomputed.keys(), key=lambda s: s.encode("utf-8"))
        ]
        if stored_prompts != expected_prompt_entries:
            refuse("binding", str(validation_path), "validation does not bind the current prompt bytes")
    else:
        refuse("binding", str(validation_path), "'prompts' must be an array")
    if faults:
        return VerifyResult(ok=False, faults=tuple(faults))

    try:
        if config.read_bytes() != config_raw:
            refuse("binding", str(config), "workflow configuration changed during verification")
            return VerifyResult(ok=False, faults=tuple(faults))
        if policy.read_bytes() != policy_raw:
            refuse("binding", str(policy), "policy file changed during verification")
            return VerifyResult(ok=False, faults=tuple(faults))
    except OSError as exc:
        refuse("binding", str(bundle_dir), f"cannot recheck inputs: {exc}")
        return VerifyResult(ok=False, faults=tuple(faults))

    if collection is not None:
        # Build the registry from the same store bytes the verification
        # hashed and review-matched above (SKV3-17): every declared member
        # is available on demand (SKV3-19), whether or not a static
        # instruction names it, as the complete ``SKILL.md`` plus each
        # reviewed ``inject`` companion whole in policy order, with
        # operator-authored notices framed outside the upstream bytes.
        # ``not-delivered`` bodies appear in no delivery. No path is
        # reopened after checking and no legacy fixed-companion constant
        # is consulted.
        bodies: dict[str, str] = {}
        for member in readopted_members:
            try:
                skill_md_text = member.skill_md_bytes.decode("utf-8")
            except UnicodeDecodeError:
                refuse(
                    "binding",
                    f"skills:{member.outcome.name}",
                    f"skill {member.outcome.name!r} SKILL.md is not valid UTF-8",
                )
                continue
            try:
                bodies[member.outcome.name] = delivery_for_review(
                    name=member.outcome.name,
                    skill_md_text=skill_md_text,
                    contents=member.content_by_path,
                    review=member.review,
                )
            except UnicodeDecodeError:
                refuse(
                    "binding",
                    f"skills:{member.outcome.name}",
                    f"skill {member.outcome.name!r} has a delivered file "
                    "that is not valid UTF-8",
                )
        if faults:
            return VerifyResult(ok=False, faults=tuple(faults))

        declared_refs = {name: ref for name, _, ref in membership}
        identity_members: list[BundleMember] = []
        for member in readopted_members:
            outcome = member.outcome
            inventory = tuple(
                InventoryFile(
                    path=str(item["path"]),
                    sha256=str(item["sha256"]),
                    executable=bool(item["executable"]),
                )
                for item in outcome.files
            )
            overlay_records: list[MemberOverlay] = []
            for raw_overlay in outcome.overlays:
                raw_paths = raw_overlay.get("paths", [])
                paths = (
                    tuple(str(path) for path in raw_paths)
                    if isinstance(raw_paths, list)
                    else ()
                )
                overlay_records.append(
                    MemberOverlay(
                        id=str(raw_overlay.get("id", "")),
                        version=str(raw_overlay.get("version", "")),
                        paths=paths,
                    )
                )
            overlays = tuple(overlay_records)
            identity_members.append(
                BundleMember(
                    name=outcome.name,
                    source_identity=outcome.source_identity,
                    resolved_commit=outcome.resolved_commit,
                    declared_ref=declared_refs.get(outcome.name),
                    relative_source_path=outcome.relative_source_path,
                    store_relative_path=outcome.store_relative_path,
                    files=inventory,
                    overlays=overlays,
                    review=member.review,
                )
            )
        collection.bodies = bodies
        collection.member_names = [member.outcome.name for member in readopted_members]
        collection.manifest_sha256 = sha256_bytes(man_raw)
        collection.members = identity_members

    return VerifyResult(ok=True, faults=())

