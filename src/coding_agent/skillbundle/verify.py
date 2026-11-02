from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
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


ROLE_BLOCK_SKILL = "code-review"
"""The Skill Bundle member whose internal structure the adapter depends on."""

ROLE_BLOCK_ANCHORS: tuple[str, ...] = (
    "### 3. Identify the standards sources",
    "### 4. Spawn both sub-agents in parallel",
    "**Standards sub-agent prompt** should include:",
    "**Spec sub-agent prompt** should include:",
    "### 5. Aggregate",
)
"""Structural anchors (ADR 0007): headings and bold labels, never prose."""

STANDARDS_SLICE_DIGEST = (
    "f7a58fa48ddacc3d9ec9c42444bd3b4392320426e06dcbbdca2c879f4e61013c"
)
"""SHA-256 of the Standards Role Block: the §3 slice plus the Standards brief."""

SPEC_SLICE_DIGEST = (
    "47861a4354c41dc74f3bdc8930916289524efca056751bda4d3cd88f9d0bd509"
)
"""SHA-256 of the Spec Role Block: the Spec brief slice up to §5."""

COMPANION_DIGESTS: dict[str, str] = {
    "tdd/tests.md": "859f9e592c188fda4fc7277dd180e4ce9c7a2e13f6efe1f6f29eccc9d28c106a",
    "tdd/mocking.md": "3ceb807fdf4a47d6a93d4d9a891e5ba6d362a6247bd08adc451feebfc17361ef",
    "codebase-design/DEEPENING.md": (
        "f3dd099ce99289bd213914d8ee3e2429b78309c3957ca4583f7659551b1d53c1"
    ),
    "codebase-design/DESIGN-IT-TWICE.md": (
        "8e740bf98446dbd4dfdc132ac4346d9a7eedaf93de6a495889171cf7f99f16bd"
    ),
}
"""SHA-256 of each companion file's bytes (ADR 0012).

Covers only the four companion files: injection is verbatim and whole, so a
reworded ``SKILL.md`` flows through on its own; it is the decision to inject
or refuse that has an expiry date, not the text.
"""


@dataclass(frozen=True)
class RoleBlockSpec:
    """The expected shape of the ``code-review`` Role Blocks (ADR 0007)."""

    skill: str
    anchors: tuple[str, ...]
    standards_digest: str
    spec_digest: str


DEFAULT_ROLE_BLOCK_SPEC = RoleBlockSpec(
    skill=ROLE_BLOCK_SKILL,
    anchors=ROLE_BLOCK_ANCHORS,
    standards_digest=STANDARDS_SLICE_DIGEST,
    spec_digest=SPEC_SLICE_DIGEST,
)


def _slice_role_blocks(text: str, anchors: tuple[str, ...]) -> tuple[str, str]:
    """Extract the (standards, spec) slices from ``text`` by anchor offsets.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the build fails on
    a moved Role Block). MVP row id: L2-19.

    The Standards block is the §3 slice (anchor 0 up to anchor 1) concatenated
    with the Standards brief (anchor 2 up to anchor 3); the Spec block is the
    Spec brief (anchor 3 up to anchor 4). Raises ``ValueError`` naming the
    anchor when one is absent.
    """
    positions: list[int] = []
    for anchor in anchors:
        position = text.find(anchor)
        if position < 0:
            raise ValueError(f"role-block anchor absent: {anchor!r}")
        positions.append(position)
    standards = text[positions[0] : positions[1]] + text[positions[2] : positions[3]]
    spec = text[positions[3] : positions[4]]
    return standards, spec


