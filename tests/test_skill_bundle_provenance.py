"""Typed state-v4 provenance reading for the Skill Bundle deployment path.

Contract: the managed-store reader accepts only the released installer's
state version 4 with an ``entries`` array of ``kind: "skill"`` records keyed
by ``sourceRoot``. Any other version, a missing version, or a non-array
``entries`` is refused with one fault naming ``state.json`` and its
reinstall remedy. Content identity supports only
``agent-installer-tree-v2``; any other recorded algorithm is refused per
member under ``skills:<name>``. Non-skill kinds are ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.skillbundle.deployment import prepare_skill_bundle
from coding_agent.skillbundle.installer import (
    SKILL_TREE_ALGORITHM_V2,
    FakeInstallerAdapter,
    InstallerFault,
    ProvenanceRecord,
    base_store_for,
    installer_tree_v2_for_files,
    load_provenance,
    recorded_content_identity,
    write_fake_store,
)
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    fake_store_spec_for,
    member_files,
    minimal_config_text,
    skills_entries_for,
    write_policy_file,
)


def _loaded_config(path: Path) -> LoadedWorkflowConfiguration:
    """The single-loader value for the config file at ``path``."""
    loaded, faults = load_workflow_configuration(path)
    assert loaded is not None, [str(fault) for fault in faults]
    return loaded


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _write_state(home: Path, value: object) -> Path:
    state_dir = home / ".agents" / "agent-installer"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps(value), encoding="utf-8")
    return state_path


def _write_v4_entry(
    home: Path,
    member: DeclaredMember,
    *,
    hash_algorithm: str | None = SKILL_TREE_ALGORITHM_V2,
    kind: str | None = "skill",
    source_key: str = "sourceRoot",
) -> None:
    """Materialise one artifact plus a single-entry v4 store for ``member``."""
    files = member_files(member)
    artifact = base_store_for(home) / member.name
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
    entry: dict[str, object] = {
        "installedHash": digest,
        "name": member.name,
        "relativeSourcePath": member.resolved_relative_source_path(),
        "resolvedCommit": member.commit,
        "sourceHash": digest,
    }
    if kind is not None:
        entry["kind"] = kind
    if hash_algorithm is not None:
        entry["hashAlgorithm"] = hash_algorithm
    entry[source_key] = member.source
    _write_state(home, {"entries": [entry], "version": 4})


def _prepare_installed(
    tmp_path: Path, members: list[DeclaredMember]
) -> tuple[Path, Path, Path, Path]:
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return config, policy, tmp_path / "bundle", home


def test_load_provenance_refuses_v3_store(tmp_path: Path) -> None:
    """A state-v3 store is refused with the reinstall remedy."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state_path = _write_state(home, {"entries": [], "version": 3})
    provenance, fault = load_provenance(home)
    assert provenance == {}
    assert fault is not None
    assert fault.path == str(state_path)
    assert "agent-installer >= 0.8.0" in fault.detail


def test_load_provenance_refuses_missing_version(tmp_path: Path) -> None:
    """A store without a version is refused with the reinstall remedy."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state_path = _write_state(home, {"entries": []})
    provenance, fault = load_provenance(home)
    assert provenance == {}
    assert fault is not None
    assert fault.path == str(state_path)
    assert "agent-installer >= 0.8.0" in fault.detail


def test_load_provenance_refuses_non_array_entries(tmp_path: Path) -> None:
    """A store whose entries are not an array is refused with the remedy."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state_path = _write_state(home, {"entries": {}, "version": 4})
    provenance, fault = load_provenance(home)
    assert provenance == {}
    assert fault is not None
    assert fault.path == str(state_path)
    assert "entries must be an array" in fault.detail
    assert "agent-installer >= 0.8.0" in fault.detail


