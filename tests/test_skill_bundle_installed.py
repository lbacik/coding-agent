"""Installed-mode adoption of a non-empty Skill Bundle (issue #392).

Contract: ``prepare-skill-bundle --mode installed`` adopts the declared
subset from the existing managed store offline and records it in the
manifest; ``verify-skill-bundle`` re-checks it against the final roots.
Expected membership comes from the test's own declaration and approval from
its own policy document, never from the fake installer's output.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from coding_agent.skillbundle.deployment import (
    prepare_skill_bundle,
    verify_skill_bundle_deployment,
)
from coding_agent.workflow.v3_validate import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)


def _loaded_config(path: Path) -> LoadedWorkflowConfiguration:
    """The single-loader value for the config file at ``path``."""
    loaded, faults, _raw = load_workflow_configuration(path)
    assert loaded is not None, [str(fault) for fault in faults]
    return loaded

from coding_agent.skillbundle.installer import (
    INSTALLER_HASH_ALGORITHM,
    base_store_for,
    installer_hash_for_files,
    write_fake_store,
)
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    COMMIT_C,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    default_skill_md_text,
    fake_store_spec_for,
    member_files,
    minimal_config_text,
    overlay_skill_md_text,
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


def _setup(
    tmp_path: Path, members: list[DeclaredMember]
) -> tuple[Path, Path, Path, Path]:
    """Write config, policy, fake store; returns (config, policy, bundle, home)."""
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))
    return config, policy, bundle_dir, home


def _setup_with_extra_store(
    tmp_path: Path, members: list[DeclaredMember], extra: dict[str, dict[str, Any]]
) -> tuple[Path, Path, Path, Path]:
    config = _write_config(tmp_path, members)
    policy = _write_policy(tmp_path, members)
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    spec = fake_store_spec_for(members)
    spec.update(extra)
    write_fake_store(home, spec)
    return config, policy, bundle_dir, home


def test_happy_path_adopts_declared_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(
            name="beta",
            source=SOURCE_B,
            commit=COMMIT_B,
            extra_files={"docs/notes.md": (b"# notes\n", False)},
        ),
    ]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [a["name"] for a in manifest["artifacts"]] == ["alpha", "beta"]
    for artifact in manifest["artifacts"]:
        assert artifact["verification"] == "recorded-installation"
        assert artifact["installerContent"]["algorithm"] == INSTALLER_HASH_ALGORITHM
    verified = verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert verified.ok, [str(f) for f in verified.faults]


def test_only_declared_members_recorded_unrelated_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    extra = {
        "unrelated": {
            "commit": COMMIT_C,
            "files": {"SKILL.md": (default_skill_md_text("unrelated").encode(), False)},
            "relativeSourcePath": "skills/unrelated",
            "source": SOURCE_B,
        }
    }
    config, policy, bundle_dir, home = _setup_with_extra_store(tmp_path, members, extra)
    # An unrelated exposure symlink is neither required nor authoritative.
    unrelated_dir = base_store_for(home) / "unrelated"
    assert unrelated_dir.is_dir()
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [a["name"] for a in manifest["artifacts"]] == ["alpha"]
    # Unrelated artifacts are left unchanged and are not activatable.
    assert unrelated_dir.is_dir()
    assert (unrelated_dir / "SKILL.md").is_file()
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_exposure_symlinks_neither_required_nor_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    # Claude exposure symlinks exist outside the Base Store: one valid link
    # into the store and one dangling link. Neither is read or recorded.
    exposure = home / ".claude" / "skills"
    exposure.mkdir(parents=True)
    (exposure / "alpha").symlink_to(base_store_for(home) / "alpha")
    (exposure / "dangling").symlink_to(base_store_for(home) / "missing")
    prepared = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [a["name"] for a in manifest["artifacts"]] == ["alpha"]
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_manifest_records_complete_artifact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [
        DeclaredMember(
            name="alpha",
            source=SOURCE_A,
            commit=COMMIT_A,
            relative_source_path="sources/skills/alpha",
            skill_md=overlay_skill_md_text("alpha"),
            extra_files={
                "docs/guide.md": (b"# guide\n", False),
                "agents/openai.yaml": (b"model: x\n", False),
            },
        )
    ]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert len(manifest["artifacts"]) == 1
    artifact = manifest["artifacts"][0]
    assert artifact["id"] == "skill:alpha"
    assert artifact["sourceIdentity"] == SOURCE_A
    assert artifact["resolvedCommit"] == COMMIT_A
    assert artifact["relativeSourcePath"] == "sources/skills/alpha"
    assert artifact["storeRelativePath"] == "alpha"
    assert artifact["verification"] == "recorded-installation"
    assert artifact["installerContent"]["algorithm"] == INSTALLER_HASH_ALGORITHM
    assert artifact["overlays"] == [
        {"id": "codex-invocation-policy", "paths": ["agents/openai.yaml"], "version": "1"}
    ]
    paths = [f["path"] for f in artifact["files"]]
    assert paths == sorted(paths, key=lambda s: s.encode("utf-8"))
    assert ".." not in paths and all(not p.startswith("/") for p in paths)
    assert "SKILL.md" in paths
    assert "docs/guide.md" in paths
    assert "agents/openai.yaml" in paths
    # treeSha256 binds the canonical file inventory.
    assert artifact["treeSha256"] == hashlib.sha256(
        json.dumps(artifact["files"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    # Installer hashes match a recomputation over the store bytes.
    entries: list[tuple[str, bytes, bool]] = []
    for record in artifact["files"]:
        raw = (base_store_for(home) / "alpha" / str(record["path"])).read_bytes()
        entries.append((str(record["path"]), raw, bool(record["executable"])))
    expected_hash = installer_hash_for_files(entries)
    assert artifact["installerContent"]["sourceHash"] == expected_hash
    assert artifact["installerContent"]["installedHash"] == expected_hash


def _mutating_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path, DeclaredMember]:
    _with_env(monkeypatch)
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=overlay_skill_md_text("alpha"),
        extra_files={
            ".hidden": (b"hidden\n", False),
            "agents/openai.yaml": (b"model: x\n", False),
            "data/blob.bin": (b"\x00\x01\x02", False),
            "docs/nested.md": (b"# nested\n", False),
        },
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok
    return config, policy, bundle_dir, home, member


def test_inventory_hidden_nested_overlay_not_delivered_are_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, policy, bundle_dir, home, _ = _mutating_member(tmp_path, monkeypatch)
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    paths = {str(f["path"]) for f in manifest["artifacts"][0]["files"]}
    assert {".hidden", "docs/nested.md", "agents/openai.yaml", "data/blob.bin"} <= paths


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "renamed", "exec-bit"])
def test_inventory_mutations_refuse_at_prepare_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    config, policy, bundle_dir, home, _ = _mutating_member(tmp_path, monkeypatch)
    artifact_dir = base_store_for(home) / "alpha"
    if mutation == "missing":
        (artifact_dir / "docs" / "nested.md").unlink()
    elif mutation == "changed":
        (artifact_dir / "docs" / "nested.md").write_bytes(b"# edited\n")
    elif mutation == "extra":
        (artifact_dir / "extra.md").write_bytes(b"# extra\n")
    elif mutation == "renamed":
        (artifact_dir / "docs" / "nested.md").rename(artifact_dir / "docs" / "renamed.md")
    elif mutation == "exec-bit":
        target = artifact_dir / "docs" / "nested.md"
        os.chmod(target, 0o755)
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok, mutation
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok is False


@pytest.mark.parametrize("mutation", ["hidden", "overlay", "not-delivered"])
def test_inventory_special_paths_refuse_when_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    config, policy, bundle_dir, home, _ = _mutating_member(tmp_path, monkeypatch)
    artifact_dir = base_store_for(home) / "alpha"
    target = {
        "hidden": artifact_dir / ".hidden",
        "overlay": artifact_dir / "agents" / "openai.yaml",
        "not-delivered": artifact_dir / "data" / "blob.bin",
    }[mutation]
    target.write_bytes(b"changed\n")
    assert not prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    assert not verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_symlink_inside_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    (base_store_for(home) / "alpha" / "link.md").symlink_to("SKILL.md")
    assert not prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    assert not verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_special_file_inside_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    os.mkfifo(base_store_for(home) / "alpha" / "pipe")
    assert not prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok


def test_only_versioned_installer_metadata_is_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    # The fake's exempt ownership file is fine; any other dotfile is content.
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    (base_store_for(home) / "alpha" / ".other-metadata.json").write_bytes(b"{}\n")
    assert not prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok


def test_unconstrained_member_adopted_at_recorded_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref=None)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert manifest["artifacts"][0]["resolvedCommit"] == COMMIT_A
    assert manifest["resolutions"] == []


def test_exact_commit_ref_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    # A declaration pinning a different exact commit refuses.
    other = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref=COMMIT_B)
    config2 = tmp_path / "workflow-other.yml"
    config2.write_text(minimal_config_text(skills_entries_for([other])), encoding="utf-8")
    refused = prepare_skill_bundle(
        config=_loaded_config(config2), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok
    assert any("exact-commit" in str(f) for f in refused.faults)


def _write_resolution_manifest(path: Path, source: str, ref: str, commit: str) -> None:
    doc: dict[str, object] = {
        "artifacts": [],
        "kind": "coding-agent.skill-bundle-manifest",
        "producer": {
            "codingAgentVersion": "test",
            "installerReportSchemaVersion": 1,
            "installerVersion": "test",
        },
        "requirementsSha256": "0" * 64,
        "resolutions": [
            {"ref": ref, "resolvedCommit": commit, "sourceIdentity": source}
        ],
        "schemaVersion": 1,
    }
    path.write_bytes(
        json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    )


def test_symbolic_ref_without_resolution_manifest_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main")
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for([member])), encoding="utf-8")
    policy = _write_policy(tmp_path, [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)])
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for([DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]))
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok
    assert any("preparation required" in str(f) for f in refused.faults)


def test_symbolic_ref_with_resolution_manifest_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    stored = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    declared = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref="main")
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for([declared])), encoding="utf-8")
    policy = _write_policy(tmp_path, [stored])
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for([stored]))
    resolution_manifest = tmp_path / "resolution.json"
    _write_resolution_manifest(resolution_manifest, SOURCE_A, "main", COMMIT_A)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        resolution_manifest=resolution_manifest,
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert manifest["artifacts"][0]["resolvedCommit"] == COMMIT_A
    assert manifest["resolutions"] == [
        {"ref": "main", "resolvedCommit": COMMIT_A, "sourceIdentity": SOURCE_A}
    ]
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_wrong_source_and_missing_provenance_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    stored = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [stored])
    # Wrong source: declaration names another identity but provenance disagrees.
    wrong = DeclaredMember(name="alpha", source=SOURCE_B, commit=COMMIT_A)
    config_wrong = tmp_path / "workflow-wrong.yml"
    config_wrong.write_text(minimal_config_text(skills_entries_for([wrong])), encoding="utf-8")
    policy_wrong = tmp_path / "policy-wrong.json"
    write_policy_file(policy_wrong, build_policy_document([wrong]))
    refused = prepare_skill_bundle(
        config=_loaded_config(config_wrong), policy=policy_wrong, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok
    assert any("wrong source" in str(f) for f in refused.faults)
    # Missing provenance: an unmanaged directory with no state entry refuses.
    (base_store_for(home) / "ghost").mkdir(parents=True, exist_ok=True)
    (base_store_for(home) / "ghost" / "SKILL.md").write_bytes(b"# ghost\n")
    ghost = DeclaredMember(name="ghost", source=SOURCE_A, commit=COMMIT_A)
    config_ghost = tmp_path / "workflow-ghost.yml"
    config_ghost.write_text(minimal_config_text(skills_entries_for([ghost])), encoding="utf-8")
    policy_ghost = tmp_path / "policy-ghost.json"
    write_policy_file(policy_ghost, build_policy_document([ghost]))
    missing = prepare_skill_bundle(
        config=_loaded_config(config_ghost), policy=policy_ghost, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not missing.ok
    assert any("provenance" in str(f) for f in missing.faults)
    # The unmanaged directory is left in place, never pruned.
    assert (base_store_for(home) / "ghost" / "SKILL.md").is_file()


def test_installer_output_cannot_redefine_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    extra = {
        "sneaky": {
            "commit": COMMIT_B,
            "files": {"SKILL.md": (default_skill_md_text("sneaky").encode(), False)},
            "relativeSourcePath": "skills/sneaky",
            "source": SOURCE_A,
        }
    }
    config, policy, bundle_dir, home = _setup_with_extra_store(tmp_path, members, extra)
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [a["name"] for a in manifest["artifacts"]] == ["alpha"]


def test_review_zero_matches_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    # Empty approval set: no review matches.
    policy.write_bytes(
        json.dumps(
            {"kind": "coding-agent.skill-bundle-policy", "reviews": [], "schemaVersion": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok
    assert any("no matching approved review" in str(f) for f in refused.faults)


def test_review_several_matches_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config = _write_config(tmp_path, [member])
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for([member]))
    doc = build_policy_document([member])
    reviews = doc["reviews"]
    assert isinstance(reviews, list)
    first_raw = reviews[0]
    assert isinstance(first_raw, dict)
    first = dict(first_raw)
    duplicate = dict(first)
    duplicate["reason"] = "second identical approval"
    reviews.append(duplicate)
    policy = tmp_path / "skill-policy.json"
    # Ambiguous duplicate approvals are refused before any adoption.
    write_policy_file(policy, doc)
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok


def test_review_expires_when_content_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    (base_store_for(home) / "alpha" / "SKILL.md").write_bytes(b"# edited skill\n")
    assert not prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok


def test_identical_content_at_new_commit_reuses_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    original = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _setup(tmp_path, [original])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    # Same bytes, new commit: the existing review still matches.
    moved = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_B)
    write_fake_store(home, fake_store_spec_for([moved]))
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert manifest["artifacts"][0]["resolvedCommit"] == COMMIT_B


@pytest.mark.parametrize(
    ("rel", "payload"),
    [
        ("run.sh", (b"#!/bin/sh\necho hi\n", False)),
        ("blob.bin", (b"\xff\xfe\x00binary", False)),
        ("tool", (b"plain text\n", True)),
        ("agents/openai.yaml", (b"model: x\n", False)),
    ],
)
def test_inject_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str, payload: tuple[bytes, bool]
) -> None:
    _with_env(monkeypatch)
    raw, executable = payload
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        extra_files={rel: (raw, executable)},
        disposition_overrides={rel: "inject"},
    )
    config = _write_config(tmp_path, [member])
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([member]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for([member]))
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok, rel
    assert any("cannot be 'inject'" in str(f) for f in refused.faults)


def test_not_delivered_allows_scripts_binaries_and_admin_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=overlay_skill_md_text("alpha"),
        extra_files={
            "run.sh": (b"#!/bin/sh\necho hi\n", False),
            "agents/openai.yaml": (b"model: x\n", False),
            "data/blob.bin": (b"\xff\xfe\x00binary", False),
        },
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    assert verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_preparation_never_prunes_unmanaged_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    unmanaged = base_store_for(home) / "unmanaged"
    unmanaged.mkdir(parents=True, exist_ok=True)
    (unmanaged / "keep.md").write_bytes(b"# keep\n")
    assert prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    assert (unmanaged / "keep.md").read_bytes() == b"# keep\n"


def test_closure_requires_declared_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(
        name="alpha", source=SOURCE_A, commit=COMMIT_A, requires=("missing",)
    )
    config, policy, bundle_dir, home = _setup(tmp_path, [member])
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok
    assert any("not declared" in str(f) for f in refused.faults)


def test_reference_origin_must_be_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    skill_text = default_skill_md_text("alpha") + "\nSee GUIDE.\n"
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=skill_text,
        extra_files={"docs/guide.md": (b"# Guide\n", False)},
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "docs/guide.md",
                "reason": "synthetic",
                "text": "nope, absent excerpt",
            },
        ),
        disposition_overrides={"docs/guide.md": "not-delivered"},
    )
    config = _write_config(tmp_path, [member])
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([member]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for([member]))
    refused = prepare_skill_bundle(
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok


def test_credential_bearing_source_never_echoed(
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
        config=_loaded_config(config), policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not refused.ok
    assert "secret-pass" not in "\n".join(str(f) for f in refused.faults)


def test_member_files_helper_uses_declaration_not_fake_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        extra_files={"docs/notes.md": (b"# notes\n", False)},
    )
    assert "docs/notes.md" in member_files(member)
    assert member_files(member)["SKILL.md"][0] == default_skill_md_text("alpha").encode("utf-8")
