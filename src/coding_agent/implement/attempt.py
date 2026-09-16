from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import StartupCheckFailed, resolve_identity
from coding_agent.identity.token import TokenRejected
from coding_agent.implement.ceilings import InMemoryUsageLedger, LoopCeilings, UsageTotals
from coding_agent.implement.delivery import (
    COMMITTED_AND_PUSHED,
    NO_CHANGE_PRODUCED,
    DeliverySnapshotOutcome,
    deliver_snapshot,
)
from coding_agent.implement.git import (
    GitFailure,
    Workspace,
    candidate_diff_signature,
    checkout_base_revision_worktree,
    remove_worktree,
)
from coding_agent.implement.context_handoff import HANDOFF_FAILURE
from coding_agent.implement.loop import ToolLoopResult, build_opening_messages, run_tool_loop
from coding_agent.implement.progress import (
    AttemptPolicy,
    BudgetSnapshot,
    PROGRESS_READINESS,
    PROGRESS_VALIDATION_RESULT,
    RunLedger,
    TerminalOutcome,
    detect_progress_markers,
    terminal_outcome,
)
from coding_agent.implement.pinned_prefix import (
    CompactionThresholdTable,
    ContextWindowTable,
    assert_no_skill_path_resolver,
    context_window_for,
)
from coding_agent.implement.readiness import ReadinessReport, prepare_environment
from coding_agent.implement.result_capping import FilesystemArtifactStore, build_read_result_slice_tool
from coding_agent.implement.skeleton import (
    SEAM_SET_CONFIRMED_STAGE,
    StageResult,
    SkeletonReport,
    compose_pinned_prefix_for_report,
    remote_url,
    run_implement_skeleton,
)
from coding_agent.implement.toolset import build_file_tools, build_test_targeted_tool
from coding_agent.profile.services import SocketServiceProber
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.toolchain import (
    MalformedToolchainMatrix,
    SupportedToolchainMatrix,
    load_toolchain_matrix,
)
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.effective_token_ceiling import (
    EffectiveTokenCeilingTable,
    effective_token_ceiling_for,
)
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.validate.baseline import ValidationEvidence
from coding_agent.validate.harness import CommandBaseRevisionRunner, CommandContext, validate
from coding_agent.validate.runner import CredentialStrippedCommandRunner

DEFAULT_TOOLCHAIN_MATRIX_PATH = Path("/opt/coding-agent/toolchain-matrix.json")
LOCAL_TOOLCHAIN_MATRIX_PATH = Path("tmp/toolchain-matrix.json")
TOOLCHAIN_MATRIX_ENV = "CODING_AGENT_TOOLCHAIN_MATRIX"

ImplementOutcome = Literal[
    "delivered-snapshot",
    "no-change-produced",
    "seam-not-confirmed",
    "failed-limit",
    "handoff-failure",
    "targeted-test-no-progress",
    "validation-failed",
    "unsupported-environment",
    "bootstrap-failed",
    "invalid-readiness-evidence",
]
"""The named outcomes `agent implement` can end its own run on, once the
Provider Capability Assertion (a pre-Attempt gate run by the caller, not
this module — `provider-capability-refused`) has already passed. Exit code
0 is reserved for `"delivered-snapshot"` alone."""


@dataclass
class AttemptReport:
    """`SkeletonReport` (issues #30, #32, #33) plus the stages this ticket
    adds: the Project Profile read from the workspace, the model's bounded
    tool loop, the Delivery Snapshot commit and push, and the Validation
    Evidence S2's harness produces against it (issue #36)."""

    skeleton: SkeletonReport
    results: list[StageResult] = field(default_factory=list)
    profile: ProjectProfile | None = None
    readiness: ReadinessReport | None = None
    tool_loop: ToolLoopResult | None = None
    delivery: DeliverySnapshotOutcome | None = None
    validation: ValidationEvidence | None = None
    ledger: RunLedger | None = None
    attempt_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.skeleton.ok and bool(self.results) and all(r.passed for r in self.results)

    def add(self, result: StageResult) -> None:
        self.results.append(result)


