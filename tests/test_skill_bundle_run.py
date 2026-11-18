"""``agent run`` on the prepared Skill Bundle deployment path (issue #395).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup in
order; a failure at any step exits non-zero with no Attempt opened and no
tracker write). ``agent run --policy/--bundle-dir/--skill-home`` repeats the
full offline deployment verification before any secret or live check, then
builds the activation registry from the same verified bytes; every declared
member is available on demand to every agent step, while static instructions
resolve declared names only.

Covered at the Phase-API seam (``verify_run_inputs`` and
``run_worker`` startup) and at the Worker-loop seam (activation from the
verified registry).

Matrix row ids: SKV3-04 (records; runtime part), SKV3-17 (``SKILL.md`` part),
SKV3-19.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from coding_agent import cli
from coding_agent.runtime.graph import workflow_graph
from coding_agent.skillbundle.activation import (
    UNKNOWN_SKILL_REASON,
    ActivationDelivered,
    ActivationRefused,
    ActivationRegistry,
    deliver_activation,
    lookup_activation,
)
from coding_agent.skillbundle.deployment import (
    DEPLOYMENT_BUNDLE_SOURCE_IDENTITY,
    RunVerificationRefusal,
    VerifiedRunInputs,
    prepare_skill_bundle,
    verify_run_inputs,
    verify_skill_bundle_deployment,
)
from coding_agent.skillbundle.installer import FakeInstallerAdapter, base_store_for, write_fake_store
from coding_agent.worker import run as worker_run
from coding_agent.worker import supervisor
from coding_agent.worker.ports import ProductionSkillBundlePort, WorkerPorts
from coding_agent.workflow.effective import skill_texts_from_registry
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from worker_fakes import WorkerFakes, make_worker_ports
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    fake_store_spec_for,
    skills_entries_for,
    write_policy_file,
)
from test_worker_run import (
    _assert_no_attempt_opened,
    _bare_remote,
    _tracker_with,
    _write_run_matrix,
)
from coding_agent.runtime.graph.workflow_graph import WorkflowResult


def _with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _run_config(
    *, skills_yaml: str = "", static_skill: str | None = None
) -> str:
    """A runnable ``version: 3`` config whose workflow is ``implement``."""
    instructions = ""
    if static_skill is not None:
        instructions = (
            "          instructions:\n"
            f"            - skill: {static_skill}\n"
        )
    return (
        "version: 3\n"
        "target:\n"
        "  kind: github\n"
        "  repository: example-owner/example-repo\n"
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
        "worker:\n"
        "  poll-interval-seconds: 5\n"
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        "    limits: {cost-usd: 6, tokens: 1500000,"
        " tool-calls: 300, wall-clock-minutes: 30}\n"
        "    start: work\n"
        "    defaults:\n"
        "      max-executions: 1\n"
        "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
        "    steps:\n"
        "      work:\n"
        "        agent:\n"
        f"{instructions}"
        "          tools: [workspace-read]\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: {end: completed}}\n"
        f"{skills_yaml}"
    )


def _skills_yaml(members: list[DeclaredMember]) -> str:
    """Render the ``skills:`` declaration for the declared members."""
    lines = ["skills:"]
    for entry in skills_entries_for(members):
        source = entry["source"]
        assert isinstance(source, str)
        lines.append(f"  - source: {source}")
        lines.append("    include:")
        include = entry["include"]
        assert isinstance(include, list)
        for name in include:
            lines.append(f"      - {name}")
        if "ref" in entry:
            lines.append(f"    ref: {entry['ref']}")
    return "\n".join(lines) + "\n"


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflow.yml"
    config.write_text(text, encoding="utf-8")
    return config


def _load_config(config: Path) -> LoadedWorkflowConfiguration:
    """Load the Workflow Configuration through the single loader."""
    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(f) for f in faults]
    return loaded


def _prepare_empty(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Prepare the empty deployment; returns (config, policy, bundle, home)."""
    config = _write_config(tmp_path, _run_config())
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([]))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    prepared = prepare_skill_bundle(
        config=_load_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    return config, policy, bundle_dir, home


def _prepare_members(
    tmp_path: Path, members: list[DeclaredMember], *, static_skill: str | None = None
) -> tuple[Path, Path, Path, Path]:
    """Prepare a non-empty deployment; returns (config, policy, bundle, home)."""
    config = _write_config(
        tmp_path, _run_config(skills_yaml=_skills_yaml(members), static_skill=static_skill)
    )
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))
    prepared = prepare_skill_bundle(
        config=_load_config(config),
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    return config, policy, bundle_dir, home


def _alpha_beta() -> list[DeclaredMember]:
    """Two declared members on distinct sources: alpha is static, beta is not."""
    return [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(name="beta", source=SOURCE_B, commit=COMMIT_B),
    ]


def _store_skill_md(home: Path, name: str) -> str:
    return (base_store_for(home) / name / "SKILL.md").read_text(encoding="utf-8")


# --- Phase-API seam: verify_run_inputs -----------------------------------------


def test_verify_for_run_collects_every_declared_member_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run seam builds one body per declared member from the verified
    bytes (SKV3-17, ``SKILL.md`` part)."""
    _with_env(monkeypatch)
    members = _alpha_beta()
    config, policy, bundle_dir, home = _prepare_members(tmp_path, members)

    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )

    assert isinstance(result, VerifiedRunInputs)
    assert tuple(member.name for member in result.members) == ("alpha", "beta")
    assert set(result.registry.bodies) == {"alpha", "beta"}
    for member in members:
        assert result.registry.bodies[member.name] == _store_skill_md(home, member.name)
    manifest_sha = hashlib.sha256((bundle_dir / "manifest.json").read_bytes()).hexdigest()
    assert result.manifest_sha256 == manifest_sha


def test_verify_for_run_empty_bundle_collects_no_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty deployment verifies and holds no members (SKV3-04, static
    part: there is nothing to name)."""
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _prepare_empty(tmp_path)

    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )

    assert isinstance(result, VerifiedRunInputs)
    assert result.registry.bodies == {}
    assert result.members == ()
    manifest_sha = hashlib.sha256((bundle_dir / "manifest.json").read_bytes()).hexdigest()
    assert result.manifest_sha256 == manifest_sha


