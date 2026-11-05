"""The M3 review subgraph: two isolated reviewer conversations.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (after the
review-snapshot push the review subgraph fans out two reviewer
conversations, Standards and Spec, each with its own message list and
read-only tools; the Reviewer Brief is composed by concatenation only — a
Seat Assignment, the Role Block cut verbatim from the installed
``code-review`` skill, and the concrete inputs the Role Block refers to,
carrying the change itself (the commit list and the diff computed at call
time against the Base Revision, bounded inline with the full diff behind
the seat's ``read_result_slice`` range reader); a
seat completes only through a ``report_review`` Review Report; the tool
result carries both Review Reports, or the seat that made none; every
invocation re-runs both seats against the tree at call time, metered against
the Attempt-wide meters with no reserved review budget) and §1 (the model's
toolset holds no GitHub mutation; a reviewer seat binds no review or
activation tool, so a seat cannot spawn another pair), with the verbatim
slices of
[ADR 0007](../../../docs/adr/0007-upstream-skill-text-is-answered-never-rewritten.md).

MVP row ids: L3-REV-1, L3-REV-2, G-REV-1, G-REV-2, P-DEL-4, W-STP-6, C-REV-1, L1-3.

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
from typing import Literal

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool

from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import UsageTotals, messages_usage
from coding_agent.runtime.git import GitFailure, commit_list_since_base, diff_since_base
from coding_agent.runtime.transcript import fork_conversation, review_conversation
from coding_agent.runtime.result_capping import (
    ArtifactStore,
    InMemoryArtifactStore,
    build_read_result_slice_tool,
    cap_tool_result,
)
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


REVIEW_DIFF_INLINE_LIMIT = 4_000
"""Characters of diff carried inline in one seat's concrete inputs (§10).

A diff at or under this bound is embedded whole. Over it, the brief carries
head plus tail with an explicit marker and a pointer, and the full text stays
reachable through the seat's ``read_result_slice`` range reader — the same
head/tail/pointer shape as the §11 result cap, but applied to brief text the
runtime composes rather than to a tool result.
"""

REVIEW_COMMIT_LIST_MAX_LINES = 50
"""Oneline commit-list entries carried inline in one seat's concrete inputs (§10).

An Attempt branch holds a handful of commits, so the list is small; beyond
this bound the brief keeps the first entries with an explicit marker naming
the total, never a silent cut.
"""


def review_diff_artifact_id(ordinal: int) -> str:
    """The artifact id the ``ordinal``-th invocation's full diff is stored under.

    Deterministic per invocation, so a seat's capped-diff pointer names an id
    the invocation's shared artifact store actually holds, for both seats.
    """
    return f"review-diff-{ordinal}"


@dataclass(frozen=True)
class ReviewChangeText:
    """The change itself, rendered for one invocation's concrete inputs (§10).

    ``commit_list`` is the oneline commit list between the Base Revision and
    the reviewed commit, bounded to ``REVIEW_COMMIT_LIST_MAX_LINES`` entries.
    ``diff`` is the unified diff over the same range: whole where it fits in
    ``REVIEW_DIFF_INLINE_LIMIT`` characters, else head plus tail with an
    explicit marker and a pointer naming the full text in the invocation's
    artifact store. Where the range cannot be read, both say so explicitly —
    a seat never has to guess whether an empty change or a failed read left
    the section blank.
    """

    commit_list: str
    diff: str


def prepare_review_change(
    *,
    workspace: Path,
    base_revision: str,
    commit_sha: str,
    ordinal: int,
    store: ArtifactStore,
) -> ReviewChangeText:
    """Compute the change between the Base Revision and ``commit_sha`` (§10).

    Runs ``git log <base>..<commit> --oneline`` and ``git diff
    <base>...<commit>`` in ``workspace`` at call time, bounds both for the
    brief, and stashes an over-bound diff's full text in ``store`` under
    ``review_diff_artifact_id(ordinal)``. A range the workspace cannot read
    (not a git checkout, base absent) yields an explicit "unavailable" note,
    never a failure: the invocation still runs, and the seats still report.
    """
    try:
        raw_commits = commit_list_since_base(workspace, base_revision, commit_sha)
        raw_diff = diff_since_base(workspace, base_revision, commit_sha)
    except (GitFailure, OSError) as exc:
        note = f"(change unavailable: the commit list and diff could not be read: {exc})"
        return ReviewChangeText(commit_list=note, diff=note)
    stripped = raw_commits.strip()
    if not stripped:
        commit_list = "(no commits between the Base Revision and the reviewed commit)"
    else:
        entries = stripped.splitlines()
        if len(entries) > REVIEW_COMMIT_LIST_MAX_LINES:
            kept = entries[:REVIEW_COMMIT_LIST_MAX_LINES]
            commit_list = "\n".join(
                [
                    *kept,
                    f"... [{len(entries)} commits total; showing the first "
                    f"{REVIEW_COMMIT_LIST_MAX_LINES}] ...",
                ]
            )
        else:
            commit_list = "\n".join(entries)
    if not raw_diff.strip():
        diff = "(no changes between the Base Revision and the reviewed commit)"
    else:
        diff = cap_tool_result(
            raw_diff,
            tool_call_id=review_diff_artifact_id(ordinal),
            limit=REVIEW_DIFF_INLINE_LIMIT,
            store=store,
        )
    return ReviewChangeText(commit_list=commit_list, diff=diff)


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
    commit_list: str,
    diff: str,
) -> str:
    """The concrete inputs the seat's Role Block refers to (contract §10).

    Supplied by juxtaposition after the verbatim Role Block, never rewritten
    into it (ADR 0007): the branch and commit under review (the workspace
    tree is at that commit), the Base Revision with the change itself — the
    commit list and the diff computed at call time by the runtime, not bare
    commands for the seat to run (it holds no shell) — and per seat: the
    standards-source file list for Standards, the Target Issue as the spec
    for Spec. A diff over ``REVIEW_DIFF_INLINE_LIMIT`` characters arrives
    bounded with a pointer; the seat reads the full text back through its
    ``read_result_slice`` range reader.

    MVP row ids: G-REV-2, L3-REV-1, L3-REV-2.
    """
    lines = [
        "Concrete inputs.",
        "",
        f"- Branch under review: {branch_name}",
        f"- Commit under review: {commit_sha} (the workspace tree is at this commit; read it with the file tools)",
        f"- Base revision: {base_revision} (review the change below, not the whole tree)",
        f"- Commit list ({base_revision}..{commit_sha} --oneline, computed at call time by the runtime):",
        commit_list,
        f"- Diff ({base_revision}...{commit_sha}, computed at call time by the runtime):",
        diff,
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
    commit_list: str,
    diff: str,
) -> str:
    """Compose one seat's Reviewer Brief by concatenation only (§10, L3-REV-2).

    Exactly three parts in order — the Seat Assignment, the seat's own Role
    Block byte-identical from the installed ``SKILL.md``, and the concrete
    inputs carrying the change itself — joined without editing a byte inside
    any part.
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
            commit_list=commit_list,
            diff=diff,
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


