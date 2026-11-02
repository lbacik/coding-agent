"""The M1 ``implement`` step's tool set.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 ("The model's
toolset contains no GitHub mutation of any kind: no label, comment,
assignment or pull-request tool") and §8 ("Every step gets the same tool
set", "by position: ... ``report_completion`` in the final step").

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-SKL-1.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool, tool

from coding_agent.runtime.toolset import build_file_tools

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


@tool("report_completion")
def report_completion(summary: str) -> str:
    """Declare the implementation stage finished with a bounded justification.

    The only non-exceptional exit from the final step (§9 in full; M1 takes
    the positional exit alone). M2 grows the schema per Acceptance Criterion.
    """
    return f"completion recorded: {summary}"


def build_implement_step_tools(workspace: Path) -> tuple[BaseTool, ...]:
    """The work tools plus ``report_completion`` — and nothing else.

    The work tools are the workspace-scoped file tools (read, write, edit,
    list, find, search, read-lines); there is no GitHub mutation tool of any
    kind in this set. ``report_completion`` is offered by position: the M1
    Attempt runs the single ``implement`` step, which is final.
    """
    return (*build_file_tools(workspace), report_completion)
