"""One Sub-agent per ``spawn_subagent`` call: a read-only worker conversation.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 ("Sub-agents": a
Workflow Step's model starts one Sub-agent per ``spawn_subagent`` call; the
fixed toolset; the Worker-authored framing followed by ``instructions``
verbatim; input validation; the code-owned round bound; the result to the
caller; the over-size report returned once; redaction and capping through the
caller's registry) and
[ADR 0020](../../../docs/adr/0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)
(one call, one Sub-agent; consecutive calls in a turn form one group).

A Sub-agent is a separate conversation answered by the Pinned Model: the
fixed read-only tool set — workspace readers, ``read_change`` for the change
itself and ``read_result_slice`` for capped results — plus its own
``report_subagent`` exit, its own message list, and no caller history. One
maximal run of consecutive calls in a turn is one group: at most the
Attempt's ``limits.max_parallel_subagents`` Sub-agents run at once from the
group runner's bounded worker pool, the rest queue FIFO in call order and are
never refused; each result joins the caller's Exchange Unit in call order and
the caller is suspended until the group ends.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_core.exceptions import ContextOverflowError as _ContextOverflowError
from langchain_core.exceptions import ModelError as _ModelError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import UsageTotals, messages_usage
from coding_agent.runtime.context_handoff import (
    ConversationContext,
    ConversationContextState,
    measure_conversation_context,
)
from coding_agent.runtime.graph.agent_model_request import ModelRequestEnding
from coding_agent.runtime.git import (
    GitFailure,
    commit_list_since_base,
    head_revision,
    worktree_diff_since_base,
)
from coding_agent.runtime.redaction import (
    Redactable,
    format_exception_redacted,
    registry_from,
)
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
    InMemoryArtifactStore,
    cap_tool_result,
)
from coding_agent.runtime.toolset import build_file_tools

if TYPE_CHECKING:
    from coding_agent.runtime.context_handoff import ContextWindowConfig
    from coding_agent.runtime.step_limits import LimitFact


SubagentToolCharge = Callable[[int], "LimitFact | None"]
"""Charge one billable Sub-agent tool-call batch before it runs.

Counts ``count`` requested calls — every requested call except the
Sub-agent's own ``report_subagent`` exit — against the calling step's
tool-call meter (Step Allowance, Work Pool or reserve, Attempt Ceiling),
exactly like the step loop's ``note_tool_calls`` charge, and flushes the
Attempt usage afterwards. Returns the crossing ``LimitFact`` (moment
``tool-call``) where the batch crossed a limit — the calls are then not
executed — else ``None``. Production passes the budgets-backed charge;
the default (``None``) leaves the run unmetered, for unit tests only.
"""

SPAWN_SUBAGENT_TOOL = "spawn_subagent"
"""The Workflow Step tool that starts one Sub-agent per call (contract §8)."""

SUBAGENT_QUEUED_KIND = "subagent_queued"
"""Run Ledger event kind: one ``spawn_subagent`` call validated and queued (§8)."""

SUBAGENT_STARTED_KIND = "subagent_started"
"""Run Ledger event kind: one Sub-agent conversation started (§8)."""

SUBAGENT_ENDED_KIND = "subagent_ended"
"""Run Ledger event kind: one Sub-agent conversation ended (§8)."""

REPORT_SUBAGENT_TOOL = "report_subagent"
"""A Sub-agent's only exit: its Review Report carrier (contract §8)."""

READ_CHANGE_TOOL = "read_change"
"""A Sub-agent's change reader: the deliverable change, no arguments (contract §8)."""

SUBAGENT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_directory",
        "find_files",
        "search_text",
        "read_lines",
        "read_result_slice",
        "read_change",
        "report_subagent",
    }
)
"""The exact bound set of a Sub-agent conversation (contract §8).

The workspace-scoped read subset of ``build_file_tools``, the ``read_change``
reader carrying the deliverable change, the capped-result range reader over
the caller's artifact store, and the Sub-agent's own ``report_subagent``
exit. No write tool, no shell, no activation tool, no step exit and no
``spawn_subagent`` of its own, so a Sub-agent can neither change the tree
nor spawn another Sub-agent.
"""

SUBAGENT_READ_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_directory",
        "find_files",
        "search_text",
        "read_lines",
    }
)
"""The workspace readers a Sub-agent may bind (contract §8)."""

SUBAGENT_DESCRIPTION_MAX_CHARS = 120
"""A Sub-agent ``description`` is at most 120 characters (contract §8)."""

SUBAGENT_INSTRUCTIONS_MAX_CHARS = 64 * 1024
"""Sub-agent ``instructions`` are at most 64 KiB (contract §8)."""

