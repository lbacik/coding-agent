from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from coding_agent.env import load_dotenv
from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import StartupCheckFailed, run_startup_checks
from coding_agent.identity.token import TokenRejected
from coding_agent.implement.attempt import (
    run_implement_attempt,
    terminal_category,
)
from coding_agent.implement.ceilings import DEFAULT_LOOP_CEILINGS, UsageTotals
from coding_agent.implement.progress import BudgetSnapshot, RunLedger
from coding_agent.implement.result_capping import DEFAULT_RESULT_CAP_LIMIT
from coding_agent.preflight.probes import run_preflight
from coding_agent.profile.parser import MissingReadinessFacts, UnknownSchema, parse_profile_yaml
from coding_agent.profile.schema import TOOLCHAIN_RUNTIME_FOR_LANGUAGE
from coding_agent.provider.capability import ProviderCapabilityRefused, assert_provider_capability
from coding_agent.provider.effective_token_ceiling import effective_token_ceiling_for
from coding_agent.provider.config import (
    DEFAULT_COMPACTION_THRESHOLDS,
    DEFAULT_EFFECTIVE_TOKEN_CEILINGS,
    DEFAULT_PRICE_TABLE,
    PINNED_MODELS,
)
from coding_agent.provider.pinned_model import build_chat_model
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
        help="S3.1+S3.2+S3.4+S3.5+S3.7 (issues #30, #32, #33, #34, #36, #51): assert the Provider "
        "Capability, fetch the Target Issue, maintain a local mirror of the Target Repository, "
        "check out a fresh workspace at the Base Revision, compute the Fingerprint, confirm the "
        "Seam Set, compose the Pinned Prefix, read the Project Profile, open the model's bounded "
        "tool loop, commit and push the Delivery Snapshot, then run S2's Validation Contract "
        "harness against it. Ends in exactly one terminal category: verified completion, "
        "implemented but unverified, or saved partial work. No review, no pull request (a later slice).",
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
        help="The Target Project's language (ADR 0012: decides whether tdd's injected "
        "companion idioms match this Attempt). Not yet read from the Project Profile — "
        "that wiring is a later ticket (S3.7).",
    )
    implement.add_argument(
        "--provider",
        default="anthropic",
        choices=sorted(PINNED_MODELS),
        help="Which configured Pinned Model this Attempt's Pinned Prefix is sized against "
        "(default: anthropic).",
    )
    implement.add_argument(
        "--attempt",
        type=int,
        default=1,
        metavar="N",
        help="This Attempt's number, `#<issue>/<n>` (default: 1). Not yet sourced from a Run "
        "Ledger, which does not exist yet — an explicit, honest input like --target-language.",
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
    print(
        f"token expiration: {result.token_expiration.expires_at.isoformat()} "
        f"({result.token_expiration.remaining} remaining)"
    )
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
) -> int:
    pin = PINNED_MODELS[provider]
    model = build_chat_model(pin)

    # The Provider Capability Assertion (ADR 0009, L1-4): no Attempt is
    # opened on a Pinned Model that cannot do what a work node needs. ADR
    # 0009 explicitly rejects paying for this per Attempt rather than once
    # per Worker lifetime -- and today, with no persistent Worker (S7) yet,
    # this call really does land at that per-Attempt frequency, since one
    # `agent implement` process handles exactly one Target Issue. S7 is
    # where this hoists up into an actual once-per-Worker-startup call; it
    # stays here only because there is nowhere else for it to live yet.
    try:
        assert_provider_capability(pin, model, DEFAULT_PRICE_TABLE)
    except ProviderCapabilityRefused as exc:
        print(f"implement: provider-capability-refused: {exc}", file=sys.stderr)
        return 1

    client = GitHubClient(token)
    mirror_dir = state_dir / "mirrors" / owner / f"{repo}.git"
    workspace_dir = state_dir / "workspaces" / owner / repo
    evidence_dir = state_dir / "evidence" / owner / repo / str(issue) / str(attempt_number)
    skills_dir = skills_home / ".agents" / "skills"
    ledger_ceilings = replace(
        DEFAULT_LOOP_CEILINGS,
        max_effective_tokens=effective_token_ceiling_for(DEFAULT_EFFECTIVE_TOKEN_CEILINGS, pin),
    )
    ledger = RunLedger(state_dir / "run-ledger.sqlite3", ceilings=ledger_ceilings)

    try:
        report = run_implement_attempt(
            client,
            owner,
            repo,
            issue,
            token,
            mirror_dir=mirror_dir,
            workspace_dir=workspace_dir,
            skills_dir=skills_dir,
            evidence_dir=evidence_dir,
            target_language=target_language,
            pin=pin,
            compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
            price_table=DEFAULT_PRICE_TABLE,
            effective_token_ceilings=DEFAULT_EFFECTIVE_TOKEN_CEILINGS,
            result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
            ceilings=DEFAULT_LOOP_CEILINGS,
            model=model,
            attempt_number=attempt_number,
            token_env=token_env,
            on_progress=lambda message: print(f"[loop] {message}", flush=True),
            ledger=ledger,
        )
    except BaseException:
        ledger.close()
        raise
    for result in report.skeleton.results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    if report.validation is not None:
        _print_validation_evidence(report.validation)

    outcome = terminal_category(report)
    tool_loop = report.tool_loop
    delivery = report.delivery
    stop_reason = tool_loop.stopped_by if tool_loop and tool_loop.stopped_by else None
    if outcome == "verified completion":
        next_action = "human review of the Delivery Snapshot"
    elif outcome == "implemented but unverified":
        next_action = "inspect Validation Evidence and decide whether to retry"
    else:
        next_action = "inspect the saved work and resume only with human direction"
    finalization_result = "validation evidence recorded" if report.validation else "not run"
    ledger.record_outcome(
        f"#{issue}/{attempt_number}",
        outcome,
        BudgetSnapshot(
            tool_loop.elapsed_seconds if tool_loop else 0.0,
            tool_loop.usage if tool_loop else UsageTotals(),
        ),
        detail=f"{outcome}; stop_reason={stop_reason or 'none'}; next_action={next_action}",
        stop_reason=stop_reason,
        next_action=next_action,
        finalization_result=finalization_result,
        artifact_ref=str(evidence_dir),
        diff_ref=delivery.commit_sha if delivery else None,
    )
    ledger.close()

    if delivery is not None and delivery.branch_name is not None:
        line = f"implement: {outcome}; branch={delivery.branch_name} sha={delivery.commit_sha}"
    else:
        line = f"implement: {outcome}"

    if outcome == "verified completion":
        print(line)
        return 0
    print(line, file=sys.stderr)
    return 1


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
    resolved_profile_path = profile_path or project_dir / "docs" / "agents" / "project-profile.yml"
    outcome = parse_profile_yaml(resolved_profile_path.read_text(encoding="utf-8"))
    if isinstance(outcome, (MissingReadinessFacts, UnknownSchema)):
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


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

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
        )

    parser.error(f"unknown command {args.command!r}")
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
