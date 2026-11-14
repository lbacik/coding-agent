"""Skill activation bodies and the immutable activation registry.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (Opening a step: the
activation result is the skill body, or the prompt file's bytes, verbatim; no
skill is injected eagerly; a Companion File reaches the model whole, with the
activation of the skill that owns it, or not at all; the activation result is
exempt from result capping and is an ordinary Exchange Unit, never a Pinned
Prefix member; Tools: ``activate_*`` for each Skill Bundle member).

MVP row ids: C-SKL-1, C-SKL-2, L3-IMP-1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableSet
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.skillbundle.verify import BUNDLE_SPEC

ACTIVATION_REGISTRY: dict[str, str] = {
    "implement": "activate_implement",
    "tdd": "activate_tdd",
    "codebase-design": "activate_codebase-design",
    "code-review": "activate_code_review",
}
"""Skill name to activation tool name (L3-IMP-1).

``implement`` is the top-level entry; ``tdd`` is nested under it,
``codebase-design`` nested under ``tdd``. All four are bound from the first
turn (C-SKL-1); neither ``implement`` nor nested ``tdd`` activates twice.
"""

TOOL_TO_SKILL: dict[str, str] = {tool: skill for skill, tool in ACTIVATION_REGISTRY.items()}

UNKNOWN_SKILL_REASON = "unknown skill"
"""The refusal reason where the name is not an exact registry member."""


@dataclass(frozen=True)
class ActivationRegistry:
    """The Skill Bundle's activations, loaded once at Worker startup.

    An immutable in-memory snapshot: every value is the activation body as
    installed (``SKILL.md`` whole plus owned Companion Files, verbatim), keyed
    by exact skill name. Nothing after startup reads the skill files again, so
    editing or deleting a file on disk changes no activation within the
    process. Later work swaps the source from the fixed Skill Bundle to the
    verified prepared generation; the agent step keeps reading this type.
    """

    bodies: Mapping[str, str]
    """Exact skill name to activation body, verbatim."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bodies", MappingProxyType(dict(self.bodies)))


def load_activation_registry(skills_dir: Path) -> ActivationRegistry:
    """Load every Skill Bundle member's activation body once (Worker startup).

    Reads through :func:`activation_body`, so a skill owning Companion Files
    pins those too. An unreadable skill raises ``OSError``; the caller turns
    that into a startup refusal before any Attempt opens.
    """
    return ActivationRegistry(
        bodies={skill: activation_body(skills_dir, skill) for skill in ACTIVATION_REGISTRY}
    )


@dataclass(frozen=True)
class ActivationDelivered:
    """A successful activation: the skill body, verbatim."""

    skill: str
    content: str

    @property
    def text(self) -> str:
        """The caller-facing text: the body, byte-identical, whatever it holds."""
        return self.content


@dataclass(frozen=True)
class ActivationRefused:
    """A normal refusal: the name is not an exact registry member.

    Unknown, undeclared, absolute, traversal (``../x``), path-shaped
    (``a/b``) and differently-cased names all land here: the lookup below is
    an exact mapping hit, so none of them touches the filesystem, marks
    anything active or changes any authority.
    """

    skill: str
    reason: str = UNKNOWN_SKILL_REASON

    @property
    def text(self) -> str:
        """The caller-facing text: a normal refusal, never the body."""
        return f"Skill {self.skill!r} is not available: {self.reason}."


@dataclass(frozen=True)
class ActivationAlreadyActive:
    """The duplicate-activation acknowledgement: the body is not sent again."""

    skill: str

    @property
    def acknowledgement(self) -> str:
        """The caller-facing acknowledgement, preserved verbatim."""
        return f"Skill {self.skill!r} is already active."

    @property
    def text(self) -> str:
        """The caller-facing text: the acknowledgement."""
        return self.acknowledgement


ActivationOutcome = ActivationDelivered | ActivationRefused | ActivationAlreadyActive
"""Every activation result: delivered content, normal refusal or ack."""