def test_verify_for_run_failure_carries_faults_and_no_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken generation refuses with faults and builds no registry."""
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _prepare_empty(tmp_path)
    with (bundle_dir / "manifest.json").open("ab") as handle:
        handle.write(b" ")

    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )

    assert isinstance(result, RunVerificationRefusal)
    assert result.error is None
    assert result.faults


def test_verify_for_run_reopens_no_path_after_checking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry comes from the in-memory bytes verification already
    hashed: the run seam reads no more paths than the verify command."""
    _with_env(monkeypatch)
    members = _alpha_beta()
    config, policy, bundle_dir, home = _prepare_members(tmp_path, members)

    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text
    calls: list[str] = []

    def _counting_read_bytes(self: Path) -> bytes:
        calls.append(f"rb:{self}")
        return real_read_bytes(self)

    def _counting_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        calls.append(f"rt:{self}")
        return real_read_text(self, *args, **kwargs)

    loaded = _load_config(config)
    monkeypatch.setattr(Path, "read_bytes", _counting_read_bytes)
    monkeypatch.setattr(Path, "read_text", _counting_read_text)
    verify_skill_bundle_deployment(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    verify_count = len(calls)
    del calls[:]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )

    assert isinstance(result, VerifiedRunInputs)
    assert len(calls) == verify_count


# --- Startup seam: run_worker with a prepared deployment ------------------------


def _prepared_ports(
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    *,
    remote: Path | None = None,
) -> tuple[WorkerFakes, WorkerPorts]:
    """Fake GitHub/model ports; the deployment verification stays real."""
    _with_env(monkeypatch)
    fakes = make_worker_ports(
        model=ScriptedModel([]),
        tracker=tracker,
        repository_url=str(remote) if remote is not None else None,
    )
    ports = WorkerPorts(
        github=fakes.github,
        model=fakes.model,
        skills=ProductionSkillBundlePort(),
    )
    return fakes, ports


def _run_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeStepTracker,
    *,
    config: Path,
    deployment: supervisor.SkillDeploymentCheck,
    remote: Path | None = None,
    max_sweeps: int | None = 1,
    max_attempts: int | None = None,
) -> tuple[int, WorkerFakes]:
    fakes, ports = _prepared_ports(monkeypatch, tracker, remote=remote)
    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=deployment,
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=ports,
        sleep=lambda seconds: None,
        max_sweeps=max_sweeps,
        max_attempts=max_attempts,
    )
    return exit_code, fakes


