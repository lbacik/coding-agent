"""The M3 review subgraph: two isolated reviewer conversations.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (after the
review-snapshot push the review subgraph fans out two reviewer
conversations, Standards and Spec, each with its own message list and
read-only tools; the Reviewer Brief is composed by concatenation only — a
Seat Assignment, the Role Block cut verbatim from the installed
``code-review`` skill, and the concrete inputs the Role Block refers to; a
seat completes only through a ``report_review`` Review Report; the tool
result carries both Review Reports, or the seat that made none; every
invocation re-runs both seats against the tree at call time, metered against
the Attempt-wide meters with no reserved review budget) and §1 (the model's
toolset holds no GitHub mutation; a reviewer seat binds no review or
activation tool, so a seat cannot spawn another pair), with the verbatim
slices of
[ADR 0007](../../../docs/adr/0007-upstream-skill-text-is-answered-never-rewritten.md).

MVP row ids: L3-REV-1, L3-REV-2, G-REV-1, G-REV-2, P-DEL-4, W-STP-6, C-REV-1.

Isolation holds by construction: each seat runs its own message list
through a read-only tool set plus its own ``report_review`` exit. No
``activate_*`` tool, no ``report_completion`` and no ``report_step`` is ever
bound in a seat, so a seat cannot spawn another pair and cannot declare the
Attempt's completion. The two seats are hard-coded in this one bounded
component; the generic mechanism is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    UsageMetadata,
)
from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.ceilings import UsageTotals, usage_breakdown
from coding_agent.runtime.review_snapshot import CompletedReview
from coding_agent.runtime.toolset import build_file_tools
from coding_agent.skillbundle.verify import (
    DEFAULT_ROLE_BLOCK_SPEC,
    RoleBlockSpec,
    extract_role_blocks,
)

STANDARDS_SEAT = "Standards"
"""The seat reviewing the code against the repo's documented standards (§10)."""

SPEC_SEAT = "Spec"
"""The seat reviewing the code against the originating issue/spec (§10)."""

SEATS: tuple[str, str] = (STANDARDS_SEAT, SPEC_SEAT)
"""The two hard-coded reviewer seats (§10)."""

REVIEW_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_directory",
        "find_files",
        "search_text",
        "read_lines",
    }
)
"""The read-only file tools a reviewer seat may bind (L3-REV-1).

The workspace-scoped read subset of ``build_file_tools``: no ``write_file``
or ``edit_file``, no shell or command execution, no targeted-test tool and
no result-slice reader of its own. A reviewer reads the pushed tree; it
changes nothing.
"""

REVIEW_FORBIDDEN_TOOL_PREFIXES = ("activate_",)
"""Tool-name prefixes never bound in a reviewer seat (L3-REV-2).

A seat binds no activation tool — ``activate_code_review`` included — so a
seat cannot spawn another pair, and no positional exit of the step
(``report_completion``, ``report_step``), so a seat cannot declare the
Attempt's completion.
"""

REVIEW_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "activate_code_review",
        "report_completion",
        "report_step",
    }
)
"""Tool names never bound in a reviewer seat, asserted alongside the prefix."""

MAX_REVIEW_ROUNDS = 8
"""Upper bound on model rounds per reviewer seat.

A round is one model response plus its tool results. The seat ends early as
soon as ``report_review`` is called with a non-blank report, or at the first
response with no tool calls. Reaching the bound without a Review Report
leaves the seat incomplete (G-REV-1).
"""

STANDARDS_SOURCE_CANDIDATES: tuple[str, ...] = (
    "CODING_STANDARDS.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
)
"""Repository-root files that may document how code should be written (§10).

The concrete inputs name whichever of these exist in the workspace; the
reviewer reads them with its file tools. The list is fixed and
deterministic, never model-chosen.
"""


def seat_assignment(seat: str) -> str:
    """The Seat Assignment for ``seat`` (contract §10, ADR 0007).

    Names the seat's single axis, states that the conversation is itself the
    reviewer the Role Block describes, and states that its review ends in a
    Review Report. Carries no review criteria of its own: no standards, no
    spec checks, no word limits, no report structure.

    MVP row id: G-REV-2.
    """
    return (
        f"You are the {seat} reviewer.\n"
        "\n"
        "This conversation is itself the reviewer the Role Block below "
        f"describes: you review only the {seat} axis.\n"
        "\n"
        "Your review ends in a Review Report: call the report_review tool "
        "once with your findings, or with an explicit \"no findings\" where "
        "there is nothing to report. A conversation that ends without "
        "report_review is not a completed review.\n"
    )


