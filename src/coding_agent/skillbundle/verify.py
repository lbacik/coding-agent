from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Deliberately naive: any slash followed by a word, wherever it appears in the
# text. It will match markdown/file-path fragments as well as real skill
# invocations (issue #15's "That last scan will produce false positives"),
# which is why every hit is judged against the Skill Bundle plus a
# human-curated ignore list rather than by trying to parse markdown structure.
SKILL_REF_RE = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")


def _artifact_name(artifact_id: str) -> str:
    _, _, name = artifact_id.partition(":")
    return name or artifact_id


@dataclass(frozen=True)
class BundleSpec:
    ids: frozenset[str]
    """Full artifact ids the Skill Bundle carries, e.g. "skill:tdd"."""
    source_identity: str
    resolved_commit: str
    companion_files: dict[str, tuple[str, ...]]
    """Bare skill name -> relative paths that must exist under its base directory."""

    @property
    def names(self) -> frozenset[str]:
        return frozenset(_artifact_name(artifact_id) for artifact_id in self.ids)


BUNDLE_SPEC = BundleSpec(
    ids=frozenset({"skill:implement", "skill:tdd", "skill:code-review", "skill:codebase-design"}),
    source_identity="git+https://github.com/mattpocock/skills.git",
    resolved_commit="3cca18b368ae95cdbdebbff572ccafa662551015",
    companion_files={
        "tdd": ("tests.md", "mocking.md"),
        "codebase-design": ("DEEPENING.md", "DESIGN-IT-TWICE.md"),
    },
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class VerificationReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.results.append(CheckResult(name, passed, detail))

    def check(self, name: str, bad: Collection[str], ok_detail: str, bad_prefix: str) -> None:
        """Records a check whose failure is "these particular ids/refs are wrong"."""
        passed = not bad
        detail = ok_detail if passed else f"{bad_prefix}: {', '.join(sorted(bad))}"
        self.add(name, passed, detail)

    def extend(self, other: VerificationReport) -> None:
        self.results.extend(other.results)


def load_ignored_refs(path: Path) -> frozenset[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(
        stripped
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


def verify_install_report(report: dict[str, Any], spec: BundleSpec = BUNDLE_SPEC) -> VerificationReport:
    result = VerificationReport()
    installed = report.get("installed", [])
    updated = report.get("updated", [])
    skipped = report.get("skipped", [])
    refused = report.get("refused", [])
    attempted = [*installed, *updated]

    present_ids = {entry["id"] for entry in attempted}
    result.check(
        "install-report:bundle-installed",
        spec.ids - present_ids,
        "every Skill Bundle member is installed or updated",
        "missing from installed/updated",
    )

    idle = {entry.get("id", "<unknown>") for entry in [*skipped, *refused]}
    result.check(
        "install-report:no-skipped-or-refused",
        idle,
        "skipped and refused are both empty",
        "non-empty skipped/refused",
    )

    bad_commit = {
        entry["id"] for entry in attempted if entry.get("resolvedCommit") != spec.resolved_commit
    }
    result.check(
        "install-report:resolved-commit-pinned",
        bad_commit,
        f"every record resolved to {spec.resolved_commit}",
        "resolvedCommit mismatch for",
    )

    bad_source = {
        entry["id"] for entry in attempted if entry.get("sourceIdentity") != spec.source_identity
    }
    result.check(
        "install-report:source-identity-pinned",
        bad_source,
        f"every record's sourceIdentity is {spec.source_identity}",
        "sourceIdentity mismatch for",
    )
    return result


def verify_list_report(report: dict[str, Any], spec: BundleSpec = BUNDLE_SPEC) -> VerificationReport:
    result = VerificationReport()
    present_ids = {artifact["id"] for artifact in report.get("artifacts", [])}
    extra = present_ids - spec.ids
    missing = spec.ids - present_ids

    details = []
    if extra:
        details.append(f"unexpected managed artifacts: {', '.join(sorted(extra))}")
    if missing:
        details.append(f"Skill Bundle members missing from the managed set: {', '.join(sorted(missing))}")
    result.add(
        "list-report:exact-bundle",
        not extra and not missing,
        "managed artifacts are exactly the Skill Bundle" if not details else "; ".join(details),
    )
    return result


def _find_bad_refs(
    skills_dir: Path, known_names: Iterable[str], ignored_refs: frozenset[str]
) -> set[str]:
    known = frozenset(known_names)
    bad_refs: set[str] = set()
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        for match in SKILL_REF_RE.finditer(text):
            ref_name = match.group(0)[1:]
            if ref_name not in known and ref_name not in ignored_refs:
                bad_refs.add(f"{skill_md.parent.name}/SKILL.md: /{ref_name}")
    return bad_refs


def verify_closure(
    home: Path,
    ignored_refs: frozenset[str],
    spec: BundleSpec = BUNDLE_SPEC,
) -> VerificationReport:
    result = VerificationReport()
    skills_dir = home / ".agents" / "skills"
    exposure_dir = home / ".claude" / "skills"
    names = spec.names

    missing_companions = {
        f"{name}/{relative}"
        for name, relatives in spec.companion_files.items()
        for relative in relatives
        if not (skills_dir / name / relative).is_file()
    }
    result.check(
        "closure:companion-files-present",
        missing_companions,
        "all companion files present",
        "missing companion files",
    )

    broken_symlinks = set()
    for name in names:
        link = exposure_dir / name
        expected_target = skills_dir / name
        if not link.is_symlink():
            broken_symlinks.add(f"{name} (not a symlink)")
        elif link.resolve() != expected_target.resolve():
            broken_symlinks.add(f"{name} (resolves to {link.resolve()})")
    result.check(
        "closure:exposure-symlinks-resolve",
        broken_symlinks,
        "every exposure symlink resolves to the base store",
        "broken symlinks",
    )

    result.check(
        "closure:skill-references-in-bundle-or-ignored",
        _find_bad_refs(skills_dir, names, ignored_refs),
        "every /<skill> reference is in the Skill Bundle or ignored",
        "unreviewed references",
    )
    return result


def verify_bundle(
    install_report: dict[str, Any],
    list_report: dict[str, Any],
    home: Path,
    ignored_refs: frozenset[str],
    spec: BundleSpec = BUNDLE_SPEC,
) -> VerificationReport:
    report = VerificationReport()
    report.extend(verify_install_report(install_report, spec))
    report.extend(verify_list_report(list_report, spec))
    report.extend(verify_closure(home, ignored_refs, spec))
    return report
