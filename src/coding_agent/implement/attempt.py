from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import StartupCheckFailed, resolve_identity
from coding_agent.identity.token import TokenRejected
from coding_agent.implement.ceilings import InMemoryUsageLedger, LoopCeilings
from coding_agent.implement.delivery import DeliverySnapshotOutcome, deliver_snapshot
from coding_agent.implement.loop import ToolLoopResult, build_opening_messages, run_tool_loop
from coding_agent.implement.pinned_prefix import CompactionThresholdTable, assert_no_skill_path_resolver
from coding_agent.implement.result_capping import FilesystemArtifactStore, build_read_result_slice_tool
from coding_agent.implement.skeleton import StageResult, SkeletonReport, remote_url, run_implement_skeleton
from coding_agent.implement.toolset import build_file_tools, build_test_targeted_tool
from coding_agent.profile.parser import MissingReadinessFacts, UnknownSchema, parse_profile_yaml
from coding_agent.profile.schema import ProjectProfile
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.runner import CredentialStrippedCommandRunner

DEFAULT_PROFILE_RELATIVE_PATH = Path("docs") / "agents" / "project-profile.yml"


@dataclass
class AttemptReport:
    """`SkeletonReport` (issues #30, #32, #33) plus the stages this ticket
    adds: the Project Profile read from the workspace, the model's bounded
    tool loop, and the Delivery Snapshot commit and push."""

    skeleton: SkeletonReport
    results: list[StageResult] = field(default_factory=list)
    profile: ProjectProfile | None = None
    tool_loop: ToolLoopResult | None = None
    delivery: DeliverySnapshotOutcome | None = None

    @property
    def ok(self) -> bool:
        return self.skeleton.ok and bool(self.results) and all(r.passed for r in self.results)

    def add(self, result: StageResult) -> None:
        self.results.append(result)


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
    result_cap_limit: int,
    ceilings: LoopCeilings,
    model: InvokableToolModel,
    attempt_number: int,
    token_env: str,
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
    Attempt count from."""
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
        ceilings=ceilings,
        usage_ledger=InMemoryUsageLedger(),
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

    return report