def test_run_prepared_failure_refuses_before_secrets_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken deployment refuses at ``skill-bundle``: no secret check, no
    Attempt, no tracker write."""
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([]))
    config = _write_config(tmp_path, _run_config())
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=tmp_path / "missing-bundle", home=tmp_path / "home"
    )
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))

    exit_code, fakes = _run_prepared(
        tmp_path, monkeypatch, tracker, config=config, deployment=deployment
    )

    assert exit_code == 1
    assert fakes.order == []
    assert fakes.github.tracker_calls == []
    assert tracker.calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    err = capsys.readouterr().err
    assert "run: startup refused at skill-bundle" in err
    assert "Skill Bundle deployment verification failed" in err


def test_run_prepared_failure_precedes_the_credential_presence_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Startup order: deployment verification runs before the selected-secret
    presence check, so a broken deployment refuses even with no token set."""
    _with_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.delenv("GITHUB_TOKEN")
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document([]))
    config = _write_config(tmp_path, _run_config())
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=tmp_path / "missing-bundle", home=tmp_path / "home"
    )
    fakes = make_worker_ports(
        model=ScriptedModel([]),
        tracker=FakeStepTracker(),
    )
    ports = WorkerPorts(
        github=fakes.github, model=fakes.model, skills=ProductionSkillBundlePort()
    )
    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=deployment,
        toolchain_matrix_path=_write_run_matrix(tmp_path),
        ports=ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    assert fakes.order == []
    assert fakes.github.tracker_calls == []
    err = capsys.readouterr().err
    assert "run: startup refused at skill-bundle" in err
    assert "is not set" not in err


def test_run_prepared_empty_bundle_reaches_ready_with_the_deployment_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty deployment verifies; the v2 pinned record carries no members
    and no activation deliveries."""
    from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION

    config, policy, bundle_dir, home = _prepare_empty(tmp_path)
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=bundle_dir, home=home
    )
    captured: dict[str, Any] = {}

    def _capture_loop(**kwargs: Any) -> int:
        captured["effective_configuration"] = kwargs["loop_config"].claim_config.effective_configuration
        return 0

    monkeypatch.setattr(worker_run, "run_worker_loop", _capture_loop)

    exit_code, _built = _run_prepared(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, deployment=deployment
    )

    assert exit_code == 0
    record = captured["effective_configuration"]
    assert record["workflow"]["id"] == "implement"
    assert record["skill_bundle"] == {"members": {}}
    assert record["activations"] == {}
    run_inputs = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(run_inputs, VerifiedRunInputs)
    assert run_inputs.framing_version == DELIVERY_FRAMING_VERSION


def test_run_prepared_static_skill_without_a_declaration_refuses_at_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With an empty bundle a static skill reference cannot exist: the
    configuration step refuses the undeclared name (SKV3-04, static part)."""
    _with_env(monkeypatch)
    _prepare_empty(tmp_path)
    policy = tmp_path / "skill-policy.json"
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=tmp_path / "bundle", home=tmp_path / "home"
    )
    # The run config names the static skill while the prepared (empty)
    # generation declares nothing.
    run_config = tmp_path / "run.yml"
    run_config.write_text(_run_config(static_skill="alpha"), encoding="utf-8")

    exit_code, fakes = _run_prepared(
        tmp_path, monkeypatch, FakeStepTracker(), config=run_config, deployment=deployment
    )

    assert exit_code == 1
    assert fakes.order == []
    assert fakes.github.tracker_calls == []
    _assert_no_attempt_opened(tmp_path / "state" / "run-ledger.sqlite3")
    assert "run: startup refused at configuration" in capsys.readouterr().err


def test_run_prepared_build_only_path_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run`` never publishes a generation: with preparation itself broken,
    a prepared deployment still runs to readiness."""
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _prepare_empty(tmp_path)
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=bundle_dir, home=home
    )

    def _boom(**kwargs: Any) -> Any:
        raise AssertionError("the build-only prepare path must not run")

    from coding_agent.skillbundle import deployment as deployment_module

    monkeypatch.setattr(deployment_module, "prepare_skill_bundle", _boom)

    exit_code, _built = _run_prepared(
        tmp_path, monkeypatch, FakeStepTracker(), config=config, deployment=deployment
    )

    assert exit_code == 0


def test_run_prepared_missing_matrix_refuses_before_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Supported Toolchain Matrix readiness check is unchanged: a missing
    matrix refuses before the Skill Bundle step runs."""
    _with_env(monkeypatch)
    config, policy, bundle_dir, home = _prepare_empty(tmp_path)
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=bundle_dir, home=home
    )
    fakes = make_worker_ports(
        model=ScriptedModel([]),
        tracker=FakeStepTracker(),
    )
    ports = WorkerPorts(
        github=fakes.github, model=fakes.model, skills=ProductionSkillBundlePort()
    )

    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=tmp_path / "state",
        skill_deployment=deployment,
        toolchain_matrix_path=tmp_path / "missing-matrix.json",
        ports=ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    assert fakes.order == []
    assert "Supported Toolchain Matrix" in capsys.readouterr().err