def verify_role_blocks(
    skills_dir: Path,
    role_spec: RoleBlockSpec = DEFAULT_ROLE_BLOCK_SPEC,
) -> VerificationReport:
    """Verify the ``code-review`` Role Blocks (ADR 0007, L2-19).

    Fails naming what was wrong when any anchor occurs other than exactly
    once, or when either slice digest has moved.
    """
    result = VerificationReport()
    skill_md = skills_dir / role_spec.skill / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        result.add(
            "role-block:anchors-exactly-once",
            False,
            f"cannot read {role_spec.skill}/SKILL.md: {exc}",
        )
        result.add(
            "role-block:slice-digests",
            False,
            f"cannot read {role_spec.skill}/SKILL.md: {exc}",
        )
        return result
    bad = sorted(
        f"{anchor!r} occurs {text.count(anchor)} times"
        for anchor in role_spec.anchors
        if text.count(anchor) != 1
    )
    result.check(
        "role-block:anchors-exactly-once",
        bad,
        "every role-block anchor occurs exactly once",
        "role-block anchors occurring other than exactly once",
    )
    try:
        standards, spec = _slice_role_blocks(text, role_spec.anchors)
    except ValueError as exc:
        result.add("role-block:slice-digests", False, str(exc))
        return result
    mismatched: list[str] = []
    if hashlib.sha256(standards.encode("utf-8")).hexdigest() != role_spec.standards_digest:
        mismatched.append(f"{role_spec.skill} Standards block digest moved")
    if hashlib.sha256(spec.encode("utf-8")).hexdigest() != role_spec.spec_digest:
        mismatched.append(f"{role_spec.skill} Spec block digest moved")
    result.check(
        "role-block:slice-digests",
        mismatched,
        "role-block slices match their committed digests",
        "role-block digest mismatch for",
    )
    return result


def verify_companion_exactness(
    skills_dir: Path,
    spec: BundleSpec = BUNDLE_SPEC,
    digests: Mapping[str, str] = COMPANION_DIGESTS,
) -> VerificationReport:
    """Verify the companion set is exactly the manifest with matching digests.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the build fails
    on an unexpected, missing or changed companion). MVP row id: L2-19.

    Fails naming what was wrong when a file under a skill's directory is not
    listed in the manifest (unlisted), or when a listed companion's content
    digest has moved. A listed companion that is absent is reported here too,
    alongside the ``closure:companion-files-present`` check.
    """
    result = VerificationReport()
    unlisted: list[str] = []
    for name in sorted(spec.names):
        skill_dir = skills_dir / name
        if not skill_dir.is_dir():
            continue
        allowed = set(spec.companion_files.get(name, ()))
        for child in sorted(skill_dir.iterdir(), key=lambda p: p.name):
            if not child.is_file() or child.name == "SKILL.md" or child.name.startswith("."):
                continue
            if child.name not in allowed:
                unlisted.append(f"{name}/{child.name}")
    result.check(
        "closure:companion-files-exact",
        unlisted,
        "no companion files beyond the manifest",
        "companion files not listed in the manifest",
    )
    moved: list[str] = []
    missing: list[str] = []
    for skill, relatives in sorted(spec.companion_files.items()):
        for relative in relatives:
            key = f"{skill}/{relative}"
            path = skills_dir / skill / relative
            if not path.is_file():
                missing.append(key)
                continue
            expected = digests.get(key)
            if expected is None:
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                moved.append(f"{key} digest moved (got {actual})")
    bad = sorted(missing + moved)
    detail_bad = ", ".join(bad)
    result.add(
        "closure:companion-digests-match",
        not bad,
        "every listed companion matches its committed digest"
        if not bad
        else f"companion digest mismatch or missing: {detail_bad}",
    )
    return result


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
    role_spec: RoleBlockSpec = DEFAULT_ROLE_BLOCK_SPEC,
    companion_digests: Mapping[str, str] = COMPANION_DIGESTS,
) -> VerificationReport:
    """Verify the installed bundle closes over itself (L2-19).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the build
    fails, naming what was wrong). MVP row id: L2-19.
    """
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
    result.extend(verify_role_blocks(skills_dir, role_spec))
    result.extend(verify_companion_exactness(skills_dir, spec, companion_digests))
    return result


def verify_bundle(
    install_report: dict[str, Any],
    list_report: dict[str, Any],
    home: Path,
    ignored_refs: frozenset[str],
    spec: BundleSpec = BUNDLE_SPEC,
    role_spec: RoleBlockSpec = DEFAULT_ROLE_BLOCK_SPEC,
    companion_digests: Mapping[str, str] = COMPANION_DIGESTS,
) -> VerificationReport:
    """Verify the full Skill Bundle: install, list and closure (L2-19).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the build
    fails, naming what was wrong). MVP row id: L2-19.
    """
    report = VerificationReport()
    report.extend(verify_install_report(install_report, spec))
    report.extend(verify_list_report(list_report, spec))
    report.extend(verify_closure(home, ignored_refs, spec, role_spec, companion_digests))
    return report
