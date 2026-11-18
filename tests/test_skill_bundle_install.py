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
    _validate_group_install_for_group,
    prepare_skill_bundle,
    verify_skill_bundle_deployment,
)
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)


def _loaded_config(path: Path) -> LoadedWorkflowConfiguration:
    """The single-loader value for the config file at ``path``."""
    loaded, faults = load_workflow_configuration(path)
    assert loaded is not None, [str(fault) for fault in faults]
    return loaded

from coding_agent.skillbundle.installer import (
    FAKE_INSTALLER_VERSION,
    FAKE_REPORT_SCHEMA_VERSION,
    MIN_INSTALLER_VERSION,
    SKILL_TREE_ALGORITHM_V2,
    FakeInstallerAdapter,
    GroupInstall,
    InstallerFault,
    MemberInstall,
    SubprocessInstallerAdapter,
    base_store_for,
    build_install_argv,
    https_locator_from_identity,
    installer_tree_v2_for_files,
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=fake,
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any("missing" in str(fault) for fault in refused.faults)
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
            config=_loaded_config(config),
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
        assert "no new successful generation" in text, report_mode
        assert "skill:beta" in text, report_mode


def test_install_updated_disposition_counts_as_positive_verification() -> None:
    """``updated`` verifies positively; an empty group refuses the generation."""
    faults: list[DeploymentFault] = []
    group = GroupInstall(
        complete=True,
        unmatched_selectors=(),
        pruned=(),
        error=None,
        members=(
            MemberInstall(
                id="skill:alpha",
                disposition="updated",
                source_identity=SOURCE_A,
                resolved_commit=COMMIT_A,
                content_algorithm=None,
            ),
        ),
    )
    assert _validate_group_install_for_group(
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        group=group,
        group_where="skills[0].source",
        faults=faults,
    )
    assert faults == []

    empty_faults: list[DeploymentFault] = []
    empty_group = GroupInstall(
        complete=True,
        unmatched_selectors=(),
        pruned=(),
        error=None,
        members=(),
    )
    assert not _validate_group_install_for_group(
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        group=empty_group,
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
        config=_loaded_config(config),
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
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=offline_bundle,
        home=home,
        mode="installed",
        resolution_manifest=bundle_dir / "manifest.json",
        installer=FakeInstallerAdapter(),
    )
    assert adopted.ok, [str(f) for f in adopted.faults]
    offline_manifest = json.loads((offline_bundle / "manifest.json").read_bytes())
    assert offline_manifest["resolutions"] == saved["resolutions"]
    assert [a["name"] for a in offline_manifest["artifacts"]] == ["alpha", "beta"]
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=offline_bundle, home=home
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
        config=_loaded_config(config),
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
            # A version above the minimum: the v3 format checks below run
            # unchanged whatever compatible release reports itself.
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.9.0\n", stderr=""
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
    assert isinstance(returned, GroupInstall)
    assert returned.complete is True
    assert returned.unmatched_selectors == ()
    assert returned.pruned == ()
    assert returned.error is None
    assert returned.members == (
        MemberInstall(
            id="skill:alpha",
            disposition="installed",
            source_identity=SOURCE_A,
            resolved_commit=COMMIT_A,
            content_algorithm="agent-installer-tree-v2",
        ),
    )
    assert captured["argv"] == build_install_argv(locator, COMMIT_A, ("skill:alpha",))
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"]["HOME"] == str(tmp_path)
    assert calls[0] == ["agent-installer", "--version"]


@pytest.mark.parametrize("version", ["0.8.0", "0.8.1", "0.9.0", "0.10.0", "1.0.0"])
def test_subprocess_adapter_accepts_minimum_and_newer_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """The gate is a numeric minimum: the floor and newer releases pass."""
    _with_env(monkeypatch)
    report = _v3_install_report()
    version_calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            version_calls.append(list(argv))
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{version}\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps(report), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = SubprocessInstallerAdapter()
    returned = adapter.install_group(
        source_identity=SOURCE_A,
        https_locator="https://example.com/org/skills-a.git",
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        home=tmp_path,
    )
    assert isinstance(returned, GroupInstall)
    assert returned.complete is True
    assert returned.members[0].disposition == "installed"
    # The recorded producer is what the binary reported, not the minimum.
    state_dir = tmp_path / ".agents" / "agent-installer"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text('{"version": 4, "entries": []}', encoding="utf-8")
    producer = adapter.producer_for(tmp_path)
    assert producer.installer_version == version
    assert producer.report_schema_version == 3
    # The gate ran once for the install plus the producer read.
    assert version_calls == [["agent-installer", "--version"]]


@pytest.mark.parametrize("version", ["0.7.2", "0.7.9", "0.0.0"])
def test_subprocess_adapter_refuses_below_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """Older releases are refused with a diagnostic naming both versions."""
    _with_env(monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{version}\n", stderr=""
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
    assert MIN_INSTALLER_VERSION in str(excinfo.value)
    assert "below the minimum" in str(excinfo.value)
    assert "refusing to install" in str(excinfo.value)


@pytest.mark.parametrize(
    "stdout",
    [
        "0.9.0-beta.1\n",
        "0.9.0+x\n",
        "v0.9.0\n",
        "0.9\n",
        "00.8.0\n",
        "0.8.0.0\n",
        "0.9.0.0.0\n",
        "agent-installer 0.9.0\n",
        "\n",
        "   \n",
        "0.9.0\ninjected-second-line\n",
    ],
)
def test_subprocess_adapter_refuses_unparsable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    """Pre-releases, build suffixes and non-release output are refused.

    The diagnostic names the minimum but never echoes the raw stdout, so a
    multi-line or hostile version string cannot leak into diagnostics.
    """
    _with_env(monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=stdout, stderr=""
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
    detail = str(excinfo.value)
    assert MIN_INSTALLER_VERSION in detail
    assert "not a released MAJOR.MINOR.PATCH" in detail
    assert "refusing to install" in detail
    for line in stdout.splitlines():
        if line.strip():
            assert line.strip() not in detail


def test_subprocess_adapter_refuses_nonzero_version_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``--version`` probe refuses with the minimum named."""
    _with_env(monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="0.9.0\n", stderr="boom"
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
    assert MIN_INSTALLER_VERSION in str(excinfo.value)
    assert "refusing to install" in str(excinfo.value)


def test_subprocess_adapter_missing_binary_names_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing binary refuses without running any install or verify."""

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("No such file or directory: 'agent-installer'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(InstallerFault) as excinfo:
        SubprocessInstallerAdapter().install_group(
            source_identity=SOURCE_A,
            https_locator="https://example.com/org/skills-a.git",
            resolved_commit=COMMIT_A,
            selectors=("skill:alpha",),
            home=tmp_path,
        )
    assert MIN_INSTALLER_VERSION in str(excinfo.value)


def test_producer_for_runs_gate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``producer_for`` on a v4 store runs the gate once and reports it."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        assert list(argv) == ["agent-installer", "--version"]
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="  0.9.0  \n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    state_dir = tmp_path / ".agents" / "agent-installer"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text('{"version": 4, "entries": []}', encoding="utf-8")
    adapter = SubprocessInstallerAdapter()
    producer = adapter.producer_for(tmp_path)
    assert producer.installer_version == "0.9.0"
    assert producer.report_schema_version == 3
    assert adapter.producer_for(tmp_path) == producer
    assert calls == [["agent-installer", "--version"]]


def test_fake_producer_for_returns_fake_identity(tmp_path: Path) -> None:
    """The fake adapter names its own producer identity for any home."""
    producer = FakeInstallerAdapter().producer_for(tmp_path)
    assert producer.installer_version == FAKE_INSTALLER_VERSION
    assert producer.report_schema_version == FAKE_REPORT_SCHEMA_VERSION


def test_nonzero_install_exit_with_valid_report_keeps_refusal_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused run that prints a valid v3 report translates with details."""
    _with_env(monkeypatch)
    report = _v3_install_report(disposition="conflict")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.9.0\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout=json.dumps(report), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    group = SubprocessInstallerAdapter().install_group(
        source_identity=SOURCE_A,
        https_locator="https://example.com/org/skills-a.git",
        resolved_commit=COMMIT_A,
        selectors=("skill:alpha",),
        home=tmp_path,
    )
    assert isinstance(group, GroupInstall)
    assert group.complete is False
    assert group.members[0].disposition == "conflict"


def test_nonzero_install_exit_with_garbage_report_refuses_generically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused run without a valid v3 report refuses without details."""
    _with_env(monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.9.0\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="not a report", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(InstallerFault):
        SubprocessInstallerAdapter().install_group(
            source_identity=SOURCE_A,
            https_locator="https://example.com/org/skills-a.git",
            resolved_commit=COMMIT_A,
            selectors=("skill:alpha",),
            home=tmp_path,
        )


def _write_v4_store(home: Path, members: list[DeclaredMember]) -> None:
    """Materialise a released-installer (state v4, tree-v2) managed store.

    Mirrors what ``agent-installer`` writes: one artifact directory per
    member, an exempt ownership marker, and a ``version: 4`` ``state.json``
    whose hashes use ``agent-installer-tree-v2``.
    """
    base = base_store_for(home)
    entries: list[dict[str, object]] = []
    for member in members:
        files = member_files(member)
        artifact = base / member.name
        for rel, (raw, executable) in files.items():
            target = artifact / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o755 if executable else 0o644)
        marker = artifact / ".agent-installer.json"
        marker.write_text(json.dumps({"id": f"skill:{member.name}"}), encoding="utf-8")
        digest = installer_tree_v2_for_files(
            [(rel, raw, executable) for rel, (raw, executable) in files.items()]
        )
        entries.append(
            {
                "hashAlgorithm": SKILL_TREE_ALGORITHM_V2,
                "installedHash": digest,
                "name": member.name,
                "relativeSourcePath": member.resolved_relative_source_path(),
                "resolvedCommit": member.commit,
                "sourceHash": digest,
                "sourceRoot": member.source,
            }
        )
    state_dir = home / ".agents" / "agent-installer"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps({"entries": entries, "version": 4}), encoding="utf-8"
    )


def _write_installed_mode_roots(
    tmp_path: Path, members: list[DeclaredMember]
) -> tuple[Path, Path, Path, Path]:
    """Write config, policy, bundle dir and a v4 store for installed-mode tests."""
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _write_v4_store(home, members)
    return config, policy, tmp_path / "bundle", home


def test_installed_mode_with_subprocess_adapter_records_reported_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--mode installed`` on a v4 store gates offline and records 0.9.0."""
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _write_installed_mode_roots(tmp_path, members)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.9.0\n", stderr=""
            )
        raise AssertionError(f"installed mode must stay offline: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=SubprocessInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert manifest["producer"]["installerVersion"] == "0.9.0"
    assert manifest["producer"]["installerReportSchemaVersion"] == 3
    assert calls == [["agent-installer", "--version"]]


def test_installed_mode_with_subprocess_adapter_refuses_old_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--mode installed`` on a v4 store refuses 0.7.2 with nothing published."""
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _write_installed_mode_roots(tmp_path, members)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(argv) == ["agent-installer", "--version"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.7.2\n", stderr=""
            )
        raise AssertionError(f"installed mode must stay offline: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    refused = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=SubprocessInstallerAdapter(),
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any(
        fault.phase == "bundle"
        and MIN_INSTALLER_VERSION in fault.detail
        and "0.7.2" in fault.detail
        for fault in refused.faults
    )


def test_installed_mode_with_subprocess_adapter_refuses_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--mode installed`` on a v4 store refuses when the binary is absent."""
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _write_installed_mode_roots(tmp_path, members)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("No such file or directory: 'agent-installer'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    refused = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=SubprocessInstallerAdapter(),
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any(
        fault.phase == "bundle" and MIN_INSTALLER_VERSION in fault.detail
        for fault in refused.faults
    )


def test_installed_mode_with_subprocess_adapter_refuses_v3_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--mode installed`` refuses a fake (state v3) store: no false provenance."""
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _write_installed_mode_roots(tmp_path, members)
    state_path = home / ".agents" / "agent-installer" / "state.json"
    state_value = json.loads(state_path.read_bytes().decode("utf-8"))
    assert state_value["version"] == 4
    state_value["version"] = 3
    state_path.write_text(json.dumps(state_value), encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"a refused store must not run the binary: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    refused = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=SubprocessInstallerAdapter(),
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any(fault.phase == "bundle" for fault in refused.faults)


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
    # A credential-bearing source refuses at the single load, before
    # prepare ever runs, without echoing the secret.
    loaded, faults = load_workflow_configuration(config)
    assert loaded is None
    assert faults
    assert "secret-pass" not in "\n".join(str(fault) for fault in faults)
