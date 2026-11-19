"""Real-installer proofs against released ``agent-installer>=0.8.0`` (issue #400).

Contract: the installer-adapter port bound to a released installer
satisfying ``agent-installer#74`` (complete Git artifact provenance, freshly
verified installed copies, offline subset verification). Live tests install
immutable Git fixtures (two sources, four skills, recorded commits) through
``SubprocessInstallerAdapter`` with ``GIT_CONFIG url.<path>.insteadOf``
mappings so no network is touched, then prove: fresh install, repeated
unchanged install, offline adoption with the sources removed, a shared store
with unrelated content, a second source, nested ``agents/openai.yaml``
overlays (added and modified) plus hidden files, host-installed conflicts
refusing instead of auto-approving (SKV3-36), and credential absence in
diagnostics. Live tests skip unless ``agent-installer --version`` on PATH
reports a released version satisfying the ``>= 0.8.0`` minimum; floor,
tree-v2, validator and redaction tests always run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
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
    MIN_INSTALLER_VERSION,
    REAL_INSTALL_REPORT_SCHEMA_VERSION,
    SKILL_TREE_ALGORITHM_V2,
    SUPPORTED_SKILL_HASH_ALGORITHMS,
    GroupInstall,
    InstallerFault,
    MemberInstall,
    SubprocessInstallerAdapter,
    build_install_argv,
    installer_tree_v2_for_files,
    query_installer_version,
    recorded_content_identity,
)
from support.skill_bundle import (
    SOURCE_A,
    SOURCE_B,
    default_disposition,
    minimal_config_text,
    write_policy_file,
)


def _binary_version() -> str | None:
    """The installed ``agent-installer`` version, or ``None`` when absent."""
    binary = shutil.which("agent-installer")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _real_available() -> bool:
    """Whether the binary on PATH passes the installer's minimum gate."""
    try:
        query_installer_version()
    except InstallerFault:
        return False
    return True


_REAL_AVAILABLE = _real_available()
requires_real_installer = pytest.mark.skipif(
    not _REAL_AVAILABLE,
    reason=f"needs agent-installer@>={MIN_INSTALLER_VERSION} on PATH",
)


def test_minimum_version_matches_image_range() -> None:
    """The port floor and the image range agree on the minimum release."""
    assert MIN_INSTALLER_VERSION == "0.8.0"
    assert REAL_INSTALL_REPORT_SCHEMA_VERSION == 3
    package_dir = (
        Path(__file__).resolve().parent.parent / "docker" / "agent-installer"
    )
    package = json.loads((package_dir / "package.json").read_bytes().decode("utf-8"))
    dependencies = package.get("dependencies", {})
    assert isinstance(dependencies, dict)
    assert dependencies.get("agent-installer") == f">={MIN_INSTALLER_VERSION}"
    lockfile = json.loads((package_dir / "package-lock.json").read_bytes().decode("utf-8"))
    resolved = lockfile["packages"]["node_modules/agent-installer"]["version"]
    assert isinstance(resolved, str)
    parsed = tuple(int(part) for part in resolved.split("."))
    assert len(parsed) == 3
    assert parsed >= (0, 8, 0)


def test_tree_v2_is_sorted_framed_and_mode_sensitive() -> None:
    """The tree-v2 digest matches an independent framing of the same inputs."""
    entries: list[tuple[str, bytes, bool]] = [
        ("b.txt", b"second", False),
        ("a.txt", b"first", True),
        ("nested/c.txt", b"third", False),
    ]
    digest = hashlib.sha256()
    digest.update(b"agent-installer-tree-v2\x00")
    for rel, raw, executable in sorted(entries, key=lambda item: item[0].encode("utf-8")):
        path_bytes = rel.encode("utf-8")
        digest.update(f"path {len(path_bytes)}\x00".encode("utf-8"))
        digest.update(path_bytes)
        digest.update(f"\x00mode {'x' if executable else '-'}\x00content {len(raw)}\x00".encode("utf-8"))
        digest.update(raw)
        digest.update(b"\x00")
    assert installer_tree_v2_for_files(entries) == digest.hexdigest()
    assert installer_tree_v2_for_files(list(reversed(entries))) == digest.hexdigest()
    flipped = [("a.txt", b"first", False), ("b.txt", b"second", False), ("nested/c.txt", b"third", False)]
    assert installer_tree_v2_for_files(flipped) != digest.hexdigest()