SUBAGENT_REPORT_MAX_CHARS = 32 * 1024
"""A Sub-agent report over 32 KiB is returned once for shortening (§8)."""

MAX_SUBAGENT_ROUNDS = 8
"""Upper bound on model rounds per Sub-agent (contract §8, code-owned).

A round is one model response plus its tool results. The run ends early as
soon as ``report_subagent`` records a report, or at the first response with
no tool calls. Reaching the bound without a report ends ``no-report`` /
``round-limit``.
"""

SUBAGENT_FRAMING = (
    "You are a sub-agent. Do the work yourself in the workspace.\n"
    "\n"
    "You hold read-only tools: read_file, list_directory, find_files, "
    "search_text, read_lines, read_change and read_result_slice. Use them "
    "to inspect the tree; you cannot change it. Call read_change with no "
    "arguments to review the change that would be delivered; you have no "
    "shell, so do not hand yourself git commands.\n"
    "\n"
    "Your run ends only through report_subagent(report): call it once with "
    'your findings, or with an explicit "no findings" where there is '
    "nothing to report. A run that ends without report_subagent is not a "
    "completed run."
)
"""The Worker-authored framing opening every Sub-agent (contract §8).

Criteria-free and axis-free: it names no Acceptance Criteria, no reviewer
axis and no report shape beyond the exit tool. The opening is this framing
followed by ``instructions`` verbatim, with no Pinned Prefix, no Attempt
Header and no caller history.
"""


def compose_subagent_opening(instructions: str) -> str:
    """The Sub-agent opening: the fixed framing, then ``instructions`` verbatim."""
    return f"{SUBAGENT_FRAMING}\n\n{instructions}"


def validate_spawn_args(description: object, instructions: object) -> str | None:
    """Refuse an invalid ``spawn_subagent`` call without starting anything (§8).

    Returns the caller-facing error text where ``description`` is not a
    non-blank string of at most 120 characters or ``instructions`` is not a
    non-blank string of at most 64 KiB, else ``None``. An invalid call starts
    nothing and writes no ledger event.
    """
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > SUBAGENT_DESCRIPTION_MAX_CHARS
    ):
        return (
            "Error: spawn_subagent 'description' must be a non-blank string "
            f"of at most {SUBAGENT_DESCRIPTION_MAX_CHARS} characters."
        )
    if (
        not isinstance(instructions, str)
        or not instructions.strip()
        or len(instructions.encode("utf-8")) > SUBAGENT_INSTRUCTIONS_MAX_CHARS
    ):
        return (
            "Error: spawn_subagent 'instructions' must be a non-blank string "
            f"of at most {SUBAGENT_INSTRUCTIONS_MAX_CHARS} bytes."
        )
    return None


@dataclass
class SubagentRecord:
    """One Sub-agent's ``report_subagent`` exit record.

    Set once, by the Sub-agent's own ``report_subagent`` call carrying a
    non-blank report within the size bound (or a repeated over-size report).
    A blank report is refused by the tool and records nothing; a first
    over-size report is returned with a request to shorten it and records
    nothing, while the second over-size report is accepted.
    """

    description: str = ""
    report: str | None = None
    oversize_returned: bool = False


def build_report_subagent_tool(record: SubagentRecord) -> BaseTool:
    """The Sub-agent's ``report_subagent`` exit tool (contract §8)."""

    def _report_subagent(report: str) -> str:
        if not isinstance(report, str) or not report.strip():
            return (
                'Error: a Sub-agent report needs findings or an explicit '
                '"no findings"; nothing was recorded.'
            )
        if len(report.encode("utf-8")) > SUBAGENT_REPORT_MAX_CHARS and not record.oversize_returned:
            record.oversize_returned = True
            return (
                f"The report is {len(report.encode('utf-8'))} bytes, over the "
                f"{SUBAGENT_REPORT_MAX_CHARS}-byte bound. Shorten it to "
                "at most that many bytes and call report_subagent again."
            )
        if record.report is not None:
            return "The Sub-agent report is already recorded."
        record.report = report
        return "The Sub-agent report is recorded."

    _report_subagent.__name__ = "report_subagent"
    return StructuredTool.from_function(
        func=_report_subagent,
        name=REPORT_SUBAGENT_TOOL,
        description=(
            "End the Sub-agent run with its report. The report is the "
            'findings, or the explicit string "no findings".'
        ),
    )