def discover_standards_sources(workspace: Path) -> tuple[str, ...]:
    """Name the existing standards-source candidates under ``workspace``.

    Deterministic subset of ``STANDARDS_SOURCE_CANDIDATES`` in listed order.
    These are the concrete inputs the Standards Role Block refers to ("the
    list of standards-source files"); the reviewer reads them with its file
    tools.
    """
    return tuple(
        candidate
        for candidate in STANDARDS_SOURCE_CANDIDATES
        if (workspace / candidate).is_file()
    )


def concrete_inputs(
    *,
    seat: str,
    branch_name: str,
    commit_sha: str,
    base_revision: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    standards_sources: tuple[str, ...],
) -> str:
    """The concrete inputs the seat's Role Block refers to (contract §10).

    Supplied by juxtaposition after the verbatim Role Block, never rewritten
    into it (ADR 0007): the branch and commit under review (the workspace
    tree is at that commit), the base revision with the diff and commit-list
    commands for reference, and per seat — the standards-source file list
    for Standards, the Target Issue as the spec for Spec.
    """
    lines = [
        "Concrete inputs.",
        "",
        f"- Branch under review: {branch_name}",
        f"- Commit under review: {commit_sha} (the workspace tree is at this commit; read it with the file tools)",
        f"- Base revision: {base_revision} (for reference: git diff {base_revision}...{commit_sha}; git log {base_revision}..{commit_sha} --oneline)",
    ]
    if seat == STANDARDS_SEAT:
        if standards_sources:
            lines.append(f"- Standards-source files: {', '.join(standards_sources)}")
        else:
            lines.append("- Standards-source files: none found.")
    elif seat == SPEC_SEAT:
        lines.append(f"- Spec: the Target Issue #{issue_number} {issue_title!r}.")
        lines.append("")
        lines.append(issue_body)
    return "\n".join(lines)


def compose_reviewer_brief(
    *,
    seat: str,
    role_block: str,
    branch_name: str,
    commit_sha: str,
    base_revision: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    standards_sources: tuple[str, ...],
) -> str:
    """Compose one seat's Reviewer Brief by concatenation only (§10, L3-REV-2).

    Exactly three parts in order — the Seat Assignment, the seat's own Role
    Block byte-identical from the installed ``SKILL.md``, and the concrete
    inputs — joined without editing a byte inside any part.
    """
    return (
        seat_assignment(seat)
        + "\n"
        + role_block
        + "\n"
        + concrete_inputs(
            seat=seat,
            branch_name=branch_name,
            commit_sha=commit_sha,
            base_revision=base_revision,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            standards_sources=standards_sources,
        )
    )


@dataclass
class SeatRecord:
    """One seat's ``report_review`` exit record.

    Set once, by the seat's own ``report_review`` call carrying a non-blank
    report. A blank report is refused by the tool and records nothing; the
    seat stays open until it reports properly or ends without reporting.
    """

    seat: str
    findings: str | None = None


def build_report_review_tool(record: SeatRecord) -> BaseTool:
    """The seat's ``report_review`` exit tool (contract §10).

    Calling it with a non-blank ``findings`` string — the seat's findings or
    an explicit "no findings" — records the seat's Review Report and ends
    the seat. A blank report is not a Review Report: the call is returned
    unrecorded so the seat can report again.
    """

    def _report_review(findings: str) -> str:
        if not isinstance(findings, str) or not findings.strip():
            return (
                "Error: a Review Report needs findings or an explicit "
                '"no findings"; nothing was recorded.'
            )
        if record.findings is not None:
            return f"The {record.seat} Review Report is already recorded."
        record.findings = findings
        return f"The {record.seat} Review Report is recorded."

    _report_review.__name__ = "report_review"
    return StructuredTool.from_function(
        func=_report_review,
        name="report_review",
        description=(
            f"End the {record.seat} review with its Review Report. Findings "
            'is the review findings, or the explicit string "no findings".'
        ),
    )


