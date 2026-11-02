"""The step subgraph: one work step's bounded model loop with silence handling.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the step
subgraph's only external write is the M3 review-snapshot push, so M2 writes
nothing), §8 (one Workflow Step's conversation with its model, its tools
and its exit; a step's first request holds the Pinned Prefix, then the
forced activation call through ``tool_choice``; the activation result is an
ordinary Exchange Unit, never a Pinned Prefix member; the final step ends
with ``report_completion``; silence is answered, not obeyed — a response
with no tool calls and no accepted exit draws one deterministic
continuation message composed by the runtime from the unmet obligations,
budget two per work node, monotonic and never reset by progress; once spent
the node ends ``completion not established`` / ``undeclared``; no
continuation is issued in the verification reserve phase or after a hard
ceiling), §9
(``report_completion`` is the only non-exceptional exit; a claim naming an
unbacked test is returned with a tool result naming every failing entry,
and a return is never a progress event; after two returns the third call is
force-accepted, and the claim-rejection budget belongs to this work node;
the only other exits are a ceiling, the verification reserve and a
classified failure) and
§11 (an oversized tool result is capped to its head, its tail and a pointer
naming the stored artifact and its full size, so the model can ask for a
range of it; activation results are exempt; within a node every assistant
turn is carried back verbatim — ADR 0010 — and Exchange Units stay whole;
cache breakpoints are applied; a Pinned Prefix that cannot fit with its
overhead is refused before any model work; Progress Gates and the
verification reserve are Attempt-wide meters observed after every model
response and every tool result; five consecutive responses without a
progress event record one soft stall; at 70% of any meter the ledger
records one durable Gate and at 80% one reserve entry; the phase is
monotonic and broad exploration cannot resume; the loop stops inside the
node as soon as a ceiling is crossed), and
[ADR 0014](../../docs/adr/0014-completion-is-declared-and-substantiated-never-inferred.md).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-SKL-1, C-SKL-2, L3-IMP-1,
L3-IMP-5, L3-IMP-13, L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-24, L3-IMP-25,
L3-IMP-26, C-IMP-1, L3-IMP-28, L3-IMP-29, L3-IMP-30.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from coding_agent.runtime.silence import (
    MAX_SILENCE_CONTINUATIONS,
    continuation_message,
)
from langgraph.graph.state import CompiledStateGraph

from coding_agent.ledger.store import BudgetSnapshot
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.ceilings import (
    LoopCeilings,
    UsageTotals,
    ceiling_crossed,
    usage_breakdown,
)
from coding_agent.runtime.completion import ClaimRecord, CriterionEvidence
from coding_agent.runtime.context_handoff import (
    ContextWindowConfig,
    estimate_context_tokens,
)
from coding_agent.runtime.exchange import ExchangeUnit
from coding_agent.runtime.graph.tools import is_activation_tool
from coding_agent.runtime.prompt_cache import apply_cache_breakpoints
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
    InMemoryArtifactStore,
    build_read_result_slice_tool,
    cap_tool_result,
)
from coding_agent.runtime.step_progress import (
    StepProgress,
    has_explicit_progress_marker,
    workspace_fingerprint,
)

FORCED_ACTIVATION_TOOL = "activate_implement"
"""The forced activation call for a ``skill: implement`` step (contract §8)."""


class StepState(TypedDict, total=False):
    """The step subgraph's conversation plus its silence budget.

    ``messages`` is the conversation only. ``silence_used`` counts the
    deterministic continuations already issued in this work node (contract §8,
    ADR 0014): it only ever goes up, and progress never resets it. A fresh
    work node opens with zero.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    silence_used: int


def _last_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


def has_report_completion(messages: list[BaseMessage]) -> bool:
    """Whether the conversation ends with a ``report_completion`` call."""
    last = _last_ai_message(messages)
    if last is None:
        return False
    return any(call["name"] == "report_completion" for call in last.tool_calls)


def report_completion_args(messages: list[BaseMessage]) -> dict[str, object]:
    """The arguments of the final ``report_completion`` call."""
    last = _last_ai_message(messages)
    assert last is not None, "no model response to read the completion from"
    for call in last.tool_calls:
        if call["name"] == "report_completion":
            return dict(call["args"])
    raise AssertionError("no report_completion call in the final model response")


