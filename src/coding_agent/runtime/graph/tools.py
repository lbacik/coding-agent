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
whole).

MVP row ids: L2-2, L3-SEL-11, L3-CLR-19, L3-IMP-9, L3-IMP-10, L3-IMP-20,
C-SKL-1, C-SKL-2, L3-IMP-1, L3-IMP-5, C-IMP-1, L3-IMP-28, L3-IMP-29,
W-STP-2, W-STP-4.
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
from coding_agent.skillbundle.activation import build_activation_tools
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


def build_activate_step_tool(prompt_bytes: bytes) -> BaseTool:
    """The ``activate_step`` tool for a ``prompt_file`` step (contract §8).

    Its result is the prompt file's bytes, verbatim and exempt from result
    capping (the ``activate_`` prefix keeps it whole). The first call returns
    the bytes decoded as UTF-8; a repeated call returns "already active", the
    same shape as the Skill Bundle activation tools. A Context-Window Handoff
    inside the step reopens it with a fresh tool over identical bytes
    (W-STP-4), so the two activations return byte-identical text.

    MVP row ids: W-STP-2, W-STP-4.
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
    force-accepts the third call — and ``report_step`` in every non-final
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
            criteria_ids, sink, criterion_evidence=criterion_evidence, claim_record=record
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
    return (
        *build_file_tools(workspace),
        *build_activation_tools(skills_dir),
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
