from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import StartupCheckFailed, resolve_identity
from coding_agent.identity.token import TokenRejected
from coding_agent.implement.ceilings import InMemoryUsageLedger, LoopCeilings
from coding_agent.implement.delivery import (
    COMMITTED_AND_PUSHED,
    NO_CHANGE_PRODUCED,
    DeliverySnapshotOutcome,
    deliver_snapshot,
)
from coding_agent.implement.git import (
    GitFailure,
    Workspace,
    checkout_base_revision_worktree,
    remove_worktree,
)
from coding_agent.implement.loop import ToolLoopResult, build_opening_messages, run_tool_loop
from coding_agent.implement.pinned_prefix import CompactionThresholdTable, assert_no_skill_path_resolver
from coding_agent.implement.result_capping import FilesystemArtifactStore, build_read_result_slice_tool
from coding_agent.implement.skeleton import (
    SEAM_SET_CONFIRMED_STAGE,
    StageResult,
    SkeletonReport,
    remote_url,
    run_implement_skeleton,
)
from coding_agent.implement.toolset import build_file_tools, build_test_targeted_tool
from coding_agent.profile.parser import MissingReadinessFacts, UnknownSchema, parse_profile_yaml
from coding_agent.profile.schema import ProjectProfile
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.effective_token_ceiling import (
    EffectiveTokenCeilingTable,
    effective_token_ceiling_for,
)
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.validate.baseline import ValidationEvidence
from coding_agent.validate.harness import CommandBaseRevisionRunner, CommandContext, validate
from coding_agent.validate.runner import CredentialStrippedCommandRunner

DEFAULT_PROFILE_RELATIVE_PATH = Path("docs") / "agents" / "project-profile.yml"

ImplementOutcome = Literal[
    "delivered-snapshot",
    "no-change-produced",
    "seam-not-confirmed",
    "failed-limit",
    "validation-failed",
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
    tool_loop: ToolLoopResult | None = None
    delivery: DeliverySnapshotOutcome | None = None
    validation: ValidationEvidence | None = None

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
    if report.tool_loop is not None and report.tool_loop.stopped_by is not None:
        return "failed-limit"
    if report.delivery is None:
        return None
    if report.delivery.kind == NO_CHANGE_PRODUCED:
        return "no-change-produced"
    if report.delivery.kind != COMMITTED_AND_PUSHED or report.validation is None:
        return None
    return "delivered-snapshot" if report.validation.clean else "validation-failed"


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
    compaction_thresholds: CompactionThresholdTable,
    price_table: PriceTable,
    effective_token_ceilings: EffectiveTokenCeilingTable,
    result_cap_limit: int,
    ceilings: LoopCeilings,
    model: InvokableToolModel,
    attempt_number: int,
    token_env: str,
    on_progress: Callable[[str], None] = lambda _message: None,
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
    )
    report = AttemptReport(skeleton=skeleton)
    if not skeleton.ok:
        return report
    assert skeleton.workspace is not None
    assert skeleton.pinned_prefix is not None
    assert skeleton.issue is not None
    workspace = skeleton.workspace
    issue = skeleton.issue

    # Reads the profile file directly rather than through
    # `profile.readiness.evaluate_readiness` — the fuller node that also
    # covers the prose fallback chain, the toolchain assertion and service
    # probing. Nothing upstream of this ticket wires `evaluate_readiness`
    # into a command yet, so this is a strict, intentionally narrower
    # subset; reconciling the two into one path is a later ticket's job.
    profile_path = workspace.path / DEFAULT_PROFILE_RELATIVE_PATH
    try:
        profile_text = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.add(
            StageResult(
                "project profile read", False, f"could not read {profile_path}: {exc}"
            )
        )
        return report
    profile_outcome = parse_profile_yaml(profile_text)
    if isinstance(profile_outcome, (MissingReadinessFacts, UnknownSchema)):
        report.add(StageResult("project profile read", False, str(profile_outcome)))
        return report
    profile = profile_outcome
    report.profile = profile
    report.add(StageResult("project profile read", True, f"language={profile.language}"))

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
    )
    result_store = FilesystemArtifactStore(root=evidence_dir / "tool-results")
    tools = (
        *build_file_tools(workspace.path),
        build_test_targeted_tool(profile, test_targeted_context),
        build_read_result_slice_tool(result_store),
    )
    assert_no_skill_path_resolver(tools)

    opening_messages = build_opening_messages(skeleton.pinned_prefix, issue)
    tool_loop_result = run_tool_loop(
        model,
        tools,
        opening_messages,
        price=price,
        compaction_threshold=compaction_thresholds[pin.key],
        result_cap_limit=result_cap_limit,
        result_store=result_store,
        ceilings=replace(ceilings, max_effective_tokens=effective_token_ceiling),
        usage_ledger=InMemoryUsageLedger(),
        on_progress=on_progress,
        cache_breakpoints=pin.provider == "anthropic",
    )
    report.tool_loop = tool_loop_result
    detail = f"{tool_loop_result.tool_call_count} tool call(s)"
    if tool_loop_result.stopped_by is not None:
        detail += f"; stopped by the {tool_loop_result.stopped_by!r} ceiling"
    report.add(
        StageResult("tool loop completed", tool_loop_result.stopped_by is None, detail)
    )

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

    # A ceiling crossed mid-loop ends the Attempt on `failed-limit` directly
    # (T12): whatever the loop managed is still committed and pushed above,
    # but it is not validated. `no-change-produced` likewise has no tree of
    # its own to validate.
    if tool_loop_result.stopped_by is None and delivery.kind == COMMITTED_AND_PUSHED:
        try:
            validation = _validate_delivery_against_base_revision(
                workspace, profile, evidence_dir, token_env
            )
        except GitFailure as exc:
            report.add(StageResult("validation", False, f"could not read the Base Revision: {exc}"))
            return report
        report.validation = validation
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