def check_opening_fits_context(
    opening: Sequence[BaseMessage],
    tools: tuple[BaseTool, ...] | list[BaseTool],
    context_window: ContextWindowConfig,
) -> str | None:
    """Refuse an opening that cannot fit the usable context (§11, L3-IMP-13).

    Returns an explanatory refusal reason where the Pinned Prefix alone —
    or the Pinned Prefix together with tool-schema and request overhead —
    already exceeds ``context_window``'s usable threshold, else ``None``.
    This is never a partial drop of the prefix: the Attempt refuses to
    open, with no model call. The caller reports the returned reason and
    never invokes the model.
    """
    tool_list = list(tools)
    estimate = estimate_context_tokens(opening, [], tool_list)
    if estimate <= context_window.usable_threshold:
        return None
    return (
        "the Pinned Prefix with tool-schema and request overhead does not fit "
        f"the configured context window (estimated ~{estimate} tokens, usable "
        f"{context_window.usable_threshold} of {context_window.threshold_tokens} "
        "tokens); refusing to open the Attempt with no model call and no "
        "partial prefix"
    )


def _ensure_slice_tool(
    tools: list[BaseTool], store: ArtifactStore
) -> list[BaseTool]:
    """Return ``tools`` with a ``read_result_slice`` reader over ``store``.

    A capped pointer names this tool, so an opening tool set without one
    would strand every capped artifact. The tool is added once, bound to
    the same store capping stashes into.
    """
    if any(tool.name == "read_result_slice" for tool in tools):
        return tools
    return [*tools, build_read_result_slice_tool(store)]


def _is_suppressed_diagnostic(content: str) -> bool:
    """Whether a tool result is a suppressed targeted diagnostic (§11).

    A suppressed, unchanged targeted diagnostic carries no new Criterion
    Evidence and changes no diff, so it enters the same soft-stall path as
    five responses without progress (L3-IMP-17) instead of resetting it.
    """
    return '"suppressed": true' in content


def _is_stop_loop_diagnostic(content: str) -> bool:
    """Whether a tool result asks the loop to stop without further calls."""
    return '"stop_loop": true' in content


