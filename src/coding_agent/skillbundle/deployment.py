"""Phase-API for the new Skill Bundle deployment path (SKV3, empty bundle).

Contract: the installation resolution (issue #373) and the phased-validation
resolution (issue #375, as amended by issue #383): ``prepare-skill-bundle``
publishes ``requirements.json``, ``manifest.json`` and ``validation.json``
together as one completed generation only after every check passes;
``verify-skill-bundle`` re-checks the prepared deployment offline and
read-only. This ticket covers the first end-to-end path for a configuration
that declares no skills.

Static and offline: only the Workflow Configuration file, the prompt files it
names, the explicit policy file and the explicitly selected Base Store and
bundle directory are read. No network call is made, no ambient discovery or
package locator is consulted, and relative CLI paths resolve against the
invocation directory. No secret value is echoed in diagnostics.

Matrix row ids: SKV3-04 (records), SKV3-11 (records), SKV3-22, SKV3-23,
SKV3-24, SKV3-32 (part).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from coding_agent.skillbundle.canonical import (
    dumps_canonical,
    sha256_bytes,
)
from coding_agent.skillbundle.policy import (
    SkillPolicy,
    load_policy_file,
)
from coding_agent.skillbundle.records import (
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


def _is_full_commit_ref(ref: str) -> bool:
    import re

    return re.match(r"^[0-9a-f]{40}$", ref) is not None


def prepare_skill_bundle(
    *,
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    mode: str,
    resolution_manifest: Path | None = None,
) -> PrepareResult:
    """Prepare an empty Skill Bundle deployment (Phase-API seam).

    Reads real deployment roots under the given paths (tests use ``tmp_path``).
    Publishes the three generated records as one completed generation only
    after every check passes; a failure leaves no new successful generation.
    """
    faults: list[DeploymentFault] = []

    def refuse(phase: str, path: str, detail: str) -> None:
        faults.append(DeploymentFault(phase=phase, path=path, detail=detail))

    if mode not in ("installed", "install"):
        refuse("usage", "--mode", f"unsupported mode {mode!r}: must be 'installed' or 'install'")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())

    # -- Stage 1: configuration (strict YAML/schema/version, graphs, static
    # skill names, prompt containment/read/UTF-8). Independent policy faults
    # are collected in stage 2; binding/publication waits for both.
    try:
        config_raw = config.read_bytes()
    except OSError as exc:
        refuse("configuration", str(config), f"cannot read workflow configuration: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    try:
        config_text_ok = config_raw.decode("utf-8")
    except UnicodeDecodeError:
        refuse("configuration", str(config), "workflow configuration is not valid UTF-8")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    _ = config_text_ok

    cfg = None
    try:
        validated, config_faults = validate_v3_file(config)
    except Exception as exc:
        refuse("configuration", str(config), f"cannot validate the workflow configuration: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    for config_fault in config_faults:
        refuse("configuration", config_fault.path or str(config), config_fault.detail)
    if validated is None:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    cfg = validated

    # -- Stage 2: policy (schema + representable consistency), independent of
    # configuration faults above (already returned) but collected alongside
    # bundle faults below.
    try:
        policy_raw = policy.read_bytes()
    except OSError as exc:
        refuse("policy", str(policy), f"cannot read policy file: {exc}")
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    from coding_agent.skillbundle.policy import parse_policy_bytes

    parsed_policy, policy_faults = parse_policy_bytes(policy_raw)
    for policy_fault in policy_faults:
        # Anchor policy-internal paths under the file without echoing contents.
        if policy_fault.path in ("(policy)", "(root)"):
            refuse("policy", str(policy), policy_fault.detail)
        else:
            refuse("policy", f"{policy}: {policy_fault.path}", policy_fault.detail)
    if parsed_policy is None:
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    policy_obj: SkillPolicy = parsed_policy

    # -- Stage 3: bundle (membership/provenance/inventory/closure). Empty only.
    sources = requirements_sources_from_skills(cfg.skills)
    if sources:
        refuse(
            "bundle",
            str(config),
            "non-empty Skill Bundle preparation is not supported in this release",
        )
        return PrepareResult(ok=False, faults=tuple(faults), wrote=())
    # Unused policy reviews are permitted; the selected review set is empty.
    _ = policy_obj

    # Optional explicit resolution manifest for symbolic constraints. An empty
    # declaration has no constrained sources, so any supplied manifest must
    # itself be structurally valid; its resolutions must be empty.
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
    _ = home  # The empty bundle needs no store writes; the root stays explicit.
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
    """Verify a prepared empty-bundle deployment offline and read-only."""
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

    # -- Stage 2 (dependent): binding comparison with current files.
    sources = requirements_sources_from_skills(cfg.skills)
    if sources:
        refuse("binding", str(config), "non-empty Skill Bundle verification is not supported in this release")
        return VerifyResult(ok=False, faults=tuple(faults))
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
    if list(man_doc.get("resolutions", [])):
        refuse("binding", str(manifest_path), "empty bundle must carry no resolutions")
    if list(man_doc.get("artifacts", [])):
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