def implement_outcome(report: AttemptReport) -> ImplementOutcome | None:
    """The single, final outcome line `agent implement` reports (issue #36),
    computed from everything this ticket and every earlier one can leave in
    an `AttemptReport`. `None` for a stage failure with no named outcome of
    its own — issue fetch, mirror, workspace checkout, project profile read
    or Pinned Prefix composition — where the caller falls back to a generic
    failure report; those never had a name in any earlier ticket either."""
    seam_stage = next(
        (result for result in report.skeleton.results if result.name == SEAM_SET_CONFIRMED_STAGE), None
    )
    if seam_stage is not None and not seam_stage.passed:
        return "seam-not-confirmed"
    if not report.skeleton.ok:
        return None
    if report.readiness is not None and not report.readiness.runnable:
        outcomes: dict[str, ImplementOutcome] = {
            "unsupported-environment": "unsupported-environment",
            "bootstrap-failed": "bootstrap-failed",
            "invalid-readiness-evidence": "invalid-readiness-evidence",
        }
        return outcomes[report.readiness.classification]
    if (
        report.tool_loop is not None
        and report.tool_loop.stopped_by == "targeted-diagnostic-no-progress"
    ):
        return "targeted-test-no-progress"
    if report.tool_loop is not None and report.tool_loop.stopped_by == HANDOFF_FAILURE:
        return "handoff-failure"
    if report.tool_loop is not None and report.tool_loop.stopped_by is not None:
        return "failed-limit"
    if report.delivery is None:
        return None
    if report.delivery.kind == NO_CHANGE_PRODUCED:
        return "no-change-produced"
    if report.delivery.kind != COMMITTED_AND_PUSHED or report.validation is None:
        return None
    return "delivered-snapshot" if report.validation.clean else "validation-failed"


def terminal_category(report: AttemptReport) -> TerminalOutcome:
    """Return the sole human-facing outcome category for an Attempt."""
    stopped_by = report.tool_loop.stopped_by if report.tool_loop is not None else "stage-failure"
    return terminal_outcome(
        delivery_pushed=(
            report.delivery is not None and report.delivery.kind == COMMITTED_AND_PUSHED
        ),
        validation_clean=report.validation is not None and report.validation.clean,
        stopped_by=stopped_by,
    )