def test_recorded_content_identity_supports_only_tree_v2() -> None:
    """Only tree-v2 rehashes; any other algorithm is refused per member."""
    inputs: list[tuple[str, bytes, bool]] = [("SKILL.md", b"# a\n", False)]
    algorithm, current, fault = recorded_content_identity(
        SKILL_TREE_ALGORITHM_V2, "a", inputs
    )
    assert fault is None
    assert algorithm == SKILL_TREE_ALGORITHM_V2
    assert current == installer_tree_v2_for_files(inputs)
    assert algorithm in SUPPORTED_SKILL_HASH_ALGORITHMS
    for bad in (None, "agent-installer-tree-v1", "agent-installer-sha256-v1", "agent-installer-sha9"):
        _, _, fault = recorded_content_identity(bad, "a", inputs)
        assert fault is not None
        assert fault.path == "skills:a"
        assert "unsupported content algorithm" in fault.detail
        assert "agent-installer >= 0.8.0" in fault.detail


def _v3_group(
    members: tuple[str, ...] = ("alpha",),
    source: str = SOURCE_A,
    commit: str = "a" * 40,
    disposition: str = "installed",
    complete: bool = True,
    unmatched_selectors: tuple[str, ...] = (),
    pruned: tuple[str, ...] = (),
    error: str | None = None,
    content_algorithm: str | None = SKILL_TREE_ALGORITHM_V2,
) -> GroupInstall:
    return GroupInstall(
        complete=complete,
        unmatched_selectors=unmatched_selectors,
        pruned=pruned,
        error=error,
        members=tuple(
            MemberInstall(
                id=f"skill:{member}",
                disposition=disposition,
                source_identity=source,
                resolved_commit=commit,
                content_algorithm=content_algorithm,
            )
            for member in members
        ),
    )


def test_v3_validator_accepts_success_dispositions() -> None:
    """Installed, updated and unchanged dispositions all verify positively."""
    for disposition in ("installed", "updated", "unchanged"):
        faults: list[DeploymentFault] = []
        assert _validate_group_install_for_group(
            source_identity=SOURCE_A,
            resolved_commit="a" * 40,
            selectors=("skill:alpha",),
            group=_v3_group(disposition=disposition),
            group_where="skills[0].source",
            faults=faults,
        )
        assert faults == []


def test_v3_validator_refuses_non_success() -> None:
    """Aborts, conflicts, mismatches and mutations all refuse the generation."""
    base = _v3_group()
    cases: list[tuple[str, GroupInstall]] = []
    cases.append(("aborted", _v3_group(complete=False)))
    cases.append(("conflict", _v3_group(disposition="conflict")))
    cases.append(("failed", _v3_group(disposition="failed")))
    cases.append(("missing", _v3_group(members=())))
    cases.append(("wrong-source", _v3_group(source=SOURCE_B)))
    cases.append(("wrong-commit", _v3_group(commit="b" * 40)))
    cases.append(("unmatched", _v3_group(unmatched_selectors=("skill:ghost",))))
    cases.append(("error", _v3_group(error='{"code":"conflict"}')))
    cases.append(("pruned", _v3_group(pruned=("skill:old",))))
    cases.append(("algorithm", _v3_group(content_algorithm="agent-installer-sha9")))
    assert base.complete is True
    for name, group in cases:
        faults: list[DeploymentFault] = []
        assert not _validate_group_install_for_group(
            source_identity=SOURCE_A,
            resolved_commit="a" * 40,
            selectors=("skill:alpha",),
            group=group,
            group_where="skills[0].source",
            faults=faults,
        ), name
        assert faults != [], name


def test_install_argv_requests_v3_schema() -> None:
    """Install passes one argument per selector with the v3 schema and no shell."""
    assert build_install_argv("https://example.com/x.git", "a" * 40, ("skill:a",))[-2:] == [
        "--json-schema",
        "3",
    ]


