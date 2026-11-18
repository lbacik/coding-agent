"""Reviewed closure, scanner rulings and unsupported-use refusal (SKV3-13/15/16).

Contract: preparation and verification prove the declared Skill Bundle is
closed under reviewed dependencies, and every scanner-flagged reference has
an explicit content-bound ruling. Scanning never adds dependencies, never
proves closure and never consults a bare-name ignore register.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.skillbundle.deployment import (
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

from coding_agent.skillbundle.installer import FakeInstallerAdapter, write_fake_store
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    COMMIT_C,
    SOURCE_A,
    DeclaredMember,
    build_policy_document,
    default_skill_md_text,
    fake_store_spec_for,
    minimal_config_text,
    skills_entries_for,
    write_policy_file,
)


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _setup(
    tmp_path: Path, members: list[DeclaredMember]
) -> tuple[Path, Path, Path, Path]:
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))
    return config, policy, bundle_dir, home


def _faults_text(result: object) -> str:
    faults: tuple[object, ...] = getattr(result, "faults", ())
    return "\n".join(str(f) for f in faults)


def _manifest_names(bundle_dir: Path) -> list[str]:
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    artifacts: object = manifest["artifacts"]
    assert isinstance(artifacts, list)
    names: list[str] = []
    for artifact in artifacts:
        assert isinstance(artifact, dict)
        name: object = artifact["name"]
        assert isinstance(name, str)
        names.append(name)
    return names


def test_transitive_closure_missing_indirect_refuses_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    alpha = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, requires=("beta",))
    beta = DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_B, requires=("gamma",))
    # Only alpha+beta declared; gamma (transitive) is missing.
    config, policy, bundle_dir, home = _setup(tmp_path, [alpha, beta])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "gamma" in _faults_text(refused)


def test_transitive_closure_complete_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, requires=("beta",)),
        DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_B, requires=("gamma",)),
        DeclaredMember(name="gamma", source=SOURCE_A, commit=COMMIT_C),
    ]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_finite_closed_cycle_accepted_without_recursive_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, requires=("beta",)),
        DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_B, requires=("alpha",)),
    ]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)
    # No recursive installation: exactly the two declared members.
    assert _manifest_names(bundle_dir) == ["alpha", "beta"]
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_requires_creates_availability_never_automatic_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    # beta is declared (available) but no static instruction names it; closure
    # is still checked for every declared member.
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, requires=("beta",)),
        DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_B),
    ]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)
    assert _manifest_names(bundle_dir) == ["alpha", "beta"]


def test_unruled_scanner_reference_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_md = default_skill_md_text("alpha", body="See /setup-foo for onboarding.")
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, skill_md=skill_md)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "unruled reference" in _faults_text(refused)
    assert "/setup-foo" in _faults_text(refused)


def test_ruled_scanner_reference_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_md = default_skill_md_text("alpha", body="See /setup-foo for onboarding.")
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "path fragment, not a skill",
                "text": "See /setup-foo for onboarding.",
            },
        ),
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_scanner_never_adds_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    # "/beta" is ruled as not-a-skill-reference; beta is not declared and must
    # not be auto-added as a dependency.
    skill_md = default_skill_md_text("alpha", body="Mention /beta in passing.")
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "passing mention",
                "text": "Mention /beta in passing.",
            },
        ),
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)
    assert _manifest_names(bundle_dir) == ["alpha"]


def test_inject_scanner_reference_requires_ruling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        extra_files={"docs/notes.md": (b"See /bar here.\n", False)},
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "unruled reference" in _faults_text(refused)

    ruled = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        extra_files={"docs/notes.md": (b"See /bar here.\n", False)},
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "docs/notes.md",
                "reason": "example",
                "text": "See /bar here.",
            },
        ),
    )
    config2 = tmp_path / "workflow2.yml"
    config2.write_text(minimal_config_text(skills_entries_for([ruled])), encoding="utf-8")
    policy2 = tmp_path / "policy2.json"
    write_policy_file(policy2, build_policy_document([ruled]))
    bundle2 = tmp_path / "bundle2"
    home2 = tmp_path / "home2"
    home2.mkdir(exist_ok=True)
    write_fake_store(home2, fake_store_spec_for([ruled]))
    prepared = prepare_skill_bundle(
        config=_loaded_config(config2), policy=policy2, bundle_dir=bundle2, home=home2, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)


def test_stale_ruling_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "stale",
                "text": "absent excerpt /gone",
            },
        ),
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "does not occur" in _faults_text(refused)


def test_waived_declared_target_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_md = default_skill_md_text("alpha", body="Run /beta setup.")
    alpha = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        references=(
            {
                "classification": "unavailable-nonrequired-reference",
                "notice": "not needed during attempt",
                "origin": "SKILL.md",
                "reason": "setup is out of scope",
                "target": "beta",
                "text": "Run /beta setup.",
            },
        ),
    )
    beta = DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_B)
    config, policy, bundle_dir, home = _setup(tmp_path, [alpha, beta])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "cannot be waived" in _faults_text(refused)


def test_unavailable_nonrequired_ruling_passes_for_undeclared_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_md = default_skill_md_text("alpha", body="Run /setup-tool now.")
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        references=(
            {
                "classification": "unavailable-nonrequired-reference",
                "notice": "setup runs outside attempts",
                "origin": "SKILL.md",
                "reason": "irrelevant during attempt",
                "target": "setup-tool",
                "text": "Run /setup-tool now.",
            },
        ),
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)


def test_unsupported_use_not_delivered_mention_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_md = default_skill_md_text("alpha", body="See data/blob.bin for details.")
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        extra_files={"data/blob.bin": (b"\x00\x01\x02", False)},
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "covers scanner candidate",
                "text": "See data/blob.bin for details.",
            },
        ),
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "not-delivered file" in _faults_text(refused)


def test_unsupported_use_packaged_script_mention_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_md = default_skill_md_text("alpha", body="Run run.sh to bootstrap.")
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_md,
        extra_files={"run.sh": (b"#!/bin/sh\necho hi\n", False)},
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert "not-delivered file" in _faults_text(refused)


def test_unused_reviews_are_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    alpha = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    ghost = DeclaredMember(name="ghost", source=SOURCE_A, commit=COMMIT_C)
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for([alpha])), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([alpha, ghost]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for([alpha]))
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, _faults_text(prepared)
    assert _manifest_names(bundle_dir) == ["alpha"]
