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

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


# -- Installed-mode helpers (issue #392) -------------------------------------
#
# The fake installer output must never define expected membership: tests
# declare membership in the Workflow Configuration and approval in the policy
# document below, then materialise a managed store with ``write_fake_store``.

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40

SOURCE_A = "git+https://example.com/org/skills-a.git"
SOURCE_B = "git+https://example.com/org/skills-b.git"


@dataclass(frozen=True)
class DeclaredMember:
    """One test-declared skill: declaration truth, not fake output."""

    name: str
    source: str = SOURCE_A
    commit: str = COMMIT_A
    ref: str | None = None
    relative_source_path: str = ""
    skill_md: str = ""
    extra_files: dict[str, tuple[bytes, bool]] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    references: tuple[dict[str, object], ...] = ()
    reason: str = "synthetic review"
    disposition_overrides: dict[str, str] = field(default_factory=dict)

    def resolved_relative_source_path(self) -> str:
        """The provenance ``relativeSourcePath`` recorded for this member."""
        if self.relative_source_path:
            return self.relative_source_path
        return f"skills/{self.name}"


def default_skill_md_text(name: str, body: str = "Synthetic body.") -> str:
    """A minimal ``SKILL.md`` carrying no overlay marker."""
    return (
        f"---\nname: {name}\ndescription: Synthetic {name} skill for tests.\n---\n\n"
        f"# {name}\n\n{body}\n"
    )


def overlay_skill_md_text(name: str) -> str:
    """A ``SKILL.md`` that triggers the invocation-policy overlay."""
    return (
        f"---\nname: {name}\ndescription: Synthetic {name} skill for tests.\n---\n\n"
        f"# {name}\n\nBody.\n\ndisable-model-invocation: true\n"
    )


def member_files(member: DeclaredMember) -> dict[str, tuple[bytes, bool]]:
    """Artifact-relative files for ``write_fake_store``, including SKILL.md."""
    skill_text = member.skill_md if member.skill_md else default_skill_md_text(member.name)
    files: dict[str, tuple[bytes, bool]] = {"SKILL.md": (skill_text.encode("utf-8"), False)}
    for rel, payload in member.extra_files.items():
        files[rel] = payload
    return files


def default_disposition(rel: str, raw: bytes, executable: bool) -> str:
    """The policy disposition the deployment rules accept for one file."""
    if rel == "agents/openai.yaml" or rel.startswith("agents/"):
        return "not-delivered"
    if executable:
        return "not-delivered"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not-delivered"
    if raw.startswith(b"#!"):
        return "not-delivered"
    if "\x00" in text:
        return "not-delivered"
    return "inject"


def build_policy_document(members: list[DeclaredMember]) -> dict[str, object]:
    """Build a ``skill-policy.json`` document approving exactly ``members``.

    The review for each member matches source, name, ``SKILL.md`` digest and
    the complete non-``SKILL.md`` file set, so approval comes from the test's
    own declaration rather than from the fake installer's output.
    """
    reviews: list[dict[str, object]] = []
    for member in members:
        files = member_files(member)
        skill_sha = hashlib.sha256(files["SKILL.md"][0]).hexdigest()
        file_records: list[dict[str, object]] = []
        for rel in sorted(files.keys()):
            if rel == "SKILL.md":
                continue
            raw, executable = files[rel]
            disposition = member.disposition_overrides.get(rel)
            if disposition is None:
                disposition = default_disposition(rel, raw, executable)
            file_records.append(
                {
                    "disposition": disposition,
                    "executable": executable,
                    "path": rel,
                    "reason": f"synthetic {disposition}",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        reviews.append(
            {
                "files": file_records,
                "name": member.name,
                "reason": member.reason,
                "references": list(member.references),
                "requires": list(member.requires),
                "skillSha256": skill_sha,
                "sourceIdentity": member.source,
            }
        )
    return {"kind": "coding-agent.skill-bundle-policy", "reviews": reviews, "schemaVersion": 1}


def write_policy_file(path: Path, doc: dict[str, object]) -> bytes:
    """Write ``doc`` as canonical JSON; returns the raw bytes written."""
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return raw


def minimal_config_text(skills: list[dict[str, object]]) -> str:
    """A minimal ``version: 3`` configuration carrying ``skills``."""
    lines: list[str] = [
        "version: 3",
        "target:",
        "  kind: github",
        "  repository: OWNER/REPO",
        "  base-branch: main",
        "  token-env: GITHUB_TOKEN",
        "models:",
        "  sonnet:",
        "    provider: anthropic",
        "    model: claude-sonnet-5",
        "    endpoint: messages",
        "    effort: medium",
        "    api-key-env: ANTHROPIC_API_KEY",
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}",
        "workflows:",
        "  review:",
        "    model: sonnet",
        "    input: {type: pull-request, label: agent:review}",
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}",
        "    start: review",
        "    defaults:",
        "      max-executions: 1",
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}",
        "    steps:",
        "      review:",
        "        agent:",
        "          tools: [workspace-read]",
        "          results:",
        "            reviewed: {body: document}",
        "        routes: {reviewed: publish}",
        "      publish:",
        "        operation: publish-review",
        "        with: {body: review.reviewed.body}",
        "        routes: {published: {end: completed}, stale-head: {end: failed}}",
    ]
    if skills:
        lines.append("skills:")
        for entry in skills:
            source = str(entry["source"])
            lines.append(f"  - source: {source}")
            lines.append("    include:")
            include = entry["include"]
            assert isinstance(include, list)
            for name in include:
                lines.append(f"      - {name}")
            if "ref" in entry:
                lines.append(f"    ref: {entry['ref']}")
    return "\n".join(lines) + "\n"


def skills_entries_for(members: list[DeclaredMember]) -> list[dict[str, object]]:
    """Group declared members by (source, ref) into config ``skills`` entries."""
    grouped: dict[tuple[str, str | None], list[str]] = {}
    order: list[tuple[str, str | None]] = []
    for member in members:
        key = (member.source, member.ref)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(member.name)
    entries: list[dict[str, object]] = []
    for source, ref in order:
        entry: dict[str, object] = {"include": list(grouped[(source, ref)]), "source": source}
        if ref is not None:
            entry["ref"] = ref
        entries.append(entry)
    return entries


def fake_store_spec_for(members: list[DeclaredMember]) -> dict[str, dict[str, Any]]:
    """Build the ``write_fake_store`` spec for the declared members."""
    spec: dict[str, dict[str, Any]] = {}
    for member in members:
        record: dict[str, Any] = {
            "commit": member.commit,
            "files": member_files(member),
            "relativeSourcePath": member.resolved_relative_source_path(),
            "source": member.source,
        }
        if member.ref is not None:
            record["ref"] = member.ref
        spec[member.name] = record
    return spec
