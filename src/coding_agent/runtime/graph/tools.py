"""The M2 step tool set, one fresh set per Workflow Step.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 ("The model's
toolset contains no GitHub mutation of any kind: no label, comment,
assignment or pull-request tool"), §8 ("Every step gets the same tool
set", "``activate_*`` for each Skill Bundle member", "and ``activate_step``
in a ``prompt_file`` step"; "by position: ``report_step`` in every
non-final step", "``report_completion`` in the final step"; "No skill is
injected eagerly"; "No tool resolves a path under a skill's directory";
a ``prompt_file`` step opens with a forced ``activate_step`` whose result
is the prompt file's bytes, verbatim and exempt from result capping),
§9 (the runtime-owned targeted-test tool runs the Validation Contract's
``test_targeted`` command with ``{path}`` substituted, is the sole writer
of the per-Attempt ``{evidence_dir}``, parses the JUnit result and records
Criterion Evidence; the ``report_completion`` schema is generated per
Attempt from that Attempt's Acceptance Criteria; a claim naming an unbacked
test is returned naming every failing entry, and after two returns the
third call is force-accepted; the model writes no JUnit file into
``{evidence_dir}`` through any tool it holds) and §11 (an oversized
tool result is capped to head plus tail plus a pointer naming the stored
artifact and its full size; activation results are exempt and always stay
whole), §10 (the review snapshot commits the current tree and pushes the
Attempt's branch before either reviewer starts), §13 (the write sequence
keyed by ``(attempt_id, write_kind)`` with the ordinal in the kind) and §18
(one branch per Attempt; the credential in the push URL is redacted from
every failure).

MVP row ids: L2-2, L3-SEL-11, L3-CLR-19, L3-IMP-9, L3-IMP-10, L3-IMP-20,
C-SKL-1, C-SKL-2, L3-IMP-1, L3-IMP-5, C-IMP-1, L3-IMP-28, L3-IMP-29,
W-STP-2, W-STP-4, L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10, P-DEL-4, C-REV-1,
L3-REV-1, L3-REV-2, G-REV-1, G-REV-2, W-STP-6.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.profile.schema import ProjectProfile
from coding_agent.runtime.completion import (
    ClaimRecord,
    CriterionEvidence,
    StepReportRecord,
    build_report_completion_tool,
    build_report_step_tool,
)
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
    InMemoryArtifactStore,
    build_read_result_slice_tool,
)
from coding_agent.runtime.toolset import (
    build_file_tools,
    build_test_targeted_tool,
)
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.git import Workspace, tree_differs_from_base
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.graph.review import (
    review_tool_result_text,
    run_review_invocation,
)
from coding_agent.runtime.review_snapshot import (
    MAX_REVIEW_INVOCATIONS,
    ReviewSnapshotRefused,
    ReviewSnapshotState,
    publish_review_snapshot,
)
from coding_agent.skillbundle.activation import activation_body, build_activation_tools
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.runner import CommandRunner, SubprocessCommandRunner

FORBIDDEN_MODEL_TOOL_NAMES = frozenset(
    {
        "apply_selection_label",
        "add_label",
        "remove_label",
        "post_comment",
        "create_comment",
        "assign_issue",
        "unassign_issue",
        "edit_issue",
        "update_issue",
        "open_pull_request",
        "create_pull_request",
        "update_pull_request",
    }
)
"""Tool names that would be a GitHub mutation (§1, §18).

No tool in the step's bound set may carry one of these names, and no node
may apply the Selection Label or edit the issue body. The structural test in
``tests/test_mvp_attempt_graph.py`` asserts both: the forbidden names appear
neither in the bound tool set nor in any graph node's source, and the
``TrackerPort`` offers no operation that could perform them through the back
door.
"""


ACTIVATION_TOOL_PREFIX = "activate_"
"""Tool-name prefix whose results are exempt from result capping (§11).