def build_spawn_subagent_tool() -> BaseTool:
    """The ``spawn_subagent`` tool bound in every Workflow Step (contract §8).

    The step loop intercepts this tool's calls before invocation — one call
    starts one Sub-agent there — so this function body only ever runs on a
    direct invocation outside that loop. It still validates its inputs the
    same way the loop does, so a direct call with invalid inputs is refused
    with the same error text.
    """

    def _spawn_subagent(description: str, instructions: str) -> str:
        error = validate_spawn_args(description, instructions)
        if error is not None:
            return error
        return (
            "Error: spawn_subagent starts a Sub-agent in the step loop; "
            "a direct invocation starts nothing."
        )

    _spawn_subagent.__name__ = "spawn_subagent"
    return StructuredTool.from_function(
        func=_spawn_subagent,
        name=SPAWN_SUBAGENT_TOOL,
        description=(
            "Start one Sub-agent with a short description and its "
            "instructions. One call starts one Sub-agent; consecutive calls "
            "in one turn form one concurrent group. The result carries the "
            "Sub-agent's report, or names why it made none."
        ),
    )


SUBAGENT_CHANGE_COMMIT_LIST_MAX_LINES = 50
"""Oneline commit-list entries carried inline in a ``read_change`` result (§8).

An Attempt branch holds a handful of commits, so the list is small; beyond
this bound the result keeps the first entries with an explicit marker naming
the total, never a silent cut. The same bound the change inputs of §10
used, reused here widened to the uncommitted tree.
"""

SUBAGENT_CHANGE_DIFF_INLINE_LIMIT = 4_000
"""Characters of worktree diff carried inline in a ``read_change`` result (§8).

A diff at or under this bound is embedded whole. Over it, the result carries
head plus tail with an explicit marker naming the total, and the full text
stays reachable through ``read_result_slice`` — the same head/tail/pointer
shape as the §10 change inputs and the §11 result cap.
"""


def build_read_change_tool(
    workspace: Path,
    base_revision: str,
    *,
    artifact_store: ArtifactStore | None = None,
    artifact_id: str = "read-change",
    diff_limit: int = SUBAGENT_CHANGE_DIFF_INLINE_LIMIT,
) -> BaseTool:
    """A Sub-agent's ``read_change`` reader: the deliverable change (contract §8).

    Takes no arguments. Returns the commit list ``base..HEAD`` and the diff
    against the Base Revision over what the Delivery Snapshot would commit:
    the working tree including untracked files, with the same exclusions
    (gitignored paths stay excluded, as under ``git add -A``). The commit
    list keeps the first ``SUBAGENT_CHANGE_COMMIT_LIST_MAX_LINES`` entries
    with a marker naming the total; the diff is bounded head/tail with a
    marker naming the total and its full text stashed in ``artifact_store``
    for ``read_result_slice``. The first call stashes under ``artifact_id``;
    each later call on the same tool stashes under ``artifact_id-2``,
    ``artifact_id-3`` and so on, so an earlier pointer keeps returning its
    own call's full diff. An unreadable range yields an explicit statement
    naming the failure, never an empty diff. Git runs only here, inside the
    runtime — the Sub-agent holds no shell.
    """
    store = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    call_index = 0

    def _read_change() -> str:
        nonlocal call_index
        call_index += 1
        call_artifact_id = artifact_id if call_index == 1 else f"{artifact_id}-{call_index}"
        try:
            head = head_revision(workspace)
            raw_commits = commit_list_since_base(workspace, base_revision, head)
            raw_diff = worktree_diff_since_base(workspace, base_revision)
        except (GitFailure, OSError) as exc:
            note = (
                "(change unavailable: the commit list and diff "
                f"could not be read: {exc})"
            )
            return (
                f"Change against Base Revision {base_revision}.\n"
                f"\nCommit list ({base_revision}..HEAD --oneline):\n{note}\n"
                f"\nDiff (working tree including untracked files):\n{note}"
            )
        stripped = raw_commits.strip()
        if not stripped:
            commit_list = "(no commits between the Base Revision and HEAD)"
        else:
            entries = stripped.splitlines()
            if len(entries) > SUBAGENT_CHANGE_COMMIT_LIST_MAX_LINES:
                kept = entries[:SUBAGENT_CHANGE_COMMIT_LIST_MAX_LINES]
                commit_list = "\n".join(
                    [
                        *kept,
                        f"... [{len(entries)} commits total; showing the first "
                        f"{SUBAGENT_CHANGE_COMMIT_LIST_MAX_LINES}] ...",
                    ]
                )
            else:
                commit_list = "\n".join(entries)
        if not raw_diff.strip():
            diff = "(no changes between the Base Revision and the working tree)"
        else:
            diff = cap_tool_result(
                raw_diff,
                tool_call_id=call_artifact_id,
                limit=diff_limit,
                store=store,
            )
        return (
            f"Change against Base Revision {base_revision}.\n"
            f"\nCommit list ({base_revision}..{head} --oneline):\n{commit_list}\n"
            f"\nDiff (working tree including untracked files):\n{diff}"
        )

    _read_change.__name__ = "read_change"
    return StructuredTool.from_function(
        func=_read_change,
        name=READ_CHANGE_TOOL,
        description=(
            "Review the change that would be delivered: the commit list "
            "base..HEAD and the diff against the Base Revision over the "
            "working tree including untracked files. Takes no arguments."
        ),
    )


