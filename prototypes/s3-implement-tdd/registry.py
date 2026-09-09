"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

The activation registry, on its own so it can be probed without a model.

The rules it enforces are the three L3-IMP rows S3 owes:

  L3-IMP-1  `/implement` and nested `/tdd` activate; neither activates twice
  L3-IMP-2  a companion file is served through `read_skill_resource`,
            resolved relative to the skill's own directory
  L3-IMP-3  `codebase-design` is activatable only nested from `tdd`,
            never by model-driven discovery

Refusals are deliberately terse. A refusal that explains what the model
*should* have done is coaching, and would contaminate every question this
prototype asks about what the model does when it is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Which skills this node may activate, and what each one requires first.
# `implement` is injected by the node itself, so it is pre-registered, never
# activated through the tool.
NODE_SKILL = "implement"
ACTIVATABLE: dict[str, str | None] = {
    "tdd": None,  # nested directly from the implementer
    "codebase-design": "tdd",  # L3-IMP-3: only ever nested from tdd
}
# Installed in the Skill Bundle, but owned by a node that does not exist while
# the implementer is running. Refused with a distinct reason so question 3 can
# be told apart from "no such skill".
OTHER_NODES = {"code-review"}


@dataclass
class Activation:
    skill: str
    granted: bool
    reason: str
    step: int


@dataclass
class ResourceRead:
    skill: str
    requested_path: str
    resolved_path: str | None
    granted: bool
    reason: str
    step: int


@dataclass
class Registry:
    """The activation registry. One per conversation."""

    bundle: Path
    #: `implement` is injected at the node, so it is in the registry from the start.
    active: list[str] = field(default_factory=lambda: [NODE_SKILL])
    activations: list[Activation] = field(default_factory=list)
    resource_reads: list[ResourceRead] = field(default_factory=list)
    step: int = 0

    # -- activation ------------------------------------------------------

    def activate(self, skill: str) -> tuple[bool, str]:
        """Return (granted, payload-or-refusal) for one activation attempt."""
        skill = skill.strip().lstrip("/")

        if skill in self.active:
            # L3-IMP-1: nothing activates twice.
            return self._record(skill, False, "already-active", f"[{skill} is already active in this conversation]")

        if skill in OTHER_NODES:
            return self._record(skill, False, "other-node", f"[{skill} is not activatable here]")

        required = ACTIVATABLE.get(skill, "__absent__")
        if required == "__absent__":
            return self._record(skill, False, "not-activatable", f"[{skill} is not activatable here]")

        if required is not None and required not in self.active:
            # L3-IMP-3 in its negative half.
            return self._record(skill, False, "requires-parent", f"[{skill} is not activatable here]")

        body = (self.bundle / skill / "SKILL.md").read_text()
        self.active.append(skill)
        return self._record(skill, True, "granted", body)

    def _record(self, skill: str, granted: bool, reason: str, payload: str) -> tuple[bool, str]:
        self.activations.append(Activation(skill, granted, reason, self.step))
        return granted, payload

    # -- companion files -------------------------------------------------

    def read_resource(self, skill: str, path: str) -> str:
        """L3-IMP-2: resolve `path` against the skill's own directory."""
        skill = skill.strip().lstrip("/")

        if skill not in self.active:
            self.resource_reads.append(
                ResourceRead(skill, path, None, False, "not-active", self.step)
            )
            return f"[{skill} is not active in this conversation]"

        skill_dir = (self.bundle / skill).resolve()
        target = (skill_dir / path).resolve()

        # The whole point of the row: the skill's directory is the root, and
        # nothing outside it is reachable through this tool.
        if not target.is_relative_to(skill_dir):
            self.resource_reads.append(
                ResourceRead(skill, path, str(target), False, "escapes-skill-dir", self.step)
            )
            return f"[{path} is outside {skill}'s own directory]"

        rel = str(target.relative_to(skill_dir))
        if not target.is_file():
            self.resource_reads.append(
                ResourceRead(skill, path, rel, False, "no-such-file", self.step)
            )
            return f"[no file {rel} in skill {skill}]"

        self.resource_reads.append(
            ResourceRead(skill, path, rel, True, "granted", self.step)
        )
        return target.read_text()


# --------------------------------------------------------------------------
# The deterministic probe. No model, no tokens: the registry's own rules,
# checked directly, so question 5's negative half is answered even if the model
# never happens to reach for `codebase-design` from the implementer's seat.
# --------------------------------------------------------------------------


def probe(bundle: Path) -> list[dict]:
    results: list[dict] = []

    def case(name: str, got: tuple[bool, str] | str, want_granted: bool) -> None:
        granted = got[0] if isinstance(got, tuple) else not got.startswith("[")
        results.append(
            {
                "case": name,
                "granted": granted,
                "expected_granted": want_granted,
                "pass": granted == want_granted,
                "detail": (got[1] if isinstance(got, tuple) else got).splitlines()[0][:120],
            }
        )

    r = Registry(bundle=bundle)
    case("codebase-design straight from implement", r.activate("codebase-design"), False)
    case("code-review from implement", r.activate("code-review"), False)
    case("tdd from implement", r.activate("tdd"), True)
    case("tdd a second time", r.activate("tdd"), False)
    case("codebase-design once tdd is active", r.activate("codebase-design"), True)
    case("tdd companion tests.md", r.read_resource("tdd", "tests.md"), True)
    case("tdd companion by full bundle path", r.read_resource("tdd", "skills/engineering/tdd/tests.md"), False)
    case("tdd companion escaping upwards", r.read_resource("tdd", "../implement/SKILL.md"), False)
    case("companion of an inactive skill", r.read_resource("code-review", "SKILL.md"), False)
    return results


if __name__ == "__main__":
    import json
    import sys

    rows = probe(Path(sys.argv[1]))
    print(json.dumps(rows, indent=2))
    print(f"\n{sum(r['pass'] for r in rows)}/{len(rows)} registry rules hold")
