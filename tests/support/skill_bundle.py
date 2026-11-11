"""A synthetic Skill Bundle for tests that bind the ``activate_*`` tools.

Tests must never read the host's ``~/.agents/skills``: it exists on a
developer machine and inside the agent's container, but not on a CI runner,
so a test that depends on it passes locally and fails in CI. Build this
bundle under ``tmp_path`` instead (the ``skills_dir`` fixture in
``tests/conftest.py`` does that) and pass it explicitly.

The bundle carries every ``BUNDLE_SPEC`` member with a YAML frontmatter
``description`` and every Companion File the spec names, so activation,
Pinned Prefix composition and companion handling all have what they read.
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.skillbundle.verify import BUNDLE_SPEC


# A ``code-review`` SKILL.md with no Role Block anchors: since #219 the Worker
# starts without cutting or verifying slices, so the fixture proves an
# anchor-less skill verifies and activates (Scenario 29 of #208).
REVIEW_SKILL_TEXT = """\
---
name: code-review
description: Synthetic code-review skill for activation tests.
---

# code-review (synthetic)

Review the change with sub-agents of the model's own devising.
"""


def skill_description(skill: str) -> str:
    return f"Synthetic {skill} skill for tests."


def skill_md(skill: str) -> str:
    if skill == "code-review":
        return REVIEW_SKILL_TEXT
    return (
        f"---\nname: {skill}\ndescription: {skill_description(skill)}\n---\n\n"
        f"# {skill}\n\nSynthetic body of the {skill} skill.\n"
    )


def companion_md(skill: str, relative: str) -> str:
    return f"# {relative}\n\nSynthetic companion {relative} of the {skill} skill.\n"


def write_skill_bundle(base: Path) -> Path:
    """Write the bundle to ``<base>/.agents/skills`` and return that path."""
    skills_dir = base / ".agents" / "skills"
    for skill in sorted(BUNDLE_SPEC.names):
        skill_dir = skills_dir / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_md(skill), encoding="utf-8")
        for relative in BUNDLE_SPEC.companion_files.get(skill, ()):
            (skill_dir / relative).write_text(companion_md(skill, relative), encoding="utf-8")
    return skills_dir
