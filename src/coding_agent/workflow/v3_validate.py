"""Static validation for `version: 3` Workflow Configuration files.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7 (strict
parsing), §2.8 (trust and paths), §2.9 (``agent config validate`` is static
and offline; every fault names its YAML path) and §3 (static validation),
plus the skills-declaration resolution behind issue #372 (SKV3-01 … SKV3-04).

A `version: 3` file is a `version: 2` file plus the optional
configuration-wide ``skills`` declaration: a list of
``{source, include, ref?}`` entries. The target, models, worker and
workflows blocks keep the `version: 2` shape and checks, so this module
reuses the ``version: 2`` phase-1 and phase-2 validators
(``coding_agent.workflow.v2_validate``) and only adds the skills
declaration, the declared-membership skill check and the typed v3 build.

Static and offline: only the YAML file and the prompt files it names are
read. No environment variable value is read, no credential is required,
and no network call is made. No Attempt is opened, no value of any
environment variable is printed and no subprocess runs. Every fault is
collected as a ``WorkflowConfigError`` naming its YAML path;
credential-bearing source components are never echoed.

Matrix row ids: SKV3-01 … SKV3-04.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from coding_agent.workflow.parser import WorkflowConfigError, strict_load_yaml
from coding_agent.workflow.v2_schema import V2Config
from coding_agent.workflow.v2_validate import (
    _build_config,
    _check_graph_compilation,
    _check_id,
    _check_keys,
    _check_limits_arithmetic,
    _check_limit_exceeded_acyclic,
    _check_model_table,
    _check_reachability,
    _check_required_inputs,
    _mapping_at,
    _Problems,
    _validate_models,
    _validate_target,
    _validate_worker,
    _validate_workflow,
    _WorkflowFacts,
)
from coding_agent.workflow.v3_schema import (
    normalize_skill_source,
    V3Config,
    V3SkillDecl,
)

_ROOT_FIELDS_V3 = frozenset({"version", "target", "models", "worker", "workflows", "skills"})
_ROOT_REQUIRED_V3 = frozenset({"version", "target", "models", "workflows"})
_SKILLS_FIELDS = frozenset({"source", "include", "ref"})
_SKILLS_REQUIRED = frozenset({"source", "include"})

_UNDECLARED_SKILL_LABEL = "`skills`"
"""How the v3 undeclared-skill refusal names the declaration to check."""


def _validate_skills(
    problems: _Problems, data: Mapping[str, Any]
) -> tuple[tuple[V3SkillDecl, ...] | None, frozenset[str]]:
    """Validate the configuration-wide ``skills`` declaration (SKV3-01–03).

    Returns the parsed entries plus the declared skill names; ``None`` (with
    faults recorded) when the block is unusable. A missing ``skills`` field
    and ``skills: []`` both mean an empty Skill Bundle (SKV3-04, static
    part). Every fault names its YAML path and never echoes a
    credential-bearing source component.
    """
    if "skills" not in data:
        return (), frozenset()
    raw = data["skills"]
    if not isinstance(raw, list):
        problems.add("skills", "'skills' must be a list of {source, include, ref?} entries")
        return None, frozenset()
    parsed: list[V3SkillDecl] = []
    seen_sources: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for index, entry in enumerate(raw):
        where = f"skills[{index}]"
        mapping = _check_keys(problems, where, entry, _SKILLS_FIELDS, _SKILLS_REQUIRED)
        if mapping is None:
            continue
        entry_ok = True
        source_raw = mapping.get("source")
        normalised: str | None = None
        if not isinstance(source_raw, str) or not source_raw:
            problems.add(f"{where}.source", "'source' must be a non-empty string")
            entry_ok = False
        else:
            try:
                normalised = normalize_skill_source(source_raw)
            except ValueError as exc:
                problems.add(f"{where}.source", str(exc))
                entry_ok = False
        names: list[str] = []
        include_raw = mapping.get("include")
        if not isinstance(include_raw, list) or not include_raw:
            problems.add(
                f"{where}.include", "'include' must be a non-empty list of skill names"
            )
            entry_ok = False
        else:
            for position, name in enumerate(include_raw):
                item_where = f"{where}.include[{position}]"
                if not isinstance(name, str) or not name:
                    problems.add(item_where, "skill name must be a non-empty string")
                    entry_ok = False
                    continue
                first = seen_names.get(name)
                if first is not None:
                    problems.add(
                        item_where,
                        f"skill {name!r} is already declared at {first}",
                    )
                    entry_ok = False
                    continue
                seen_names[name] = item_where
                names.append(name)
        ref: str | None = None
        if "ref" in mapping:
            ref_raw = mapping["ref"]
            if not isinstance(ref_raw, str) or not ref_raw:
                problems.add(f"{where}.ref", "'ref' must be a non-empty string")
                entry_ok = False
            else:
                ref = ref_raw
        if not entry_ok or normalised is None:
            continue
        first_source = seen_sources.get(normalised)
        if first_source is not None:
            problems.add(
                where,
                f"duplicate skill source {normalised!r}: already declared at {first_source}",
            )
            continue
        seen_sources[normalised] = where
        parsed.append(V3SkillDecl(source=normalised, include=tuple(names), ref=ref))
    if problems:
        return None, frozenset(seen_names)
    return tuple(parsed), frozenset(seen_names)


def validate_v3_file(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[V3Config | None, list[WorkflowConfigError]]:
    """Validate a `version: 3` file; returns the config plus every fault.

    Static and offline: reads the YAML file (and stats each prompt file)
    and makes no network call. No runtime secret is required or read: only
    credential *names* and the code-owned model tables are checked. The
    ``env`` argument is ignored and kept only for backward compatibility.
    The config is only returned when the fault list is empty.
    """
    source = Path(config_path)
    _ = env
    problems = _Problems()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        problems.add(str(source), f"cannot read workflow configuration: {exc}")
        return None, problems.items
    try:
        data = strict_load_yaml(text)
    except WorkflowConfigError as exc:
        detail = exc.detail
        if "anchors are not allowed" in detail or "aliases are not allowed" in detail:
            detail = "anchors/aliases are not allowed"
        problems.add(exc.path, detail)
        return None, problems.items
    if not isinstance(data, dict):
        problems.add("(root)", "must be a YAML mapping at the top level")
        return None, problems.items
    _check_keys(problems, "", data, _ROOT_FIELDS_V3, _ROOT_REQUIRED_V3)
    version = data.get("version")
    if type(version) is not int or version != 3:
        problems.add("version", f"unsupported version {version!r}: must be 3")
        return None, problems.items

    base = source.parent.resolve()
    skills, declared_names = _validate_skills(problems, data)
    target = _validate_target(problems, data)
    models = _validate_models(problems, data)
    worker = _validate_worker(problems, data)

    raw_workflows = data.get("workflows")
    workflows_mapping = _mapping_at(raw_workflows, "workflows", problems, "'workflows'")
    if workflows_mapping is not None and not workflows_mapping:
        problems.add("workflows", "'workflows' must declare at least one workflow")
        workflows_mapping = None
    facts_by_workflow: dict[str, tuple[dict[str, Any], _WorkflowFacts]] = {}
    if workflows_mapping is not None:
        for wid in workflows_mapping:
            if _check_id(wid, f"workflows.{wid}", problems, "workflow") is None:
                continue
            facts = _WorkflowFacts()
            _validate_workflow(
                problems,
                str(wid),
                workflows_mapping[wid],
                models,
                base,
                declared_names,
                facts,
                undeclared_skill_label=_UNDECLARED_SKILL_LABEL,
            )
            if isinstance(workflows_mapping[wid], dict):
                facts_by_workflow[str(wid)] = (workflows_mapping[wid], facts)

    if workflow_id is not None and (
        workflows_mapping is None or workflow_id not in workflows_mapping
    ):
        available = ", ".join(sorted(workflows_mapping)) if workflows_mapping else ""
        problems.add(
            "workflows",
            f"unknown workflow {workflow_id!r}: available: {available}",
        )

    if target is not None and models is not None:
        pairs, endpoints = _code_owned_tables()
        _check_model_table(problems, models, pairs, endpoints)

    # Phase 2 runs per Workflow whose phase 1 left no faults behind.
    for wid, (workflow_raw, facts) in facts_by_workflow.items():
        workflow_where = f"workflows.{wid}"
        before = len(problems)
        steps_raw = workflow_raw.get("steps")
        if not isinstance(steps_raw, dict) or not steps_raw:
            continue
        _check_limits_arithmetic(problems, workflow_where, facts)
        _check_reachability(problems, workflow_where, facts)
        _check_limit_exceeded_acyclic(problems, workflow_where, facts)
        if len(problems) != before:
            continue
        _check_required_inputs(problems, workflow_where, steps_raw, facts)

    # End labels never equal a Selection Label in the same file (§3).
    selection_labels: dict[str, str] = {}
    for wid, (_raw, facts) in facts_by_workflow.items():
        if facts.label is not None:
            selection_labels[wid] = facts.label
    if selection_labels:
        claimed = set(selection_labels.values())
        for wid, (_raw, facts) in facts_by_workflow.items():
            for outcome, labels in facts.outcomes.items():
                for label in labels:
                    if label in claimed:
                        owner = next(
                            owner_id
                            for owner_id, owned in selection_labels.items()
                            if owned == label
                        )
                        problems.add(
                            f"workflows.{wid}.outcomes.{outcome}",
                            f"end label {label!r} equals the Selection Label of "
                            f"workflow {owner!r}: the Worker never applies "
                            "a Selection Label",
                        )

    if problems or target is None or models is None or worker is None:
        return None, problems.items
    if workflows_mapping is None or skills is None:
        return None, problems.items
    try:
        v2_config = _build_config(data, source, target, models, worker)
    except (KeyError, TypeError, ValueError, AssertionError) as exc:
        problems.add("(root)", f"does not parse: {exc}")
        return None, problems.items
    _check_graph_compilation(problems, v2_config)
    if problems:
        return None, problems.items
    return (
        V3Config(
            version=3,
            target=v2_config.target,
            models=v2_config.models,
            worker=v2_config.worker,
            workflows=v2_config.workflows,
            skills=skills,
            source=source,
        ),
        [],
    )


def load_v3_config(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> V3Config:
    """Load and validate a `version: 3` file, raising the first fault.

    Static and offline like ``validate_v3_file``; prefer that entry point
    when every fault must be named.
    """
    config, faults = validate_v3_file(config_path, workflow_id, env=env)
    if config is None:
        raise faults[0] if faults else WorkflowConfigError("(root)", "invalid")
    return config


def _code_owned_tables() -> tuple[frozenset[tuple[str, str]], dict[str, frozenset[str]]]:
    """The code-owned (provider, model) table, via the v2 validator."""
    from coding_agent.workflow.v2_validate import _code_owned_tables as _v2_tables

    return _v2_tables()


def peek_config_version(config_path: Path | str) -> int | None:
    """Read the ``version`` field without any strictness.

    Like ``cli._peek_config_version``: ``yaml.safe_load`` collapses duplicate
    keys and resolves anchors instead of refusing them; that is fine here
    because the version only selects which strict validator runs on the file
    next, and that validator re-reads the file strictly.
    """
    import yaml

    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        version = data.get("version")
        if type(version) is int:
            return version
    return None


def validate_config_file(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[V2Config | V3Config | None, list[WorkflowConfigError]]:
    """Validate a Workflow Configuration file of either version.

    Dispatches on the peeked ``version``: ``3`` runs the v3 validator (with
    the ``skills`` declaration), anything else runs the v2 validator, which
    refuses non-``2`` versions itself. Static and offline either way.
    """
    if peek_config_version(config_path) == 3:
        return validate_v3_file(config_path, workflow_id, env=env)
    from coding_agent.workflow.v2_validate import validate_v2_file

    return validate_v2_file(config_path, workflow_id, env=env)