def test_run_prepared_end_to_end_registry_activation_and_effective_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker-loop seam: the Attempt's registry holds exactly the declared,
    verified members; the on-demand-only member activates (SKV3-19) and the
    pinned record carries the declared set."""
    _with_env(monkeypatch)
    members = _alpha_beta()
    config, policy, bundle_dir, home = _prepare_members(
        tmp_path, members, static_skill="alpha"
    )
    deployment = supervisor.SkillDeploymentCheck(
        policy=policy, bundle_dir=bundle_dir, home=home
    )
    captured: dict[str, Any] = {}
    real_run_worker_loop = worker_run.run_worker_loop

    def _capture_loop(**kwargs: Any) -> int:
        captured["effective_configuration"] = kwargs[
            "loop_config"
        ].claim_config.effective_configuration
        return real_run_worker_loop(**kwargs)

    monkeypatch.setattr(worker_run, "run_worker_loop", _capture_loop)

    def _stub_workflow(**kwargs: Any) -> WorkflowResult:
        captured["agent_deps"] = kwargs["agent_deps"]
        ledger = kwargs["ledger"]
        attempt_id = str(kwargs["attempt_id"])
        ledger.record_terminal_outcome(
            attempt_id, outcome="completed", reason="verified-completion"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="completed",
            reason="verified-completion",
            ending_step="work",
            step_result="done",
            workflow_end="completed",
        )

    monkeypatch.setattr(workflow_graph, "run_workflow", _stub_workflow)
    tracker = _tracker_with((40, "2026-10-01T00:00:00Z"))
    remote = _bare_remote(tmp_path)

    exit_code, fakes = _run_prepared(
        tmp_path,
        monkeypatch,
        tracker,
        config=config,
        deployment=deployment,
        remote=remote,
        max_sweeps=None,
        max_attempts=1,
    )

    assert exit_code == 0
    assert fakes.order == ["github", "provider"]
    registry = captured["agent_deps"].activation_registry
    assert isinstance(registry, ActivationRegistry)
    assert set(registry.bodies) == {"alpha", "beta"}
    for member in members:
        assert registry.bodies[member.name] == _store_skill_md(home, member.name)
    # The on-demand-only member (named by no static instruction) activates.
    beta = lookup_activation(registry, "beta")
    assert isinstance(beta, ActivationDelivered)
    assert beta.text == _store_skill_md(home, "beta")
    # The v2 identity core covers every available member with exact
    # activation deliveries under the versioned framing.
    from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION

    record = captured["effective_configuration"]
    assert set(record["skill_bundle"]["members"]) == {"alpha", "beta"}
    assert set(record["activations"]) == {"alpha", "beta"}
    for member in members:
        expected = _store_skill_md(home, member.name)
        assert record["activations"][member.name]["framing_version"] == (
            DELIVERY_FRAMING_VERSION
        )
        assert record["activations"][member.name]["sha256"] == hashlib.sha256(
            expected.encode("utf-8")
        ).hexdigest()


# --- Worker-loop seam: activation from the verified registry --------------------


def test_on_demand_only_member_activates_from_verified_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every declared member is available on demand (SKV3-19), whether or
    not a static instruction names it."""
    _with_env(monkeypatch)
    members = _alpha_beta()
    config, policy, bundle_dir, home = _prepare_members(
        tmp_path, members, static_skill="alpha"
    )
    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    registry = result.registry

    outcome = lookup_activation(registry, "beta")

    assert isinstance(outcome, ActivationDelivered)
    assert outcome.text == _store_skill_md(home, "beta")
    activated: set[str] = set()
    first = deliver_activation(registry, "beta", activated)
    assert isinstance(first, ActivationDelivered)
    second = deliver_activation(registry, "beta", activated)
    assert not isinstance(second, ActivationDelivered)
    assert second.text == "Skill 'beta' is already active."