def run_implement_attempt(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    token: str,
    *,
    mirror_dir: Path,
    workspace_dir: Path,
    skills_dir: Path,
    evidence_dir: Path,
    target_language: str,
    pin: PinnedModel,
    compaction_thresholds: ContextWindowTable | CompactionThresholdTable,
    price_table: PriceTable,
    effective_token_ceilings: EffectiveTokenCeilingTable,
    result_cap_limit: int,
    ceilings: LoopCeilings,
    model: InvokableToolModel,
    attempt_number: int,
    token_env: str,
    toolchain_matrix_path: Path = DEFAULT_TOOLCHAIN_MATRIX_PATH,
    on_progress: Callable[[str], None] = lambda _message: None,
    ledger: RunLedger | None = None,
) -> AttemptReport:
    """S3.5 (issue #34): everything `run_implement_skeleton` stops short of
    — read the Project Profile at the Base Revision, open the model's
    bounded tool loop on a toolset scoped to the workspace (file
    read/write/edit and `test_targeted`, and nothing that can touch GitHub),
    then commit and push the Delivery Snapshot.

    The Agent Identity is resolved here, lazily, right before it is first
    needed (the commit) — not eagerly up front — so a Target Issue that
    fails an earlier stage never spends the `GET /user` call. `attempt_number`
    is an explicit, honest input (the same trade-off `run_implement_skeleton`
    makes for `target_language`): no Run Ledger exists yet to source the
    Attempt count from.

    `on_progress` is forwarded to `run_tool_loop` as-is (see its docstring)
    -- the tool loop is the one stage here that can run for a long time
    and, without it, reports nothing back until it returns or a ceiling
    stops it."""
    attempt_id = f"#{issue_number}/{attempt_number}"
    skeleton = run_implement_skeleton(
        client,
        owner,
        repo,
        issue_number,
        token,
        mirror_dir=mirror_dir,
        workspace_dir=workspace_dir,
        skills_dir=skills_dir,
        target_language=target_language,
        pin=pin,
        compaction_thresholds=compaction_thresholds,
        compose_prefix=False,
    )
    report = AttemptReport(skeleton=skeleton, ledger=ledger, attempt_id=attempt_id)
    if not skeleton.ok:
        return report
    assert skeleton.workspace is not None
    assert skeleton.issue is not None
    workspace = skeleton.workspace
    issue = skeleton.issue

    try:
        matrix = _load_supported_toolchain_matrix(toolchain_matrix_path)
    except (OSError, json.JSONDecodeError, MalformedToolchainMatrix, ValueError) as exc:
        report.add(
            StageResult(
                "environment readiness", False, f"could not load Supported Toolchain Matrix: {exc}"
            )
        )
        return report

    continuation_exists = ledger is not None and any(
        record.kind == "handoff" for record in ledger.records(attempt_id)
    )
    if evidence_dir.exists() and not continuation_exists:
        shutil.rmtree(evidence_dir)
    readiness = prepare_environment(
        workspace.path,
        evidence_dir,
        matrix,
        os.environ,
        SocketServiceProber(),
        CredentialStrippedCommandRunner([token_env]),
    )
    report.readiness = readiness
    report.profile = readiness.profile
    if readiness.profile is not None:
        report.add(StageResult("project profile read", True, f"language={readiness.profile.language}"))
    report.add(StageResult("environment readiness", readiness.runnable, readiness.detail))
    if ledger is not None and readiness.runnable:
        ledger.record_progress(
            attempt_id,
            PROGRESS_READINESS,
            BudgetSnapshot(0.0, UsageTotals()),
            artifact_ref=str(readiness.artifact_dir) if readiness.artifact_dir else None,
        )
    if not readiness.runnable:
        return report
    assert readiness.profile is not None
    profile = readiness.profile

    compose_pinned_prefix_for_report(
        skeleton, skills_dir, target_language, pin, compaction_thresholds
    )
    if not skeleton.ok:
        return report
    assert skeleton.pinned_prefix is not None
    context_window = context_window_for(pin, compaction_thresholds)
    if context_window is None:
        report.add(
            StageResult(
                "context-window configuration",
                False,
                f"no context-window configuration for pinned model {pin.key!r}",
            )
        )
        return report

    # Checked before any model call, like the Price Table entry ADR 0009
    # requires at startup: no provider exposes a rate through any API, so
    # this is knowable up front rather than discovered mid-loop.
    price = price_for(price_table, pin)
    if price is None:
        report.add(
            StageResult(
                "price table entry", False, f"no Price Table entry for pinned model {pin.key!r}"
            )
        )
        return report

    # Like price and compaction entries, this is pin-specific configuration
    # known before model invocation. It budgets effective model work: fresh
    # input (including cache writes) and output, never repeated cache reads.
    effective_token_ceiling = effective_token_ceiling_for(effective_token_ceilings, pin)
    if effective_token_ceiling is None:
        report.add(
            StageResult(
                "effective token ceiling entry",
                False,
                f"no effective token ceiling configured for pinned model {pin.key!r}",
            )
        )
        return report

    evidence_dir.mkdir(parents=True, exist_ok=True)
    test_targeted_context = CommandContext(
        CredentialStrippedCommandRunner([token_env]),
        workspace.path / profile.working_directory,
        evidence_dir,
        workspace_root=workspace.path,
    )
    result_store = FilesystemArtifactStore(root=evidence_dir / "tool-results")
    tools = (
        *build_file_tools(workspace.path),
        build_test_targeted_tool(
            profile,
            test_targeted_context,
            artifact_store=result_store,
            inline_limit=result_cap_limit,
            attempt_id=attempt_id,
            redactions={token_env: token},
        ),
        build_read_result_slice_tool(result_store),
    )
    assert_no_skill_path_resolver(tools)

    opening_messages = build_opening_messages(skeleton.pinned_prefix, issue)
    active_ceilings = replace(ceilings, max_effective_tokens=effective_token_ceiling)
    policy = AttemptPolicy(attempt_id, active_ceilings, ledger=ledger)
    tool_loop_result = run_tool_loop(
        model,
        tools,
        opening_messages,
        price=price,
        context_window=context_window,
        result_cap_limit=result_cap_limit,
        result_store=result_store,
        ceilings=active_ceilings,
        usage_ledger=InMemoryUsageLedger(),
        on_progress=on_progress,
        cache_breakpoints=pin.provider == "anthropic",
        policy=policy,
        progress_probe=lambda: candidate_diff_signature(workspace),
        progress_detector=lambda response: detect_progress_markers(response.content),
        redactions={token_env: token},
    )
    report.tool_loop = tool_loop_result
    detail = f"{tool_loop_result.tool_call_count} tool call(s)"
    if tool_loop_result.stopped_by is not None:
        if tool_loop_result.stopped_by == "targeted-diagnostic-no-progress":
            detail += f"; stopped by {tool_loop_result.stopped_by!r}"
        else:
            detail += f"; stopped by the {tool_loop_result.stopped_by!r} ceiling"
    report.add(
        StageResult("tool loop completed", tool_loop_result.stopped_by is None, detail)
    )
    if tool_loop_result.stopped_by == HANDOFF_FAILURE:
        return report

    try:
        identity, _expiration, _response = resolve_identity(client)
    except (StartupCheckFailed, TokenRejected) as exc:
        report.add(
            StageResult("agent identity resolved", False, f"could not resolve identity: {exc}")
        )
        return report
    report.add(StageResult("agent identity resolved", True, f"login={identity.login}"))

    delivery = deliver_snapshot(
        workspace,
        remote_url=remote_url(owner, repo, token),
        identity=identity,
        issue_number=issue.number,
        issue_title=issue.title,
        attempt_number=attempt_number,
        redact=token,
    )
    report.delivery = delivery
    report.add(StageResult("delivery", True, delivery.kind))

    # A hard limit now enters deterministic finalization: the existing tree
    # is already committed and pushed under ADR 0002, and one bounded harness
    # invocation may establish Validation Evidence. No model call is made.
    finalization_stops = {
        None,
        "wall_clock",
        "cost",
        "tokens",
        "tool_calls",
        "verification-reserve",
    }
    if tool_loop_result.stopped_by in finalization_stops and delivery.kind == COMMITTED_AND_PUSHED:
        deadline = tool_loop_result.finalization_deadline
        if deadline is not None and time.monotonic() >= deadline:
            report.add(
                StageResult(
                    "validation",
                    False,
                    "finalization reserve was exhausted; validation was not started",
                )
            )
            return report
        try:
            validation = _validate_delivery_against_base_revision(
                workspace, profile, evidence_dir, token_env
            )
        except GitFailure as exc:
            report.add(StageResult("validation", False, f"could not read the Base Revision: {exc}"))
            return report
        if deadline is not None and time.monotonic() >= deadline:
            report.add(
                StageResult(
                    "validation",
                    False,
                    "finalization validation exceeded the remaining wall-clock budget",
                )
            )
            return report
        report.validation = validation
        if ledger is not None:
            ledger.record_progress(
                attempt_id,
                PROGRESS_VALIDATION_RESULT,
                BudgetSnapshot(tool_loop_result.elapsed_seconds, tool_loop_result.usage),
                artifact_ref=str(evidence_dir / "validate"),
                diff_ref=delivery.commit_sha,
            )
        report.add(
            StageResult(
                "validation",
                validation.clean,
                f"{len(validation.passed)} passed, {len(validation.baseline_failures)} baseline "
                f"failure(s) excused, {len(validation.regressions)} regression(s), "
                f"{len(validation.unclaimable)} unclaimable, {len(validation.missing_evidence)} "
                f"missing evidence",
            )
        )

    return report


