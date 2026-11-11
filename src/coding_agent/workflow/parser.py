"""Strict offline loader for the Workflow Configuration YAML file.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table),
§2.2 (strict parsing), §2.3 (trust and paths: relative ``prompt_file`` paths
resolve against the YAML file's directory and stay inside it after symlink
resolution), §2.4 (``agent config validate`` is static and offline; every
refusal exits non-zero naming the YAML path) and
[ADR 0016](../docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md).

MVP row ids: W-CFG-1 (missing or unknown ``version``), W-CFG-2 (unknown
field), W-CFG-3 (duplicate key), W-CFG-4 (anchor, alias, merge key, custom
tag), W-CFG-7 (both/neither of ``skill``/``prompt_file``, empty ``steps``,
duplicated step id), W-CFG-8 (missing or escaping ``prompt_file``), C-CFG-2
(id outside ``[a-z0-9-]+``), W-CFG-13 (validate half: no network call).

Rejected here: a missing or unknown ``version`` (``1`` is the only value);
unknown fields at any level; duplicate keys; anchors, aliases, merge keys
(``<<``) and custom tags; ids outside ``[a-z0-9-]+``; an empty ``steps``
list; a step with both or neither of ``skill``/``prompt_file``; a duplicated
step id; a ``prompt_file`` that is missing or escapes the YAML directory
through ``..`` or a symlink.

Not checked here — the next ticket's semantic pass builds on the returned
``WorkflowConfig``: model references, the skill allowlist, the
(provider, model) table, environment variables, secrets and labels, and the
Effective Configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    Event,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from coding_agent.workflow.schema import (
    LimitsDecl,
    ModelDecl,
    SelectorDecl,
    StepDecl,
    TargetDecl,
    TargetLabels,
    TokenPrices,
    WorkerDecl,
    WorkflowConfig,
    WorkflowDecl,
)

_ID_RE = re.compile(r"[a-z0-9-]+")

_TOP_LEVEL_FIELDS = frozenset({"version", "target", "models", "worker", "workflows"})
_TARGET_FIELDS = frozenset({"kind", "repository", "base_branch", "token_env", "labels"})
_TARGET_LABEL_FIELDS = frozenset({"ready_for_human"})
_MODEL_FIELDS = frozenset(
    {"provider", "model", "endpoint", "effort", "api_key_env", "prices_usd_per_mtok"}
)
_PRICES_FIELDS = frozenset({"input", "output", "cache_read", "cache_write"})
_WORKER_FIELDS = frozenset({"poll_interval_seconds", "shutdown_grace_seconds"})
_WORKFLOW_FIELDS = frozenset({"model", "selector", "limits", "steps"})
_SELECTOR_FIELDS = frozenset({"label", "require_all", "require_any", "exclude"})
_LIMITS_FIELDS = frozenset(
    {"wall_clock_minutes", "cost_usd", "tokens", "tool_calls", "max_parallel_subagents"}
)
_STEP_FIELDS = frozenset({"id", "skill", "prompt_file"})


@dataclass(frozen=True)
class WorkflowConfigError(Exception):
    """A structural refusal of a Workflow Configuration file (§2.4).

    ``path`` is the YAML path of the fault (for example
    ``workflows.implement.steps[1].prompt_file``); ``str(error)`` always
    carries it, so the CLI names the fault by printing the error.
    """

    path: str
    detail: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.detail}"
        return self.detail


@dataclass
class _MapFrame:
    """One open mapping in the anchor/alias/tag event scan."""

    path: str
    pending_key: str | None = None


@dataclass
class _SequenceFrame:
    """One open sequence in the anchor/alias/tag event scan."""

    path: str
    index: int = 0


def _value_path(stack: list[_MapFrame | _SequenceFrame]) -> str:
    """The YAML path of the value position the scan currently sits at."""
    if not stack:
        return "(root)"
    top = stack[-1]
    if isinstance(top, _MapFrame):
        if top.pending_key is None:
            return top.path if top.path else "(root)"
        if top.path:
            return f"{top.path}.{top.pending_key}"
        return top.pending_key
    if top.path:
        return f"{top.path}[{top.index}]"
    return f"[{top.index}]"


def _is_custom_tag(tag: str | None) -> bool:
    return tag is not None and not tag.startswith("tag:yaml.org,2002:")


def _scan_events(events: list[Event]) -> None:
    """Refuse anchors, aliases, merge keys and custom tags (W-CFG-4).

    The scan walks the raw event stream so an anchor or alias is seen before
    PyYAML resolves it away. Every refusal names the YAML path of the fault.
    """
    stack: list[_MapFrame | _SequenceFrame] = []
    for event in events:
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            fault_path = _value_path(stack)
            if event.anchor is not None:
                raise WorkflowConfigError(
                    fault_path, f"anchors are not allowed: {event.anchor!r}"
                )
            if _is_custom_tag(event.tag):
                raise WorkflowConfigError(
                    fault_path, f"custom tags are not allowed: {event.tag!r}"
                )
            frame_path = "" if not stack else fault_path
            if isinstance(event, MappingStartEvent):
                stack.append(_MapFrame(path=frame_path))
            else:
                stack.append(_SequenceFrame(path=frame_path))
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            if stack:
                stack.pop()
            if stack:
                parent = stack[-1]
                if isinstance(parent, _MapFrame):
                    parent.pending_key = None
                else:
                    parent.index += 1
        elif isinstance(event, AliasEvent):
            raise WorkflowConfigError(
                _value_path(stack), f"aliases are not allowed: {event.anchor!r}"
            )
        elif isinstance(event, ScalarEvent):
            if not stack:
                if event.anchor is not None:
                    raise WorkflowConfigError("(root)", "anchors are not allowed")
                if _is_custom_tag(event.tag):
                    raise WorkflowConfigError("(root)", "custom tags are not allowed")
                continue
            top = stack[-1]
            if isinstance(top, _MapFrame) and top.pending_key is None:
                if event.anchor is not None:
                    key_path = f"{top.path}.{event.value}" if top.path else event.value
                    raise WorkflowConfigError(key_path, "anchors are not allowed")
                if _is_custom_tag(event.tag):
                    key_path = f"{top.path}.{event.value}" if top.path else event.value
                    raise WorkflowConfigError(key_path, "custom tags are not allowed")
                if event.value == "<<":
                    key_path = f"{top.path}.<<" if top.path else "<<"
                    raise WorkflowConfigError(key_path, "merge keys ('<<') are not allowed")
                top.pending_key = event.value
            else:
                path = _value_path(stack)
                if event.anchor is not None:
                    raise WorkflowConfigError(path, "anchors are not allowed")
                if _is_custom_tag(event.tag):
                    raise WorkflowConfigError(path, "custom tags are not allowed")
                if isinstance(top, _MapFrame):
                    top.pending_key = None
                else:
                    top.index += 1


def _node_path_key(path: str, key: str) -> str:
    if path:
        return f"{path}.{key}"
    return key


def _check_tree(node: Node, path: str) -> None:
    """Refuse duplicate keys, merge keys and custom tags (W-CFG-3, W-CFG-4).

    ``yaml.compose`` preserves the mapping pairs ``yaml.safe_load`` would
    silently collapse, so a duplicated key is seen here before the plain data
    is built.
    """
    if isinstance(node, MappingNode):
        if _is_custom_tag(node.tag):
            raise WorkflowConfigError(path if path else "(root)", "custom tags are not allowed")
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, ScalarNode) else ""
            key_path = _node_path_key(path, key)
            if _is_custom_tag(key_node.tag):
                raise WorkflowConfigError(key_path, "custom tags are not allowed")
            if key == "<<":
                raise WorkflowConfigError(key_path, "merge keys ('<<') are not allowed")
            if isinstance(key_node, ScalarNode) and key in seen:
                raise WorkflowConfigError(key_path, f"duplicate key {key!r}")
            if isinstance(key_node, ScalarNode):
                seen.add(key)
            _check_tree(value_node, key_path)
    elif isinstance(node, SequenceNode):
        if _is_custom_tag(node.tag):
            raise WorkflowConfigError(path if path else "(root)", "custom tags are not allowed")
        children: list[Node] = list(node.value)
        for index, child in enumerate(children):
            _check_tree(child, f"{path}[{index}]" if path else f"[{index}]")
    elif isinstance(node, ScalarNode):
        if _is_custom_tag(node.tag):
            raise WorkflowConfigError(path if path else "(root)", "custom tags are not allowed")


def _strict_load(text: str) -> Any:
    """Parse YAML text, refusing everything §2.2 forbids at the syntax level."""
    try:
        events = list(yaml.parse(text))
    except yaml.YAMLError as exc:
        raise WorkflowConfigError("(root)", f"does not parse: {exc}") from exc
    _scan_events(events)
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise WorkflowConfigError("(root)", f"does not parse: {exc}") from exc
    if root is None:
        raise WorkflowConfigError("version", "missing required field: 'version' must be 1")
    _check_tree(root, "")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowConfigError("(root)", f"does not parse: {exc}") from exc


def strict_load_yaml(text: str) -> Any:
    """Parse YAML text with the §2.2/§2.7 syntax-level refusals.

    Duplicate keys, anchors, aliases, merge keys (``<<``) and custom tags
    are refused here, before the plain data is built. Reused by the
    ``version: 2`` step-workflow validator
    (``coding_agent.workflow.v2_validate``) so both configuration versions
    share one strict loader.
    """
    return _strict_load(text)


def _reject_unknown_fields(mapping: dict[Any, Any], allowed: frozenset[str], path: str) -> None:
    for key in mapping:
        if not isinstance(key, str) or key not in allowed:
            key_path = _node_path_key(path, str(key))
            raise WorkflowConfigError(key_path, f"unknown field {key!r}")


def _require_mapping(value: Any, path: str, what: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise WorkflowConfigError(path, f"{what} must be a mapping")
    return value


def _require_nonempty_str(value: Any, path: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowConfigError(path, f"{what} must be a non-empty string")
    return value


def _require_number(value: Any, path: str, what: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkflowConfigError(path, f"{what} must be a number, got {value!r}")
    return value


def _require_str_list(value: Any, path: str, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry for entry in value
    ):
        raise WorkflowConfigError(path, f"{what} must be a list of non-empty strings")
    return tuple(value)


def _check_id(value: Any, path: str, what: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise WorkflowConfigError(path, f"{what} id {value!r} must match [a-z0-9-]+")
    return value


def _parse_target(value: Any, path: str) -> TargetDecl:
    mapping = _require_mapping(value, path, "'target'")
    _reject_unknown_fields(mapping, _TARGET_FIELDS, path)
    kind = _require_nonempty_str(mapping.get("kind"), f"{path}.kind", "'target.kind'")
    repository = _require_nonempty_str(
        mapping.get("repository"), f"{path}.repository", "'target.repository'"
    )
    base_branch = _require_nonempty_str(
        mapping.get("base_branch"), f"{path}.base_branch", "'target.base_branch'"
    )
    token_env = _require_nonempty_str(
        mapping.get("token_env"), f"{path}.token_env", "'target.token_env'"
    )
    labels = TargetLabels()
    if "labels" in mapping:
        labels_mapping = _require_mapping(mapping["labels"], f"{path}.labels", "'target.labels'")
        _reject_unknown_fields(labels_mapping, _TARGET_LABEL_FIELDS, f"{path}.labels")
        if "ready_for_human" in labels_mapping:
            labels = TargetLabels(
                ready_for_human=_require_nonempty_str(
                    labels_mapping["ready_for_human"],
                    f"{path}.labels.ready_for_human",
                    "'target.labels.ready_for_human'",
                )
            )
    return TargetDecl(
        kind=kind, repository=repository, base_branch=base_branch, token_env=token_env,
        labels=labels,
    )


def _parse_prices(value: Any, path: str) -> TokenPrices:
    mapping = _require_mapping(value, path, "'prices_usd_per_mtok'")
    _reject_unknown_fields(mapping, _PRICES_FIELDS, path)
    return TokenPrices(
        input=float(_require_number(mapping.get("input"), f"{path}.input", "'input'")),
        output=float(_require_number(mapping.get("output"), f"{path}.output", "'output'")),
        cache_read=float(
            _require_number(mapping.get("cache_read"), f"{path}.cache_read", "'cache_read'")
        ),
        cache_write=float(
            _require_number(mapping.get("cache_write"), f"{path}.cache_write", "'cache_write'")
        ),
    )


def _parse_models(value: Any, path: str) -> dict[str, ModelDecl]:
    mapping = _require_mapping(value, path, "'models'")
    if not mapping:
        raise WorkflowConfigError(path, "'models' must declare at least one model")
    models: dict[str, ModelDecl] = {}
    for model_id in mapping:
        model_path = _node_path_key(path, str(model_id))
        checked_id = _check_id(model_id, model_path, "model")
        entry = _require_mapping(mapping[model_id], model_path, "model entry")
        _reject_unknown_fields(entry, _MODEL_FIELDS, model_path)
        models[checked_id] = ModelDecl(
            id=checked_id,
            provider=_require_nonempty_str(
                entry.get("provider"), f"{model_path}.provider", "'provider'"
            ),
            model=_require_nonempty_str(entry.get("model"), f"{model_path}.model", "'model'"),
            endpoint=_require_nonempty_str(
                entry.get("endpoint"), f"{model_path}.endpoint", "'endpoint'"
            ),
            effort=_require_nonempty_str(
                entry.get("effort"), f"{model_path}.effort", "'effort'"
            ),
            api_key_env=_require_nonempty_str(
                entry.get("api_key_env"), f"{model_path}.api_key_env", "'api_key_env'"
            ),
            prices=_parse_prices(
                entry.get("prices_usd_per_mtok"), f"{model_path}.prices_usd_per_mtok"
            ),
        )
    return models


def _parse_worker(value: Any, path: str) -> WorkerDecl:
    mapping = _require_mapping(value, path, "'worker'")
    _reject_unknown_fields(mapping, _WORKER_FIELDS, path)
    poll_interval: int | float | None = None
    shutdown_grace: int | float | None = None
    if "poll_interval_seconds" in mapping:
        poll_interval = _require_number(
            mapping["poll_interval_seconds"], f"{path}.poll_interval_seconds",
            "'poll_interval_seconds'",
        )
    if "shutdown_grace_seconds" in mapping:
        shutdown_grace = _require_number(
            mapping["shutdown_grace_seconds"], f"{path}.shutdown_grace_seconds",
            "'shutdown_grace_seconds'",
        )
    return WorkerDecl(
        poll_interval_seconds=poll_interval, shutdown_grace_seconds=shutdown_grace
    )


def _parse_selector(value: Any, path: str) -> SelectorDecl:
    mapping = _require_mapping(value, path, "'selector'")
    _reject_unknown_fields(mapping, _SELECTOR_FIELDS, path)
    if "label" not in mapping:
        raise WorkflowConfigError(f"{path}.label", "missing required field: 'selector.label'")
    label = _require_nonempty_str(mapping["label"], f"{path}.label", "'selector.label'")
    require_all = (
        _require_str_list(mapping["require_all"], f"{path}.require_all", "'require_all'")
        if "require_all" in mapping
        else ()
    )
    require_any = (
        _require_str_list(mapping["require_any"], f"{path}.require_any", "'require_any'")
        if "require_any" in mapping
        else ()
    )
    exclude = (
        _require_str_list(mapping["exclude"], f"{path}.exclude", "'exclude'")
        if "exclude" in mapping
        else ()
    )
    return SelectorDecl(label=label, require_all=require_all, require_any=require_any,
                        exclude=exclude)


def _require_max_parallel_subagents(value: Any, path: str) -> int:
    """Refuse a ``max_parallel_subagents`` that is not an integer >= 1 (§2.2)."""
    if isinstance(value, bool) or type(value) is not int or value < 1:
        raise WorkflowConfigError(
            path, f"'max_parallel_subagents' must be an integer >= 1, got {value!r}"
        )
    return value


def _parse_limits(value: Any, path: str) -> LimitsDecl:
    mapping = _require_mapping(value, path, "'limits'")
    _reject_unknown_fields(mapping, _LIMITS_FIELDS, path)
    fields: dict[str, int | float | None] = {
        "wall_clock_minutes": None,
        "cost_usd": None,
        "tokens": None,
        "tool_calls": None,
    }
    for name in fields:
        if name in mapping:
            fields[name] = _require_number(mapping[name], f"{path}.{name}", f"'{name}'")
    wall_clock = fields["wall_clock_minutes"]
    cost = fields["cost_usd"]
    tokens = fields["tokens"]
    calls = fields["tool_calls"]
    parallel: int | None = None
    if "max_parallel_subagents" in mapping:
        parallel = _require_max_parallel_subagents(
            mapping["max_parallel_subagents"], f"{path}.max_parallel_subagents"
        )
    return LimitsDecl(
        wall_clock_minutes=wall_clock,
        cost_usd=cost,
        tokens=tokens,
        tool_calls=calls,
        max_parallel_subagents=parallel,
    )


def _check_prompt_file(value: str, step_path: str, config_dir: Path) -> None:
    """Refuse a ``prompt_file`` that is missing or escapes the YAML directory (W-CFG-8)."""
    path = f"{step_path}.prompt_file"
    candidate = (config_dir / value).resolve()
    if candidate != config_dir and config_dir not in candidate.parents:
        raise WorkflowConfigError(path, f"escapes the configuration directory: {value!r}")
    if not candidate.is_file():
        raise WorkflowConfigError(path, f"prompt file does not exist: {value!r}")


def _parse_steps(value: Any, path: str, config_dir: Path) -> tuple[StepDecl, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowConfigError(path, "'steps' must be a non-empty list")
    steps: list[StepDecl] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        step_path = f"{path}[{index}]"
        step = _require_mapping(entry, step_path, "step")
        _reject_unknown_fields(step, _STEP_FIELDS, step_path)
        if "id" not in step:
            raise WorkflowConfigError(f"{step_path}.id", "missing required field: step 'id'")
        step_id = _check_id(step["id"], f"{step_path}.id", "step")
        has_skill = "skill" in step
        has_prompt_file = "prompt_file" in step
        if has_skill and has_prompt_file:
            raise WorkflowConfigError(
                step_path, "must have exactly one of 'skill' or 'prompt_file', not both"
            )
        if not has_skill and not has_prompt_file:
            raise WorkflowConfigError(
                step_path, "must have exactly one of 'skill' or 'prompt_file'"
            )
        if step_id in seen:
            raise WorkflowConfigError(
                f"{step_path}.id", f"duplicate step id {step_id!r}"
            )
        seen.add(step_id)
        skill: str | None = None
        prompt_file: str | None = None
        if has_skill:
            skill = _require_nonempty_str(step["skill"], f"{step_path}.skill", "'skill'")
        else:
            prompt_file = _require_nonempty_str(
                step["prompt_file"], f"{step_path}.prompt_file", "'prompt_file'"
            )
            _check_prompt_file(prompt_file, step_path, config_dir)
        steps.append(StepDecl(id=step_id, skill=skill, prompt_file=prompt_file))
    return tuple(steps)


def _parse_workflows(
    value: Any, path: str, config_dir: Path
) -> dict[str, WorkflowDecl]:
    mapping = _require_mapping(value, path, "'workflows'")
    if not mapping:
        raise WorkflowConfigError(path, "'workflows' must declare at least one workflow")
    workflows: dict[str, WorkflowDecl] = {}
    for workflow_id in mapping:
        workflow_path = _node_path_key(path, str(workflow_id))
        checked_id = _check_id(workflow_id, workflow_path, "workflow")
        entry = _require_mapping(mapping[workflow_id], workflow_path, "workflow entry")
        _reject_unknown_fields(entry, _WORKFLOW_FIELDS, workflow_path)
        if "model" not in entry:
            raise WorkflowConfigError(
                f"{workflow_path}.model", "missing required field: workflow 'model'"
            )
        if "selector" not in entry:
            raise WorkflowConfigError(
                f"{workflow_path}.selector", "missing required field: workflow 'selector'"
            )
        if "steps" not in entry:
            raise WorkflowConfigError(
                f"{workflow_path}.steps", "missing required field: workflow 'steps'"
            )
        model = _require_nonempty_str(
            entry["model"], f"{workflow_path}.model", "workflow 'model'"
        )
        selector = _parse_selector(entry["selector"], f"{workflow_path}.selector")
        limits = (
            _parse_limits(entry["limits"], f"{workflow_path}.limits")
            if "limits" in entry
            else None
        )
        steps = _parse_steps(entry["steps"], f"{workflow_path}.steps", config_dir)
        workflows[checked_id] = WorkflowDecl(
            id=checked_id, model=model, selector=selector, steps=steps, limits=limits
        )
    return workflows


def load_workflow_config(
    config_path: Path | str, *, workflow_id: str | None = None
) -> WorkflowConfig:
    """Load and structurally validate a Workflow Configuration file (§2.4).

    Static and offline: reads the YAML file (and stats each ``prompt_file``)
    and makes no network call (W-CFG-13). Every refusal raises
    ``WorkflowConfigError`` naming the YAML path of the fault.

    ``workflow_id`` selects one Workflow: an unknown id is refused with the
    available ids listed (§2.4). The semantic checks — model and skill
    references, the allowlist, environment variables, secrets and labels —
    are the next ticket's; the returned ``WorkflowConfig`` is what they build
    on.
    """
    source = Path(config_path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowConfigError(
            str(source), f"cannot read workflow configuration: {exc}"
        ) from exc
    data = _strict_load(text)
    if not isinstance(data, dict):
        raise WorkflowConfigError("(root)", "must be a YAML mapping at the top level")
    _reject_unknown_fields(data, _TOP_LEVEL_FIELDS, "")
    if "version" not in data:
        raise WorkflowConfigError("version", "missing required field: 'version' must be 1")
    version = data["version"]
    if type(version) is not int or version != 1:
        raise WorkflowConfigError(
            "version", f"unsupported version {version!r}: must be 1"
        )
    for required in ("target", "models", "workflows"):
        if required not in data:
            raise WorkflowConfigError(
                required, f"missing required field {required!r}"
            )
    config_dir = source.parent.resolve()
    target = _parse_target(data["target"], "target")
    models = _parse_models(data["models"], "models")
    worker = _parse_worker(data["worker"], "worker") if "worker" in data else None
    workflows = _parse_workflows(data["workflows"], "workflows", config_dir)
    if workflow_id is not None and workflow_id not in workflows:
        available = ", ".join(sorted(workflows))
        raise WorkflowConfigError(
            "workflows", f"unknown workflow {workflow_id!r}: available: {available}"
        )
    return WorkflowConfig(
        version=1, target=target, models=models, workflows=workflows, source=source,
        worker=worker,
    )
