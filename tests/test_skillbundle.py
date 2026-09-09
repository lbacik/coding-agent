from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.skillbundle.verify import (
    BUNDLE_SPEC,
    BundleSpec,
    load_ignored_refs,
    verify_bundle,
    verify_closure,
    verify_install_report,
    verify_list_report,
)

SOURCE = BUNDLE_SPEC.source_identity
COMMIT = BUNDLE_SPEC.resolved_commit
BUNDLE_IDS = BUNDLE_SPEC.ids


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


def _write_bundle_home(home: Path, spec: BundleSpec = BUNDLE_SPEC, skill_md_text: dict[str, str] | None = None) -> None:
    skills_dir = home / ".agents" / "skills"
    exposure_dir = home / ".claude" / "skills"
    skill_md_text = skill_md_text or {}
    for artifact_id in spec.ids:
        name = artifact_id.partition(":")[2]
        base = skills_dir / name
        base.mkdir(parents=True)
        (base / "SKILL.md").write_text(skill_md_text.get(name, f"# {name}\n"), encoding="utf-8")
        for relative in spec.companion_files.get(name, ()):
            (base / relative).write_text("", encoding="utf-8")
        exposure_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(base, exposure_dir / name)


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
        result = verify_closure(tmp_path, ignored_refs=frozenset())
        assert result.ok

    def test_missing_companion_file_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        (tmp_path / ".agents" / "skills" / "tdd" / "mocking.md").unlink()
        result = verify_closure(tmp_path, ignored_refs=frozenset())
        check = next(r for r in result.results if r.name == "closure:companion-files-present")
        assert not check.passed
        assert "tdd/mocking.md" in check.detail

    def test_unreviewed_skill_reference_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(
            tmp_path,
            skill_md_text={"implement": "See `/setup-matt-pocock-skills` for onboarding.\n"},
        )
        result = verify_closure(tmp_path, ignored_refs=frozenset())
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
        result = verify_closure(tmp_path, ignored_refs=frozenset({"setup-matt-pocock-skills"}))
        assert result.ok

    def test_broken_exposure_symlink_fails(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        link = tmp_path / ".claude" / "skills" / "tdd"
        link.unlink()
        link.symlink_to(tmp_path / ".agents" / "skills" / "implement")
        result = verify_closure(tmp_path, ignored_refs=frozenset())
        check = next(r for r in result.results if r.name == "closure:exposure-symlinks-resolve")
        assert not check.passed
        assert "tdd" in check.detail


class TestVerifyBundle:
    def test_happy_path_passes(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        report = verify_bundle(_install_report(), _list_report(), tmp_path, frozenset())
        assert report.ok

    def test_aggregates_failures_from_all_three_layers(self, tmp_path: Path) -> None:
        _write_bundle_home(tmp_path)
        (tmp_path / ".agents" / "skills" / "codebase-design" / "DEEPENING.md").unlink()
        broken_install = _install_report(refused=[_entry("skill:tdd", status="refused")])
        report = verify_bundle(broken_install, _list_report(), tmp_path, frozenset())
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