def test_load_provenance_reads_v4_source_root_only(tmp_path: Path) -> None:
    """A v4 entry with only ``sourceRoot`` loads as a typed record."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    _write_v4_entry(home, member)
    provenance, fault = load_provenance(home)
    assert fault is None
    record = provenance["alpha"]
    assert isinstance(record, ProvenanceRecord)
    assert record.name == "alpha"
    assert record.source_identity == SOURCE_A
    assert record.resolved_commit == COMMIT_A
    assert record.relative_source_path == "skills/alpha"
    assert record.hash_algorithm == SKILL_TREE_ALGORITHM_V2
    assert record.source_hash == record.installed_hash
    assert record.installed_hash is not None


def test_load_provenance_ignores_prompt_kind(tmp_path: Path) -> None:
    """A ``kind: "prompt"`` entry never becomes skill provenance."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    _write_v4_entry(home, member)
    state_path = home / ".agents" / "agent-installer" / "state.json"
    value = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    entries = value["entries"]
    assert isinstance(entries, list)
    entries.append(
        {
            "hashAlgorithm": "sha256",
            "installedHash": "0" * 64,
            "kind": "prompt",
            "name": "helper-prompt",
            "sourceHash": "0" * 64,
            "sourceRoot": SOURCE_A,
        }
    )
    state_path.write_text(json.dumps(value), encoding="utf-8")
    provenance, fault = load_provenance(home)
    assert fault is None
    assert set(provenance.keys()) == {"alpha"}


@pytest.mark.parametrize(
    "algorithm", [None, "agent-installer-tree-v1", "agent-installer-sha256-v1"]
)
def test_recorded_content_identity_refuses_legacy_algorithms(
    algorithm: str | None,
) -> None:
    """Missing, tree-v1 and legacy digests are refused per member."""
    inputs: list[tuple[str, bytes, bool]] = [("SKILL.md", b"# a\n", False)]
    _, _, fault = recorded_content_identity(algorithm, "alpha", inputs)
    assert fault is not None
    assert isinstance(fault, InstallerFault)
    assert fault.path == "skills:alpha"
    assert "agent-installer >= 0.8.0" in fault.detail


@pytest.mark.parametrize(
    "algorithm", [None, "agent-installer-tree-v1", "agent-installer-sha256-v1"]
)
def test_installed_mode_refuses_unsupported_algorithm_per_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, algorithm: str | None
) -> None:
    """A skill entry with a non-tree-v2 algorithm refuses under its name."""
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _prepare_installed(tmp_path, [member])
    _write_v4_entry(home, member, hash_algorithm=algorithm)
    refused = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not refused.ok
    assert any(
        fault.phase == "bundle"
        and fault.path == "skills:alpha"
        and "agent-installer >= 0.8.0" in fault.detail
        for fault in refused.faults
    )


def test_installed_mode_adopts_source_root_only_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v4 store carrying only ``sourceRoot`` adopts successfully."""
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _prepare_installed(tmp_path, [member])
    _write_v4_entry(home, member)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [artifact["name"] for artifact in manifest["artifacts"]] == ["alpha"]
    assert manifest["artifacts"][0]["installerContent"]["algorithm"] == (
        SKILL_TREE_ALGORITHM_V2
    )


def test_installed_mode_ignores_prompt_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that also holds a prompt still adopts its skill members."""
    _with_env(monkeypatch)
    member = DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)
    config, policy, bundle_dir, home = _prepare_installed(tmp_path, [member])
    _write_v4_entry(home, member)
    state_path = home / ".agents" / "agent-installer" / "state.json"
    value = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    entries = value["entries"]
    assert isinstance(entries, list)
    entries.append(
        {
            "hashAlgorithm": "sha256",
            "installedHash": "0" * 64,
            "kind": "prompt",
            "name": "helper-prompt",
            "sourceHash": "0" * 64,
            "sourceRoot": SOURCE_B,
        }
    )
    state_path.write_text(json.dumps(value), encoding="utf-8")
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert [artifact["name"] for artifact in manifest["artifacts"]] == ["alpha"]


def test_fake_store_writes_released_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``write_fake_store`` writes a v4 tree-v2 store the reader accepts."""
    _with_env(monkeypatch)
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(name="beta", source=SOURCE_B, commit=COMMIT_B),
    ]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))
    state_path = home / ".agents" / "agent-installer" / "state.json"
    value = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    assert value["version"] == 4
    entries = value["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        assert entry["kind"] == "skill"
        assert entry["hashAlgorithm"] == SKILL_TREE_ALGORITHM_V2
        assert "sourceRoot" in entry
        assert "sourceIdentity" not in entry
    provenance, fault = load_provenance(home)
    assert fault is None
    assert set(provenance.keys()) == {"alpha", "beta"}
    for record in provenance.values():
        assert record.hash_algorithm == SKILL_TREE_ALGORITHM_V2
    config, policy, bundle_dir, _ = _prepare_installed(tmp_path, members)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    for artifact in manifest["artifacts"]:
        assert artifact["installerContent"]["algorithm"] == SKILL_TREE_ALGORITHM_V2