def build_reviewer_tools(workspace: Path, record: SeatRecord) -> tuple[BaseTool, ...]:
    """One seat's isolated, read-only tool set (contract §1, §10).

    The workspace-scoped read tools plus the seat's own ``report_review``
    exit. No work tool that writes, no shell, no activation tool and no
    step exit: asserted structurally, so a seat can neither edit the tree
    nor spawn another pair (L3-REV-1, L3-REV-2).
    """
    read_tools = tuple(
        tool
        for tool in build_file_tools(workspace)
        if tool.name in REVIEW_READ_ONLY_TOOL_NAMES
    )
    assert {tool.name for tool in read_tools} == set(REVIEW_READ_ONLY_TOOL_NAMES), (
        "the reviewer read set drifted from build_file_tools"
    )
    tools = (*read_tools, build_report_review_tool(record))
    for tool in tools:
        assert not tool.name.startswith(REVIEW_FORBIDDEN_TOOL_PREFIXES), (
            f"reviewer seats bind no activation tool, got {tool.name!r}"
        )
        assert tool.name not in REVIEW_FORBIDDEN_TOOL_NAMES, (
            f"reviewer seats bind no step exit, got {tool.name!r}"
        )
    return tools


@dataclass
class SeatResult:
    """One reviewer conversation's end: its messages and its Review Report."""

    seat: str
    messages: list[BaseMessage]
    findings: str | None

    @property
    def completed(self) -> bool:
        """Whether the seat made a Review Report (G-REV-1)."""
        return self.findings is not None


def run_review_seat(
    model: InvokableToolModel,
    *,
    seat: str,
    brief: str,
    workspace: Path,
    max_rounds: int = MAX_REVIEW_ROUNDS,
) -> SeatResult:
    """Run one reviewer conversation against ``brief`` to its Review Report.

    A bounded tool loop over the seat's isolated read-only tool set: each
    round binds the same tools with no forced choice, invokes once, and runs
    the requested calls. ``report_review`` with a non-blank report ends the
    seat as completed; a response with no tool calls, or the round bound
    without a report, ends it incomplete (G-REV-1). The returned messages
    are this seat's own list; no other conversation shares it (L3-REV-1).
    """
    record = SeatRecord(seat=seat)
    tools = build_reviewer_tools(workspace, record)
    tools_by_name = {tool.name for tool in tools}
    assert "report_review" in tools_by_name
    bound = model.bind_tools(list(tools))
    messages: list[BaseMessage] = [HumanMessage(content=brief)]
    by_name: dict[str, BaseTool] = {tool.name: tool for tool in tools}
    for _ in range(max_rounds):
        response = bound.invoke(list(messages))
        assert isinstance(response, BaseMessage)
        messages.append(response)
        if not isinstance(response, AIMessage) or not response.tool_calls:
            return SeatResult(seat=seat, messages=messages, findings=record.findings)
        results: list[ToolMessage] = []
        for index, call in enumerate(response.tool_calls):
            name = str(call.get("name"))
            raw_id = call.get("id")
            call_id = str(raw_id) if raw_id else f"unidentified-{index}"
            tool = by_name.get(name)
            if tool is None:
                results.append(
                    ToolMessage(content=f"Error: no such tool {name!r}", tool_call_id=call_id)
                )
                continue
            try:
                content = str(tool.invoke(call.get("args", {})))
            except Exception as exc:
                content = f"Error: {exc}"
            results.append(ToolMessage(content=content, tool_call_id=call_id))
        messages.extend(results)
        if record.findings is not None:
            return SeatResult(seat=seat, messages=messages, findings=record.findings)
    return SeatResult(seat=seat, messages=messages, findings=record.findings)


ReviewSeatName = Literal["Standards", "Spec"]


@dataclass
class ReviewInvocationResult:
    """Both seats' end: their reports, messages and meter spend."""

    ordinal: int
    branch_name: str
    commit_sha: str
    standards: SeatResult
    spec: SeatResult
    usage: UsageTotals = field(default_factory=UsageTotals)

    @property
    def completed(self) -> bool:
        """Whether both seats made a Review Report (§10)."""
        return self.standards.completed and self.spec.completed

    @property
    def missing_seats(self) -> tuple[str, ...]:
        """Seats that ended without ``report_review`` (G-REV-1)."""
        return tuple(
            result.seat for result in (self.standards, self.spec) if not result.completed
        )

    def to_completed_review(self) -> CompletedReview:
        """File this invocation's seat reports as Attempt-wide review state."""
        return CompletedReview(
            ordinal=self.ordinal,
            commit_sha=self.commit_sha,
            branch_name=self.branch_name,
            standards_findings=self.standards.findings,
            spec_findings=self.spec.findings,
            usage=self.usage,
        )


