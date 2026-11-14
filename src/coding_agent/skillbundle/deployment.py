"""Phase-API for the Skill Bundle deployment path (SKV3).

Contract: the installation resolution (issue #373) and the phased-validation
resolution (issue #375, as amended by issue #383): ``prepare-skill-bundle``
publishes ``requirements.json``, ``manifest.json`` and ``validation.json``
together as one completed generation only after every check passes;
``verify-skill-bundle`` re-checks the prepared deployment offline and
read-only. The empty bundle was the tracer bullet (issue #391); this module
also adopts a declared non-empty subset from an existing managed store
offline in ``--mode installed`` (issue #392).

Static and offline: only the Workflow Configuration file, the prompt files
it names, the explicit policy file and the explicitly selected Base Store and
bundle directory are read. No network call is made, no ambient discovery or
package locator is consulted, and relative CLI paths resolve against the
invocation directory. No secret value is echoed in diagnostics.

Matrix row ids: SKV3-04 (records), SKV3-07 (part), SKV3-10, SKV3-11,
SKV3-12, SKV3-14, SKV3-22, SKV3-23, SKV3-24, SKV3-32 (part).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from coding_agent.skillbundle.canonical import (
    dumps_canonical,
    sha256_bytes,
)
from coding_agent.skillbundle.installer import (
    FAKE_INSTALLER_VERSION,
    FAKE_REPORT_SCHEMA_VERSION,
    INSTALLER_HASH_ALGORITHM,
    collect_inventory,
    detect_overlays,
    installer_hash_for_files,
    is_full_commit_ref,
    load_provenance,
    provenance_source,
)
from coding_agent.skillbundle.installer import (
    base_store_for as _base_store_for,
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
from coding_agent.workflow.v3_validate import validate_v3_file

REQUIREMENTS_NAME = "requirements.json"
MANIFEST_NAME = "manifest.json"
VALIDATION_NAME = "validation.json"

INSTALLER_VERSION_EMPTY = "empty-bundle"
INSTALLER_REPORT_SCHEMA_VERSION = 1


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
    name: str
    source: str
    resolved_commit: str
    relative_source_path: str
    store_relative_path: str
    installer_algorithm: str
    source_hash: str
    installed_hash: str
    overlays: list[dict[str, object]]
    files: list[dict[str, object]]
    tree_sha256: str
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
                detail="symbolic ref requires preparation: supply --resolution-manifest with the saved resolution",
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
                            f"symbolic ref for skill {name!r} requires preparation: "
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
        current_hash = installer_hash_for_files(hash_inputs)
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
                name=name,
                source=source,
                resolved_commit=required,
                relative_source_path=relative_source,
                store_relative_path=name,
                installer_algorithm=INSTALLER_HASH_ALGORITHM,
                source_hash=current_hash,
                installed_hash=current_hash,
                overlays=[dict(item) for item in overlays],
                files=[dict(item) for item in records],
                tree_sha256=tree_sha,
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
    by_name = {member.name: member for member in adopted}
    for member in adopted:
        for required_name in member.review.requires:
            if required_name not in declared_names or required_name not in by_name:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.name}",
                        detail=f"skill {member.name!r} requires {required_name!r} which is not declared",
                    )
                )
    if faults:
        return None
    for member in adopted:
        record_by_path = {str(record["path"]): record for record in member.files}
        inject_paths = {
            review_file.path for review_file in member.review.files if review_file.disposition == "inject"
        }
        for review_file in member.review.files:
            inv = record_by_path.get(review_file.path)
            if inv is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.name}",
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
                    DeploymentFault(phase="bundle", path=f"skills:{member.name}", detail=problem)
                )
        if faults:
            return None
        for reference in member.review.references:
            origin = reference.origin
            if origin != "SKILL.md" and origin not in inject_paths:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.name}",
                        detail=f"reference origin {origin!r} is not delivered content",
                    )
                )
                continue
            origin_raw = member.content_by_path.get(origin)
            if origin_raw is None:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.name}",
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
                        path=f"skills:{member.name}",
                        detail=f"reference origin {origin!r} is not UTF-8 text",
                    )
                )
                continue
            if reference.text not in origin_text:
                faults.append(
                    DeploymentFault(
                        phase="bundle",
                        path=f"skills:{member.name}",
                        detail=f"reference excerpt does not occur in {origin!r}",
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


def _artifact_record(member: _AdoptedMember) -> dict[str, object]:
    return {
        "id": f"skill:{member.name}",
        "installerContent": {
            "algorithm": member.installer_algorithm,
            "installedHash": member.installed_hash,
            "sourceHash": member.source_hash,
        },
        "name": member.name,
        "overlays": [dict(item) for item in member.overlays],
        "relativeSourcePath": member.relative_source_path,
        "resolvedCommit": member.resolved_commit,
        "sourceIdentity": member.source,
        "storeRelativePath": member.store_relative_path,
        "treeSha256": member.tree_sha256,
        "files": [dict(item) for item in member.files],
        "verification": "recorded-installation",
    }


def prepare_skill_bundle(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    mode: str,
    resolution_manifest: Path | None = None,
) -> PrepareResult:
    """Prepare a Skill Bundle deployment (Phase-API seam).

    Reads real deployment roots under the given paths (tests use ``tmp_path``).
    Publishes the three generated records as one completed generation only
    after every check passes; a failure leaves no new successful generation.
    ``--mode installed`` adopts the declared subset from the existing managed
    store offline; ``--mode install`` with a non-empty declaration requires
    the real installer binding of a later ticket.
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

    # Non-empty declaration: installed adoption only in this release.
    if mode != "installed":
        refuse(
            "bundle",
            str(config),
            "non-empty Skill Bundle installation is not supported in this release",
        )
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    # Unused policy reviews are permitted; only declared members are adopted.
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
    faults: list[DeploymentFault],
) -> PrepareResult:
    """Publish the non-empty installed-bundle generation."""

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
        FAKE_INSTALLER_VERSION,
        FAKE_REPORT_SCHEMA_VERSION,
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
        artifact_dir = resolved_base / member.name
        records, hash_inputs, inv_fault = collect_inventory(artifact_dir, base_store=resolved_base)
        if inv_fault is not None:
            refuse("binding", inv_fault.path, f"artifact changed during preparation: {inv_fault.detail}")
            return PrepareResult(ok=False, faults=tuple(faults), wrote=())
        if installer_hash_for_files(hash_inputs) != member.installed_hash:
            refuse("binding", str(artifact_dir), f"skill {member.name!r} changed during preparation")
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


def verify_skill_bundle_deployment(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
) -> VerifyResult:
    """Verify a prepared bundle deployment offline and read-only."""
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
        stored = by_stored.get(member.name)
        if not isinstance(stored, dict):
            refuse("binding", str(manifest_path), f"manifest is missing skill {member.name!r}")
            continue
        expected_record = _artifact_record(member)
        if stored != expected_record:
            refuse(
                "binding",
                str(manifest_path),
                f"manifest record for skill {member.name!r} does not match the installed store",
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
    return VerifyResult(ok=True, faults=())

