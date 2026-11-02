"""The M1 ``implement`` step's tool set.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 ("The model's
toolset contains no GitHub mutation of any kind: no label, comment,
assignment or pull-request tool"), §8 ("Every step gets the same tool
set", "``activate_*`` for each Skill Bundle member"; "by position:
``report_completion`` in the final step"; "No skill is injected eagerly";
"No tool resolves a path under a skill's directory"), §9 (the
runtime-owned targeted-test tool runs the Validation Contract's
``test_targeted`` command with ``{path}`` substituted, is the sole writer
of the per-Attempt ``{evidence_dir}``, parses the JUnit result and records
Criterion Evidence; the ``report_completion`` schema is generated per
Attempt from that Attempt's Acceptance Criteria) and §11 (an oversized
tool result is capped to head plus tail plus a pointer naming the stored
artifact and its full size; activation results are exempt and always stay
whole).

MVP row ids: L2-2, L3-SEL-11, L3-CLR-19, L3-IMP-9, L3-IMP-10, L3-IMP-20,
C-SKL-1, C-SKL-2, L3-IMP-1, L3-IMP-5.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from langchain_core.tools import BaseTool

from coding_agent.profile.schema import ProjectProfile
from coding_agent.runtime.completion import (
    CriterionEvidence,
    build_report_completion_tool,
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
    artifact_store: ArtifactStore | None = None,
    inline_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    attempt_id: str = "attempt",
    redactions: Mapping[str, str] | None = None,
) -> tuple[BaseTool, ...]:
    """The work tools, the activation tools, ``test_targeted`` and ``report_completion``.

    The work tools are the workspace-scoped file tools (read, write, edit,
    list, find, search, read-lines); there is no GitHub mutation tool of any
    kind in this set, and there is no shell tool — tests the model runs
    through any channel other than ``test_targeted`` count for nothing
    because nothing else writes Criterion Evidence (§9). The four
    ``activate_*`` tools are bound from the first turn (C-SKL-1), each
    carrying its skill's frontmatter ``description`` verbatim; a repeated
    activation returns "already active", not the body. ``test_targeted`` is
    the runtime-owned targeted-test tool (L2-2): it runs the Validation
    Contract's ``test_targeted`` command with ``{path}`` substituted, is the
    sole writer of the per-Attempt ``{evidence_dir}`` (file tools are scoped
    to the workspace, which never contains that directory), parses the JUnit
    result and records Criterion Evidence as a history across the Attempt.
    ``report_completion`` is offered by position (the M1 Attempt runs the
    single ``implement`` step, which is final): its schema is generated per
    Attempt from that Attempt's Acceptance Criteria, with one required
    property per ``AC-n`` id — the covering test identifiers or the explicit
    ``uncovered`` string.

    ``skills_dir`` has no default: the caller names the Skill Bundle it
    installed, so nothing silently reads the current user's home.
    ``artifact_store`` owns the full text of capped results; the
    ``read_result_slice`` tool the capped pointer names reads from that
    same store, so a slice the model asks for comes back from what was
    stashed (L3-IMP-5). A caller that passes none gets a private
    in-memory store — fine for tests, never shared across Attempts.
    """
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
    return (
        *build_file_tools(workspace),
        *build_activation_tools(skills_dir),
        build_test_targeted_tool(
            profile,
            targeted_context,
            artifact_store=store,
            inline_limit=inline_limit,
            attempt_id=attempt_id,
            redactions=redactions,
            on_junit=criterion_evidence.record_junit,
        ),
        build_report_completion_tool(criteria_ids, sink),
        build_read_result_slice_tool(store),
    )
