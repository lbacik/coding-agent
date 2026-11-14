"""Install-mode preparation through the installer-adapter port (issue #394).

Contract: ``prepare-skill-bundle --mode install`` resolves each source
group's symbolic ref once, installs the declared subset at the immutable
commit through the installer-adapter port, positively re-verifies every
member from the store, records resolutions for later offline adoption, and
never echoes credential-bearing Git arguments. Network access happens only
in this mode. Exercised here against the fake adapter; the real installer
is bound later.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coding_agent.skillbundle.deployment import (
    DeploymentFault,
    _validate_install_report_for_group,
    prepare_skill_bundle,
    verify_skill_bundle_deployment,
)
from coding_agent.skillbundle.installer import (
    REAL_INSTALLER_VERSION,
    FakeInstallerAdapter,
    InstallerFault,
    SubprocessInstallerAdapter,
    build_install_argv,
    https_locator_from_identity,
)
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    member_files,
    minimal_config_text,
    skills_entries_for,
    write_policy_file,
)


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _write_config(tmp_path: Path, members: list[DeclaredMember]) -> Path:
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    return config


def _write_policy(tmp_path: Path, members: list[DeclaredMember]) -> Path:
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    return policy


def _seed_files(fake: FakeInstallerAdapter, members: list[DeclaredMember]) -> None:
    for member in members:
        fake.seed_member(member.source, member.name, member_files(member))


def test_install_symbolic_ref_resolved_once_per_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main"),
        DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_A, ref="main"),
    ]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    fake.seed_resolution(SOURCE_A, "main", COMMIT_A)
    _seed_files(fake, members)
    prepared = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    assert fake.resolve_calls == [(SOURCE_A, "main")]
    assert len(fake.install_calls) == 1
    call = fake.install_calls[0]
    assert call["commit"] == COMMIT_A
    assert call["selectors"] == ("skill:alpha", "skill:beta")
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [a["name"] for a in manifest["artifacts"]] == ["alpha", "beta"]
    for artifact in manifest["artifacts"]:
        assert artifact["resolvedCommit"] == COMMIT_A
        assert artifact["verification"] == "source"
    assert manifest["resolutions"] == [
        {"ref": "main", "resolvedCommit": COMMIT_A, "sourceIdentity": SOURCE_A}
    ]


def test_install_unconstrained_groups_resolve_head_once_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(name="beta", source=SOURCE_B, commit=COMMIT_B),
    ]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    fake.seed_resolution(SOURCE_A, "HEAD", COMMIT_A)
    fake.seed_resolution(SOURCE_B, "HEAD", COMMIT_B)
    _seed_files(fake, members)
    prepared = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    assert fake.resolve_calls == [(SOURCE_A, "HEAD"), (SOURCE_B, "HEAD")]
    assert len(fake.install_calls) == 2
    assert fake.install_calls[0]["commit"] == COMMIT_A
    assert fake.install_calls[1]["commit"] == COMMIT_B
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert manifest["resolutions"] == []
    by_name = {a["name"]: a for a in manifest["artifacts"]}
    assert by_name["alpha"]["resolvedCommit"] == COMMIT_A
    assert by_name["beta"]["resolvedCommit"] == COMMIT_B


def test_install_exact_commit_ref_needs_no_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref=COMMIT_A)]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    _seed_files(fake, members)
    prepared = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    assert fake.resolve_calls == []
    assert fake.install_calls[0]["commit"] == COMMIT_A


def test_install_identical_bytes_new_commit_reuses_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    extra = {"docs/notes.md": (b"# notes\n", False)}
    first = DeclaredMember(
        name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main", extra_files=dict(extra)
    )
    policy = _write_policy(tmp_path, [first])
    config = _write_config(tmp_path, [first])

    first_home = tmp_path / "home-a"
    first_home.mkdir(exist_ok=True)
    first_fake = FakeInstallerAdapter()
    first_fake.seed_resolution(SOURCE_A, "main", COMMIT_A)
    _seed_files(first_fake, [first])
    first_bundle = tmp_path / "bundle-a"
    first_prepared = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=first_bundle,
        home=first_home,
        mode="install",
        installer=first_fake,
    )
    assert first_prepared.ok, [str(f) for f in first_prepared.faults]

    # Identical bytes at the newly requested commit reuse the same approval
    # but record the new commit.
    second_home = tmp_path / "home-b"
    second_home.mkdir(exist_ok=True)
    second_fake = FakeInstallerAdapter()
    second_fake.seed_resolution(SOURCE_A, "main", COMMIT_B)
    _seed_files(second_fake, [first])
    second_bundle = tmp_path / "bundle-b"
    second_prepared = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=second_bundle,
        home=second_home,
        mode="install",
        installer=second_fake,
    )
    assert second_prepared.ok, [str(f) for f in second_prepared.faults]
    manifest = json.loads((second_bundle / "manifest.json").read_bytes())
    assert manifest["artifacts"][0]["resolvedCommit"] == COMMIT_B
    assert manifest["resolutions"] == [
        {"ref": "main", "resolvedCommit": COMMIT_B, "sourceIdentity": SOURCE_A}
    ]


def test_install_stale_provenance_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_B, ref="main")]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    fake.seed_resolution(SOURCE_A, "main", COMMIT_B)
    fake.seed_resolution(SOURCE_A, "HEAD", COMMIT_A)
    _seed_files(fake, members)
    fake.report_mode = "stale"
    refused = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any("immutable commit" in str(fault) for fault in refused.faults)


def test_install_empty_report_refuses_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main")]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    fake.seed_resolution(SOURCE_A, "main", COMMIT_A)
    _seed_files(fake, members)
    fake.report_mode = "empty"
    refused = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any("no installed or updated members" in str(fault) for fault in refused.faults)
    assert any("no new successful generation" in str(fault) for fault in refused.faults)


def test_install_non_success_buckets_refuse_and_disclose_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    for report_mode in ("partial", "skipped", "refused", "failed"):
        case_dir = tmp_path / report_mode
        members = [
            DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main"),
            DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_A, ref="main"),
        ]
        config = case_dir / "workflow.yml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
        policy = case_dir / "skill-policy.json"
        write_policy_file(policy, build_policy_document(members))
        bundle_dir = case_dir / "bundle"
        home = case_dir / "home"
        home.mkdir(exist_ok=True)
        fake = FakeInstallerAdapter()
        fake.seed_resolution(SOURCE_A, "main", COMMIT_A)
        _seed_files(fake, members)
        fake.report_mode = report_mode
        refused = prepare_skill_bundle(
            config=config,
            policy=policy,
            bundle_dir=bundle_dir,
            home=home,
            mode="install",
            installer=fake,
        )
        assert not refused.ok, report_mode
        assert refused.wrote == (), report_mode
        assert not (bundle_dir / "manifest.json").exists(), report_mode
        text = "\n".join(str(fault) for fault in refused.faults)
        assert report_mode in text or "missing" in text, report_mode
        assert "no new successful generation" in text, report_mode
        assert "skill:alpha" in text, report_mode


def test_install_updated_bucket_counts_as_positive_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    faults: list[DeploymentFault] = []
    report: dict[str, Any] = {
        "failed": [],
        "installed": [],
        "refused": [],
        "skipped": [],
        "updated": [
            {
                "id": "skill:alpha",
                "resolvedCommit": COMMIT_A,
                "sourceIdentity": SOURCE_A,
            }
        ],
    }
    assert _validate_install_report_for_group(
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        report=report,
        group_where="skills[0].source",
        faults=faults,
    )
    assert faults == []

    empty_faults: list[DeploymentFault] = []
    empty_report: dict[str, Any] = {
        "failed": [],
        "installed": [],
        "refused": [],
        "skipped": [],
        "updated": [],
    }
    assert not _validate_install_report_for_group(
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        report=empty_report,
        group_where="skills[0].source",
        faults=empty_faults,
    )
    assert empty_faults != []


def test_install_resolutions_adopted_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main"),
        DeclaredMember(name="beta", source=SOURCE_B, commit=COMMIT_B),
    ]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    fake.seed_resolution(SOURCE_A, "main", COMMIT_A)
    fake.seed_resolution(SOURCE_B, "HEAD", COMMIT_B)
    _seed_files(fake, members)
    installed = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert installed.ok, [str(f) for f in installed.faults]
    saved = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert saved["resolutions"] == [
        {"ref": "main", "resolvedCommit": COMMIT_A, "sourceIdentity": SOURCE_A}
    ]

    offline_bundle = tmp_path / "offline-bundle"
    adopted = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=offline_bundle,
        home=home,
        mode="installed",
        resolution_manifest=bundle_dir / "manifest.json",
    )
    assert adopted.ok, [str(f) for f in adopted.faults]
    offline_manifest = json.loads((offline_bundle / "manifest.json").read_bytes())
    assert offline_manifest["resolutions"] == saved["resolutions"]
    assert [a["name"] for a in offline_manifest["artifacts"]] == ["alpha", "beta"]
    assert verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=offline_bundle, home=home
    ).ok


def test_install_unresolvable_ref_refuses_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main")]
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = FakeInstallerAdapter()
    _seed_files(fake, members)
    refused = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any("cannot resolve ref" in str(fault) for fault in refused.faults)


def test_build_install_argv_passes_one_argument_per_selector() -> None:
    locator = "https://example.com/org/skills-a.git"
    assert build_install_argv(locator, COMMIT_A, ("skill:alpha", "skill:beta")) == [
        "agent-installer",
        "install",
        locator,
        "--ref",
        COMMIT_A,
        "--only",
        "skill:alpha",
        "--only",
        "skill:beta",
        "--json",
        "--json-schema",
        "3",
    ]
    # Hostile selector content stays a single argument: no shell to interpret it.
    hostile = build_install_argv(locator, COMMIT_A, ("skill:a b;rm -rf",))
    assert hostile.count("--only") == 1
    assert "skill:a b;rm -rf" in hostile
    assert len(hostile) == 10


def test_https_locator_strips_transport_marker() -> None:
    assert https_locator_from_identity(SOURCE_A) == "https://example.com/org/skills-a.git"


def test_subprocess_adapter_resolves_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=f"{COMMIT_A}\trefs/heads/main\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SubprocessInstallerAdapter().resolve_ref(SOURCE_A, "main") == COMMIT_A
    assert captured["argv"] == [
        "git",
        "ls-remote",
        "https://example.com/org/skills-a.git",
        "main",
    ]
    assert captured["kwargs"]["shell"] is False


def _v3_install_report(
    member: str = "alpha",
    source: str = SOURCE_A,
    commit: str = COMMIT_A,
    disposition: str = "installed",
    status: str = "new",
) -> dict[str, Any]:
    """A released-installer v3 install report for one requested member."""
    return {
        "artifacts": [
            {
                "basePath": f"/tmp/home/.agents/skills/{member}",
                "content": {
                    "algorithm": "agent-installer-tree-v2",
                    "expectedHash": "0" * 64,
                    "installedHash": "0" * 64,
                },
                "disposition": disposition,
                "exposures": [],
                "files": [],
                "id": f"skill:{member}",
                "kind": "skill",
                "name": member,
                "overlays": [],
                "relativeSourcePath": f"skills/{member}",
                "requestedRef": commit,
                "resolvedCommit": commit,
                "sourceIdentity": source,
                "status": status,
                "storeRelativePath": member,
            }
        ],
        "baseStore": {
            "prompts": "/tmp/home/.agents/prompts",
            "skills": "/tmp/home/.agents/skills",
        },
        "exposureOutcome": "not-configured",
        "kind": "agent-installer.install-report",
        "outcome": "complete",
        "pruned": [],
        "schemaVersion": 3,
        "unmatchedSelectors": [],
    }


def test_subprocess_adapter_installs_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    report = _v3_install_report()
    captured: dict[str, Any] = {}
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{REAL_INSTALLER_VERSION}\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps(report), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    locator = "https://example.com/org/skills-a.git"
    returned = SubprocessInstallerAdapter().install_group(
        source_identity=SOURCE_A,
        https_locator=locator,
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        home=tmp_path,
    )
    assert returned == report
    assert captured["argv"] == build_install_argv(locator, COMMIT_A, ("skill:alpha",))
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"]["HOME"] == str(tmp_path)
    assert calls[0] == ["agent-installer", "--version"]


def test_subprocess_adapter_refuses_unpinned_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.7.2\n", stderr=""
            )
        raise AssertionError(f"installer must not run: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(InstallerFault) as excinfo:
        SubprocessInstallerAdapter().install_group(
            source_identity=SOURCE_A,
            https_locator="https://example.com/org/skills-a.git",
            resolved_commit=COMMIT_A,
            selectors=("skill:alpha",),
            home=tmp_path,
        )
    assert REAL_INSTALLER_VERSION in str(excinfo.value)
    assert "0.7.2" not in str(excinfo.value)


def test_subprocess_diagnostics_never_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_source = "git+https://user:secret-pass@example.com/org/repo.git"

    def failing_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(InstallerFault) as excinfo:
        SubprocessInstallerAdapter().resolve_ref(secret_source, "main")
    assert "secret-pass" not in str(excinfo.value)


def test_install_credential_source_never_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config = tmp_path / "workflow.yml"
    config.write_text(
        minimal_config_text(
            [{"source": "https://user:secret-pass@example.com/org/repo.git", "include": ["alpha"]}]
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    refused = prepare_skill_bundle(
        config=config,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "secret-pass" not in "\n".join(str(fault) for fault in refused.faults)
