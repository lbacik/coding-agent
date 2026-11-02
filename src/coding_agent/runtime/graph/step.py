"""The M1 step subgraph: one ``implement`` step's bounded model loop.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the step
subgraph's only external write is the M3 review-snapshot push, so M1 writes
nothing), §8 (one Workflow Step's conversation with its model, its tools
and its exit; a step's first request holds the Pinned Prefix, then the
forced activation call through ``tool_choice``; the activation result is an
ordinary Exchange Unit, never a Pinned Prefix member; the final step ends
with ``report_completion``) and §11 (an oversized tool result is capped to
its head, its tail and a pointer naming the stored artifact and its full
size, so the model can ask for a range of it; activation results are
exempt; within a node every assistant turn is carried back verbatim —
ADR 0010 — and Exchange Units stay whole; cache breakpoints are applied;
a Pinned Prefix that cannot fit with its overhead is refused before any
model work).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-SKL-1, C-SKL-2, L3-IMP-1,
L3-IMP-5, L3-IMP-13.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from coding_agent.provider.pinned_model import InvokableToolModel
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

FORCED_ACTIVATION_TOOL = "activate_implement"
"""The forced activation call for a ``skill: implement`` step (contract §8)."""


class StepState(TypedDict):
    """The step subgraph's conversation only."""

    messages: Annotated[list[BaseMessage], add_messages]


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
    opening: list[BaseMessage],
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


def build_implement_step_graph(
    model: InvokableToolModel,
    tools: tuple[BaseTool, ...] | list[BaseTool],
    *,
    forced_tool: str | None = FORCED_ACTIVATION_TOOL,
    result_cap_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    artifact_store: ArtifactStore | None = None,
    cache_breakpoints: bool = True,
    prefix_length: int | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the ``open → model ⇄ tools → exit`` subgraph (no checkpointer).

    The first model request binds ``tools`` with ``tool_choice=forced_tool``
    so the step opens with the forced activation call (contract §8). Later
    requests bind the same tools with no forced choice.

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

    def open_step(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = (state, config)
        return {"messages": []}

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
        return {"messages": list(unit.results)}

    def route_after_model(state: StepState) -> Literal["tools", "exit"]:
        last = _last_ai_message(state["messages"])
        if last is None or not last.tool_calls:
            return "exit"
        if any(call["name"] == "report_completion" for call in last.tool_calls):
            return "exit"
        return "tools"

    builder = StateGraph(StepState)
    builder.add_node("open", open_step)
    builder.add_node("model", call_model)
    builder.add_node("tools", run_tools)
    builder.add_edge(START, "open")
    builder.add_edge("open", "model")
    builder.add_conditional_edges("model", route_after_model, {"tools": "tools", "exit": END})
    builder.add_edge("tools", "model")
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
) -> list[BaseMessage]:
    """Run one ``implement`` step and return its final message list.

    ``opening`` is the Pinned Prefix as a message or message list (Attempt
    Header, then the Target Issue). The first model request carries it with
    ``tool_choice`` forcing the activation call; the activation result is an
    ordinary Exchange Unit handled by the tools node like any other, except
    it is never capped. Every other oversized tool result is capped (§11,
    L3-IMP-5) and readable back through ``read_result_slice``.
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
    )
    result = graph.invoke({"messages": initial})
    assert isinstance(result, dict)
    messages = result["messages"]
    assert isinstance(messages, list)
    return messages