def test_resolve_ref_failure_redacts_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing resolve never echoes the credential-bearing locator."""
    secret = "git+https://user:secret-pass@example.com/org/repo.git"

    def failing_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="fatal")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(InstallerFault) as excinfo:
        SubprocessInstallerAdapter().resolve_ref(secret, "main")
    assert "secret-pass" not in str(excinfo.value)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


def _commit_repo(path: Path, files: dict[str, tuple[bytes, bool]]) -> str:
    """Write ``files`` into a new single-commit repository; return its SHA."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    for rel, (raw, executable) in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if executable:
            os.chmod(target, 0o755)
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=skill-test",
        "-c",
        "user.email=skill-test@example.com",
        "commit",
        "-m",
        "immutable fixture",
    )
    return _git(path, "rev-parse", "HEAD").strip()


def _skill_md(name: str, overlay: bool = False) -> bytes:
    frontmatter = f"---\nname: {name}\ndescription: Synthetic {name} skill for live proofs.\n"
    if overlay:
        frontmatter += "disable-model-invocation: true\n"
    return (frontmatter + f"---\n\n# {name}\n\nDo the work.\n").encode("utf-8")


@dataclass(frozen=True)
class LiveEnv:
    """Immutable Git fixtures plus the approved policy that binds them."""

    config: Path
    policy: Path
    repo_a: Path
    repo_b: Path
    commit_a: str
    commit_b: str
    members_a: tuple[str, ...]
    members_b: tuple[str, ...]