def lookup_activation(
    registry: ActivationRegistry, name: object
) -> ActivationDelivered | ActivationRefused:
    """The one exact-name lookup both activation paths share.

    An exact mapping hit delivers the body verbatim — even where the body
    itself begins with ``Error:`` — and every other name (unknown,
    undeclared, absolute, traversal, path-shaped, differently-cased, or not a
    string at all) is a typed normal refusal. No filesystem access: the
    registry is the whole source.
    """
    if isinstance(name, str) and name in registry.bodies:
        return ActivationDelivered(skill=name, content=registry.bodies[name])
    shown = name if isinstance(name, str) else repr(name)
    return ActivationRefused(skill=shown, reason=UNKNOWN_SKILL_REASON)


def deliver_activation(
    registry: ActivationRegistry, name: object, activated: MutableSet[str]
) -> ActivationOutcome:
    """The one delivery function both activation paths share.

    A refused name stays refused without touching ``activated``; a delivered
    skill already in ``activated`` is acknowledged, never sent again;
    otherwise the skill is marked active and its body delivered. Static
    instructions pass a throwaway set per opening (each opening carries the
    bodies verbatim); the on-demand tool passes its per-execution set (the
    duplicate acknowledgement is preserved there).
    """
    outcome = lookup_activation(registry, name)
    if isinstance(outcome, ActivationRefused):
        return outcome
    if outcome.skill in activated:
        return ActivationAlreadyActive(skill=outcome.skill)
    activated.add(outcome.skill)
    return outcome


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def skill_description(skills_dir: Path, skill: str) -> str:
    """The skill's frontmatter ``description``, verbatim (C-SKL-1)."""
    text = (skills_dir / skill / "SKILL.md").read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"skill {skill!r} has no YAML frontmatter")
    frontmatter: Any = yaml.safe_load(match.group(1))
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError(f"skill {skill!r} has no frontmatter description")
    return description


def activation_body(skills_dir: Path, skill: str) -> str:
    """The activation result for ``skill``: its body plus companions, verbatim.

    The body is the whole ``SKILL.md`` as installed (ADR 0007: answered,
    never rewritten). Companion Files travel whole with the activation of the
    skill that owns them, or not at all (ADR 0012): ``tdd`` carries its two
    companions; ``codebase-design``'s two are refused (the Attempt Header
    answers them instead); ``implement`` and ``code-review`` carry none (in
    M1 ``activate_code_review`` returns the skill body only).
    """
    body = (skills_dir / skill / "SKILL.md").read_text(encoding="utf-8")
    if skill == "tdd":
        parts = [body]
        for relative in BUNDLE_SPEC.companion_files.get("tdd", ()):
            companion = (skills_dir / skill / relative).read_text(encoding="utf-8")
            parts.append(f"----- {skill}/{relative} -----\n{companion}")
        return "\n\n".join(parts)
    return body


def build_activation_tools(skills_dir: Path) -> tuple[BaseTool, ...]:
    """All four ``activate_*`` tools, bound from the first turn (C-SKL-1).

    Each tool's description is its skill's frontmatter ``description``,
    verbatim. The first call returns the activation body whole; a second
    call to the same tool returns a short "already active" acknowledgement,
    never the body again. The tools take no path argument, so no tool
    resolves a path under a skill's directory (C-SKL-2).
    """
    activated: set[str] = set()
    bodies: dict[str, str] = {skill: activation_body(skills_dir, skill) for skill in ACTIVATION_REGISTRY}
    descriptions: dict[str, str] = {
        skill: skill_description(skills_dir, skill) for skill in ACTIVATION_REGISTRY
    }

    def _make(skill: str, tool_name: str) -> BaseTool:
        description = descriptions[skill]

        def _activate() -> str:
            if skill in activated:
                return f"Skill {skill!r} is already active."
            activated.add(skill)
            return bodies[skill]

        _activate.__name__ = tool_name
        return StructuredTool.from_function(
            func=_activate,
            name=tool_name,
            description=description,
        )

    return tuple(
        _make(skill, tool_name) for skill, tool_name in ACTIVATION_REGISTRY.items()
    )
