"""The ``agent`` command line: one subcommand per Worker capability.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.9
(``config validate`` is static and offline), §4 (``run`` is the Worker:
startup in order, then the poll loop), §18 (``preflight`` and
``startup-check`` are the operator's write probes and the non-mutating
startup checks) and ADR 0009 (``provider-check`` is the Provider
Capability Assertion run alone).

Matrix row ids: CFG-38, REC-11, OPC-8, OP-15, OP-16.

``run --config <file> --workflow <id>`` is the only command that opens an
Attempt; ``worker.supervisor`` wires it. ``config validate`` checks a
``version: 4`` step-workflow file offline: no Attempt, no network.
``preflight``, ``startup-check``, ``provider-check``,
``verify-skill-bundle`` and ``validate`` are operator tools.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coding_agent.workflow.configuration import LoadedWorkflowConfiguration

from coding_agent.env import EnvFileError, load_application_environment
from coding_agent.identity.startup import StartupCheckFailed
from coding_agent.identity.token import TokenRejected
from coding_agent.inputs import github as github_adapter
from coding_agent.preflight.probes import run_preflight
from coding_agent.profile.parser import (
    InvalidProfile,
    MissingReadinessFacts,
    UnknownSchema,
    parse_profile_yaml,
)
from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    ProviderCapabilityReport,
    assert_provider_capability,
    redact_report,
)
from coding_agent.provider.pinned_model import build_chat_model
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.worker.startup import PROVIDER_KEY_ENV_VARS
from coding_agent.validate import (
    CommandContext,
    RawCommandResult,
    SubprocessCommandRunner,
    classify,
    run_targeted_test,
    run_validation_contract,
)
from coding_agent.worker.supervisor import (
    SkillDeploymentCheck,
    run_worker,
)
from coding_agent.workflow.configuration import load_workflow_configuration
from coding_agent.workflow.jsonschema import dump_workflow_json_schema


def split_repo(value: str) -> tuple[str, str]:
    owner, _, repo = value.partition("/")
    if not owner or not repo:
        raise argparse.ArgumentTypeError(f"expected OWNER/REPO, got {value!r}")
    return owner, repo


def resolve_state_dir(value: str) -> Path:
    """Resolve a `--state-dir` argument to an absolute path.

    A relative path resolves against the CLI process's cwd, but `evidence_dir`
    (derived from `state_dir`) is later interpolated into Validation Contract
    shell commands that run with a different cwd (the checked-out workspace).
    Left relative, evidence lands in and pollutes the Target Repository's
    workspace instead of under `--state-dir` (issue #38).
    """
    return Path(value).absolute()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument(
        "--app-env-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Supplement the implicit `.env` with environment variables from this file "
        "before dispatching any command. Useful where a launcher (e.g. a PyCharm uv run "
        "configuration) does not forward uv's own --env-file. Existing environment "
        "variables and values from `.env` still take precedence. A missing or unreadable "
        "file is a CLI error.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="Every write probe contract §18's Preflight subsection requires, "
        "run against a sandbox repository. Never run this against the real "
        "Target Repository.",
    )
    startup_check = subparsers.add_parser(
        "startup-check",
        help="The non-mutating startup checks contract §18 requires. No write is performed.",
    )

    run = subparsers.add_parser(
        "run",
        help="The Worker (step-workflow contract §4): start in order — "
        "validate the version: 4 Workflow Configuration, verify the Skill "
        "Bundle, run the non-mutating GitHub checks, run the Provider "
        "Capability Assertion, open the Run Ledger — then select, claim and "
        "deliver one input at a time until a halt. A startup refusal exits "
        "non-zero with no Attempt and no GitHub write.",
    )
    run.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="Path to the Workflow Configuration YAML file (contract §2).",
    )
    run.add_argument(
        "--workflow",
        required=True,
        metavar="ID",
        help="Which Workflow of --config to run; the whole file is always "
        "validated and the whole `skills` declaration always counts as the "
        "bundle membership. An unknown id lists the available ids.",
    )
    run.add_argument(
        "--state-dir",
        type=resolve_state_dir,
        default=Path("/var/lib/coding-agent"),
        metavar="PATH",
        help="Root of the Run Ledger, the mirror, the workspace and the evidence "
        "(default: /var/lib/coding-agent). A relative path is resolved to absolute "
        "against the current directory.",
    )
    run.add_argument(
        "--toolchain-matrix",
        type=Path,
        metavar="PATH",
        help="Path to the Supported Toolchain Matrix. In the image this defaults to "
        "/opt/coding-agent/toolchain-matrix.json; locally use the file generated by "
        "scripts/generate-toolchain-matrix.py.",
    )
    run.add_argument(
        "--policy",
        type=Path,
        required=True,
        metavar="FILE",
        help="Path to skill-policy.json (prepared Skill Bundle deployment). "
        "Startup repeats the full offline deployment verification before any "
        "secret or live check and builds the activation registry from the "
        "verified bytes.",
    )
    run.add_argument(
        "--bundle-dir",
        type=Path,
        required=True,
        metavar="PATH",
        help="Directory holding the prepared requirements.json, manifest.json "
        "and validation.json (prepared Skill Bundle deployment).",
    )
    run.add_argument(
        "--skill-home",
        type=Path,
        required=True,
        metavar="PATH",
        help="The runtime user's home directory whose managed store the "
        "prepared deployment is verified against.",
    )
    for sub in (preflight, startup_check):
        sub.add_argument("--repo", required=True, metavar="OWNER/REPO")
        sub.add_argument(
            "--token-env",
            default="GITHUB_TOKEN",
            help="Environment variable holding the credential (default: GITHUB_TOKEN).",
        )

    provider_check = subparsers.add_parser(
        "provider-check",
        help="The Provider Capability Assertion (issue #31, ADR 0009): one live call "
        "proving the Pinned Model can call a tool, honours the pinned effort, and "
        "reports non-zero usage, plus that a Price Table entry exists for it. No "
        "GitHub access; a refusal exits nonzero with nothing opened.",
    )
    provider_check.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="Path to the Workflow Configuration YAML file holding the pin.",
    )
    provider_check.add_argument(
        "--provider",
        required=True,
        metavar="PIN",
        help="Which configured Pinned Model (models.<id>) to assert against.",
    )

    prepare_skill_bundle = subparsers.add_parser(
        "prepare-skill-bundle",
        help="Prepare the Skill Bundle deployment (SKV3): validate the "
        "configuration, policy and prompts, then publish requirements.json, "
        "manifest.json and validation.json as one completed generation. "
        "No network in 'installed' for an empty bundle; no GitHub access.",
    )
    prepare_skill_bundle.add_argument(
        "--mode",
        required=True,
        choices=("installed", "install"),
        help="Preparation mode: 'installed' adopts the existing store "
        "offline, 'install' resolves and installs (empty bundle: no store writes).",
    )
    prepare_skill_bundle.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="Path to the Workflow Configuration YAML file.",
    )
    prepare_skill_bundle.add_argument(
        "--policy",
        required=True,
        type=Path,
        metavar="FILE",
        help="Path to skill-policy.json (kind coding-agent.skill-bundle-policy).",
    )
    prepare_skill_bundle.add_argument(
        "--bundle-dir",
        required=True,
        type=Path,
        metavar="PATH",
        help="Directory holding the generated requirements.json, manifest.json "
        "and validation.json.",
    )
    prepare_skill_bundle.add_argument(
        "--home",
        required=True,
        type=Path,
        metavar="PATH",
        help="The runtime user's home directory the Skill Bundle is installed under.",
    )
    prepare_skill_bundle.add_argument(
        "--resolution-manifest",
        required=False,
        default=None,
        type=Path,
        metavar="FILE",
        help="Optional previous manifest carrying saved symbolic-ref resolutions.",
    )
    verify_skill_bundle = subparsers.add_parser(
        "verify-skill-bundle",
        help="Verify the prepared Skill Bundle deployment "
        "(--config/--policy/--bundle-dir/--home) offline and read-only. "
        "No GitHub access.",
    )
    verify_skill_bundle.add_argument(
        "--home",
        required=True,
        default=None,
        type=Path,
        metavar="PATH",
        help="The runtime user's home directory the Skill Bundle was installed into.",
    )
    verify_skill_bundle.add_argument(
        "--config",
        required=True,
        default=None,
        type=Path,
        metavar="FILE",
        help="Path to the Workflow Configuration YAML file.",
    )
    verify_skill_bundle.add_argument(
        "--policy",
        required=True,
        default=None,
        type=Path,
        metavar="FILE",
        help="Path to skill-policy.json.",
    )
    verify_skill_bundle.add_argument(
        "--bundle-dir",
        required=True,
        default=None,
        type=Path,
        metavar="PATH",
        help="Directory holding requirements.json, manifest.json and validation.json.",
    )
    draft_skill_policy = subparsers.add_parser(
        "draft-skill-policy",
        help="Draft a Skill Bundle policy for operator review (offline): carry "
        "forward the reviews that still match and draft placeholder-only "
        "reviews for new or changed members. The draft always fails policy "
        "validation until the operator resolves every placeholder. No network, "
        "no GitHub access.",
    )
    draft_skill_policy.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="Path to the Workflow Configuration YAML file.",
    )
    draft_skill_policy.add_argument(
        "--home",
        required=True,
        type=Path,
        metavar="PATH",
        help="The runtime user's home directory holding the installed store "
        "to read declared members from (nothing is installed).",
    )
    draft_skill_policy.add_argument(
        "--policy",
        required=False,
        default=None,
        type=Path,
        metavar="FILE",
        help="Path to the current skill-policy.json, used only as the "
        "carry-forward source (never modified). Omit for no carry-forward.",
    )
    draft_skill_policy.add_argument(
        "--out",
        required=True,
        type=Path,
        metavar="FILE",
        help="Where the draft policy is written (must differ from --policy).",
    )

    validate = subparsers.add_parser(
        "validate",
        help="Run a Project Profile's Validation Contract for real against a checkout "
        "(S2c, issue #18): bootstrap, then test_all and every check, or one "
        "test_targeted file. No GitHub access, no Base Revision comparison — "
        "this is the harness node's commands run standalone.",
    )
    validate.add_argument(
        "--project-dir",
        required=True,
        type=Path,
        metavar="PATH",
        help="The checkout root the Project Profile's paths are relative to.",
    )
    validate.add_argument(
        "--profile",
        type=Path,
        metavar="PATH",
        help="Path to project-profile.yml (default: <project-dir>/docs/agents/project-profile.yml).",
    )
    validate.add_argument(
        "--evidence-dir",
        required=True,
        type=Path,
        metavar="PATH",
        help="Directory the Validation Contract's evidence paths are rendered into.",
    )
    validate.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help="Don't run the profile's bootstrap command first (it already ran).",
    )
    validate.add_argument(
        "--targeted",
        metavar="PATH",
        help="Run test_targeted against this one test file instead of test_all and checks (L2-2).",
    )

    config = subparsers.add_parser(
        "config",
        help="Offline Workflow Configuration commands (step-workflow "
        "contract §2.9): static, no network, no Attempt.",
    )
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_validate = config_subparsers.add_parser(
        "validate",
        help="Validate a version: 4 Workflow Configuration "
        "file offline (CFG-38, SKV3-01). Every refusal exits non-zero "
        "naming the YAML path.",
    )
    config_validate.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="Path to the Workflow Configuration YAML file.",
    )
    config_validate.add_argument(
        "--workflow",
        default=None,
        metavar="ID",
        help="Select this Workflow id; the whole file is always validated "
        "and the whole `skills` declaration always counts (SKV3-05). "
        "An unknown id lists the available ids.",
    )
    config_subparsers.add_parser(
        "schema",
        help="Print the draft 2020-12 JSON Schema for a version: 4 Workflow "
        "Configuration file to stdout. Offline; an editor aid only, never a "
        "second validator.",
    )

    return parser


def load_token(token_env: str) -> str:
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(f"{token_env} is not set")
    return token


def run_preflight_command(owner: str, repo: str, token: str) -> int:
    transport = github_adapter.make_probe_transport(token)
    report = run_preflight(transport, owner, repo)
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    if report.ok:
        print("preflight: all probes passed")
        return 0
    print("preflight: FAILED", file=sys.stderr)
    return 1


def run_startup_check_command(owner: str, repo: str, token: str) -> int:
    try:
        result = github_adapter.run_startup_checks(token, owner, repo)
    except (TokenRejected, StartupCheckFailed) as exc:
        print(f"startup-check: FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"identity: id={result.identity.account_id} login={result.identity.login}")
    print(f"push permission: {result.push_allowed}")
    print(f"rate limit headroom: {result.rate_limit_remaining}/{result.rate_limit_limit}")

    if not result.push_allowed:
        print("startup-check: FAILED: permissions.push is not true", file=sys.stderr)
        return 1
    print("startup-check: passed; no write performed")
    return 0


def run_run_command(
    config: Path,
    workflow: str,
    state_dir: Path,
    *,
    toolchain_matrix_path: Path | None = None,
    policy: Path,
    bundle_dir: Path,
    skill_home: Path,
) -> int:
    """``agent run``: start the Worker and run its poll loop (contract §4).

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4
    (startup in order, then the poll loop until a halt). The Supervisor in
    ``worker.supervisor`` owns the sequence; this only maps arguments.

    The Worker verifies the prepared Skill Bundle deployment before any secret
    or live check. There is no skip-bundle, skip-credentials or build-mode
    switch.

    Matrix row ids: REC-11, OPC-8, OP-15, OP-16.
    """
    return run_worker(
        config=config,
        workflow=workflow,
        state_dir=state_dir,
        skill_deployment=SkillDeploymentCheck(
            policy=policy,
            bundle_dir=bundle_dir,
            home=skill_home,
        ),
        toolchain_matrix_path=toolchain_matrix_path,
    )


def print_capability_report(report: ProviderCapabilityReport) -> None:
    """Print one Provider Capability Report the way ``agentctl status`` carries it.

    Evidence is redacted through the provider key registry and capped at
    write time by :func:`redact_report`, so a secret in a provider error
    never reaches this output.
    """
    redacted = redact_report(
        report.to_dict(),
        SecretRegistry.from_environment(PROVIDER_KEY_ENV_VARS),
    )
    print(f"pinned model: {redacted['pin_key']}")
    print(f"assertion ran at: {redacted['ran_at']}")
    checks = redacted.get("checks")
    if not isinstance(checks, list):
        return
    for entry in checks:
        if not isinstance(entry, dict):
            continue
        line = (
            f"check {entry.get('check')}: "
            f"{'gating' if entry.get('gating') else 'diagnostic'} "
            f"{entry.get('outcome')}"
        )
        via = entry.get("via")
        if isinstance(via, str):
            line += f" via {via}"
        if isinstance(entry.get("reported"), str):
            line += f" reported={entry['reported']!r} pinned={entry.get('pinned')!r}"
        elif isinstance(entry.get("pinned"), str):
            line += f" pinned={entry['pinned']!r}"
        evidence = entry.get("evidence")
        if isinstance(evidence, str) and evidence:
            line += f": {evidence}"
        print(line)


def run_provider_check_command(config: Path, provider: str) -> int:
    """Assert one configured pin's provider capability (§2.1, ADR 0009).

    The pin's prices and output cap come from the Workflow Configuration
    alone: the probe is sent with ``max_tokens`` set to the pin's
    ``output-cap-tokens``, so an output cap above the model's maximum is
    refused there. Static and offline first: the file is validated before
    any network call, and a refusal exits nonzero with nothing opened.
    """
    from coding_agent.provider.price_table import PriceTable
    from coding_agent.worker.startup import configured_pin

    try:
        loaded, faults = load_workflow_configuration(config)
    except UnicodeDecodeError as exc:
        print(
            f"provider-check: FAILED: {config}: cannot validate the "
            f"workflow configuration: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"provider-check: FAILED: {config}: cannot validate the "
            f"workflow configuration: {exc}",
            file=sys.stderr,
        )
        return 1
    if loaded is None:
        detail = "; ".join(
            f"{fault.path}: {fault.detail}" for fault in faults
        ) or "invalid workflow configuration"
        print(f"provider-check: FAILED: {config}: {detail}", file=sys.stderr)
        return 1
    cfg = loaded.config
    try:
        model_decl = cfg.models[provider]
    except KeyError:
        available = ", ".join(sorted(cfg.models))
        print(
            f"provider-check: FAILED: {config}: unknown model {provider!r}: "
            f"available: {available}",
            file=sys.stderr,
        )
        return 1
    try:
        pin, price = configured_pin(model_decl)
    except ValueError as exc:
        print(f"provider-check: FAILED: {config}: {exc}", file=sys.stderr)
        return 1
    api_key = os.environ.get(model_decl.api_key_env)
    if not api_key:
        print(
            f"provider-check: FAILED: {config}: "
            f"models.{model_decl.id}.api-key-env: "
            f"{model_decl.api_key_env} is not set",
            file=sys.stderr,
        )
        return 1
    model = build_chat_model(pin, api_key=api_key)
    price_table: PriceTable = {pin.key: price}
    try:
        result = assert_provider_capability(
            pin, model, price_table, max_tokens=pin.output_cap_tokens
        )
    except ProviderCapabilityRefused as exc:
        if exc.report is not None:
            print_capability_report(exc.report)
        print(f"provider-check: FAILED: {exc}", file=sys.stderr)
        return 1

    print_capability_report(result.report)
    print("provider-check: passed; no Attempt opened, no GitHub access")
    return 0


def _load_bundle_configuration(
    command: str, config: Path
) -> LoadedWorkflowConfiguration | None:
    """Read and validate the Workflow Configuration once for a bundle command.

    Prepare and verify take the loaded value and never re-read the file, so
    this is the one read. A load failure is reported exactly as the command
    used to report its own configuration-stage fault — ``prepare`` names an
    undecodable file directly while ``verify`` reports it as a validation
    failure — and the caller exits 1.
    """
    from coding_agent.skillbundle.deployment import DeploymentFault
    from coding_agent.workflow.configuration import load_workflow_configuration

    try:
        loaded, faults = load_workflow_configuration(config)
    except UnicodeDecodeError as exc:
        if command == "prepare-skill-bundle":
            fault: DeploymentFault = DeploymentFault(
                phase="configuration",
                path=str(config),
                detail="workflow configuration is not valid UTF-8",
            )
        else:
            fault = DeploymentFault(
                phase="configuration",
                path=str(config),
                detail=f"cannot validate the workflow configuration: {exc}",
            )
        print(f"{command}: FAILED: {fault}", file=sys.stderr)
        return None
    except Exception as exc:
        print(
            f"{command}: FAILED: configuration: {config}: "
            f"cannot validate the workflow configuration: {exc}",
            file=sys.stderr,
        )
        return None
    if loaded is None:
        for validation_fault in faults:
            mapped = DeploymentFault(
                phase="configuration",
                path=validation_fault.path or str(config),
                detail=validation_fault.detail,
            )
            print(f"{command}: FAILED: {mapped}", file=sys.stderr)
        return None
    return loaded


def run_prepare_skill_bundle_command(
    config: Path,
    policy: Path,
    bundle_dir: Path,
    home: Path,
    mode: str,
    resolution_manifest: Path | None = None,
) -> int:
    """Prepare the Skill Bundle deployment (SKV3, empty bundle path).

    Validates the configuration, policy and prompts, then publishes
    ``requirements.json``, ``manifest.json`` and ``validation.json`` as one
    completed generation. Exit 0 on success, 1 on refusal/failure, 2 on a
    bad mode.
    """
    if mode not in ("installed", "install"):
        print(
            f"prepare-skill-bundle: FAILED: unsupported mode {mode!r}: "
            "must be 'installed' or 'install'",
            file=sys.stderr,
        )
        return 2
    loaded = _load_bundle_configuration("prepare-skill-bundle", config)
    if loaded is None:
        return 1
    from coding_agent.skillbundle.deployment import prepare_skill_bundle
    from coding_agent.skillbundle.installer import SubprocessInstallerAdapter

    result = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode=mode,
        resolution_manifest=resolution_manifest,
        installer=SubprocessInstallerAdapter(),
    )
    if result.ok:
        print(
            "prepare-skill-bundle: prepared Skill Bundle "
            "(requirements, manifest and validation)"
        )
        return 0
    for fault in result.faults:
        print(f"prepare-skill-bundle: FAILED: {fault}", file=sys.stderr)
    return 1


def run_verify_skill_bundle_deployment_command(
    config: Path, policy: Path, bundle_dir: Path, home: Path
) -> int:
    """Verify the prepared Skill Bundle deployment offline and read-only.

    Re-checks the prepared configuration, prompts, policy and bundle. Exit 0
    on success, 1 on refusal/failure. Never refreshes or repairs records.
    """
    loaded = _load_bundle_configuration("verify-skill-bundle", config)
    if loaded is None:
        return 1
    from coding_agent.skillbundle.deployment import verify_skill_bundle_deployment

    result = verify_skill_bundle_deployment(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
    )
    if result.ok:
        print(
            "verify-skill-bundle: prepared deployment verified "
            "(configuration, prompts, policy and bundle)"
        )
        return 0
    for fault in result.faults:
        print(f"verify-skill-bundle: FAILED: {fault}", file=sys.stderr)
    return 1


def run_draft_skill_policy_command(
    config: Path, home: Path, policy: Path | None, out: Path
) -> int:
    """Draft a Skill Bundle policy for operator review (offline).

    Reads the declared members from the installed store, carries forward
    the current policy reviews that still match, and drafts placeholder-only
    (JSON ``null``) reviews for the rest. The draft is written to ``out``
    and a review report is printed to stdout. The output always fails
    policy validation until the operator resolves every placeholder, so it
    can never be used as an approval. Exit 0 when the draft is written,
    1 on refusal/failure. No network, no GitHub access, no writes except
    ``out``.
    """
    from coding_agent.skillbundle.draft import draft_to

    result = draft_to(config=config, home=home, policy=policy, out=out)
    if not result.ok:
        for fault in result.faults:
            print(f"draft-skill-policy: FAILED: {fault}", file=sys.stderr)
        return 1
    print(result.report)
    print(f"draft-skill-policy: draft written to {out}")
    return 0


def _print_command_result(result: RawCommandResult) -> bool:
    outcome = classify(result)
    mark = "PASS" if outcome == "passed" else "FAIL"
    executed = result.junit.executed if result.junit is not None else None
    failures = sorted(result.junit.failure_ids) if result.junit is not None else []
    print(
        f"[{mark}] {result.name}: command={result.command!r} exit={result.exit_code} "
        f"executed={executed} failures={failures} ({outcome})"
    )
    return outcome == "passed"


def run_validate_command(
    project_dir: Path,
    profile_path: Path | None,
    evidence_dir: Path,
    *,
    skip_bootstrap: bool,
    targeted: str | None,
) -> int:
    resolved_profile_path = project_dir / "docs" / "agents" / "project-profile.yml"
    if profile_path is not None:
        resolved_profile_path = profile_path
    outcome = parse_profile_yaml(resolved_profile_path.read_text(encoding="utf-8"))
    if isinstance(outcome, (MissingReadinessFacts, UnknownSchema, InvalidProfile)):
        print(f"validate: profile is not ready: {outcome}", file=sys.stderr)
        return 1
    profile = outcome

    working_directory = project_dir / profile.working_directory
    runner = SubprocessCommandRunner()

    if not skip_bootstrap:
        bootstrap = runner.run(profile.bootstrap, cwd=working_directory)
        bootstrap_result = RawCommandResult(
            name="bootstrap",
            command=bootstrap.command,
            exit_code=bootstrap.exit_code,
            junit=None,
            evidence_declared=False,
            stdout=bootstrap.stdout,
            stderr=bootstrap.stderr,
            timed_out=bootstrap.timed_out,
        )
        if not _print_command_result(bootstrap_result):
            print("validate: FAILED: bootstrap did not succeed", file=sys.stderr)
            return 1

    context = CommandContext(runner, working_directory, evidence_dir)

    if targeted is not None:
        ok = _print_command_result(run_targeted_test(profile, context, targeted))
    else:
        results = run_validation_contract(profile, context)
        # A list comprehension, not `all(... for ...)`: every command must be
        # printed, not just those up to the first failure.
        ok = all([_print_command_result(result) for result in results])

    if ok:
        print("validate: all commands passed")
        return 0
    print("validate: FAILED", file=sys.stderr)
    return 1


def run_step_config_validate_v3_command(config: Path, workflow: str | None = None) -> int:
    """Validate a ``version: 4`` step-workflow file offline (contract §2.9).

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7
    (strict parsing), §2.8 (trust and paths), §2.9 (static and offline;
    every refusal exits non-zero naming the YAML path of every fault) and
    §3 (static validation), plus the skills-declaration resolution behind
    issue #372 (SKV3-01 … SKV3-04).

    Matrix row ids: SKV3-01 (``agent config validate`` accepts or refuses a
    ``version: 4`` file with an optional configuration-wide ``skills``
    declaration), SKV3-05 (``--workflow`` only selects; the whole file is
    always validated).

    Static and offline: only the YAML file, the prompt files it names and
    the ``target.profile`` file it names are read. No environment variable
    value is read, no credential is required, no Attempt is opened, no value
    of any environment variable is printed, and no network call is made.
    The whole file is always validated even when ``workflow`` selects one
    Workflow.
    """
    return run_config_validate_command(config, workflow)


def run_config_schema_command() -> int:
    """Print the draft 2020-12 JSON Schema for a ``version: 4`` file.

    Offline and deterministic: the schema is built from the validator's own
    vocabulary, pretty-printed with stable key order. It is an editor aid
    only; contract §3 stays the authority.
    """
    print(dump_workflow_json_schema(), end="")
    return 0


def run_config_validate_command(config: Path, workflow: str | None = None) -> int:
    """Validate a Workflow Configuration file offline (contract §2.9).

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7
    (strict parsing), §2.8 (trust and paths), §2.9 (static and offline;
    every refusal exits non-zero naming the YAML path) and §3 (static
    validation).

    Matrix row ids: CFG-38 (offline, names every fault, lists workflows),
    SKV3-01 (``version: 2`` refused with a migration diagnostic), SKV3-05
    (``--workflow`` only selects; the whole file is always validated).

    Only ``version: 4`` step-workflow files are accepted. A ``version: 3`` file is refused with a migration diagnostic; a ``version: 2``
    file is refused with a migration diagnostic before anything else; the
    file is left untouched. Anything else is refused naming the ``version``
    path; a ``version: 1`` file keeps the §16.4 drain note (drain MVP
    Attempts with the last pre-change release before upgrading).

    Static and offline: only the YAML file, the prompt files it names and
    the ``target.profile`` file it names are read. No environment variable
    value is read, no credential is required, no Attempt is opened, no value
    of any environment variable is printed, and no network call is made.
    The whole file is always validated even when ``workflow`` selects one
    Workflow.
    """
    try:
        loaded, faults = load_workflow_configuration(config, workflow)
    except UnicodeDecodeError as exc:
        print(
            f"config validate: FAILED: {config}: cannot validate the "
            f"workflow configuration: {exc}",
            file=sys.stderr,
        )
        return 1
    if loaded is None:
        for fault in faults:
            print(f"config validate: FAILED: {fault}", file=sys.stderr)
        return 1
    assert loaded is not None
    if loaded.config.target.profile is not None:
        from coding_agent.workflow.instance_profile import (
            InstanceProfileError,
            assert_instance_profile_toolchain,
            parse_instance_profile,
        )

        try:
            parsed = parse_instance_profile(loaded.profile_text or "")
        except InstanceProfileError as exc:
            print(f"config validate: FAILED: target.profile: {exc}", file=sys.stderr)
            return 1
        from coding_agent.profile.toolchain import resolve_toolchain_matrix_path

        matrix_path = resolve_toolchain_matrix_path(None)
        if not matrix_path.is_file():
            print(
                "config validate: NOTICE: no Supported Toolchain Matrix "
                f"at {matrix_path}: skipping target.profile toolchain check",
            )
        else:
            from coding_agent.profile.toolchain import (
                MalformedToolchainMatrix,
                load_supported_toolchain_matrix,
            )

            try:
                matrix = load_supported_toolchain_matrix(matrix_path)
            except (OSError, ValueError, MalformedToolchainMatrix) as exc:
                print(
                    "config validate: FAILED: target.profile: "
                    f"cannot load the Supported Toolchain Matrix "
                    f"from {matrix_path}: {exc}",
                    file=sys.stderr,
                )
                return 1
            try:
                assert_instance_profile_toolchain(parsed, matrix)
            except InstanceProfileError as exc:
                print(f"config validate: FAILED: {exc}", file=sys.stderr)
                return 1
    cfg = loaded.config
    if loaded.config.target.profile is None:
        # ADR 0037: a Workflow that needs a Validation Contract reads it
        # from the Target Repository at startup. Never a refusal: the
        # repository is not contacted here.
        if workflow is not None:
            candidates = (
                [cfg.workflows[workflow]] if workflow in cfg.workflows else []
            )
        else:
            candidates = list(cfg.workflows.values())
        if any(candidate.needs_validation_contract for candidate in candidates):
            print(
                "config validate: NOTICE: no target.profile: the Validation "
                "Contract will be read from the Target Repository's "
                "docs/agents/project-profile.yml at startup",
            )
    if workflow is not None:
        if workflow not in cfg.workflows:
            # The loader already faults unknown workflows; this is a safety net.
            print(
                f"config validate: FAILED: workflows: unknown workflow "
                f"{workflow!r}: available: {', '.join(sorted(cfg.workflows))}",
                file=sys.stderr,
            )
            return 1
        selected = cfg.workflows[workflow]
        print(
            f"config validate: OK (static only; no secrets checked): "
            f"workflow {workflow!r} ({len(selected.steps)} step(s)) in {config}"
        )
    else:
        available = ", ".join(sorted(cfg.workflows))
        print(
            f"config validate: OK (static only; no secrets checked): "
            f"{config} (workflows: {available})"
        )
    return 0


_REMOVED_RUN_FLAGS = (
    "--skills-home",
    "--skill-bundle-dir",
    "--skill-bundle-ignore-file",
)
"""``run`` flags removed with the legacy fixed Skill Bundle path (SKV3-32)."""

_REMOVED_VERIFY_FLAGS = (
    "--install-report",
    "--list-report",
    "--ignore-file",
)
"""``verify-skill-bundle`` flags removed with the legacy path (SKV3-32)."""


def _removed_flag(argv: Sequence[str] | None, flags: tuple[str, ...]) -> str | None:
    """The first removed flag present in ``argv``, or ``None``.

    Matches both ``--flag value`` and ``--flag=value`` spellings. Checked
    before ``argparse`` runs so the refusal can name the replacement
    interface instead of only reporting an unrecognized argument.
    """
    if argv is None:
        return None
    for token in argv:
        for flag in flags:
            if token == flag or token.startswith(flag + "="):
                return flag
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    removed_run = _removed_flag(argv, _REMOVED_RUN_FLAGS)
    if removed_run is not None:
        parser.error(
            f"{removed_run} was removed with the legacy fixed Skill Bundle "
            "path; use --policy, --bundle-dir and --skill-home "
            "(the prepared Skill Bundle deployment)"
        )
    removed_verify = _removed_flag(argv, _REMOVED_VERIFY_FLAGS)
    if removed_verify is not None:
        parser.error(
            f"{removed_verify} was removed with the legacy fixed Skill Bundle "
            "path; use --config, --policy, --bundle-dir and --home "
            "(the prepared Skill Bundle deployment)"
        )
    args = parser.parse_args(argv)

    try:
        load_application_environment(args.app_env_file)
    except EnvFileError as exc:
        parser.error(str(exc))

    if args.command == "config":
        if args.config_command == "validate":
            return run_config_validate_command(args.config, args.workflow)
        if args.config_command == "schema":
            return run_config_schema_command()
        parser.error(f"unknown config subcommand {args.config_command!r}")
        raise AssertionError("unreachable")
    if args.command == "provider-check":
        return run_provider_check_command(args.config, args.provider)
    if args.command == "prepare-skill-bundle":
        return run_prepare_skill_bundle_command(
            args.config,
            args.policy,
            args.bundle_dir,
            args.home,
            args.mode,
            resolution_manifest=args.resolution_manifest,
        )
    if args.command == "verify-skill-bundle":
        return run_verify_skill_bundle_deployment_command(
            args.config, args.policy, args.bundle_dir, args.home
        )
    if args.command == "draft-skill-policy":
        return run_draft_skill_policy_command(
            args.config, args.home, args.policy, args.out
        )
    if args.command == "validate":
        return run_validate_command(
            args.project_dir,
            args.profile,
            args.evidence_dir,
            skip_bootstrap=args.skip_bootstrap,
            targeted=args.targeted,
        )

    if args.command == "run":
        return run_run_command(
            args.config,
            args.workflow,
            args.state_dir,
            toolchain_matrix_path=args.toolchain_matrix,
            policy=args.policy,
            bundle_dir=args.bundle_dir,
            skill_home=args.skill_home,
        )

    owner, repo = split_repo(args.repo)
    token = load_token(args.token_env)

    if args.command == "preflight":
        return run_preflight_command(owner, repo, token)
    if args.command == "startup-check":
        return run_startup_check_command(owner, repo, token)
    parser.error(f"unknown command {args.command!r}")
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
