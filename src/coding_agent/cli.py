"""The ``agent`` command line: one subcommand per Worker capability.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.4 (``config
validate`` is static and offline), §3 (``run`` is the Worker: startup in
order, then the poll loop), §18 (``preflight`` and ``startup-check`` are the
operator's write probes and the non-mutating startup checks) and ADR 0009
(``provider-check`` is the Provider Capability Assertion run alone).

MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8, W-CFG-1, W-CFG-2,
W-CFG-3, W-CFG-4, W-CFG-5, W-CFG-6, W-CFG-7, W-CFG-8, W-CFG-9, W-CFG-10,
W-CFG-11, W-CFG-12, C-CFG-1, C-CFG-2, C-CFG-3, W-CFG-13 (validate: static
and offline, full semantic checks).

``run --config <file> --workflow <id>`` is the only command that opens an
Attempt; ``worker.supervisor`` wires it. ``config validate`` checks a
Workflow Configuration file offline: no Attempt, no network.
``preflight``, ``startup-check``, ``provider-check``,
``verify-skill-bundle`` and ``validate`` are operator tools.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from coding_agent.env import EnvFileError, load_application_environment
from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import StartupCheckFailed, run_startup_checks
from coding_agent.identity.token import TokenRejected
from coding_agent.preflight.probes import run_preflight
from coding_agent.profile.parser import (
    InvalidProfile,
    MissingReadinessFacts,
    UnknownSchema,
    parse_profile_yaml,
)
from coding_agent.profile.toolchain import resolve_toolchain_matrix_path
from coding_agent.provider.capability import ProviderCapabilityRefused, assert_provider_capability
from coding_agent.provider.config import DEFAULT_PRICE_TABLE, PINNED_MODELS
from coding_agent.provider.pinned_model import build_chat_model
from coding_agent.skillbundle.verify import load_ignored_refs, verify_bundle
from coding_agent.validate import (
    CommandContext,
    RawCommandResult,
    SubprocessCommandRunner,
    classify,
    run_targeted_test,
    run_validation_contract,
)
from coding_agent.worker.supervisor import (
    DEFAULT_SKILL_BUNDLE_DIR,
    DEFAULT_SKILL_BUNDLE_IGNORE_FILE,
    SkillBundleCheck,
    run_worker,
)
from coding_agent.workflow import (
    EffectiveConfiguration,
    WorkflowConfigError,
    load_workflow_config,
    resolve_effective_configuration,
    validate_workflow_semantics,
)


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
        help="Every write probe contract §10's Preflight subsection requires, "
        "run against a sandbox repository. Never run this against the real "
        "Target Repository.",
    )
    startup_check = subparsers.add_parser(
        "startup-check",
        help="The non-mutating startup checks contract §10 requires. No write is performed.",
    )

    run = subparsers.add_parser(
        "run",
        help="The Worker (contract §3): start in order — validate the Workflow "
        "Configuration, verify the Skill Bundle, run the non-mutating GitHub "
        "checks, run the Provider Capability Assertion, open the Run Ledger — "
        "then select, claim and deliver the Selection Label's issues one "
        "Attempt at a time until a halt. A startup refusal exits non-zero "
        "with no Attempt and no GitHub write.",
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
        help="Which Workflow of --config to run; an unknown id lists the available ids.",
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
        "--skills-home",
        type=Path,
        default=Path.home(),
        metavar="PATH",
        help="The runtime user's home directory the Skill Bundle is installed under; skill "
        "files are read from <home>/.agents/skills (default: the current user's home).",
    )
    run.add_argument(
        "--skill-bundle-dir",
        type=Path,
        default=DEFAULT_SKILL_BUNDLE_DIR,
        metavar="PATH",
        help="Directory holding the build-time install-report.json and "
        f"list-report.json the Skill Bundle is verified against at startup "
        f"(default: {DEFAULT_SKILL_BUNDLE_DIR}).",
    )
    run.add_argument(
        "--skill-bundle-ignore-file",
        type=Path,
        default=DEFAULT_SKILL_BUNDLE_IGNORE_FILE,
        metavar="PATH",
        help="The reviewed-dependency register of /<skill> references to ignore "
        f"(default: {DEFAULT_SKILL_BUNDLE_IGNORE_FILE}).",
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
        "--provider",
        required=True,
        choices=sorted(PINNED_MODELS),
        help="Which configured Pinned Model to assert against.",
    )

    verify_skill_bundle = subparsers.add_parser(
        "verify-skill-bundle",
        help="The three verification layers issue #15 requires against a build-time "
        "agent-installer run. No GitHub access; reads local build artifacts only.",
    )
    verify_skill_bundle.add_argument(
        "--install-report",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to `agent-installer install --json`'s output.",
    )
    verify_skill_bundle.add_argument(
        "--list-report",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to `agent-installer list --json`'s output, run after installation.",
    )
    verify_skill_bundle.add_argument(
        "--home",
        required=True,
        type=Path,
        metavar="PATH",
        help="The runtime user's home directory the Skill Bundle was installed into.",
    )
    verify_skill_bundle.add_argument(
        "--ignore-file",
        required=True,
        type=Path,
        metavar="PATH",
        help="The reviewed-dependency register of /<skill> references to ignore.",
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
        help="Offline Workflow Configuration commands (contract §2.4): static, "
        "no network, no Attempt.",
    )
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_validate = config_subparsers.add_parser(
        "validate",
        help="Validate a Workflow Configuration file offline (W-CFG-1…12, "
        "C-CFG-1…3). Every refusal exits non-zero naming "
        "the YAML path; a selected workflow prints its Effective "
        "Configuration digest.",
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
        help="Validate only this Workflow id; an unknown id lists the available ids.",
    )

    return parser


def load_token(token_env: str) -> str:
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(f"{token_env} is not set")
    return token


def run_preflight_command(owner: str, repo: str, token: str) -> int:
    client = GitHubClient(token)
    report = run_preflight(client, owner, repo)
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    if report.ok:
        print("preflight: all probes passed")
        return 0
    print("preflight: FAILED", file=sys.stderr)
    return 1


def run_startup_check_command(owner: str, repo: str, token: str) -> int:
    client = GitHubClient(token)
    try:
        result = run_startup_checks(client, token, owner, repo)
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
    skills_home: Path,
    skill_bundle_dir: Path,
    skill_bundle_ignore_file: Path,
    *,
    toolchain_matrix_path: Path | None = None,
) -> int:
    """``agent run``: start the Worker and run its poll loop (contract §3).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup in
    order, then the poll loop until a halt). The Supervisor in
    ``worker.supervisor`` owns the sequence; this only maps arguments.

    MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8.
    """
    return run_worker(
        config=config,
        workflow=workflow,
        state_dir=state_dir,
        skills_home=skills_home,
        skill_bundle=SkillBundleCheck(
            install_report=skill_bundle_dir / "install-report.json",
            list_report=skill_bundle_dir / "list-report.json",
            home=skills_home,
            ignore_file=skill_bundle_ignore_file,
        ),
        toolchain_matrix_path=toolchain_matrix_path,
    )


def run_provider_check_command(provider: str) -> int:
    pin = PINNED_MODELS[provider]
    model = build_chat_model(pin)
    try:
        result = assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    except ProviderCapabilityRefused as exc:
        print(f"provider-check: FAILED: {exc}", file=sys.stderr)
        return 1

    tool_call = result.response.tool_calls[0]
    print(f"pinned model: {pin.key}")
    print(f"tool call arrived: {tool_call['name']}({tool_call['args']})")
    print(f"usage: {result.response.usage_metadata}")
    print(f"invalid effort refused: {result.effort_refusal[:200]}")
    print("provider-check: passed; no Attempt opened, no GitHub access")
    return 0


def run_verify_skill_bundle_command(
    install_report_path: Path, list_report_path: Path, home: Path, ignore_file: Path
) -> int:
    install_report = json.loads(install_report_path.read_text(encoding="utf-8"))
    list_report = json.loads(list_report_path.read_text(encoding="utf-8"))
    ignored_refs = load_ignored_refs(ignore_file)

    report = verify_bundle(install_report, list_report, home, ignored_refs)
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    if report.ok:
        print("verify-skill-bundle: all checks passed")
        return 0
    print("verify-skill-bundle: FAILED", file=sys.stderr)
    return 1


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


def run_config_validate_command(config: Path, workflow: str | None = None) -> int:
    """Validate a Workflow Configuration file offline (contract §2.4).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema
    table), §2.2 (strict parsing), §2.3 (trust and paths), §2.4 (static and
    offline; every refusal exits non-zero naming the YAML path, with no
    Attempt and no GitHub write), §2.5 (a valid file yields the Effective
    Configuration with its digest).

    MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-5, W-CFG-6,
    W-CFG-7, W-CFG-8, W-CFG-9, W-CFG-10, W-CFG-11, W-CFG-12, C-CFG-1,
    C-CFG-2, C-CFG-3, W-CFG-13 (validate: static and offline).

    Static and offline: only the YAML file, each ``prompt_file`` and the
    process environment are read. No Attempt is opened, no value of any
    environment variable is printed, and no network call is made.
    """
    try:
        cfg = load_workflow_config(config, workflow_id=workflow)
        if workflow is not None:
            effective: EffectiveConfiguration | None = resolve_effective_configuration(
                cfg, workflow
            )
        else:
            effective = None
            validate_workflow_semantics(cfg)
    except WorkflowConfigError as exc:
        print(f"config validate: FAILED: {exc}", file=sys.stderr)
        return 1
    if workflow is not None:
        assert effective is not None
        selected = cfg.workflows[workflow]
        print(
            f"config validate: OK: workflow {workflow!r} "
            f"({len(selected.steps)} step(s)) in {config}"
        )
        print(f"config validate: effective configuration digest: {effective.digest}")
    else:
        available = ", ".join(sorted(cfg.workflows))
        print(f"config validate: OK: {config} (workflows: {available})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        load_application_environment(args.app_env_file)
    except EnvFileError as exc:
        parser.error(str(exc))

    if args.command == "config":
        if args.config_command == "validate":
            return run_config_validate_command(args.config, args.workflow)
        parser.error(f"unknown config subcommand {args.config_command!r}")
        raise AssertionError("unreachable")
    if args.command == "provider-check":
        return run_provider_check_command(args.provider)
    if args.command == "verify-skill-bundle":
        return run_verify_skill_bundle_command(
            args.install_report, args.list_report, args.home, args.ignore_file
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
            args.skills_home,
            args.skill_bundle_dir,
            args.skill_bundle_ignore_file,
            toolchain_matrix_path=resolve_toolchain_matrix_path(args.toolchain_matrix),
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
