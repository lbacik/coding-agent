"""Strict offline YAML loader for the Workflow Configuration file.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7 (strict
parsing: duplicate keys, anchors, aliases, merge keys and custom tags are
refused naming the YAML path) and §2.9 (``agent config validate`` is static
and offline).

Matrix row id: CFG-38.

This module owns only the syntax-level loader shared by the step-workflow
validators (``coding_agent.workflow.v2_validate`` for the archived
``version: 2`` shape, ``coding_agent.workflow.v3_validate`` for the running
``version: 3`` shape):
``WorkflowConfigError`` (a refusal naming the YAML path of the fault) and
``strict_load_yaml``. All ``version: 1`` shapes are gone at the S10 cutover.
"""

from __future__ import annotations

from dataclasses import dataclass
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
        raise WorkflowConfigError("version", "missing required field: 'version' must be 3")
    _check_tree(root, "")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowConfigError("(root)", f"does not parse: {exc}") from exc


def strict_load_yaml(text: str) -> Any:
    """Parse YAML text with the §2.7 syntax-level refusals.

    Duplicate keys, anchors, aliases, merge keys (``<<``) and custom tags
    are refused here, before the plain data is built. Used by the
    step-workflow validators (``coding_agent.workflow.v2_validate`` for
    the archived ``version: 2`` shape, ``coding_agent.workflow.v3_validate``
    for the running ``version: 3`` shape).
    """
    return _strict_load(text)