def _validate_delivery_against_base_revision(
    workspace: Workspace, profile: ProjectProfile, evidence_dir: Path, token_env: str
) -> ValidationEvidence:
    """S2's harness (`coding_agent.validate.harness.validate`), reused rather
    than reimplemented (issue #36): the Validation Contract against the
    Delivery Snapshot the workspace now holds, then lazily against the Base
    Revision for whatever failed — read from a throwaway `git worktree`
    rather than a second full clone."""
    delivery_evidence_dir = evidence_dir / "validate" / "delivery"
    delivery_evidence_dir.mkdir(parents=True, exist_ok=True)
    delivery_context = CommandContext(
        CredentialStrippedCommandRunner([token_env]),
        workspace.path / profile.working_directory,
        delivery_evidence_dir,
    )
    base_worktree_dir = evidence_dir / "base-revision-worktree"
    checkout_base_revision_worktree(workspace, base_worktree_dir)
    try:
        base_evidence_dir = evidence_dir / "validate" / "base"
        base_evidence_dir.mkdir(parents=True, exist_ok=True)
        base_context = CommandContext(
            CredentialStrippedCommandRunner([token_env]),
            base_worktree_dir / profile.working_directory,
            base_evidence_dir,
        )
        base_runner = CommandBaseRevisionRunner(profile, base_context)
        return validate(profile, delivery_context, base_runner)
    finally:
        remove_worktree(workspace, base_worktree_dir)


def _load_supported_toolchain_matrix(path: Path) -> SupportedToolchainMatrix:
    """Load the image-produced matrix before the pre-model readiness gate."""
    return load_toolchain_matrix(json.loads(path.read_text(encoding="utf-8")))


def load_supported_toolchain_matrix(path: Path) -> SupportedToolchainMatrix:
    """Load a Supported Toolchain Matrix for the command-level readiness gate."""
    return _load_supported_toolchain_matrix(path)


def resolve_toolchain_matrix_path(path: Path | None = None) -> Path:
    """Resolve the matrix source for the container and local workflows.

    The image keeps its immutable build output at ``/opt``. A host checkout
    uses the generated file under ``tmp`` instead, unless the operator passes
    a path explicitly or sets ``CODING_AGENT_TOOLCHAIN_MATRIX``.
    """
    if path is not None:
        return path.expanduser().absolute()
    configured = os.environ.get(TOOLCHAIN_MATRIX_ENV)
    if configured:
        return Path(configured).expanduser().absolute()
    if DEFAULT_TOOLCHAIN_MATRIX_PATH.is_file():
        return DEFAULT_TOOLCHAIN_MATRIX_PATH
    return (Path.cwd() / LOCAL_TOOLCHAIN_MATRIX_PATH).absolute()