def build_subagent_tools(
    workspace: Path,
    record: SubagentRecord,
    *,
    artifact_store: ArtifactStore | None = None,
    base_revision: str | None = None,
    change_artifact_id: str = "read-change",
) -> tuple[BaseTool, ...]:
    """One Sub-agent's isolated, read-only tool set (contract §8).

    The workspace-scoped read tools, the ``read_change`` reader over what the
    Delivery Snapshot would commit, the ``read_result_slice`` range reader
    over the caller's artifact store, plus the Sub-agent's own
    ``report_subagent`` exit. The bound set is exactly
    ``SUBAGENT_TOOL_NAMES`` — asserted structurally, so a Sub-agent can
    neither change the tree nor spawn another Sub-agent.
    """
    read_tools = tuple(
        tool for tool in build_file_tools(workspace) if tool.name in SUBAGENT_READ_TOOL_NAMES
    )
    assert {tool.name for tool in read_tools} == set(SUBAGENT_READ_TOOL_NAMES), (
        "the Sub-agent read set drifted from build_file_tools"
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
        ),
        build_read_result_slice_tool(store),
        build_report_subagent_tool(record),
    )
    assert {tool.name for tool in tools} == set(SUBAGENT_TOOL_NAMES), (
        f"a Sub-agent binds exactly {sorted(SUBAGENT_TOOL_NAMES)}"
    )
    for tool in tools:
        assert tool.name != SPAWN_SUBAGENT_TOOL, (
            "a Sub-agent binds no spawn_subagent tool"
        )
        assert not tool.name.startswith("activate_"), (
            f"a Sub-agent binds no activation tool, got {tool.name!r}"
        )
        assert tool.name not in ("report_completion", "report_step"), (
            f"a Sub-agent binds no step exit, got {tool.name!r}"
        )
    return tools


SubagentStatus = Literal["reported", "no-report"]
"""Whether the Sub-agent made a report (contract §8)."""

SubagentReason = Literal[
    "reported", "silent", "round-limit", "ceiling", "context-window", "stopped", "provider-failure"
]
"""Why the Sub-agent ended that way: reported, silent, round-limited (§8),
ceiling or context-window (§11), stopped on SIGTERM (§11), or
provider-failure where the Pinned Model failed after its retries (§11)."""


@dataclass
class SubagentResult:
    """One Sub-agent conversation's end: its report and its meter spend."""

    call_id: str
    description: str
    status: SubagentStatus
    reason: SubagentReason
    report: str | None
    messages: list[BaseMessage]
    usage: UsageTotals = field(default_factory=UsageTotals)
    billable_tool_calls: int = 0
    """Every billable tool call this run requested, including a crossing batch.

    Every requested call except the ``report_subagent`` exit — including calls
    to unknown tool names (§11.5 counts at request time) and including a batch
    whose crossing stopped the run (§11.5 still counts the requested calls).
    """
    limit_fact: "LimitFact | None" = None
    """The tool-call crossing fact where a batch crossed a limit, else ``None``.

    Carried back to the spawn group so the calling Step Execution can end
    ``limit-exceeded`` with it (moment ``tool-call``).
    """
    provider_error: str | None = None
    """The redacted provider error class and status (§9.8).

    Set only where the Sub-agent ended ``no-report`` / ``provider-failure``:
    the model request failed after its retries, so the caller records it in
    ``subagent_ended`` beside the answering model. ``None`` everywhere else.
    """
    end_context: ConversationContext | None = None
    """The Conversation Context compared for the crossing request (§9.8).

    Set only where the Sub-agent ended ``no-report`` / ``context-window``:
    the value (and basis) that crossed the usable threshold, so the caller
    records it on ``subagent_ended``. ``None`` on every other ending.
    """

    @property
    def completed(self) -> bool:
        """Whether the Sub-agent made a report (contract §8)."""
        return self.report is not None


