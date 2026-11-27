"""One Advisor per ``ask_advisor`` call: a read-only consultation (contract §9).

An agent step that declares the ``advisor`` tool group lets its model ask
questions with ``ask_advisor(question)``. Each call opens a fresh Advisor
conversation on the pin the step's ``agent.advisor`` names — or the Pinned
Model where it names none — with no caller history. The Advisor binds
exactly the Sub-agent read-only set plus its own ``report_advice`` exit,
runs the same bounded read-only loop as a Sub-agent
(:func:`coding_agent.runtime.graph.subagent.run_subagent`), and returns
its report (or its ending) to the caller as the tool result.

[ADR 0036](../../../docs/adr/0036-an-advisor-answers-on-the-pin-its-step-names-in-a-fresh-read-only-conversation.md):
one question, one fresh conversation; no persistent session; no concurrent
Advisors; the runtime never checks whether the caller follows an answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.runtime.conversation_context import (
    ConversationContextState,
    measure_conversation_context,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_READ_TOOL_NAMES,
    SubagentRecord,
    SubagentResult,
    build_read_change_tool,
)
from coding_agent.runtime.result_capping import (
    ArtifactStore,
    InMemoryArtifactStore,
)
from coding_agent.runtime.toolset import build_file_tools

ASK_ADVISOR_TOOL = "ask_advisor"
"""The Workflow Step tool that asks one Advisor question (contract §9)."""

REPORT_ADVICE_TOOL = "report_advice"
"""An Advisor's only exit: its answer carrier (contract §9)."""

ADVISOR_STARTED_KIND = "advisor_started"
"""Run Ledger event kind: one Advisor conversation started (contract §9)."""

ADVISOR_ENDED_KIND = "advisor_ended"
"""Run Ledger event kind: one Advisor conversation ended (contract §9)."""

AdvisorEnding = Literal[
    "reported",
    "silent",
    "round-limit",
    "ceiling",
    "context-window",
    "stopped",
    "provider-failure",
    "pin-violation",
]
"""One ``advisor_ended`` event's ending (contract §9).

The run's own reason — ``reported``, ``silent``, ``round-limit``,
``ceiling``, ``context-window``, ``stopped`` or ``provider-failure`` —
plus ``pin-violation`` where the Advisor's request violated its pin and
the violation propagated instead of returning a result. An Admission
refusal on the Advisor pin ends ``ceiling``, never ``refused``: every
Advisor request is admitted individually, with no spawn-group
pre-admission.
"""

ADVISOR_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_directory",
        "find_files",
        "search_text",
        "read_lines",
        "read_result_slice",
        "read_change",
        "report_advice",
    }
)
"""The exact bound set of an Advisor conversation (contract §9).

The Sub-agent read-only set — the workspace readers, the ``read_change``
reader carrying the deliverable change, the capped-result range reader —
with the Advisor's own ``report_advice`` exit in place of the Sub-agent
report. No write tool, no shell, no activation tool, no step exit, no
``spawn_subagent`` and no ``ask_advisor`` of its own, so an Advisor can
neither change the tree nor consult another Advisor.
"""

ADVISOR_QUESTION_MAX_CHARS = 64 * 1024
"""An Advisor ``question`` is at most 64 KiB (contract §9)."""

ADVISOR_REPORT_MAX_CHARS = 32 * 1024
"""An Advisor answer over 32 KiB is returned once for shortening (§9).

The same bound as a Sub-agent report: a first over-size answer is
returned with a request to shorten it and records nothing, while the
second over-size answer is accepted.
"""

ADVISOR_QUESTION_PREVIEW_CHARS = 120
"""Characters of the question the Execution Summary lists per consultation."""


def advisor_question_preview(question: str) -> str:
    """The first 120 characters of one Advisor question (§9)."""
    return question[:ADVISOR_QUESTION_PREVIEW_CHARS]


ADVISOR_FRAMING = (
    "You are an Advisor. Answer the question under the operator's role below.\n"
    "\n"
    "You hold read-only tools: read_file, list_directory, find_files, "
    "search_text, read_lines, read_change and read_result_slice. Use them "
    "to inspect the tree; you cannot change it. Call read_change with no "
    "arguments to review the change that would be delivered; you have no "
    "shell, so do not hand yourself git commands.\n"
    "\n"
    "Your run ends only through report_advice(answer): call it once with "
    "your answer to the question. A run that ends without report_advice is "
    "not a completed run. There is no user to ask: work only from the "
    "question, the operator's role and the Attempt data below."
)
"""The Worker-authored framing opening every Advisor conversation (§9).

It names the role (Advisor, answering under the operator's role below),
the read-only bound, the single report exit and the missing user. The
opening is this framing, then items 1–3 of the caller's Pinned Prefix
(the Attempt Header facts, the input framed as data and the caller's
declared inputs — never the caller's Step Result contract), then the
operator's ``advisor.prompt`` bytes verbatim, then the question verbatim.
"""


