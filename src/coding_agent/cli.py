"""The ``agent`` command line: one subcommand per Worker capability.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the Attempt graph
owns every external write), §5 (the Claim), §7 (readiness and the baseline
run before any model call), §10 (the review snapshot commits the current
tree and pushes the Attempt's branch before either reviewer starts), §13
(the write sequence keyed by ``(attempt_id, write_kind)``), §14
("Recording before publishing": the Terminal Outcome is recorded in the
Run Ledger before it is printed), §17 (the Run Ledger tables) and §18 (one
branch per Attempt).

MVP row ids: L1-6, P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4, C-RDY-1, C-RDY-2,
C-RDY-3, C-RDY-4, L2-1, L2-12, L2-13, L2-14, L3-DEL-19, L3-DEL-20,
L3-ERR-9, L3-ERR-10, P-DEL-4, C-REV-1, L3-REV-1, L3-REV-2, G-REV-1, G-REV-2,
W-STP-6, W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-7, W-CFG-8, C-CFG-2,
W-CFG-13 (validate half).

``implement`` runs the Attempt graph over the temporary ``local`` tracker
adapter: the Target Issue is read from GitHub, nothing is written there,
and the Terminal Outcome is recorded in the Run Ledger and printed to
stdout. ``config validate`` checks a Workflow Configuration file offline
(contract §2.4): no Attempt, no network. ``preflight``,
``startup-check``, ``provider-check``, ``verify-skill-bundle`` and
``validate`` are unchanged by the M1 cut.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from coding_agent.env import EnvFileError, load_application_environment
from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import StartupCheckFailed, run_startup_checks
from coding_agent.identity.token import TokenRejected
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.ceilings import DEFAULT_LOOP_CEILINGS
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.runtime.git import (
    GitFailure,
    checkout_workspace,
    ensure_mirror,
    remote_url,
    target_repository_url,
)
from coding_agent.runtime.graph.attempt import DELIVERED, run_attempt
from coding_agent.runtime.pinned_prefix import context_window_for
from coding_agent.runtime.result_capping import DEFAULT_RESULT_CAP_LIMIT
from coding_agent.tracker.local import LocalTracker
from coding_agent.preflight.probes import run_preflight
from coding_agent.profile.parser import (
    InvalidProfile,
    MissingReadinessFacts,
    UnknownSchema,
    parse_profile_yaml,
)
from coding_agent.profile.schema import TOOLCHAIN_RUNTIME_FOR_LANGUAGE
from coding_agent.profile.toolchain import (
    MalformedToolchainMatrix,
    load_supported_toolchain_matrix,
    resolve_toolchain_matrix_path,
)
from coding_agent.provider.capability import ProviderCapabilityRefused, assert_provider_capability
from coding_agent.provider.effective_token_ceiling import effective_token_ceiling_for
from coding_agent.provider.config import (
    DEFAULT_CONTEXT_WINDOWS,
    DEFAULT_EFFECTIVE_TOKEN_CEILINGS,
    DEFAULT_PRICE_TABLE,
    PINNED_MODELS,
)
from coding_agent.provider.pinned_model import build_chat_model
from coding_agent.provider.price_table import price_for
from coding_agent.skillbundle.verify import load_ignored_refs, verify_bundle
from coding_agent.validate import (
    CommandContext,
    RawCommandResult,
    SubprocessCommandRunner,
    ValidationEvidence,
    classify,
    run_targeted_test,
    run_validation_contract,
)
from coding_agent.validate.runner import CredentialStrippedCommandRunner
from coding_agent.workflow import WorkflowConfigError, load_workflow_config


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

    implement = subparsers.add_parser(
        "implement",
        help="M1 walking skeleton: run the Attempt graph for the Target Issue "
        "over the temporary read-only `local` tracker adapter. The issue is "
        "read from GitHub and nothing is written there; the Terminal Outcome "
        "is recorded in the Run Ledger and printed to stdout.",
    )
    implement.add_argument(
        "--issue", required=True, type=int, metavar="N", help="The Target Issue number."
    )
    implement.add_argument(
        "--state-dir",
        type=resolve_state_dir,
        default=Path("/var/lib/coding-agent"),
        metavar="PATH",
        help="Root the mirror and workspace are kept under (default: /var/lib/coding-agent). "
        "A relative path is resolved to absolute against the current directory.",
    )
    implement.add_argument(
        "--toolchain-matrix",
        type=Path,
        metavar="PATH",
        help="Path to the Supported Toolchain Matrix. In the image this defaults to "
        "/opt/coding-agent/toolchain-matrix.json; locally use the file generated by "
        "scripts/generate-toolchain-matrix.py.",
    )
    implement.add_argument(
        "--skills-home",
        type=Path,
        default=Path.home(),
        metavar="PATH",
        help="The runtime user's home directory the Skill Bundle is installed under; skill "
        "files are read from <home>/.agents/skills (default: the current user's home).",
    )
    implement.add_argument(
        "--target-language",
        required=True,
        choices=sorted(TOOLCHAIN_RUNTIME_FOR_LANGUAGE),
        help="The Target Project's language (decides which companion idioms the "
        "Attempt Header answers with).",
    )
    implement.add_argument(
        "--provider",
        default="anthropic",
        choices=sorted(PINNED_MODELS),
        help="Which configured Pinned Model this Attempt runs against "
        "(default: anthropic).",
    )
    implement.add_argument(
        "--attempt",
        type=int,
        default=1,
        metavar="N",
        help="This Attempt's number, `#<issue>/<n>` (default: 1).",
    )

    for sub in (preflight, startup_check, implement):
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
        help="Validate a Workflow Configuration file offline (W-CFG-1…4, "
        "W-CFG-7, W-CFG-8, C-CFG-2). Every refusal exits non-zero naming "
        "the YAML path.",
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


PROVIDER_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
"""The provider credentials redacted from the transcript where they are set."""


def transcript_redactions(token_env: str, token: str) -> dict[str, str]:
    """Every registered secret the transcript must never carry (§18, L3-ERR-11)."""
    redactions = {token_env: token}
    for name in PROVIDER_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            redactions[name] = value
    return redactions


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


def _classify_validation_command(name: str, validation: ValidationEvidence) -> str:
    if name in validation.passed:
        return "passed"
    if any(failure.command_name == name for failure in validation.baseline_failures):
        return "baseline-excused"
    if any(regression.command_name == name for regression in validation.regressions):
        return "regression"
    if any(entry.command_name == name for entry in validation.unclaimable):
        return "unclaimable"
    if any(entry.command_name == name for entry in validation.missing_evidence):
        return "missing-evidence"
    raise AssertionError(f"unreachable: {name!r} classified nowhere in {validation!r}")


def _print_validation_evidence(validation: ValidationEvidence) -> None:
    for command in validation.commands:
        classification = _classify_validation_command(command.name, validation)
        mark = "PASS" if classification == "passed" else "FAIL"
        failures = sorted(command.failure_ids) if command.failure_ids else []
        print(
            f"[{mark}] validate {command.name}: command={command.command!r} "
            f"exit={command.exit_code} executed={command.executed} failures={failures} "
            f"({classification})"
        )


def run_implement_command(
    owner: str,
    repo: str,
    issue: int,
    token: str,
    state_dir: Path,
    skills_home: Path,
    target_language: str,
    provider: str,
    token_env: str = "GITHUB_TOKEN",
    attempt_number: int = 1,
    *,
    toolchain_matrix_path: Path | None = None,
) -> int:
    """Run one Attempt through the Attempt graph and report its Terminal Outcome.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (the Supported
    Toolchain Matrix refusal, the Provider Capability Assertion and the
    context-window check all land before any model call, as do the
    Readiness and baseline refusals inside the graph), §10 (the Agent
    Identity is resolved and the credentialed remote is bound before any
    model call, so ``activate_code_review`` runs the snapshot push and both
    reviewer seats), §13 (the write sequence keyed by
    ``(attempt_id, write_kind)``; the token is redacted from every push
    failure), §14 (the Terminal Outcome is recorded in the Run Ledger
    before it is printed), §17 (the ledger file) and §18 (one branch per
    Attempt).

    MVP row ids: L1-6, P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4, L3-DEL-19,
    L3-DEL-20, L3-ERR-9, L3-ERR-10, P-DEL-4, C-REV-1, L3-REV-1, L3-REV-2,
    G-REV-1, G-REV-2, W-STP-6.
    """
    resolved_matrix_path = resolve_toolchain_matrix_path(toolchain_matrix_path)
    try:
        matrix = load_supported_toolchain_matrix(resolved_matrix_path)
    except (OSError, json.JSONDecodeError, MalformedToolchainMatrix, ValueError) as exc:
        print(
            "implement: environment setup is not ready: could not load Supported Toolchain "
            f"Matrix from {resolved_matrix_path}: {exc}. Generate a local matrix with "
            "`uv run python scripts/generate-toolchain-matrix.py` or pass "
            "--toolchain-matrix PATH.",
            file=sys.stderr,
        )
        return 1

    # The Agent Identity resolves the way ``startup-check`` does, before any
    # model call and before the Attempt starts: a rejected token ends the
    # command with no Attempt opened, no model call and no write (§10).
    client = GitHubClient(token)
    try:
        startup_result = run_startup_checks(client, token, owner, repo)
    except (TokenRejected, StartupCheckFailed) as exc:
        print(f"implement: startup-check FAILED: {exc}", file=sys.stderr)
        return 1
    if not startup_result.push_allowed:
        print(
            "implement: startup-check FAILED: permissions.push is not true",
            file=sys.stderr,
        )
        return 1
    agent_identity = startup_result.identity
    credentialed_remote = remote_url(owner, repo, token)

    pin = PINNED_MODELS[provider]
    model = build_chat_model(pin)

    # The Provider Capability Assertion (ADR 0009, L1-4): no Attempt is
    # opened on a Pinned Model that cannot do what a work node needs. Until
    # the Worker exists to hoist this once per lifetime (M5), one
    # `agent implement` process handles exactly one Target Issue, so the
    # assertion lands at that frequency.
    try:
        assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    except ProviderCapabilityRefused as exc:
        print(f"implement: provider-capability-refused: {exc}", file=sys.stderr)
        return 1

    # Like the compaction entry below, these are pin-specific configuration
    # known before any model call — not discovered mid-Attempt.
    if price_for(DEFAULT_PRICE_TABLE, pin) is None:
        print(
            f"implement: environment setup is not ready: no Price Table entry "
            f"for pinned model {pin.key!r}.",
            file=sys.stderr,
        )
        return 1
    effective_token_ceiling = effective_token_ceiling_for(
        DEFAULT_EFFECTIVE_TOKEN_CEILINGS, pin
    )
    if effective_token_ceiling is None:
        print(
            "implement: environment setup is not ready: no effective token ceiling "
            f"configured for pinned model {pin.key!r}.",
            file=sys.stderr,
        )
        return 1
    context_window = context_window_for(pin, DEFAULT_CONTEXT_WINDOWS)
    if context_window is None:
        print(
            "implement: environment setup is not ready: no context-window "
            f"configuration for pinned model {pin.key!r}.",
            file=sys.stderr,
        )
        return 1

    tracker = LocalTracker(owner=owner, repo=repo, client=client)
    mirror_dir = state_dir / "mirrors" / owner / f"{repo}.git"
    workspace_dir = state_dir / "workspaces" / owner / repo
    evidence_dir = state_dir / "evidence" / owner / repo / str(issue) / str(attempt_number)
    skills_dir = skills_home / ".agents" / "skills"

    try:
        ensure_mirror(mirror_dir, credentialed_remote, redact=token)
    except GitFailure as exc:
        print(f"implement: could not update the mirror: {exc}", file=sys.stderr)
        return 1
    try:
        workspace = checkout_workspace(
            mirror_dir,
            workspace_dir,
            origin_url=target_repository_url(owner, repo),
        )
    except GitFailure as exc:
        print(f"implement: could not check out the workspace: {exc}", file=sys.stderr)
        return 1

    evidence_dir.mkdir(parents=True, exist_ok=True)
    # Every model request and response of the Attempt, redacted, beside its
    # evidence (§9, §18): the implement step and both reviewer seats share
    # this one model, so wrapping it records them all.
    transcript = TranscriptWriter(
        evidence_dir / TRANSCRIPT_FILE_NAME,
        transcript_redactions(token_env, token),
    )
    model = TranscriptModel(model, transcript)
    ledger_ceilings = replace(
        DEFAULT_LOOP_CEILINGS, max_effective_tokens=effective_token_ceiling
    )
    ledger = RunLedger(state_dir / "run-ledger.sqlite3", ceilings=ledger_ceilings)

    # Until M5 the pinned Effective Configuration is the CLI-derived
    # settings the Attempt opens with; the digest beside the Fingerprint is
    # what the L1-6 live run reads back (the Pinned Model, endpoint included).
    effective_configuration: dict[str, object] = {
        "repo": f"{owner}/{repo}",
        "issue": issue,
        "provider": pin.key,
        "model": pin.model,
        "endpoint": pin.endpoint,
        "effort": pin.effort,
        "target_language": target_language,
    }
    try:
        result = run_attempt(
            issue_number=issue,
            attempt_number=attempt_number,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace.path,
            effective_configuration=effective_configuration,
            skills_dir=skills_dir,
            toolchain_matrix=matrix,
            command_runner=CredentialStrippedCommandRunner([token_env]),
            evidence_dir=evidence_dir,
            result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
            context_window=context_window,
            cache_breakpoints=(pin.provider == "anthropic"),
            remote_url=credentialed_remote,
            agent_identity=agent_identity,
            review_redact=token,
        )
    except BaseException:
        ledger.close()
        raise
    outcome = ledger.get_outcome(result.attempt_id)
    ledger.close()
    assert outcome is not None, (
        f"the Attempt graph always records the Terminal Outcome for {result.attempt_id}"
    )

    print(f"implement: {outcome.outcome} ({outcome.reason}); attempt={result.attempt_id}")
    print(f"implement: transcript {transcript.path}")
    return 0 if outcome.outcome == DELIVERED else 1


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

    Contract: ``docs/contract/mvp-runtime-contract.md`` §2.2 (strict parsing),
    §2.3 (trust and paths), §2.4 (static and offline; every refusal exits
    non-zero naming the YAML path, with no Attempt and no GitHub write).

    MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-7, W-CFG-8,
    C-CFG-2, W-CFG-13 (validate half).

    Static and offline: only the YAML file (and each ``prompt_file``'s
    existence) is read. No Attempt is opened and no network call is made.
    """
    try:
        cfg = load_workflow_config(config, workflow_id=workflow)
    except WorkflowConfigError as exc:
        print(f"config validate: FAILED: {exc}", file=sys.stderr)
        return 1
    if workflow is not None:
        selected = cfg.workflows[workflow]
        print(
            f"config validate: OK: workflow {workflow!r} "
            f"({len(selected.steps)} step(s)) in {config}"
        )
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

    owner, repo = split_repo(args.repo)
    token = load_token(args.token_env)

    if args.command == "preflight":
        return run_preflight_command(owner, repo, token)
    if args.command == "startup-check":
        return run_startup_check_command(owner, repo, token)
    if args.command == "implement":
        return run_implement_command(
            owner,
            repo,
            args.issue,
            token,
            args.state_dir,
            args.skills_home,
            args.target_language,
            args.provider,
            args.token_env,
            args.attempt,
            toolchain_matrix_path=resolve_toolchain_matrix_path(args.toolchain_matrix),
        )

    parser.error(f"unknown command {args.command!r}")
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