def subagent_tool_result_text(result: SubagentResult) -> str:
    """Render one ``spawn_subagent`` call's tool result (contract §8, §11).

    Headed by the call's ``description``: the report verbatim where
    ``reported``, else ``no-report`` with its reason (``silent``,
    ``round-limit``, ``ceiling``, ``context-window``, ``stopped`` or
    ``provider-failure``) and no content.
    """
    if result.status == "reported":
        assert result.report is not None
        return f"Sub-agent {result.description!r} reported:\n{result.report}"
    if result.reason == "silent":
        return (
            f"Sub-agent {result.description!r}: no-report (silent). "
            "The Sub-agent ended without calling report_subagent."
        )
    if result.reason == "ceiling":
        return (
            f"Sub-agent {result.description!r}: no-report (ceiling). "
            "The Sub-agent stopped when the Attempt-wide ceiling was crossed."
        )
    if result.reason == "context-window":
        return (
            f"Sub-agent {result.description!r}: no-report (context-window). "
            "The Sub-agent stopped before a request that would exceed the usable context."
        )
    if result.reason == "stopped":
        return (
            f"Sub-agent {result.description!r}: no-report (stopped). "
            "The Sub-agent was stopped before it could report."
        )
    if result.reason == "provider-failure":
        return (
            f"Sub-agent {result.description!r}: no-report (provider-failure). "
            "The Sub-agent's model request failed after its retries."
        )
    return (
        f"Sub-agent {result.description!r}: no-report (round-limit). "
        f"The Sub-agent reached the {MAX_SUBAGENT_ROUNDS}-round bound "
        "without calling report_subagent."
    )


SubagentModelRequest = Callable[
    [object, list[BaseMessage], list[BaseTool]],
    BaseMessage | ModelRequestEnding,
]
"""One Sub-agent model request: the bound model, the live messages, the tools.

Returns the accepted response — appended to ``messages`` before returning —
or the shared lifecycle's ending where Admission refused the request or the
settled spend crossed a limit. Production passes the step loop's shared
model-request lifecycle (admission, retry, settlement); the default sends
one unmetered request, for unit tests only.
"""


def estimate_spawn_admission(
    instructions: str, tools: Sequence[BaseTool]
) -> int:
    """One spawn member's admission estimate, the shared request way (§11.4).

    The opening the member would send (the fixed framing plus
    ``instructions`` verbatim) measured as the Conversation Context of a
    fresh conversation — basis ``estimated``, there being no measured
    response yet — exactly the value the shared model-request lifecycle
    admits the member's first request with, never ``len(instructions) // 4``.
    The spawn group admits every member up front in call order with this
    estimate, so the first request's headroom is counted once (LIM-7).
    """
    return measure_conversation_context(
        [HumanMessage(content=compose_subagent_opening(instructions))],
        list(tools),
        ConversationContextState(),
    ).value


def _direct_request(
    bound_model: object,
    request_messages: list[BaseMessage],
    request_tools: list[BaseTool],
) -> BaseMessage | ModelRequestEnding:
    """The unit-test model request: one direct invoke, appended, unmetered.

    A single attempt with no Admission, no retry and no settlement — the
    caller keeps whatever metering it had. Production never uses this: it
    passes the shared model-request lifecycle instead.
    """
    _ = request_tools
    invoke = cast(Any, getattr(bound_model, "invoke"))
    response = cast(BaseMessage, invoke(list(request_messages)))
    assert isinstance(response, BaseMessage)
    request_messages.append(response)
    return response