def build_reviewer_tools(
    workspace: Path, record: SeatRecord, *, artifact_store: ArtifactStore | None = None
) -> tuple[BaseTool, ...]:
    """One seat's isolated, read-only tool set (contract §1, §10).

    The workspace-scoped read tools plus the seat's own ``report_review``
    exit, plus the ``read_result_slice`` range reader over the invocation's
    artifact store, through which the seat reads back the full diff where the
    brief's inline copy is bounded. No work tool that writes, no shell, no
    activation tool and no step exit: asserted structurally, so a seat can
    neither edit the tree nor spawn another pair (L3-REV-1, L3-REV-2, G-REV-2).
    """
    read_tools = tuple(
        tool
        for tool in build_file_tools(workspace)
        if tool.name in REVIEW_READ_ONLY_TOOL_NAMES
    )
    assert {tool.name for tool in read_tools} == set(REVIEW_READ_ONLY_TOOL_NAMES), (
        "the reviewer read set drifted from build_file_tools"
    )
    store = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    tools = (*read_tools, build_read_result_slice_tool(store), build_report_review_tool(record))
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
    artifact_store: ArtifactStore | None = None,
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
    tools = build_reviewer_tools(workspace, record, artifact_store=artifact_store)
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


def _seat_usage(messages: list[BaseMessage], price: TokenPrices | None) -> UsageTotals:
    """One seat's meter spend: provider usage, its cost per token bucket and
    its tool calls (§11, L1-3). The brief (``messages[0]``) spends nothing."""
    return messages_usage(messages[1:], price)


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
    artifact_store: ArtifactStore | None = None,
    price: TokenPrices | None = None,
) -> ReviewInvocationResult:
    """Fan out the two reviewer conversations against the pushed tree (§10).

    Cuts each seat's own Role Block byte-identical from the installed
    ``code-review`` ``SKILL.md`` (L3-REV-2), computes the change itself at
    call time against the Base Revision and composes each brief by
    concatenation, and runs Standards then Spec, each with its own message
    list and read-only tool set (L3-REV-1, G-REV-2). Both seats review the
    same pushed commit; each seat ends only through ``report_review``
    (G-REV-1). The returned ``usage`` sums both seats' spend for the
    Attempt-wide meters, with no reserved review budget (W-STP-6).
    """
    standards_block, spec_block = extract_role_blocks(skills_dir, role_spec)
    standards_sources = discover_standards_sources(workspace)
    store: ArtifactStore = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    change = prepare_review_change(
        workspace=workspace,
        base_revision=base_revision,
        commit_sha=commit_sha,
        ordinal=ordinal,
        store=store,
    )
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
        commit_list=change.commit_list,
        diff=change.diff,
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
        commit_list=change.commit_list,
        diff=change.diff,
    )
    standards_model = fork_conversation(model, review_conversation(ordinal, STANDARDS_SEAT))
    spec_model = fork_conversation(model, review_conversation(ordinal, SPEC_SEAT))
    standards = run_review_seat(
        standards_model, seat=STANDARDS_SEAT, brief=standards_brief, workspace=workspace,
        max_rounds=max_rounds, artifact_store=store,
    )
    spec = run_review_seat(
        spec_model, seat=SPEC_SEAT, brief=spec_brief, workspace=workspace, max_rounds=max_rounds,
        artifact_store=store,
    )
    usage = _seat_usage(standards.messages, price) + _seat_usage(spec.messages, price)
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
    seat_word = "seats" if len(result.missing_seats) > 1 else "seat"
    parts = [f"{header}incomplete: no Review Report from the {missing} {seat_word}."]
    for seat_result in (result.standards, result.spec):
        if seat_result.completed:
            assert seat_result.findings is not None
            parts.append(f"## {seat_result.seat} Review Report\n{seat_result.findings}")
    return "\n".join(parts)