def compose_advisor_opening(
    *,
    attempt_header: str,
    workflow_input: str,
    comments: str | None,
    declared_inputs: str | None,
    prompt_text: str,
    question: str,
) -> str:
    """One Advisor's opening: framing, caller data, prompt, question (§9)."""
    parts = [ADVISOR_FRAMING, attempt_header, workflow_input]
    if comments is not None:
        parts.append(comments)
    if declared_inputs is not None:
        parts.append(declared_inputs)
    parts.append(prompt_text)
    parts.append(question)
    return "\n\n".join(parts)


def validate_advisor_args(question: object) -> str | None:
    """Refuse an invalid ``ask_advisor`` call without starting anything (§9).

    Returns the caller-facing error text where ``question`` is not a
    non-blank string of at most 64 KiB, else ``None``. An invalid call
    starts nothing and writes no ledger event — like an invalid
    ``spawn_subagent`` call.
    """
    if not isinstance(question, str) or not question.strip():
        return (
            "Error: ask_advisor 'question' must be a non-blank string."
        )
    if len(question.encode("utf-8")) > ADVISOR_QUESTION_MAX_CHARS:
        return (
            "Error: ask_advisor 'question' must be at most "
            f"{ADVISOR_QUESTION_MAX_CHARS} bytes."
        )
    return None


def build_report_advice_tool(record: SubagentRecord) -> BaseTool:
    """An Advisor's ``report_advice`` exit tool (contract §9).

    Shares the Sub-agent record shape: a blank answer is refused and
    records nothing; a first over-size answer is returned with a request
    to shorten it and records nothing, while the second over-size answer
    is accepted.
    """

    def _report_advice(answer: str) -> str:
        if not isinstance(answer, str) or not answer.strip():
            return (
                "Error: an Advisor answer needs findings or an explicit "
                '"no findings"; nothing was recorded.'
            )
        if len(answer.encode("utf-8")) > ADVISOR_REPORT_MAX_CHARS and not record.oversize_returned:
            record.oversize_returned = True
            return (
                f"The answer is {len(answer.encode('utf-8'))} bytes, over the "
                f"{ADVISOR_REPORT_MAX_CHARS}-byte bound. Shorten it to "
                "at most that many bytes and call report_advice again."
            )
        if record.report is not None:
            return "The Advisor answer is already recorded."
        record.report = answer
        return "The Advisor answer is recorded."

    _report_advice.__name__ = "report_advice"
    return StructuredTool.from_function(
        func=_report_advice,
        name=REPORT_ADVICE_TOOL,
        description=(
            "End the Advisor run with its answer. The answer is the "
            'response to the question, or the explicit string "no findings".'
        ),
    )


def build_ask_advisor_tool() -> BaseTool:
    """The ``ask_advisor`` tool bound in a declaring step (contract §9).

    The step loop intercepts this tool's calls before invocation — one
    call opens one Advisor there — so this function body only ever runs on
    a direct invocation outside that loop. It still validates its input
    the same way the loop does, so a direct call with an invalid question
    is refused with the same error text.
    """

    def _ask_advisor(question: str) -> str:
        error = validate_advisor_args(question)
        if error is not None:
            return error
        return (
            "Error: ask_advisor opens an Advisor in the step loop; "
            "a direct invocation opens nothing."
        )

    _ask_advisor.__name__ = "ask_advisor"
    return StructuredTool.from_function(
        func=_ask_advisor,
        name=ASK_ADVISOR_TOOL,
        description=(
            "Ask the Advisor one question. One call opens one fresh "
            "read-only Advisor conversation on the step's Advisor pin; "
            "the result carries the Advisor's answer, or names why it "
            "made none. Calls in one turn run one at a time in call order."
        ),
    )