def run_subagent(
    model: InvokableToolModel,
    *,
    description: str,
    instructions: str,
    call_id: str,
    workspace: Path,
    artifact_store: ArtifactStore | None = None,
    result_cap_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    redactions: Redactable = None,
    price: TokenPrices | None = None,
    base_revision: str | None = None,
    is_gateway: bool = False,
    on_usage: Callable[[UsageTotals], None] | None = None,
    on_missing_usage: Callable[[], None] | None = None,
    on_response: Callable[[BaseMessage], None] | None = None,
    on_unreported_cache_write: Callable[[], None] | None = None,
    should_stop: Callable[[], str | None] | None = None,
    is_stopped: Callable[[], bool] | None = None,
    context_window: ContextWindowConfig | None = None,
    request_model: SubagentModelRequest | None = None,
    charge_tool_calls: SubagentToolCharge | None = None,
    context_state: ConversationContextState | None = None,
) -> SubagentResult:
    """Run one Sub-agent conversation to its report (contract §8, §11).

    A bounded tool loop over the fixed read-only tool set: each round binds
    the same tools with no forced choice, invokes once, and runs the
    requested calls. ``report_subagent`` with an accepted report ends the run
    as ``reported``; a response with no tool calls ends it ``no-report`` /
    ``silent``, and the round bound without a report ends it ``no-report`` /
    ``round-limit``. A call to any other name returns "no such tool". Every
    tool result is redacted and capped through the caller's registry, limit
    and store — the spawn result carrying the report to the caller is not
    capped here (the report carries its own 32 KiB bound). The returned
    messages are this Sub-agent's own list; no other conversation shares it.
    Each model request runs through ``request_model`` — production passes the
    step loop's shared model-request lifecycle, so every round and every
    provider retry passes Admission under its own hold and settles it (§11.4,
    ADR 0029); the default sends one unmetered request, for unit tests only.
    A refused request, or a settled spend that crossed a limit, ends the run
    ``no-report`` / ``ceiling`` with the request never sent twice. A
    non-retryable ``ModelError``, or a retryable one whose retries are
    exhausted, ends the run ``no-report`` / ``provider-failure`` with the
    redacted error class and status on the result (§9.8); a
    ``ContextOverflowError`` ends it ``no-report`` / ``context-window``. A
    ``PinViolation`` propagates untouched: it never becomes ``no-report``.
    The failed request's hold stays charged at its worst case (§11.4).

    Attempt-wide meters (§11): each model response's spend is reported through
    ``on_usage`` at once — tokens, cost per token bucket and its tool calls —
    so the caller flushes it to the Attempt-wide meters before the group ends,
    including the response that precedes a stop. A response without usage
    metadata adds nothing to the totals (budget enforcement keeps its current
    arithmetic) and is reported through ``on_missing_usage`` at once, so the
    caller increments the Attempt's durable missing-usage counter for the
    Status Snapshot. Where ``is_gateway`` holds, spend prices an unreported
    cache write's remainder at max(input, cache-write) with token buckets as
    reported (§11.4), and each such response is reported through
    ``on_unreported_cache_write`` at once, so the caller records the
    Attempt's durable unreported-cache-write marker. Each model response is also reported through
    ``on_response`` at once — before the stop and ceiling checks, so the
    response that precedes a stop is reported too — so the caller records
    its observed identity in the Run Ledger (§13). Where ``is_stopped`` reports
    the stop signal (SIGTERM, or a sibling Sub-agent's provider failure), the
    run ends ``no-report`` / ``stopped`` with no content, even where a report
    is ready: the stop takes precedence, and no new model call starts after
    it is observed. Where ``should_stop`` names a crossed ceiling, the run
    ends ``no-report`` / ``ceiling`` with no content, even where a report is
    ready: the ceiling takes precedence over the report but not over the stop
    signal. Where ``context_window`` is set, the run stops before a request
    that would exceed its usable threshold and ends ``no-report`` /
    ``context-window`` with no content; a Sub-agent never hands off. The guard
    and every admission through ``request_model`` compare the same Conversation
    Context: the last accepted response's reported ``input_tokens`` plus
    ``bytes/4`` of the messages appended since (basis ``anchored``), or the
    whole-request estimate (basis ``estimated``) with no measured response yet.
    The caller may pass its own ``context_state`` to share the anchoring — the
    production spawn path passes a fresh state per admitted member; a response
    without complete usage metadata never moves the anchor.

    Tool-call meters: every requested call except the ``report_subagent`` exit
    is billable — including calls to unknown tool names, which count at request
    time — and each batch is charged through ``charge_tool_calls`` before any
    call in the batch runs. The ``report_subagent`` exit is the run's end,
    never metered work, like the step loop's own report. Where the charge
    crosses a tool-call limit, no call in that batch runs and the run ends
    ``no-report`` / ``ceiling`` carrying the crossing fact; the crossed calls
    still count toward the spend the fact reports. The default (``None``)
    leaves tool calls unmetered, for unit tests only.

    The stop signal is observed after every model response and every tool
    result. A provider call already blocked inside its request runs to the
    provider's own timeout and its result is discarded at the next stop point,
    the same shape as the caller loop — only the runtime's own commands are
    cancelled mid-flight.
    """
    from coding_agent.runtime.ceilings import (
        response_lacks_usage_metadata as _lacks_usage,
    )
    from coding_agent.runtime.ceilings import (
        response_has_unreported_gateway_cache_write as _unreported,
    )
    from coding_agent.runtime.ceilings import response_usage as _response_usage

    state = context_state if context_state is not None else ConversationContextState()
    """This conversation's anchoring state: a Sub-agent anchors on its own responses only."""

    def _ceiling() -> str | None:
        if should_stop is None:
            return None
        return should_stop()

    def _stop() -> bool:
        if is_stopped is None:
            return False
        return is_stopped()

    def _over_context(current: list[BaseMessage], tool_list: list[BaseTool]) -> bool:
        if context_window is None:
            return False
        return (
            state.measure(list(current), list(tool_list)).value
            > context_window.usable_threshold
        )

    billable_total = 0
    tool_crossing: "LimitFact | None" = None

    def _no_report_result(
        current: list[BaseMessage],
        reason: SubagentReason,
        *,
        limit_fact: "LimitFact | None" = None,
    ) -> SubagentResult:
        return SubagentResult(
            call_id=call_id,
            description=description,
            status="no-report",
            reason=reason,
            report=None,
            messages=list(current),
            usage=messages_usage(current, price, is_gateway=is_gateway),
            billable_tool_calls=billable_total,
            limit_fact=limit_fact,
        )

    def _note_unreported_cache_write(response: BaseMessage) -> None:
        """Report one gateway response with an unreported cache write (§11.4).

        Token buckets stay as reported; only the remainder's dollars use the
        max rate, and the caller records the ledger marker at once.
        """
        if on_unreported_cache_write is None:
            return
        if _unreported(response, is_gateway=is_gateway):
            try:
                on_unreported_cache_write()
            except Exception:
                pass

    def _stopped_result(current: list[BaseMessage]) -> SubagentResult:
        return _no_report_result(current, "stopped")

    def _ceiling_result(
        current: list[BaseMessage],
        *,
        limit_fact: "LimitFact | None" = None,
    ) -> SubagentResult:
        return _no_report_result(current, "ceiling", limit_fact=limit_fact)

    def _context_result(
        current: list[BaseMessage], tool_list: list[BaseTool]
    ) -> SubagentResult:
        result = _no_report_result(current, "context-window")
        result.end_context = state.measure(list(current), list(tool_list))
        return result

    def _provider_failure_result(
        current: list[BaseMessage],
        exc: BaseException,
        tool_list: list[BaseTool],
    ) -> SubagentResult:
        """End ``no-report`` on a model request that failed (§9.8).

        A context overflow ends ``context-window``, the same ending as the
        context guard, carrying the compared Conversation Context; any other
        non-retryable or exhausted ``ModelError`` ends ``provider-failure``
        carrying the redacted provider error class and status for the
        caller's ``subagent_ended`` event.
        """
        from coding_agent.runtime.graph.agent_model_request import (
            provider_failure_label,
        )

        if isinstance(exc, _ContextOverflowError):
            return _context_result(current, tool_list)
        result = _no_report_result(current, "provider-failure")
        result.provider_error = registry.redact(provider_failure_label(exc))
        return result

    record = SubagentRecord(description=description)
    tools = build_subagent_tools(
        workspace,
        record,
        artifact_store=artifact_store,
        base_revision=base_revision,
        change_artifact_id=f"read-change-{call_id}",
    )
    bound = model.bind_tools(list(tools))
    messages: list[BaseMessage] = [HumanMessage(content=compose_subagent_opening(instructions))]
    by_name: dict[str, BaseTool] = {tool.name: tool for tool in tools}
    registry = registry_from(redactions)
    store = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    tool_list = list(tools)
    send_request = request_model if request_model is not None else _direct_request
    response_index = 0
    for _ in range(MAX_SUBAGENT_ROUNDS):
        if _stop():
            return _stopped_result(messages)
        if _ceiling() is not None:
            return _ceiling_result(messages)
        if _over_context(messages, tool_list):
            return _context_result(messages, tool_list)
        pre_request_count = len(messages)
        try:
            request_result = send_request(bound, messages, tool_list)
        except _ContextOverflowError as exc:
            return _provider_failure_result(messages, exc, tool_list)
        except _ModelError as exc:
            return _provider_failure_result(messages, exc, tool_list)
        if isinstance(request_result, ModelRequestEnding):
            return _no_report_result(messages, "ceiling")
        response = request_result
        assert isinstance(response, BaseMessage)
        response_index += 1
        # Anchor this conversation on the accepted response where it reports
        # complete usage; a response without usage leaves the anchor untouched.
        state.note_response(
            response,
            pre_request_count,
            f"subagent:{call_id}#response-{response_index}",
        )
        if on_response is not None:
            on_response(response)
        if on_usage is not None:
            on_usage(_response_usage(response, price, is_gateway=is_gateway))
        _note_unreported_cache_write(response)
        if on_missing_usage is not None and _lacks_usage(response):
            try:
                on_missing_usage()
            except Exception:
                pass
        if _stop():
            return _stopped_result(messages)
        if _ceiling() is not None:
            return _ceiling_result(messages)
        if not isinstance(response, AIMessage) or not response.tool_calls:
            return SubagentResult(
                call_id=call_id,
                description=description,
                status="no-report",
                reason="silent",
                report=None,
                messages=messages,
                usage=messages_usage(messages, price, is_gateway=is_gateway),
                billable_tool_calls=billable_total,
            )
        billable_calls = [
            call
            for call in response.tool_calls
            if str(call.get("name")) != REPORT_SUBAGENT_TOOL
        ]
        if billable_calls and charge_tool_calls is not None:
            tool_crossing = charge_tool_calls(len(billable_calls))
            billable_total += len(billable_calls)
            if tool_crossing is not None:
                return _ceiling_result(messages, limit_fact=tool_crossing)
        elif billable_calls:
            billable_total += len(billable_calls)
        results: list[ToolMessage] = []
        stopped_mid_turn = False
        for index, call in enumerate(response.tool_calls):
            name = str(call.get("name"))
            raw_id = call.get("id")
            tool_call_id = str(raw_id) if raw_id else f"unidentified-{index}"
            tool = by_name.get(name)
            if tool is None:
                results.append(
                    ToolMessage(
                        content=f"Error: no such tool {name!r}",
                        tool_call_id=tool_call_id,
                    )
                )
            else:
                args: Mapping[str, Any] = call.get("args", {})
                if not isinstance(args, Mapping):
                    args = {}
                try:
                    content = registry.redact(str(tool.invoke(dict(args))))
                except Exception as exc:
                    content = f"Error: {format_exception_redacted(exc, registry)}"
                if name != REPORT_SUBAGENT_TOOL:
                    content = cap_tool_result(
                        content,
                        tool_call_id=tool_call_id,
                        limit=result_cap_limit,
                        store=store,
                    )
                results.append(ToolMessage(content=content, tool_call_id=tool_call_id))
            if _stop():
                stopped_mid_turn = True
                break
            if _ceiling() is not None:
                messages.extend(results)
                return _ceiling_result(messages)
        messages.extend(results)
        if stopped_mid_turn:
            return _stopped_result(messages)
        if record.report is not None:
            if _stop():
                return _stopped_result(messages)
            if _ceiling() is not None:
                return _ceiling_result(messages)
            return SubagentResult(
                call_id=call_id,
                description=description,
                status="reported",
                reason="reported",
                report=record.report,
                messages=messages,
                usage=messages_usage(messages, price, is_gateway=is_gateway),
                billable_tool_calls=billable_total,
            )
    if _stop():
        return _stopped_result(messages)
    if _ceiling() is not None:
        return _ceiling_result(messages)
    return SubagentResult(
        call_id=call_id,
        description=description,
        status="no-report",
        reason="round-limit",
        report=None,
        messages=messages,
        usage=messages_usage(messages, price, is_gateway=is_gateway),
        billable_tool_calls=billable_total,
    )


