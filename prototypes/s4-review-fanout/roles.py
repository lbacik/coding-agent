"""THROWAWAY PROTOTYPE — see README.md. Not production code.

Slice one reviewer role's instructions out of the upstream `code-review`
SKILL.md, mechanically, by heading. Whether this is cleanly possible is itself
one of the four things issue #20 asks the prototype to record, so the split is
done by the machine rather than by hand: if it needed hand-editing, that is a
finding about S4's adapter, not a detail of this file.
"""

from __future__ import annotations

from pathlib import Path

STANDARDS = "standards"
SPEC = "spec"


def _between(text: str, start: str, end: str | None) -> str:
    """The slice of `text` from `start` up to (not including) `end`."""
    begin = text.index(start)
    stop = text.index(end, begin) if end else len(text)
    return text[begin:stop].strip()


def whole_skill(skill_md: Path) -> str:
    """The entire skill, front matter and all — what arm B hands a reviewer."""
    return skill_md.read_text()


def role_instructions(skill_md: Path, role: str) -> str:
    """Only `role`'s own instructions, as the contract requires for arm A.

    Everything that belongs to the orchestrator — the two-axis rationale, the
    fixed-point negotiation, the aggregation step, and above all the heading
    "Spawn both sub-agents in parallel" — is left behind.
    """
    text = whole_skill(skill_md)

    section_4 = _between(
        text,
        "**Standards sub-agent prompt** should include:",
        "### 5. Aggregate",
    )

    if role == STANDARDS:
        block = _between(
            section_4,
            "**Standards sub-agent prompt** should include:",
            "**Spec sub-agent prompt** should include:",
        )
        # The Standards brief refers to "the smell baseline from step 3" and
        # says the sub-agent "has no other access to it", so the baseline
        # travels with the role.
        baseline = _between(
            text,
            "On top of whatever the repo documents,",
            "### 4. Spawn both sub-agents in parallel",
        )
        return f"{block}\n\n---\n\n{baseline}"

    if role == SPEC:
        return _between(
            section_4,
            "**Spec sub-agent prompt** should include:",
            "If the spec is missing,",
        )

    raise ValueError(f"unknown role: {role}")


if __name__ == "__main__":
    import sys

    skill = Path(sys.argv[1])
    for name in (STANDARDS, SPEC):
        print(f"{'=' * 70}\n{name.upper()}\n{'=' * 70}")
        print(role_instructions(skill, name))
        print()