def test_empty_registry_refuses_on_demand_activation_with_a_typed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an empty bundle on-demand activation refuses with a typed
    outcome and never touches the filesystem (SKV3-04, runtime)."""
    _with_env(monkeypatch)
    registry = ActivationRegistry(bodies={})

    outcome = lookup_activation(registry, "alpha")

    assert isinstance(outcome, ActivationRefused)
    assert outcome.reason == UNKNOWN_SKILL_REASON
    assert "not available" in outcome.text
    activated: set[str] = set()
    refused = deliver_activation(registry, "alpha", activated)
    assert isinstance(refused, ActivationRefused)
    assert activated == set()
    with pytest.raises(OSError):
        skill_texts_from_registry(registry, ["alpha"])


def test_activation_delivers_the_complete_skill_md_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation delivers the complete ``SKILL.md`` verbatim (SKV3-17,
    ``SKILL.md`` part): byte-identical to the verified store content."""
    _with_env(monkeypatch)
    members = _alpha_beta()
    config, policy, bundle_dir, home = _prepare_members(tmp_path, members)
    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    registry = result.registry

    for member in members:
        raw = (base_store_for(home) / member.name / "SKILL.md").read_bytes()
        delivered = lookup_activation(registry, member.name)
        assert isinstance(delivered, ActivationDelivered)
        assert delivered.text == raw.decode("utf-8")
        assert delivered.text == result.registry.bodies[member.name]


def test_static_texts_resolve_declared_names_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``skill_texts_from_registry`` serves declared names verbatim and
    raises for anything else: static instructions cannot reach past the
    verified membership."""
    _with_env(monkeypatch)
    members = _alpha_beta()
    config, policy, bundle_dir, home = _prepare_members(tmp_path, members)
    result = verify_run_inputs(
        config=_load_config(config), policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs)
    registry = result.registry

    assert skill_texts_from_registry(registry, ["alpha"]) == {
        "alpha": _store_skill_md(home, "alpha")
    }
    with pytest.raises(OSError):
        skill_texts_from_registry(registry, ["gamma"])


# --- CLI seam: the prepared run flags --------------------------------------------


def test_run_prepared_flags_reach_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--policy/--bundle-dir/--skill-home`` map to a deployment check while
    ``--workflow/--toolchain-matrix/--state-dir`` pass through unchanged."""
    captured: dict[str, Any] = {}

    def fake_run_worker(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        [
            "run",
            "--config",
            "workflow.yml",
            "--workflow",
            "implement",
            "--state-dir",
            "state",
            "--toolchain-matrix",
            "/opt/matrix.json",
            "--policy",
            "/opt/policy.json",
            "--bundle-dir",
            "/opt/prepared",
            "--skill-home",
            "/home/deployed",
        ]
    )

    assert exit_code == 0
    assert captured["workflow"] == "implement"
    assert captured["state_dir"] == tmp_path / "state"
    assert captured["toolchain_matrix_path"] == Path("/opt/matrix.json")
    assert captured["skill_deployment"] == supervisor.SkillDeploymentCheck(
        policy=Path("/opt/policy.json"),
        bundle_dir=Path("/opt/prepared"),
        home=Path("/home/deployed"),
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--policy", "/opt/policy.json"],
        ["--bundle-dir", "/opt/prepared"],
        ["--skill-home", "/home/deployed"],
        ["--policy", "/opt/policy.json", "--bundle-dir", "/opt/prepared"],
        ["--policy", "/opt/policy.json", "--skill-home", "/home/deployed"],
        ["--bundle-dir", "/opt/prepared", "--skill-home", "/home/deployed"],
    ],
)
def test_run_partial_prepared_flags_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    """The prepared flags are all required: a partial set is a usage error."""
    monkeypatch.setattr(cli, "run_worker", lambda **kwargs: 0)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "--config", "w.yml", "--workflow", "implement", *extra])

    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "flag",
    ["--skip-bundle", "--skip-credentials", "--build-mode"],
)
def test_run_has_no_skip_or_build_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """``run`` has no skip-bundle, skip-credentials or build-mode switch:
    the phase API makes the build-only path unreachable from ``run``."""
    monkeypatch.setattr(cli, "run_worker", lambda **kwargs: 0)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "run",
                "--config",
                "w.yml",
                "--workflow",
                "implement",
                "--policy",
                "/opt/policy.json",
                "--bundle-dir",
                "/opt/bundle",
                "--skill-home",
                "/home/agent",
                flag,
            ]
        )

    assert excinfo.value.code == 2