@dataclass
class SpawnRequest:
    """One intercepted ``spawn_subagent`` call: its raw args and ledger place."""

    description: object
    instructions: object
    call_id: str
    group_ordinal: int
    position_in_group: int


SpawnRunner = Callable[[SpawnRequest], str]
"""Runs one validated-or-not spawn call to its caller-facing result text."""


GroupSpawnRunner = Callable[[list[SpawnRequest]], list[str]]
"""Runs one consecutive ``spawn_subagent`` group to its result texts in order.

The caller is suspended until the group ends: every result joins the caller's
Exchange Unit in call order, one entry per request, including validation
errors (which start nothing). Within the group at most the Attempt's
``limits.max_parallel_subagents`` Sub-agents run at once; the rest queue FIFO
in call order and are never refused.
"""


@dataclass
class SubagentGroupTracker:
    """Attempt-wide counter assigning group ordinals to consecutive spawn runs.

    One maximal run of consecutive ``spawn_subagent`` calls in one assistant
    turn is one group; every other tool call is a barrier. Ordinals count up
    across the whole Attempt, so the queued/started/ended ledger events show
    queueing in call order.
    """

    next_ordinal: int = 0


@dataclass(frozen=True)
class SpawnHarness:
    """What the step loop needs to run ``spawn_subagent`` calls in turn order."""

    run_group: GroupSpawnRunner
    groups: SubagentGroupTracker