def _policy_doc_for_store(
    base: Path, specs: list[tuple[str, str]]
) -> dict[str, object]:
    """Approve exactly the installed store bytes (overlay output included)."""
    reviews: list[dict[str, object]] = []
    for source, name in specs:
        artifact = base / name
        skill_raw = (artifact / "SKILL.md").read_bytes()
        skill_sha = hashlib.sha256(skill_raw).hexdigest()
        records: list[dict[str, object]] = []
        for child in sorted(artifact.rglob("*"), key=lambda p: str(p)):
            if not child.is_file():
                continue
            rel = str(child.relative_to(artifact))
            if rel in ("SKILL.md", ".agent-installer.json"):
                continue
            raw = child.read_bytes()
            executable = bool(child.stat().st_mode & stat.S_IXUSR)
            records.append(
                {
                    "disposition": default_disposition(rel, raw, executable),
                    "executable": executable,
                    "path": rel,
                    "reason": "synthetic live proof",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        records.sort(key=lambda item: str(item["path"]))
        reviews.append(
            {
                "files": records,
                "name": name,
                "reason": "synthetic live proof",
                "references": [],
                "requires": [],
                "skillSha256": skill_sha,
                "sourceIdentity": source,
            }
        )
    return {"kind": "coding-agent.skill-bundle-policy", "reviews": reviews, "schemaVersion": 1}


def _live_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveEnv:
    """Build two committed fixture repositories and the policy approving them.

    ``repo-a`` carries three skills (plain with hidden and nested files plus
    an executable, an overlay-added skill, an overlay-modified skill);
    ``repo-b`` carries the second source's skill. A scratch install learns the
    exact overlay output bytes so the policy approves precisely what the
    released installer writes.
    """
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    commit_a = _commit_repo(
        repo_a,
        {
            "skills/implement/SKILL.md": (_skill_md("implement"), False),
            "skills/implement/.hidden-notes": (b"hidden installer input\n", False),
            "skills/implement/docs/guide.md": (b"# guide\n\nPlain companion text.\n", False),
            "skills/implement/tools/run.sh": (b"#!/bin/sh\nexit 0\n", True),
            "skills/tdd/SKILL.md": (_skill_md("tdd", overlay=True), False),
            "skills/merged/SKILL.md": (_skill_md("merged", overlay=True), False),
            "skills/merged/agents/openai.yaml": (
                b"interface:\n  arguments:\n    - name: path\npolicy:\n  allow_implicit_invocation: true\n",
                False,
            ),
        },
    )
    commit_b = _commit_repo(
        repo_b,
        {"skills/cr/SKILL.md": (_skill_md("cr"), False)},
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{repo_a}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://example.com/org/skills-a.git")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{repo_b}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "https://example.com/org/skills-b.git")
    members_a = ("implement", "tdd", "merged")
    members_b = ("cr",)
    adapter = SubprocessInstallerAdapter()
    scratch = tmp_path / "scratch-home"
    scratch.mkdir(exist_ok=True)
    adapter.install_group(
        source_identity=SOURCE_A,
        https_locator="https://example.com/org/skills-a.git",
        resolved_commit=commit_a,
        selectors=tuple(f"skill:{name}" for name in members_a),
        home=scratch,
    )
    adapter.install_group(
        source_identity=SOURCE_B,
        https_locator="https://example.com/org/skills-b.git",
        resolved_commit=commit_b,
        selectors=tuple(f"skill:{name}" for name in members_b),
        home=scratch,
    )
    specs = [(SOURCE_A, name) for name in members_a] + [(SOURCE_B, name) for name in members_b]
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, _policy_doc_for_store(scratch / ".agents" / "skills", specs))
    config = tmp_path / "workflow.yml"
    config.write_text(
        minimal_config_text(
            [
                {
                    "include": list(members_a),
                    "ref": "HEAD",
                    "source": SOURCE_A,
                },
                {
                    "include": list(members_b),
                    "ref": commit_b,
                    "source": SOURCE_B,
                },
            ]
        ),
        encoding="utf-8",
    )
    return LiveEnv(
        config=config,
        policy=policy,
        repo_a=repo_a,
        repo_b=repo_b,
        commit_a=commit_a,
        commit_b=commit_b,
        members_a=members_a,
        members_b=members_b,
    )


def _read_state(home: Path) -> dict[str, Any]:
    state_path = home / ".agents" / "agent-installer" / "state.json"
    value = json.loads(state_path.read_bytes().decode("utf-8"))
    assert isinstance(value, dict)
    return value


@requires_real_installer
def test_real_fresh_install_two_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install records source, commit, version, format and policy."""
    env = _live_env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bundle_dir = tmp_path / "bundle"
    prepared = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    # The manifest records the version the binary reported, not the floor.
    assert manifest["producer"]["installerVersion"] == _binary_version()
    assert manifest["producer"]["installerReportSchemaVersion"] == 3
    assert [a["name"] for a in manifest["artifacts"]] == [
        "implement",
        "tdd",
        "merged",
        "cr",
    ]
    by_name = {a["name"]: a for a in manifest["artifacts"]}
    assert by_name["implement"]["resolvedCommit"] == env.commit_a
    assert by_name["cr"]["resolvedCommit"] == env.commit_b
    assert by_name["cr"]["sourceIdentity"] == SOURCE_B
    for artifact in manifest["artifacts"]:
        content = artifact["installerContent"]
        assert content["algorithm"] == SKILL_TREE_ALGORITHM_V2
        assert content["sourceHash"] == content["installedHash"]
    implement_files = {f["path"]: f for f in by_name["implement"]["files"]}
    assert set(["SKILL.md", ".hidden-notes", "docs/guide.md", "tools/run.sh"]) <= set(
        implement_files
    )
    assert implement_files["tools/run.sh"]["executable"] is True
    tdd_overlays = by_name["tdd"]["overlays"]
    assert tdd_overlays == [
        {"id": "codex-invocation-policy", "paths": ["agents/openai.yaml"], "version": "1"}
    ]
    assert any(f["path"] == "agents/openai.yaml" for f in by_name["tdd"]["files"])
    merged_overlays = by_name["merged"]["overlays"]
    assert merged_overlays == [
        {"id": "codex-invocation-policy", "paths": ["agents/openai.yaml"], "version": "1"}
    ]
    state = _read_state(home)
    assert state["version"] == 4
    assert manifest["resolutions"] == [
        {"ref": "HEAD", "resolvedCommit": env.commit_a, "sourceIdentity": SOURCE_A}
    ]
    assert verify_skill_bundle_deployment(
        config=_loaded_config(env.config), policy=env.policy, bundle_dir=bundle_dir, home=home
    ).ok


@requires_real_installer
def test_real_repeat_install_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated install verifies unchanged copies and keeps the records."""
    env = _live_env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    first_bundle = tmp_path / "bundle-a"
    first = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=first_bundle,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert first.ok, [str(fault) for fault in first.faults]
    second_bundle = tmp_path / "bundle-b"
    second = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=second_bundle,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert second.ok, [str(fault) for fault in second.faults]
    first_manifest = (first_bundle / "manifest.json").read_bytes()
    second_manifest = (second_bundle / "manifest.json").read_bytes()
    assert first_manifest == second_manifest
    assert json.loads(first_manifest) == json.loads(second_manifest)


@requires_real_installer
def test_real_offline_adoption_after_sources_vanish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption re-verifies the store with the network (sources) disabled."""
    env = _live_env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bundle_dir = tmp_path / "bundle"
    installed = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert installed.ok, [str(fault) for fault in installed.faults]
    env.repo_a.rename(tmp_path / "repo-a-gone")
    env.repo_b.rename(tmp_path / "repo-b-gone")
    offline_bundle = tmp_path / "offline-bundle"
    adopted = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=offline_bundle,
        home=home,
        mode="installed",
        resolution_manifest=bundle_dir / "manifest.json",
        installer=SubprocessInstallerAdapter(),
    )
    assert adopted.ok, [str(fault) for fault in adopted.faults]
    assert verify_skill_bundle_deployment(
        config=_loaded_config(env.config), policy=env.policy, bundle_dir=offline_bundle, home=home
    ).ok


@requires_real_installer
def test_real_shared_store_keeps_unrelated_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrelated store entries survive the declared-subset install (SKV3-10)."""
    env = _live_env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    repo_u = tmp_path / "repo-u"
    commit_u = _commit_repo(
        repo_u, {"skills/unrelated/SKILL.md": (_skill_md("unrelated"), False)}
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", f"url.{repo_u}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "https://example.com/org/skills-u.git")
    adapter = SubprocessInstallerAdapter()
    adapter.install_group(
        source_identity="git+https://example.com/org/skills-u.git",
        https_locator="https://example.com/org/skills-u.git",
        resolved_commit=commit_u,
        selectors=("skill:unrelated",),
        home=home,
    )
    bundle_dir = tmp_path / "bundle"
    prepared = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    assert (home / ".agents" / "skills" / "unrelated" / "SKILL.md").is_file()
    state = _read_state(home)
    assert isinstance(state.get("entries"), list)
    assert "unrelated" in {e["name"] for e in state["entries"] if isinstance(e, dict)}
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert "unrelated" not in [a["name"] for a in manifest["artifacts"]]


@requires_real_installer
def test_real_host_conflict_is_not_auto_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-installed unmanaged copy refuses instead of adopting (SKV3-36)."""
    env = _live_env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    squat = home / ".agents" / "skills" / "tdd"
    squat.mkdir(parents=True, exist_ok=True)
    (squat / "SKILL.md").write_bytes(b"# squatter\n")
    bundle_dir = tmp_path / "bundle"
    refused = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert not refused.ok
    assert refused.wrote == ()
    assert not (bundle_dir / "manifest.json").exists()
    assert any("conflict" in str(fault) for fault in refused.faults)
    assert (squat / "SKILL.md").read_bytes() == b"# squatter\n"


@requires_real_installer
def test_real_installed_mode_refuses_unmanaged_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed-mode adoption never auto-approves host-installed variants."""
    env = _live_env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bundle_dir = tmp_path / "bundle"
    installed = prepare_skill_bundle(
        config=_loaded_config(env.config),
        policy=env.policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert installed.ok, [str(fault) for fault in installed.faults]
    config = tmp_path / "ghost.yml"
    config.write_text(
        minimal_config_text([{"include": ["ghost"], "source": SOURCE_A}]),
        encoding="utf-8",
    )
    refused = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=env.policy,
        bundle_dir=tmp_path / "ghost-bundle",
        home=home,
        mode="installed",
        resolution_manifest=bundle_dir / "manifest.json",
        installer=SubprocessInstallerAdapter(),
    )
    assert not refused.ok
    assert any("ghost" in str(fault) for fault in refused.faults)