def _seat_usage(messages: list[BaseMessage]) -> UsageTotals:
    """One seat's meter spend: provider usage plus its tool calls (§11)."""
    tokens = 0
    effective_tokens = 0
    tool_calls = 0
    for message in messages[1:]:
        if not isinstance(message, AIMessage):
            continue
        raw_usage = getattr(message, "usage_metadata", None)
        metadata = (
            cast(UsageMetadata | None, raw_usage)
            if isinstance(raw_usage, dict) or raw_usage is None
            else None
        )
        breakdown = usage_breakdown(metadata)
        tokens += breakdown.total_tokens
        effective_tokens += breakdown.effective_tokens
        tool_calls += len(message.tool_calls or [])
    return UsageTotals(
        tokens=tokens,
        effective_tokens=effective_tokens,
        cost_usd=0.0,
        tool_calls=tool_calls,
    )


def run_review_invocation(
    model: InvokableToolModel,
    *,
    skills_dir: Path,
    workspace: Path,
    ordinal: int,
    branch_name: str,
    commit_sha: str,
    base_revision: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    role_spec: RoleBlockSpec = DEFAULT_ROLE_BLOCK_SPEC,
    max_rounds: int = MAX_REVIEW_ROUNDS,
) -> ReviewInvocationResult:
    """Fan out the two reviewer conversations against the pushed tree (§10).

    Cuts each seat's own Role Block byte-identical from the installed
    ``code-review`` ``SKILL.md`` (L3-REV-2), composes each brief by
    concatenation, and runs Standards then Spec, each with its own message
    list and read-only tool set (L3-REV-1). Both seats review the same pushed
    commit; each seat ends only through ``report_review`` (G-REV-1). The
    returned ``usage`` sums both seats' spend for the Attempt-wide meters,
    with no reserved review budget (W-STP-6).
    """
    standards_block, spec_block = extract_role_blocks(skills_dir, role_spec)
    standards_sources = discover_standards_sources(workspace)
    standards_brief = compose_reviewer_brief(
        seat=STANDARDS_SEAT,
        role_block=standards_block,
        branch_name=branch_name,
        commit_sha=commit_sha,
        base_revision=base_revision,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        standards_sources=standards_sources,
    )
    spec_brief = compose_reviewer_brief(
        seat=SPEC_SEAT,
        role_block=spec_block,
        branch_name=branch_name,
        commit_sha=commit_sha,
        base_revision=base_revision,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        standards_sources=standards_sources,
    )
    standards = run_review_seat(
        model, seat=STANDARDS_SEAT, brief=standards_brief, workspace=workspace,
        max_rounds=max_rounds,
    )
    spec = run_review_seat(
        model, seat=SPEC_SEAT, brief=spec_brief, workspace=workspace, max_rounds=max_rounds,
    )
    usage = UsageTotals(
        tokens=_seat_usage(standards.messages).tokens + _seat_usage(spec.messages).tokens,
        effective_tokens=(
            _seat_usage(standards.messages).effective_tokens
            + _seat_usage(spec.messages).effective_tokens
        ),
        cost_usd=0.0,
        tool_calls=(
            _seat_usage(standards.messages).tool_calls + _seat_usage(spec.messages).tool_calls
        ),
    )
    return ReviewInvocationResult(
        ordinal=ordinal,
        branch_name=branch_name,
        commit_sha=commit_sha,
        standards=standards,
        spec=spec,
        usage=usage,
    )


def review_tool_result_text(
    *,
    outcome_branch_name: str,
    outcome_remote_sha: str,
    outcome_pushed: bool,
    write_kind: str,
    result: ReviewInvocationResult,
) -> str:
    """Render the ``activate_code_review`` tool result (§10).

    Carries the pushed snapshot and both Review Reports where both seats
    reported; where a seat ended without ``report_review`` the result names
    that seat instead (G-REV-1).
    """
    pushed_note = "pushed" if outcome_pushed else "already pushed"
    header = (
        f"Review snapshot {write_kind} {pushed_note}: "
        f"{outcome_remote_sha} on {outcome_branch_name}. "
        f"Review invocation {result.ordinal}: "
    )
    if result.completed:
        assert result.standards.findings is not None
        assert result.spec.findings is not None
        return (
            f"{header}completed with both Review Reports.\n"
            f"## {STANDARDS_SEAT} Review Report\n"
            f"{result.standards.findings}\n"
            f"## {SPEC_SEAT} Review Report\n"
            f"{result.spec.findings}"
        )
    missing = ", ".join(result.missing_seats)
    parts = [f"{header}incomplete: no Review Report from the {missing} seat."]
    for seat_result in (result.standards, result.spec):
        if seat_result.completed:
            assert seat_result.findings is not None
            parts.append(f"## {seat_result.seat} Review Report\n{seat_result.findings}")
    return "\n".join(parts)