def resolve_max_parallel_subagents(
    effective_configuration: Mapping[str, object] | None,
) -> int:
    """The Attempt's ``max_parallel_subagents`` (§2.1, §8, ADR 0020).

    Reads ``workflow.max_parallel_subagents`` from the Effective
    Configuration record where present (falling back to the legacy
    ``workflow.limits.max_parallel_subagents`` rows from older releases
    carry), else the contract default of 2. A missing, non-integer or
    ``< 1`` value falls back to 2: the strict parser refuses such files
    before any Attempt runs (C-CFG-4), so a fallback here only covers
    scripted tests and older records.
    """
    from coding_agent.workflow.schema import DEFAULT_MAX_PARALLEL_SUBAGENTS

    if effective_configuration is None:
        return DEFAULT_MAX_PARALLEL_SUBAGENTS
    workflow = effective_configuration.get("workflow")
    if not isinstance(workflow, Mapping):
        return DEFAULT_MAX_PARALLEL_SUBAGENTS
    value = workflow.get("max_parallel_subagents")
    if value is None:
        limits = workflow.get("limits")
        if isinstance(limits, Mapping):
            value = limits.get("max_parallel_subagents")
    if isinstance(value, bool) or type(value) is not int or value < 1:
        return DEFAULT_MAX_PARALLEL_SUBAGENTS
    return value
