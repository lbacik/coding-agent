"""First end-to-end deployment path for an empty Skill Bundle (SKV3).

Contract: installation resolution (issue #373) and phased validation (issue
#375, as amended by issue #383): ``prepare-skill-bundle --mode installed``
publishes ``requirements.json``, ``manifest.json`` and ``validation.json`` as
one completed generation; ``verify-skill-bundle`` re-checks the prepared
deployment offline and read-only. Covered at the Phase-API seam with real
deployment roots under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.skillbundle.deployment import (
    prepare_skill_bundle,
    verify_skill_bundle_deployment,
)


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _minimal_v3_no_skills(extra_workflows: str = "") -> str:
    base = (
        "version: 3\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: review\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      review:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            reviewed: {body: document}\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )
    return base + extra_workflows


def _minimal_v3_with_prompt(prompt: str) -> str:
    return (
        "version: 3\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: review\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      review:\n"
        "        agent:\n"
        "          instructions:\n"
        f"            - prompt: {prompt}\n"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            reviewed: {body: document}\n"
        "        routes: {reviewed: publish}\n"
        "      publish:\n"
        "        operation: publish-review\n"
        "        with: {body: review.reviewed.body}\n"
        "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
    )


def _write_roots(
    tmp_path: Path, *, config_text: str, prompt_files: dict[str, bytes] | None = None
) -> tuple[Path, Path, Path, Path]:
    config = tmp_path / "workflow.yml"
    config.write_text(config_text, encoding="utf-8")
    if prompt_files:
        for name, raw in prompt_files.items():
            (tmp_path / name).write_bytes(raw)
    policy = tmp_path / "skill-policy.json"
    policy.write_text(
        json.dumps(
            {"kind": "coding-agent.skill-bundle-policy", "reviews": [], "schemaVersion": 1},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir()
    return config, policy, bundle_dir, home


def test_prepare_and_verify_empty_bundle_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())

    prepared = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    assert (bundle_dir / "requirements.json").is_file()
    assert (bundle_dir / "manifest.json").is_file()
    assert (bundle_dir / "validation.json").is_file()

    # Canonical JSON: sorted keys, compact, literal non-ASCII, no trailing newline.
    for name in ("requirements.json", "manifest.json", "validation.json"):
        raw = (bundle_dir / name).read_bytes()
        assert not raw.endswith(b"\n")
        raw.decode("utf-8")
        parsed = json.loads(raw)
        assert parsed["schemaVersion"] == 1
        assert raw == json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()

    requirements = json.loads((bundle_dir / "requirements.json").read_bytes())
    assert requirements["kind"] == "coding-agent.skill-requirements"
    assert requirements["sources"] == []
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    assert manifest["kind"] == "coding-agent.skill-bundle-manifest"
    assert manifest["artifacts"] == []
    assert manifest["resolutions"] == []
    validation = json.loads((bundle_dir / "validation.json").read_bytes())
    assert validation["kind"] == "coding-agent.prepared-validation"
    assert validation["prompts"] == []
    assert validation["workflowSha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert validation["policySha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert validation["requirementsSha256"] == hashlib.sha256(
        (bundle_dir / "requirements.json").read_bytes()
    ).hexdigest()
    assert validation["manifestSha256"] == hashlib.sha256(
        (bundle_dir / "manifest.json").read_bytes()
    ).hexdigest()

    verified = verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert verified.ok, [str(f) for f in verified.faults]


def test_config_validate_neither_requires_nor_creates_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_env(monkeypatch)
    config = tmp_path / "workflow.yml"
    config.write_text(_minimal_v3_no_skills(), encoding="utf-8")
    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert not (tmp_path / "bundle").exists()
    assert not (tmp_path / "skill-policy.json").exists()
    assert "policy" not in capsys.readouterr().out.lower()


def test_prompts_are_bound_and_relocated_tree_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    raw_prompt = "line one\r\ncafé ✓\r\n".encode("utf-8")
    config, policy, bundle_dir, home = _write_roots(
        tmp_path,
        config_text=_minimal_v3_with_prompt("notes/prompt.md"),
        prompt_files={"notes/prompt.md": raw_prompt} if False else None,
    )
    # Write nested prompt file.
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "prompt.md").write_bytes(raw_prompt)
    prepared = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    validation = json.loads((bundle_dir / "validation.json").read_bytes())
    assert validation["prompts"] == [
        {"reference": "notes/prompt.md", "sha256": hashlib.sha256(raw_prompt).hexdigest()}
    ]

    # Relocate the identical tree to new roots: verification succeeds.
    new_root = tmp_path / "relocated"
    new_root.mkdir()
    new_config = new_root / "workflow.yml"
    new_config.write_bytes(config.read_bytes())
    (new_root / "notes").mkdir()
    (new_root / "notes" / "prompt.md").write_bytes(raw_prompt)
    new_policy = new_root / "skill-policy.json"
    new_policy.write_bytes(policy.read_bytes())
    new_bundle = new_root / "bundle"
    shutil.copytree(bundle_dir, new_bundle)
    new_home = new_root / "home"
    new_home.mkdir()
    relocated = verify_skill_bundle_deployment(
        config=new_config, policy=new_policy, bundle_dir=new_bundle, home=new_home
    )
    assert relocated.ok, [str(f) for f in relocated.faults]

    # An incomplete copy (prompt missing) refuses.
    (new_root / "notes" / "prompt.md").unlink()
    incomplete = verify_skill_bundle_deployment(
        config=new_config, policy=new_policy, bundle_dir=new_bundle, home=new_home
    )
    assert not incomplete.ok


def test_verify_refuses_semantically_equivalent_byte_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    raw_prompt = b"hello\n"
    config, policy, bundle_dir, home = _write_roots(
        tmp_path, config_text=_minimal_v3_with_prompt("prompt.md")
    )
    (tmp_path / "prompt.md").write_bytes(raw_prompt)
    assert prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    before = (bundle_dir / "validation.json").read_bytes()
    # Semantically equivalent YAML (extra comment) changes raw bytes: refuse.
    config.write_text(config.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
    refused = verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert not refused.ok
    # Records are never refreshed or repaired by verification.
    assert (bundle_dir / "validation.json").read_bytes() == before


def test_failure_leaves_no_new_generation_and_old_generation_must_revalidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    first = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert first.ok
    first_validation = (bundle_dir / "validation.json").read_bytes()
    # Break the configuration: preparation fails and leaves no new generation.
    config.write_text("not: [valid\n", encoding="utf-8")
    failed = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not failed.ok
    assert (bundle_dir / "validation.json").read_bytes() == first_validation
    # The earlier generation is not trusted blindly: it is invalid now.
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_strict_record_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    assert prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    # Duplicate keys in a generated record are refused.
    (bundle_dir / "requirements.json").write_bytes(b'{"kind":"x","kind":"y"}')
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok
    # Unknown fields are refused.
    (bundle_dir / "requirements.json").write_bytes(
        json.dumps(
            {
                "kind": "coding-agent.skill-requirements",
                "schemaVersion": 1,
                "sources": [],
                "workflowConfigVersion": 3,
                "extra": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_cli_prepare_and_verify_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    assert (
        cli.main(
            [
                "prepare-skill-bundle",
                "--mode",
                "installed",
                "--config",
                str(config),
                "--policy",
                str(policy),
                "--bundle-dir",
                str(bundle_dir),
                "--home",
                str(home),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "verify-skill-bundle",
                "--config",
                str(config),
                "--policy",
                str(policy),
                "--bundle-dir",
                str(bundle_dir),
                "--home",
                str(home),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "configuration" in out and "policy" in out and "bundle" in out


def test_cli_exit_codes_and_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    Path("workflow.yml").write_text(_minimal_v3_no_skills(), encoding="utf-8")
    Path("skill-policy.json").write_text(
        json.dumps({"kind": "coding-agent.skill-bundle-policy", "reviews": [], "schemaVersion": 1}),
        encoding="utf-8",
    )
    Path("home").mkdir()
    # Relative CLI paths resolve against the invocation directory.
    assert (
        cli.main(
            [
                "prepare-skill-bundle",
                "--mode",
                "installed",
                "--config",
                "workflow.yml",
                "--policy",
                "skill-policy.json",
                "--bundle-dir",
                "bundle",
                "--home",
                "home",
            ]
        )
        == 0
    )
    # Missing required input is usage (exit 2).
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["prepare-skill-bundle", "--mode", "installed"])
    assert excinfo.value.code == 2
    # Bad mode is usage (exit 2).
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "prepare-skill-bundle",
                "--mode",
                "bogus",
                "--config",
                "workflow.yml",
                "--policy",
                "skill-policy.json",
                "--bundle-dir",
                "bundle",
                "--home",
                "home",
            ]
        )
    assert excinfo.value.code == 2
    # A refusal exits 1 and names the phase without echoing secrets.
    Path("workflow.yml").write_text("not: [valid\n", encoding="utf-8")
    capsys.readouterr()
    assert (
        cli.main(
            [
                "prepare-skill-bundle",
                "--mode",
                "installed",
                "--config",
                "workflow.yml",
                "--policy",
                "skill-policy.json",
                "--bundle-dir",
                "bundle",
                "--home",
                "home",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "prepare-skill-bundle" in err and "configuration" in err


def _prepare_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    result = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert result.ok, [str(f) for f in result.faults]
    return config, policy, bundle_dir, home


def test_records_reject_nulls_bad_digests_and_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, policy, bundle_dir, home = _prepare_ok(tmp_path, monkeypatch)
    good_req = (bundle_dir / "requirements.json").read_bytes()

    # Nulls refused.
    (bundle_dir / "requirements.json").write_bytes(b'{"kind":null,"schemaVersion":1,"sources":[],"workflowConfigVersion":3}')
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok

    # Malformed digests refused (manifest requirementsSha256).
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    manifest["requirementsSha256"] = "not-a-digest"
    (bundle_dir / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    )
    (bundle_dir / "requirements.json").write_bytes(good_req)
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok

    # Unknown versions refused.
    validation = json.loads((bundle_dir / "validation.json").read_bytes())
    validation["schemaVersion"] = 2
    (bundle_dir / "validation.json").write_bytes(
        json.dumps(validation, sort_keys=True, separators=(",", ":")).encode()
    )
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_records_reject_inconsistent_cross_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, policy, bundle_dir, home = _prepare_ok(tmp_path, monkeypatch)
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    manifest["requirementsSha256"] = "0" * 64
    (bundle_dir / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    )
    result = verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert not result.ok
    assert any("bind" in str(f) for f in result.faults)


def test_non_canonical_record_bytes_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, policy, bundle_dir, home = _prepare_ok(tmp_path, monkeypatch)
    parsed = json.loads((bundle_dir / "requirements.json").read_bytes())
    pretty = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    (bundle_dir / "requirements.json").write_bytes(pretty.encode())
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok


def test_policy_bytes_change_refused_even_when_semantically_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, policy, bundle_dir, home = _prepare_ok(tmp_path, monkeypatch)
    before = (bundle_dir / "validation.json").read_bytes()
    # Same JSON value, different raw bytes (pretty-printed): binding refuses.
    parsed = json.loads(policy.read_bytes())
    policy.write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok
    assert (bundle_dir / "validation.json").read_bytes() == before


def test_prompt_bytes_change_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(
        tmp_path, config_text=_minimal_v3_with_prompt("prompt.md")
    )
    (tmp_path / "prompt.md").write_bytes(b"hello\n")
    assert prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    # Same semantic text with different line ending still changes raw bytes.
    (tmp_path / "prompt.md").write_bytes(b"hello\r\n")
    refused = verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert not refused.ok
    assert any("prompt" in str(f).lower() for f in refused.faults)


def test_policy_strictness_and_no_secret_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    secret_source = "https://user:secret-pass@github.com/org/repo.git"
    policy.write_bytes(
        json.dumps(
            {
                "kind": "coding-agent.skill-bundle-policy",
                "reviews": [],
                "schemaVersion": 1,
            }
        ).encode()
    )
    # Credential-bearing source in a review is refused without echoing the secret.
    bad = {
        "kind": "coding-agent.skill-bundle-policy",
        "reviews": [
            {
                "files": [],
                "name": "s",
                "reason": "r",
                "references": [],
                "requires": [],
                "skillSha256": "0" * 64,
                "sourceIdentity": "git+" + secret_source,
            }
        ],
        "schemaVersion": 1,
    }
    policy.write_bytes(json.dumps(bad).encode())
    result = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not result.ok
    assert "secret-pass" not in "\n".join(str(f) for f in result.faults)
    # Unknown policy fields are refused.
    policy.write_bytes(
        json.dumps(
            {"kind": "coding-agent.skill-bundle-policy", "reviews": [], "schemaVersion": 1, "extra": 1}
        ).encode()
    )
    assert not prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok


def test_independent_faults_collected_dependent_checks_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    assert prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    ).ok
    # Corrupt the policy file (independent stage) and delete validation.json.
    policy.write_bytes(b"not json")
    (bundle_dir / "validation.json").unlink()
    result = verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert not result.ok
    phases = {f.phase for f in result.faults}
    assert "policy" in phases
    assert "records" in phases
    # Binding is dependent: it must not be reported as passing.
    assert not any("prepared deployment verified" in str(f) for f in result.faults)


def test_prepare_collects_configuration_and_policy_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _write_roots(tmp_path, config_text=_minimal_v3_no_skills())
    config.write_bytes(b"not: [valid yaml")
    policy.write_bytes(b"not json")
    result = prepare_skill_bundle(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed"
    )
    assert not result.ok
    phases = {f.phase for f in result.faults}
    assert "configuration" in phases
    assert "policy" in phases


def test_manifest_rejects_non_one_installer_report_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, policy, bundle_dir, home = _prepare_ok(tmp_path, monkeypatch)
    manifest = json.loads((bundle_dir / "manifest.json").read_bytes())
    manifest["producer"]["installerReportSchemaVersion"] = 2
    (bundle_dir / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert not verify_skill_bundle_deployment(
        config=config, policy=policy, bundle_dir=bundle_dir, home=home
    ).ok
