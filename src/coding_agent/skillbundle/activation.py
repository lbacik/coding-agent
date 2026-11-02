"""Skill activation bodies and the per-Attempt activation registry.

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
from pathlib import Path
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