An activation result is the skill body (plus Companion Files), verbatim;
capping it would corrupt the skill the model was just given. Every
``activate_*`` tool — and the M2 ``activate_step`` — carries this prefix.
"""


def is_activation_tool(name: str) -> bool:
    """Whether ``name`` names an activation tool exempt from capping (§11)."""
    return name.startswith(ACTIVATION_TOOL_PREFIX)



def _with_review_snapshot(
    activation_tools: tuple[BaseTool, ...],
    *,
    skills_dir: Path,
    ledger: RunLedger | None,
    review_workspace: Workspace | None,
    attempt_id: str | None,
    issue_number: int | None,
    attempt_number: int | None,
    issue_title: str | None,
    identity: Identity | None,
    remote_url: str | None,
    state: ReviewSnapshotState,
    model: InvokableToolModel | None = None,
    review_issue_body: str | None = None,
    review_base_revision: str | None = None,
    redact: str | None = None,
) -> tuple[BaseTool, ...]:
    """Wrap ``activate_code_review`` with the review-snapshot push (§10, §13, §18).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (the runtime
    commits the current tree and pushes the Attempt's branch before any
    reviewer starts), §13 (the write sequence keyed by
    ``(attempt_id, write_kind)`` with the ordinal in the kind) and §18 (one
    branch per Attempt, fast-forward only; the credential in the push URL is
    redacted from every failure).

    MVP row ids: L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10, P-DEL-4, C-REV-1,
    L3-REV-1, L3-REV-2, G-REV-1, G-REV-2, W-STP-6.

    Where no review context is bound (``ledger``, ``review_workspace``,
    ``identity`` or ``remote_url`` missing) the tool stays the plain
    skill-body activation, so earlier rows that bind the tool set without a
    remote stay green. Where bound, each call runs the snapshot push first:
    the fourth invocation in an Attempt is refused with a tool result and
    pushes nothing; otherwise the intent is recorded, the remote is looked
    up, the branch is pushed fast-forward only when absent or behind, and
    the receipt is recorded. Both reviewer seats start only after that
    receipt is recorded: the review subgraph then fans out the Standards
    and Spec conversations against the pushed tree, each with its own
    message list and read-only tool set, and the tool result carries both
    Review Reports or names the seat that made none. Where no ``model`` is
    bound (the unit-test seam) the reviewers cannot run: the result returns
    the pushed sha and states that no review ran, and the review status
    stays unmet (``incomplete``). Push refusals raise nothing out of the
    tool: the ``workflows-path`` failure is recorded on ``state`` for
    ``run_step`` to end the Attempt ``failed`` / ``workflows-path``,
    classified by write kind and target path, never by error string.
    """
    from langchain_core.tools import StructuredTool

    if (
        ledger is None
        or review_workspace is None
        or attempt_id is None
        or issue_number is None
        or attempt_number is None
        or issue_title is None
        or identity is None
        or remote_url is None
    ):
        return activation_tools
    original: BaseTool | None = next(
        (tool for tool in activation_tools if tool.name == "activate_code_review"), None
    )
    if original is None:
        return activation_tools
    description = original.description
    body = activation_body(skills_dir, "code-review")

    def _activate_code_review() -> str:
        if state.invocations >= MAX_REVIEW_INVOCATIONS:
            return (
                "Code review invocation limit reached: at most three "
                "invocations per Attempt. No review ran."
            )
        state.invocations += 1
        ordinal = state.invocations
        try:
            outcome = publish_review_snapshot(
                workspace=review_workspace,
                ledger=ledger,
                attempt_id=attempt_id,
                issue_number=issue_number,
                attempt_number=attempt_number,
                issue_title=issue_title,
                identity=identity,
                remote_url=remote_url,
                ordinal=ordinal,
                redact=redact,
            )
        except ReviewSnapshotRefused as exc:
            state.failure = exc.reason
            state.failure_write_kind = exc.write_kind
            state.failure_target = exc.target_path
            return (
                f"Review snapshot refused ({exc.reason}): {exc.target_path} "
                f"(write {exc.write_kind}). No review ran."
            )
        state.snapshots.append(outcome.remote_sha)
        if model is None:
            pushed_note = "pushed" if outcome.pushed else "already pushed"
            return (
                f"{body}\n\nReview snapshot {outcome.write_kind} {pushed_note}: "
                f"{outcome.remote_sha} on {outcome.branch_name}. No review ran."
            )
        result = run_review_invocation(
            model,
            skills_dir=skills_dir,
            workspace=review_workspace.path,
            ordinal=ordinal,
            branch_name=outcome.branch_name,
            commit_sha=outcome.remote_sha,
            base_revision=(
                review_base_revision
                if review_base_revision is not None
                else review_workspace.base_revision
            ),
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=review_issue_body if review_issue_body is not None else "",
        )
        state.record_completed_review(result.to_completed_review())
        return review_tool_result_text(
            outcome_branch_name=outcome.branch_name,
            outcome_remote_sha=outcome.remote_sha,
            outcome_pushed=outcome.pushed,
            write_kind=outcome.write_kind,
            result=result,
        )

    _activate_code_review.__name__ = "activate_code_review"
    wrapped = StructuredTool.from_function(
        func=_activate_code_review,
        name="activate_code_review",
        description=description,
    )
    return tuple(wrapped if tool.name == "activate_code_review" else tool for tool in activation_tools)


def build_activate_step_tool(prompt_bytes: bytes) -> BaseTool:
    """The ``activate_step`` tool for a ``prompt_file`` step (contract §8).

    Its result is the prompt file's bytes, verbatim and exempt from result
    capping (the ``activate_`` prefix keeps it whole). The first call returns
    the bytes decoded as UTF-8; a repeated call returns "already active", the
    same shape as the Skill Bundle activation tools. A Context-Window Handoff
    inside the step reopens it with a fresh tool over identical bytes
    (W-STP-4), so the two activations return byte-identical text.

    MVP row ids: W-STP-2, W-STP-4, L3-DEL-19, L3-DEL-20.
    """
    text = prompt_bytes.decode("utf-8")
    activated = False

    def _activate_step() -> str:
        nonlocal activated
        if activated:
            return "Step prompt is already active."
        activated = True
        return text

    _activate_step.__name__ = "activate_step"
    return StructuredTool.from_function(
        func=_activate_step,
        name="activate_step",
        description=(
            "Activate this Workflow Step's prompt file. Returns the prompt "
            "file's bytes verbatim."
        ),
    )


def build_implement_step_tools(
    workspace: Path,
    *,
    skills_dir: Path,
    profile: ProjectProfile,
    command_runner: CommandRunner | None = None,
    evidence_dir: Path,
    criterion_evidence: CriterionEvidence,
    criteria_ids: tuple[str, ...],
    claim_sink: dict[str, object] | None = None,
    claim_record: ClaimRecord | None = None,
    artifact_store: ArtifactStore | None = None,
    inline_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    attempt_id: str = "attempt",
    redactions: Mapping[str, str] | None = None,
    is_final: bool = True,
    prompt_bytes: bytes | None = None,
    step_report_record: StepReportRecord | None = None,
    review_ledger: RunLedger | None = None,
    review_workspace: Workspace | None = None,
    review_attempt_id: str | None = None,
    review_issue_number: int | None = None,
    review_attempt_number: int | None = None,
    review_issue_title: str | None = None,
    review_identity: Identity | None = None,
    review_remote_url: str | None = None,
    review_state: ReviewSnapshotState | None = None,
    review_model: InvokableToolModel | None = None,
    review_issue_body: str | None = None,
    review_base_revision: str | None = None,
    review_redact: str | None = None,
) -> tuple[BaseTool, ...]:
    """The work tools, the activation tools, ``test_targeted`` and the positional exit.

    The work tools are the workspace-scoped file tools (read, write, edit,
    list, find, search, read-lines); there is no GitHub mutation tool of any
    kind in this set, and there is no shell tool — tests the model runs
    through any channel other than ``test_targeted`` count for nothing
    because nothing else writes Criterion Evidence (§9, L3-IMP-28). The four
    ``activate_*`` tools are bound from the first turn (C-SKL-1), each
    carrying its skill's frontmatter ``description`` verbatim; a repeated
    activation returns "already active", not the body. ``test_targeted`` is
    the runtime-owned targeted-test tool (L2-2): it runs the Validation
    Contract's ``test_targeted`` command with ``{path}`` substituted, is the
    sole writer of the per-Attempt ``{evidence_dir}`` (file tools are scoped
    to the workspace, which never contains that directory), parses the JUnit
    result and records Criterion Evidence as a history across the Attempt.
    The exit is positional (contract §8, W-STP-2): ``report_completion`` in
    the final step only — its schema is generated per Attempt from that
    Attempt's Acceptance Criteria, with one required property per ``AC-n``
    id (L3-IMP-29); with ``criterion_evidence`` and ``claim_record`` bound
    it returns unbacked claims naming every failing entry (C-IMP-1) and
    force-accepts the third call; with ``review_state`` bound it also
    returns, once per Attempt, a claim made with a candidate diff (measured
    against ``review_base_revision``) and no completed review, naming the
    missing review (§9 claim check 2, P-DEL-3) — and ``report_step`` in every non-final
    step, bound to ``step_report_record``. A non-final step without a record
    is a caller error. ``activate_step`` is included only where
    ``prompt_bytes`` is set: a ``prompt_file`` step whose result is the bytes
    verbatim and exempt from result capping (W-STP-4).

    ``skills_dir`` has no default: the caller names the Skill Bundle it
    installed, so nothing silently reads the current user's home.
    ``artifact_store`` owns the full text of capped results; the
    ``read_result_slice`` tool the capped pointer names reads from that
    same store, so a slice the model asks for comes back from what was
    stashed (L3-IMP-5). A caller that passes none gets a private
    in-memory store — fine for tests, never shared across Attempts.
    """
    workspace_resolved = workspace.resolve()
    evidence_resolved = evidence_dir.resolve()
    if evidence_resolved == workspace_resolved or workspace_resolved in evidence_resolved.parents:
        raise ValueError(
            "the per-Attempt evidence_dir must live outside the workspace: "
            "file tools are workspace-scoped, and only the runtime-owned "
            "targeted-test tool may write Criterion Evidence (L3-IMP-28)"
        )
    store: ArtifactStore = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    runner: CommandRunner = (
        command_runner if command_runner is not None else SubprocessCommandRunner()
    )
    working_directory = workspace / profile.working_directory
    targeted_context = CommandContext(
        runner,
        working_directory,
        evidence_dir,
        workspace_root=workspace,
    )
    sink: dict[str, object] = claim_sink if claim_sink is not None else {}
    record = claim_record if claim_record is not None else ClaimRecord()
    if is_final:
        exit_tool: BaseTool = build_report_completion_tool(
            criteria_ids,
            sink,
            criterion_evidence=criterion_evidence,
            claim_record=record,
            review_gate=review_state,
            has_candidate_diff=(
                (lambda: tree_differs_from_base(workspace, review_base_revision))
                if review_state is not None
                else None
            ),
        )
    else:
        if step_report_record is None:
            raise ValueError(
                "a non-final Workflow Step needs a StepReportRecord "
                "for its report_step exit tool (contract §8, W-STP-2)"
            )
        exit_tool = build_report_step_tool(step_report_record)
    extra: tuple[BaseTool, ...] = (
        (build_activate_step_tool(prompt_bytes),) if prompt_bytes is not None else ()
    )
    activation_tools = build_activation_tools(skills_dir)
    if review_state is not None:
        activation_tools = _with_review_snapshot(
            activation_tools,
            skills_dir=skills_dir,
            ledger=review_ledger,
            review_workspace=review_workspace,
            attempt_id=review_attempt_id,
            issue_number=review_issue_number,
            attempt_number=review_attempt_number,
            issue_title=review_issue_title,
            identity=review_identity,
            remote_url=review_remote_url,
            state=review_state,
            model=review_model,
            review_issue_body=review_issue_body,
            review_base_revision=review_base_revision,
            redact=review_redact,
        )
    return (
        *build_file_tools(workspace),
        *activation_tools,
        *extra,
        build_test_targeted_tool(
            profile,
            targeted_context,
            artifact_store=store,
            inline_limit=inline_limit,
            attempt_id=attempt_id,
            redactions=redactions,
            on_junit=criterion_evidence.record_junit,
        ),
        exit_tool,
        build_read_result_slice_tool(store),
    )