def build_implement_step_graph(
    model: InvokableToolModel,
    tools: tuple[BaseTool, ...] | list[BaseTool],
    *,
    forced_tool: str | None = FORCED_ACTIVATION_TOOL,
    result_cap_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    artifact_store: ArtifactStore | None = None,
    cache_breakpoints: bool = True,
    prefix_length: int | None = None,
    max_silence_continuations: int = MAX_SILENCE_CONTINUATIONS,
    silence_message_for_sequence: Callable[[int], str] | None = None,
    claim_record: ClaimRecord | None = None,
    progress: StepProgress | None = None,
    ceilings: LoopCeilings | None = None,
    workspace: Path | None = None,
    criterion_evidence: CriterionEvidence | None = None,
    started_at: float | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the ``open → model ⇄ tools/silence → exit`` subgraph (no checkpointer).

    The first model request binds ``tools`` with ``tool_choice=forced_tool``
    so the step opens with the forced activation call (contract §8). Later
    requests bind the same tools with no forced choice.

    Silence handling (contract §8 "Exit", §9, ADR 0014, L3-IMP-24/25):

    - a response with no tool calls and no accepted exit never yields an
      Implementation Outcome on its own: it draws one deterministic
      continuation message from ``silence_message_for_sequence`` (or a generic
      fallback where no composer is given), which never comes from a model
      call;
    - the budget is ``max_silence_continuations`` per work node (two in
      production), monotonic and never reset by progress;
    - once spent the node ends ``completion not established`` / ``undeclared``
      (decided by the caller from the absence of ``report_completion``);
    - no continuation is issued in the verification reserve phase or after
      a hard ceiling; there the node exits for the caller's cutoff mapping;
    - the only exits out of the tool loop are ``report_completion``, a
      ceiling, the verification reserve and a classified failure. Ceilings
      stop the loop inside the node with no further model call and no
      model-initiated tool call; the reserve phase and failure
      classification end broad exploration the same way, and never map
      silence to an outcome.

    Claim returns (contract §9, C-IMP-1): ``report_completion`` runs as an
    ordinary tool. Its tool implementation returns unbacked claims with a
    result naming every failing entry, so the model can react on the next
    turn; an accepted (or force-accepted) claim ends the step from the tools
    node. A return records no progress event. ``claim_record`` is the work
    node's rejection budget; a fresh subgraph opens with a fresh record.

    Progress Gates and the verification reserve (contract §11, L3-IMP-16,
    L3-IMP-17, L3-IMP-18, L3-IMP-30): where ``progress`` is bound, every
    model response and every tool result is observed against the
    Attempt-wide meters. A response records a progress event only when it
    resolves an Acceptance Criterion or Seam Set uncertainty, changes the
    candidate diff, or produces new Criterion Evidence after such a change;
    a suppressed, unchanged targeted diagnostic enters the same soft-stall
    path without resetting progress. Five consecutive responses without a
    progress event record one soft stall; at 70% of any meter the ledger
    records one durable Gate and at 80% one reserve entry; the phase only
    moves forward. Where ``progress`` is None (unit-test seam) no ledger
    row is written and the loop behaves as the pre-reserve shape.

    Bounded context (§11, L3-IMP-5, L3-IMP-13):

    - every non-activation tool result over ``result_cap_limit`` characters
      is capped to head plus tail plus a pointer naming the stored artifact
      and its full size; the full text lives in ``artifact_store`` and the
      ``read_result_slice`` tool reads a chosen range back. Activation
      results are exempt and always stay whole.
    - the outgoing request is tagged with Anthropic cache breakpoints at
      the Pinned Prefix end and the tail end, on a copy only; the stored
      conversation keeps exactly what the provider or a tool produced, and
      every assistant turn is carried back verbatim (ADR 0010).
    - ``prefix_length`` is the Pinned Prefix's message count, locating the
      prefix breakpoint. ``run_implement_step`` passes its opening length;
      a direct caller that passes none tags the tail only.
    """
    tool_list = _ensure_slice_tool(
        list(tools), artifact_store if artifact_store is not None else InMemoryArtifactStore()
    )
    store: ArtifactStore = (
        artifact_store if artifact_store is not None else InMemoryArtifactStore()
    )
    # Where the slice reader from ``_ensure_slice_tool`` reads from must be
    # the same store capping stashes into. When the caller passed none, the
    # reader above was bound to a throwaway store; rebind it to ``store``.
    if artifact_store is None:
        tool_list = [tool for tool in tool_list if tool.name != "read_result_slice"]
        tool_list = [*tool_list, build_read_result_slice_tool(store)]
    # Bind the forced activation first: the scripted-model test seam records
    # every ``bind_tools`` call in order, and C-SKL-2 asserts the first
    # recorded ``tool_choice`` is the forced activation call. Functionally the
    # order is irrelevant — each binding is a distinct bound model — but the
    # recording order is the bytes-on-the-wire proof the test asserts on.
    forced_first = model.bind_tools(tool_list, tool_choice=forced_tool) if forced_tool is not None else None
    unforced = model.bind_tools(tool_list)
    forced = forced_first if forced_first is not None else unforced
    tools_by_name: dict[str, BaseTool] = {tool.name: tool for tool in tool_list}
    prefix_len: int = prefix_length if prefix_length is not None else 0

    track = progress
    ceilings_eff = ceilings if ceilings is not None else (
        track.ceilings if track is not None else LoopCeilings()
    )
    workspace_eff = workspace
    evidence = criterion_evidence
    start = started_at if started_at is not None else time.monotonic()
    loop_state: dict[str, object] = {
        "tool_calls": 0,
        "tokens": 0,
        "effective_tokens": 0,
        "cost_usd": 0.0,
        "stop_failure": False,
        "fingerprint_before": (
            workspace_fingerprint(workspace_eff) if workspace_eff is not None else None
        ),
        "evidence_len_before": len(evidence.history()) if evidence is not None else 0,
        "diff_ever_changed": False,
    }

    def _snapshot() -> BudgetSnapshot:
        return BudgetSnapshot(
            elapsed_seconds=time.monotonic() - start,
            usage=UsageTotals(
                tokens=int(loop_state["tokens"]),
                effective_tokens=int(loop_state["effective_tokens"]),
                cost_usd=float(loop_state["cost_usd"]),
                tool_calls=int(loop_state["tool_calls"]),
            ),
        )

    def _note_ceiling(snapshot: BudgetSnapshot) -> None:
        if track is None:
            return
        name = ceiling_crossed(snapshot.usage, snapshot.elapsed_seconds, ceilings_eff)
        if name is not None:
            track.ceiling = name

    def open_step(state: StepState, config: RunnableConfig) -> dict[str, object]:
        _ = (state, config)
        return {"messages": [], "silence_used": 0}

    def call_model(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = config
        # One opening message list means no model response yet: force the
        # activation call. Everything after answers normally.
        has_ai = any(isinstance(message, AIMessage) for message in state["messages"])
        bound = forced if not has_ai else unforced
        to_send: list[BaseMessage] = list(state["messages"])
        if cache_breakpoints:
            to_send = apply_cache_breakpoints(to_send, prefix_length=prefix_len)
        # The assistant turn is stored exactly as returned — never rebuilt
        # from ``.content``/``.tool_calls`` (ADR 0010) — so provider-specific
        # blocks survive to the next request.
        response = bound.invoke(to_send)
        assert isinstance(response, BaseMessage)
        if track is not None:
            raw_usage = getattr(response, "usage_metadata", None)
            breakdown = usage_breakdown(
                raw_usage if isinstance(raw_usage, dict) or raw_usage is None else None
            )
            loop_state["tokens"] = int(loop_state["tokens"]) + breakdown.total_tokens
            loop_state["effective_tokens"] = (
                int(loop_state["effective_tokens"]) + breakdown.effective_tokens
            )
            snapshot = _snapshot()
            track.observe_budget(snapshot)
            _note_ceiling(snapshot)
        return {"messages": [response]}

    def run_tools(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = config
        last = _last_ai_message(state["messages"])
        if last is None:
            return {"messages": []}
        tool_results: list[ToolMessage] = []
        for index, call in enumerate(last.tool_calls):
            name = str(call["name"])
            raw_id = call.get("id")
            call_id = str(raw_id) if raw_id else f"unidentified-{index}"
            tool = tools_by_name.get(name)
            if tool is None:
                content = f"Error: no such tool {name!r}"
            else:
                try:
                    content = str(tool.invoke(call.get("args", {})))
                except Exception as exc:
                    content = f"Error: {exc}"
                if not is_activation_tool(name):
                    content = cap_tool_result(
                        content,
                        tool_call_id=call_id,
                        limit=result_cap_limit,
                        store=store,
                    )
            tool_results.append(ToolMessage(content=content, tool_call_id=call_id))
        # Each assistant turn plus its results is one Exchange Unit: the
        # assistant and all of its results move together, never split, so
        # no request carries a tool result without the assistant turn that
        # called it or vice versa.
        unit = ExchangeUnit(assistant=last, results=tuple(tool_results))
        assert unit.messages[0] is last
        loop_state["tool_calls"] = int(loop_state["tool_calls"]) + len(last.tool_calls)
        if track is not None:
            snapshot = _snapshot()
            fingerprint_before = loop_state["fingerprint_before"]
            fingerprint_after = (
                workspace_fingerprint(workspace_eff)
                if workspace_eff is not None
                else fingerprint_before
            )
            diff_changed = (
                fingerprint_before is not None
                and fingerprint_after is not None
                and fingerprint_after != fingerprint_before
            )
            if diff_changed:
                loop_state["diff_ever_changed"] = True
            evidence_before = int(loop_state["evidence_len_before"])
            evidence_after = (
                len(evidence.history()) if evidence is not None else evidence_before
            )
            evidence_grew = evidence_after > evidence_before
            progressed = False
            if diff_changed:
                track.record_progress(
                    "diff",
                    snapshot,
                    diff_ref=(
                        f"workspace:{fingerprint_after[:12]}"
                        if isinstance(fingerprint_after, str)
                        else None
                    ),
                )
                progressed = True
            elif evidence_grew and bool(loop_state["diff_ever_changed"]):
                track.record_progress("targeted_result", snapshot)
                progressed = True
            elif has_explicit_progress_marker(last.content):
                track.record_progress("acceptance_criterion", snapshot)
                progressed = True
            if not progressed:
                contents = [str(message.content) for message in tool_results]
                if any(_is_stop_loop_diagnostic(text) for text in contents):
                    loop_state["stop_failure"] = True
                    track.observe_response(snapshot, progress=False)
                elif any(_is_suppressed_diagnostic(text) for text in contents):
                    track.observe_diagnostic(snapshot)
                else:
                    track.observe_response(snapshot, progress=False)
            track.observe_budget(snapshot)
            _note_ceiling(snapshot)
            loop_state["fingerprint_before"] = fingerprint_after
            loop_state["evidence_len_before"] = evidence_after
        return {"messages": list(unit.results)}

    def run_silence(state: StepState, config: RunnableConfig) -> dict[str, object]:
        """Answer silence with one deterministic continuation (contract §8).

        Composed by the runtime from the unmet obligations, never by a model
        call. The budget only ever goes up here; progress never resets it.
        In the verification reserve phase or after a hard ceiling the
        caller exits instead of reaching this node, so no continuation is
        ever composed there.
        """
        _ = config
        if track is not None:
            snapshot = _snapshot()
            track.observe_response(snapshot, progress=False)
            track.observe_budget(snapshot)
            _note_ceiling(snapshot)
        used = int(state.get("silence_used", 0))
        sequence_number = used + 1
        if silence_message_for_sequence is not None:
            text = silence_message_for_sequence(sequence_number)
        else:
            text = (
                "Implementation cannot be inferred from silence "
                f"(continuation {sequence_number}/{max_silence_continuations}). "
                "Call report_completion to declare completion, or use the work "
                "tools to make progress. Silence alone never establishes completion."
            )
        return {
            "messages": [continuation_message(sequence_number, text)],
            "silence_used": sequence_number,
        }

    def route_after_model(state: StepState) -> Literal["tools", "silence", "exit"]:
        """Route one model response (contract §8 "Exit", §9, L3-IMP-25, C-IMP-1).

        - a response with tool calls runs tools — ``report_completion``
          included, so its tool implementation can return an unbacked claim
          or accept it (the only non-exceptional exit is an accepted claim
          leaving the tools node);
        - a response with no tool calls and no accepted exit draws a
          continuation while budget remains, else exits for ``undeclared``.
        A response with no tool calls never yields an Implementation Outcome
        on its own. The only other exits out of the tool loop are a ceiling,
        the verification reserve and a classified failure (their tickets own
        those branches; this routing reserves them and never maps silence to
        an outcome).
        """
        last = _last_ai_message(state["messages"])
        if last is not None and last.tool_calls:
            return "tools"
        # A ceiling stops the loop inside the node with no further model
        # call and no model-initiated tool call; the verification reserve
        # phase issues no continuation either. Silence in either state
        # exits for the caller's cutoff mapping.
        if track is not None:
            if track.ceiling is not None or bool(loop_state.get("stop_failure")):
                return "exit"
            if track.phase == "verification_reserve":
                return "exit"
        # Silence: no tool calls and no accepted exit. Answer while budget
        # remains; once spent the caller ends ``completion not established`` /
        # ``undeclared``. Ceilings, the verification reserve phase and
        # classified failures are the only other exits (their own tickets).
        used = int(state.get("silence_used", 0))
        if used >= max_silence_continuations:
            return "exit"
        return "silence"

    def route_after_tools(state: StepState) -> Literal["model", "exit"]:
        # An accepted (or force-accepted) claim ends the step here, with no
        # further model call. A returned claim leaves ``accepted_raw`` unset
        # and the model reacts on the next turn (contract §9, C-IMP-1).
        if claim_record is not None:
            if claim_record.accepted_raw is not None:
                return "exit"
            # A ceiling crossed mid tool loop stops the loop inside the node:
            # no further model call and no model-initiated tool call. The
            # caller maps the cutoff from the claim state and the diff.
            if track is not None and (
                track.ceiling is not None or bool(loop_state.get("stop_failure"))
            ):
                return "exit"
            return "model"
        # No record (unit-test seam): the tool records unconditionally, so a
        # turn that called ``report_completion`` is done once its results ran.
        last = _last_ai_message(state["messages"])
        if last is not None and any(
            call["name"] == "report_completion" for call in last.tool_calls
        ):
            return "exit"
        if track is not None and (
            track.ceiling is not None or bool(loop_state.get("stop_failure"))
        ):
            return "exit"
        return "model"

    builder = StateGraph(StepState)
    builder.add_node("open", open_step)
    builder.add_node("model", call_model)
    builder.add_node("tools", run_tools)
    builder.add_node("silence", run_silence)
    builder.add_edge(START, "open")
    builder.add_edge("open", "model")
    builder.add_conditional_edges(
        "model", route_after_model, {"tools": "tools", "silence": "silence", "exit": END}
    )
    builder.add_conditional_edges(
        "tools", route_after_tools, {"model": "model", "exit": END}
    )
    builder.add_edge("silence", "model")
    return builder.compile(checkpointer=None)


def run_implement_step(
    model: InvokableToolModel,
    tools: tuple[BaseTool, ...] | list[BaseTool],
    opening: BaseMessage | list[BaseMessage],
    *,
    forced_tool: str | None = FORCED_ACTIVATION_TOOL,
    result_cap_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    artifact_store: ArtifactStore | None = None,
    cache_breakpoints: bool = True,
    max_silence_continuations: int = MAX_SILENCE_CONTINUATIONS,
    silence_message_for_sequence: Callable[[int], str] | None = None,
    claim_record: ClaimRecord | None = None,
    progress: StepProgress | None = None,
    ceilings: LoopCeilings | None = None,
    workspace: Path | None = None,
    criterion_evidence: CriterionEvidence | None = None,
    started_at: float | None = None,
) -> list[BaseMessage]:
    """Run one ``implement`` step and return its final message list.

    ``opening`` is the Pinned Prefix as a message or message list (Attempt
    Header, then the Target Issue). The first model request carries it with
    ``tool_choice`` forcing the activation call; the activation result is an
    ordinary Exchange Unit handled by the tools node like any other, except
    it is never capped. Every other oversized tool result is capped (§11,
    L3-IMP-5) and readable back through ``read_result_slice``.

    Silence handling (contract §8, ADR 0014, L3-IMP-24): a turn with no tool
    calls and no accepted exit draws one deterministic continuation from
    ``silence_message_for_sequence`` (or a generic fallback), at most
    ``max_silence_continuations`` per work node, monotonic and never reset by
    progress. Once spent the step ends for ``completion not established`` /
    ``undeclared`` (decided by the caller). No continuation is issued in
    the verification reserve phase or after a hard ceiling.

    ``claim_record`` carries the work node's claim-rejection budget
    (contract §9, C-IMP-1): returned claims loop back to the model, and an
    accepted claim exits from the tools node. A fresh step passes a fresh
    record.

    ``progress`` carries the work node's stall, Gate and reserve policy
    (contract §11, L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-30): where bound,
    every model response and every tool result is observed against the
    Attempt-wide meters, a ceiling stops the loop inside the node, and the
    verification reserve phase issues no silence continuation. Where None
    (unit-test seam) no ledger row is written.
    """
    from langchain_core.messages import BaseMessage as _BaseMessage

    if isinstance(opening, _BaseMessage):
        initial: list[BaseMessage] = [opening]
    else:
        initial = list(opening)
    store: ArtifactStore = (
        artifact_store if artifact_store is not None else InMemoryArtifactStore()
    )
    graph = build_implement_step_graph(
        model,
        tools,
        forced_tool=forced_tool,
        result_cap_limit=result_cap_limit,
        artifact_store=store,
        cache_breakpoints=cache_breakpoints,
        prefix_length=len(initial),
        max_silence_continuations=max_silence_continuations,
        silence_message_for_sequence=silence_message_for_sequence,
        claim_record=claim_record,
        progress=progress,
        ceilings=ceilings,
        workspace=workspace,
        criterion_evidence=criterion_evidence,
        started_at=started_at,
    )
    result = graph.invoke({"messages": initial, "silence_used": 0})
    assert isinstance(result, dict)
    messages = result["messages"]
    assert isinstance(messages, list)
    return messages