def build_advisor_tools(
    workspace: Path,
    record: SubagentRecord,
    *,
    artifact_store: ArtifactStore | None = None,
    base_revision: str | None = None,
    change_artifact_id: str = "read-change",
    base_tip: str | None = None,
) -> tuple[BaseTool, ...]:
    """One Advisor's isolated, read-only tool set (contract §9).

    The workspace-scoped read tools, the ``read_change`` reader over what
    the Delivery Snapshot would commit, the ``read_result_slice`` range
    reader over the caller's artifact store, plus the Advisor's own
    ``report_advice`` exit. The bound set is exactly
    ``ADVISOR_TOOL_NAMES`` — asserted structurally, so an Advisor can
    neither change the tree, spawn a Sub-agent nor consult another
    Advisor.
    """
    read_tools = tuple(
        tool for tool in build_file_tools(workspace) if tool.name in SUBAGENT_READ_TOOL_NAMES
    )
    assert {tool.name for tool in read_tools} == set(SUBAGENT_READ_TOOL_NAMES), (
        "the Advisor read set drifted from build_file_tools"
    )
    store = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    from coding_agent.runtime.result_capping import build_read_result_slice_tool

    tools = (
        *read_tools,
        build_read_change_tool(
            workspace,
            base_revision if base_revision is not None else "",
            artifact_store=store,
            artifact_id=change_artifact_id,
            base_tip=base_tip,
        ),
        build_read_result_slice_tool(store),
        build_report_advice_tool(record),
    )
    assert {tool.name for tool in tools} == set(ADVISOR_TOOL_NAMES), (
        f"an Advisor binds exactly {sorted(ADVISOR_TOOL_NAMES)}"
    )
    for tool in tools:
        assert tool.name != ASK_ADVISOR_TOOL, (
            "an Advisor binds no ask_advisor tool"
        )
        assert tool.name != "spawn_subagent", (
            "an Advisor binds no spawn_subagent tool"
        )
        assert not tool.name.startswith("activate_"), (
            f"an Advisor binds no activation tool, got {tool.name!r}"
        )
        assert tool.name not in ("report_completion", "report_step"), (
            f"an Advisor binds no step exit, got {tool.name!r}"
        )
    return tools


def estimate_advisor_admission(
    opening_text: str, tools: Sequence[BaseTool]
) -> int:
    """One Advisor call's admission estimate, the shared request way (§11.4).

    The opening the member would send (the framing, the caller's Attempt
    facts, input and declared inputs, the operator prompt and the
    question) measured as the Conversation Context of a fresh
    conversation — basis ``estimated``, there being no measured response
    yet — exactly the value the shared model-request lifecycle admits the
    first request with, never ``len(question) // 4``.
    """
    return measure_conversation_context(
        [HumanMessage(content=opening_text)],
        list(tools),
        ConversationContextState(),
    ).value


def advisor_tool_result_text(result: SubagentResult, preview: str) -> str:
    """Render one ``ask_advisor`` call's tool result (contract §9, §11).

    Headed by the question preview: the answer verbatim where
    ``reported``, else ``no-report`` with its reason (``silent``,
    ``round-limit``, ``ceiling``, ``context-window``, ``stopped`` or
    ``provider-failure``) and no content.
    """
    if result.status == "reported":
        assert result.report is not None
        return f"Advisor {preview!r} reported:\n{result.report}"
    if result.reason == "silent":
        return (
            f"Advisor {preview!r}: no-report (silent). "
            "The Advisor ended without calling report_advice."
        )
    if result.reason == "ceiling":
        return (
            f"Advisor {preview!r}: no-report (ceiling). "
            "The Advisor stopped when a limit was crossed or its Admission "
            "was refused."
        )
    if result.reason == "context-window":
        if result.context_cause == "context-overflow":
            signal = result.overflow_signal or "refused"
            return (
                f"Advisor {preview!r}: no-report (context-window). "
                f"The Advisor's model request hit a Context Overflow "
                f"(signal {signal})."
            )
        return (
            f"Advisor {preview!r}: no-report (context-window). "
            "The Advisor stopped before a request that would exceed the usable context."
        )
    if result.reason == "stopped":
        return (
            f"Advisor {preview!r}: no-report (stopped). "
            "The Advisor was stopped before it could report."
        )
    if result.reason == "provider-failure":
        return (
            f"Advisor {preview!r}: no-report (provider-failure). "
            "The Advisor's model request failed after its retries."
        )
    return (
        f"Advisor {preview!r}: no-report (round-limit). "
        f"The Advisor reached the {result.max_rounds}-round bound "
        "without calling report_advice."
    )


@dataclass
class AdvisorConsultation:
    """One ended ``ask_advisor`` call, for the Execution Summary (§9)."""

    question_preview: str
    ending: str
    pin_id: str


@dataclass
class AdvisorCallTally:
    """Attempt-wide counter assigning call ids to Advisor calls.

    Call ids are unique across the Attempt (``advisor-<n>``), so the
    started/ended ledger events pair across Step Executions and the
    Transcript's ``advisor:<call id>`` labels never collide with a
    Sub-agent's ``subagent:<call id>`` labels.
    """

    next_ordinal: int = 0

    def next_call_id(self) -> str:
        """The next Advisor call id, in opening order."""
        call_id = f"advisor-{self.next_ordinal}"
        self.next_ordinal += 1
        return call_id
