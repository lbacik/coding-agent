"""Skill Bundle verification, including the L2-19 build gate.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the build fails,
naming what was wrong). MVP row id: L2-19.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from coding_agent.skillbundle.verify import (
    BUNDLE_SPEC,
    DEFAULT_ROLE_BLOCK_SPEC,
    BundleSpec,
    RoleBlockSpec,
    _slice_role_blocks,
    load_ignored_refs,
    verify_bundle,
    verify_closure,
    verify_companion_exactness,
    verify_install_report,
    verify_list_report,
    verify_role_blocks,
)

SOURCE = BUNDLE_SPEC.source_identity
COMMIT = BUNDLE_SPEC.resolved_commit
BUNDLE_IDS = BUNDLE_SPEC.ids

SYNTHETIC_CODE_REVIEW_TEXT = """# code-review

### 3. Identify the standards sources

synthetic standards sources

### 4. Spawn both sub-agents in parallel

**Standards sub-agent prompt** should include:

synthetic standards brief

**Spec sub-agent prompt** should include:

synthetic spec brief

### 5. Aggregate

synthetic aggregate
"""

SYNTHETIC_COMPANION_TEXT: dict[str, str] = {
    f"{skill}/{relative}": f"synthetic {skill}/{relative}\n"
    for skill, relatives in BUNDLE_SPEC.companion_files.items()
    for relative in relatives
}


def _synthetic_role_spec(text: str = SYNTHETIC_CODE_REVIEW_TEXT) -> RoleBlockSpec:
    """A role spec whose digests match the synthetic bundle text."""
    standards, spec = _slice_role_blocks(text, DEFAULT_ROLE_BLOCK_SPEC.anchors)
    return RoleBlockSpec(
        skill=DEFAULT_ROLE_BLOCK_SPEC.skill,
        anchors=DEFAULT_ROLE_BLOCK_SPEC.anchors,
        standards_digest=hashlib.sha256(standards.encode("utf-8")).hexdigest(),
        spec_digest=hashlib.sha256(spec.encode("utf-8")).hexdigest(),
    )


def _synthetic_companion_digests(
    companion_text: dict[str, str] | None = None,
) -> dict[str, str]:
    """Digests matching the synthetic companion contents."""
    text = SYNTHETIC_COMPANION_TEXT if companion_text is None else companion_text
    return {
        key: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for key, content in text.items()
    }


def _entry(artifact_id: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": artifact_id,
        "kind": "skill",
        "name": artifact_id.partition(":")[2],
        "status": "installed",
        "sourceIdentity": SOURCE,
        "resolvedCommit": COMMIT,
    }
    entry.update(overrides)
    return entry


def _install_report(**overrides: object) -> dict[str, object]:
    report = {
        "schemaVersion": 1,
        "installed": [_entry(artifact_id) for artifact_id in sorted(BUNDLE_IDS)],
        "updated": [],
        "skipped": [],
        "refused": [],
        "pruned": [],
    }
    report.update(overrides)
    return report


def _list_report(ids: frozenset[str] = BUNDLE_IDS) -> dict[str, object]:
    return {"schemaVersion": 1, "artifacts": [_entry(artifact_id) for artifact_id in sorted(ids)]}


def _write_bundle_home(
    home: Path,
    spec: BundleSpec = BUNDLE_SPEC,
    skill_md_text: dict[str, str] | None = None,
    companion_text: dict[str, str] | None = None,
) -> None:
    skills_dir = home / ".agents" / "skills"
    exposure_dir = home / ".claude" / "skills"
    skill_md_text = skill_md_text or {}
    companions = SYNTHETIC_COMPANION_TEXT if companion_text is None else companion_text
    for artifact_id in spec.ids:
        name = artifact_id.partition(":")[2]
        base = skills_dir / name
        base.mkdir(parents=True, exist_ok=True)
        if name in skill_md_text:
            (base / "SKILL.md").write_text(skill_md_text[name], encoding="utf-8")
        elif name == DEFAULT_ROLE_BLOCK_SPEC.skill:
            (base / "SKILL.md").write_text(SYNTHETIC_CODE_REVIEW_TEXT, encoding="utf-8")
        else:
            (base / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        for relative in spec.companion_files.get(name, ()):
            key = f"{name}/{relative}"
            (base / relative).write_text(companions.get(key, ""), encoding="utf-8")
        exposure_dir.mkdir(parents=True, exist_ok=True)
        link = exposure_dir / name
        if not link.is_symlink():
            os.symlink(base, link)


class TestVerifyInstallReport:
    def test_happy_path_passes(self) -> None:
        report = verify_install_report(_install_report())
        assert report.ok

    def test_bundle_member_missing_from_installed_or_updated(self) -> None:
        report = _install_report(
            installed=[_entry(i) for i in sorted(BUNDLE_IDS) if i != "skill:tdd"]
        )
        result = verify_install_report(report)
        assert not result.ok
        check = next(r for r in result.results if r.name == "install-report:bundle-installed")
        assert not check.passed
        assert "skill:tdd" in check.detail

    def test_non_empty_skipped_or_refused_fails(self) -> None:
        report = _install_report(refused=[_entry("skill:codebase-design", status="refused")])
        result = verify_install_report(report)
        check = next(r for r in result.results if r.name == "install-report:no-skipped-or-refused")
        assert not check.passed
        assert "skill:codebase-design" in check.detail

    def test_resolved_commit_other_than_pin_fails(self) -> None:
        report = _install_report(
            installed=[
                _entry(i, resolvedCommit="deadbeef" if i == "skill:implement" else COMMIT)
                for i in sorted(BUNDLE_IDS)
            ]
        )
        result = verify_install_report(report)
        check = next(r for r in result.results if r.name == "install-report:resolved-commit-pinned")
        assert not check.passed
        assert "skill:implement" in check.detail

    def test_source_identity_other_than_expected_fails(self) -> None:
        report = _install_report(
            installed=[
                _entry(i, sourceIdentity="git+https://github.com/someone-else/skills.git")
                if i == "skill:tdd"
                else _entry(i)
                for i in sorted(BUNDLE_IDS)
            ]
        )
        result = verify_install_report(report)
        check = next(r for r in result.results if r.name == "install-report:source-identity-pinned")
        assert not check.passed
        assert "skill:tdd" in check.detail


class TestVerifyListReport:
    def test_happy_path_passes(self) -> None:
        assert verify_list_report(_list_report()).ok

    def test_managed_artifact_outside_bundle_fails(self) -> None:
        report = _list_report(BUNDLE_IDS | {"skill:setup-matt-pocock-skills"})
        result = verify_list_report(report)
        check = result.results[0]
        assert not check.passed
        assert "skill:setup-matt-pocock-skills" in check.detail

    def test_bundle_shrinkage_fails(self) -> None:
        report = _list_report(frozenset(i for i in BUNDLE_IDS if i != "skill:code-review"))
        result = verify_list_report(report)
        check = result.results[0]
        assert not check.passed
        assert "skill:code-review" in check.detail


class TestVerifyClosure:
    def test_happy_path_passes(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        result = verify_closure(
            tmp_path,
            ignored_refs=frozenset(),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        assert result.ok

    def test_missing_companion_file_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        (tmp_path / ".agents" / "skills" / "tdd" / "mocking.md").unlink()
        result = verify_closure(
            tmp_path,
            ignored_refs=frozenset(),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        check = next(r for r in result.results if r.name == "closure:companion-files-present")
        assert not check.passed
        assert "tdd/mocking.md" in check.detail

    def test_unreviewed_skill_reference_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(
            tmp_path,
            skill_md_text={"implement": "See `/setup-matt-pocock-skills` for onboarding.\n"},
        )
        result = verify_closure(
            tmp_path,
            ignored_refs=frozenset(),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        check = next(
            r for r in result.results if r.name == "closure:skill-references-in-bundle-or-ignored"
        )
        assert not check.passed
        assert "/setup-matt-pocock-skills" in check.detail

    def test_bundle_and_ignored_references_pass(self, tmp_path: Path) -> None:
        _write_bundle_home(
            tmp_path,
            skill_md_text={
                "implement": "Use /tdd where possible. See /setup-matt-pocock-skills too.\n"
            },
        )
        result = verify_closure(
            tmp_path,
            ignored_refs=frozenset({"setup-matt-pocock-skills"}),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        assert result.ok

    def test_broken_exposure_symlink_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        link = tmp_path / ".claude" / "skills" / "tdd"
        link.unlink()
        link.symlink_to(tmp_path / ".agents" / "skills" / "implement")
        result = verify_closure(
            tmp_path,
            ignored_refs=frozenset(),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        check = next(r for r in result.results if r.name == "closure:exposure-symlinks-resolve")
        assert not check.passed
        assert "tdd" in check.detail


class TestVerifyBundle:
    def test_happy_path_passes(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        report = verify_bundle(
            _install_report(),
            _list_report(),
            tmp_path,
            frozenset(),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        assert report.ok

    def test_aggregates_failures_from_all_three_layers(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        (tmp_path / ".agents" / "skills" / "codebase-design" / "DEEPENING.md").unlink()
        broken_install = _install_report(refused=[_entry("skill:tdd", status="refused")])
        report = verify_bundle(
            broken_install,
            _list_report(),
            tmp_path,
            frozenset(),
            role_spec=_synthetic_role_spec(),
            companion_digests=_synthetic_companion_digests(),
        )
        assert not report.ok
        names = {r.name for r in report.results if not r.passed}
        assert "install-report:no-skipped-or-refused" in names
        assert "closure:companion-files-present" in names


class TestLoadIgnoredRefs:
    def test_ignores_blank_lines_and_comments(self, tmp_path: Path) -> None:
        ignore_file = tmp_path / "ignore.txt"
        ignore_file.write_text(
            "\n".join(
                [
                    "# reviewed false positives",
                    "",
                    "hunk",
                    "  spec  ",
                    "# another comment",
                    "setup-matt-pocock-skills",
                ]
            ),
            encoding="utf-8",
        )
        assert load_ignored_refs(ignore_file) == frozenset(
            {"hunk", "spec", "setup-matt-pocock-skills"}
        )


@pytest.mark.parametrize("skill_id", sorted(BUNDLE_IDS))
def test_bundle_spec_names_strip_kind_prefix(skill_id: str) -> None:
    assert BUNDLE_SPEC.names == {"implement", "tdd", "code-review", "codebase-design"}


class TestVerifyL219:
    """L2-19: the build fails on all eleven failure modes, naming what was wrong.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3. MVP row id: L2-19.

    The six S1 modes (missing member, skipped/refused, moved commit, outside
    artifact, missing companion, unreviewed reference), plus a Role Block
    anchor occurring other than exactly once, plus a Role Block digest
    mismatch, plus an unlisted, missing or moved-digest Companion File.
    """

    def _ok_bundle(
        self, tmp_path: Path
    ) -> tuple[dict[str, object], dict[str, object], RoleBlockSpec, dict[str, str]]:
        _write_bundle_home(tmp_path)
        return (
            _install_report(),
            _list_report(),
            _synthetic_role_spec(),
            _synthetic_companion_digests(),
        )

    def _check(
        self, tmp_path: Path, name: str, **overrides: object
    ) -> tuple[bool, str]:
        install, listing, role_spec, digests = self._ok_bundle(tmp_path)
        if "install" in overrides:
            install = overrides["install"]  # type: ignore[assignment]
        if "listing" in overrides:
            listing = overrides["listing"]  # type: ignore[assignment]
        if "role_spec" in overrides:
            role_spec = overrides["role_spec"]  # type: ignore[assignment]
        if "digests" in overrides:
            digests = overrides["digests"]  # type: ignore[assignment]
        mutate = overrides.get("mutate")
        if callable(mutate):
            mutate(tmp_path)
        report = verify_bundle(
            install,
            listing,
            tmp_path,
            frozenset(),
            role_spec=role_spec,
            companion_digests=digests,
        )
        check = next(r for r in report.results if r.name == name)
        return check.passed, check.detail

    def test_01_bundle_member_missing_from_installed_fails(self, tmp_path: Path) -> None:
        install = _install_report(
            installed=[_entry(i) for i in sorted(BUNDLE_IDS) if i != "skill:tdd"]
        )
        passed, detail = self._check(tmp_path, "install-report:bundle-installed", install=install)
        assert not passed
        assert "skill:tdd" in detail

    def test_02_skipped_or_refused_fails(self, tmp_path: Path) -> None:
        install = _install_report(refused=[_entry("skill:tdd", status="refused")])
        passed, detail = self._check(
            tmp_path, "install-report:no-skipped-or-refused", install=install
        )
        assert not passed
        assert "skill:tdd" in detail

    def test_03_resolved_commit_mismatch_fails(self, tmp_path: Path) -> None:
        install = _install_report(
            installed=[
                _entry(i, resolvedCommit="deadbeef" if i == "skill:implement" else COMMIT)
                for i in sorted(BUNDLE_IDS)
            ]
        )
        passed, detail = self._check(
            tmp_path, "install-report:resolved-commit-pinned", install=install
        )
        assert not passed
        assert "skill:implement" in detail

    def test_04_managed_artifact_outside_bundle_fails(self, tmp_path: Path) -> None:
        listing = _list_report(BUNDLE_IDS | {"skill:setup-matt-pocock-skills"})
        passed, detail = self._check(tmp_path, "list-report:exact-bundle", listing=listing)
        assert not passed
        assert "skill:setup-matt-pocock-skills" in detail

    def test_05_missing_companion_fails(self, tmp_path: Path) -> None:
        def mutate(home: Path) -> None:
            (home / ".agents" / "skills" / "tdd" / "mocking.md").unlink()

        passed, detail = self._check(
            tmp_path, "closure:companion-files-present", mutate=mutate
        )
        assert not passed
        assert "tdd/mocking.md" in detail

    def test_06_unreviewed_reference_fails(self, tmp_path: Path) -> None:
        def mutate(home: Path) -> None:
            path = home / ".agents" / "skills" / "implement" / "SKILL.md"
            path.write_text("See `/setup-matt-pocock-skills`.\n", encoding="utf-8")

        passed, detail = self._check(
            tmp_path, "closure:skill-references-in-bundle-or-ignored", mutate=mutate
        )
        assert not passed
        assert "/setup-matt-pocock-skills" in detail

    def test_07_role_block_anchor_absent_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        skills_dir = tmp_path / ".agents" / "skills"
        text = (skills_dir / "code-review" / "SKILL.md").read_text(encoding="utf-8")
        text = text.replace("### 5. Aggregate\n", "")
        (skills_dir / "code-review" / "SKILL.md").write_text(text, encoding="utf-8")
        result = verify_role_blocks(skills_dir, _synthetic_role_spec())
        check = next(r for r in result.results if r.name == "role-block:anchors-exactly-once")
        assert not check.passed
        assert "### 5. Aggregate" in check.detail

    def test_08_role_block_anchor_duplicate_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        skills_dir = tmp_path / ".agents" / "skills"
        path = skills_dir / "code-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text + "\n### 5. Aggregate\n", encoding="utf-8")
        result = verify_role_blocks(skills_dir, _synthetic_role_spec())
        check = next(r for r in result.results if r.name == "role-block:anchors-exactly-once")
        assert not check.passed
        assert "### 5. Aggregate" in check.detail

    def test_09_role_block_digest_mismatch_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        skills_dir = tmp_path / ".agents" / "skills"
        path = skills_dir / "code-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("synthetic spec brief", "reworded spec brief"), encoding="utf-8")
        result = verify_role_blocks(skills_dir, _synthetic_role_spec())
        check = next(r for r in result.results if r.name == "role-block:slice-digests")
        assert not check.passed
        assert "Spec" in check.detail

    def test_10_unlisted_companion_fails(self, tmp_path: Path) -> None:
        def mutate(home: Path) -> None:
            (home / ".agents" / "skills" / "tdd" / "extra.md").write_text("extra\n", encoding="utf-8")

        passed, detail = self._check(
            tmp_path, "closure:companion-files-exact", mutate=mutate
        )
        assert not passed
        assert "tdd/extra.md" in detail

    def test_11_companion_digest_moved_fails(self, tmp_path: Path) -> None:
        def mutate(home: Path) -> None:
            (home / ".agents" / "skills" / "tdd" / "tests.md").write_text(
                "changed\n", encoding="utf-8"
            )

        passed, detail = self._check(
            tmp_path, "closure:companion-digests-match", mutate=mutate
        )
        assert not passed
        assert "tdd/tests.md" in detail


def test_default_role_block_spec_matches_installed_bundle() -> None:
    """The committed Role Block digests match the installed Skill Bundle.

    Skipped where no bundle is installed (CI): the synthetic tests above
    prove the verification logic, and this proves the constants wherever a
    bundle exists to check them against.
    """
    from pathlib import Path as _Path

    skills_dir = _Path.home() / ".agents" / "skills"
    skill_md = skills_dir / DEFAULT_ROLE_BLOCK_SPEC.skill / "SKILL.md"
    if not skill_md.is_file():
        pytest.skip("no Skill Bundle installed")
    result = verify_role_blocks(skills_dir, DEFAULT_ROLE_BLOCK_SPEC)
    assert result.ok, [f"{r.name}: {r.detail}" for r in result.results if not r.passed]


def test_default_companion_digests_match_installed_bundle() -> None:
    """The committed companion digests match the installed Skill Bundle."""
    from pathlib import Path as _Path

    skills_dir = _Path.home() / ".agents" / "skills"
    if not (skills_dir / "tdd" / "tests.md").is_file():
        pytest.skip("no Skill Bundle installed")
    result = verify_companion_exactness(skills_dir)
    assert result.ok, [f"{r.name}: {r.detail}" for r in result.results if not r.passed]
