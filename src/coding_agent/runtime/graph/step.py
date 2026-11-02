"""The M1 step subgraph: one ``implement`` step's model loop.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the step
subgraph's only external write is the M3 review-snapshot push, so M1 writes
nothing), §8 (one Workflow Step's conversation with its model, its tools
and its exit; a step's first request holds the Pinned Prefix, then the
forced activation call through ``tool_choice``; the activation result is an
ordinary Exchange Unit, never a Pinned Prefix member; the final step ends
with ``report_completion``) and §11 (activation results are exempt from
result capping).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-SKL-1, C-SKL-2, L3-IMP-1.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from coding_agent.provider.pinned_model import InvokableToolModel

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


def build_implement_step_graph(
    model: InvokableToolModel,
    tools: tuple[BaseTool, ...] | list[BaseTool],
    *,
    forced_tool: str | None = FORCED_ACTIVATION_TOOL,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the ``open → model ⇄ tools → exit`` subgraph (no checkpointer).

    The first model request binds ``tools`` with ``tool_choice=forced_tool``
    so the step opens with the forced activation call (contract §8). Later
    requests bind the same tools with no forced choice.
    """
    tool_list = list(tools)
    # Bind the forced activation first: the scripted-model test seam records
    # every ``bind_tools`` call in order, and C-SKL-2 asserts the first
    # recorded ``tool_choice`` is the forced activation call. Functionally the
    # order is irrelevant — each binding is a distinct bound model — but the
    # recording order is the bytes-on-the-wire proof the test asserts on.
    forced_first = model.bind_tools(tool_list, tool_choice=forced_tool) if forced_tool is not None else None
    unforced = model.bind_tools(tool_list)
    forced = forced_first if forced_first is not None else unforced

    def open_step(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = (state, config)
        return {"messages": []}

    def call_model(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = config
        # One opening message list means no model response yet: force the
        # activation call. Everything after answers normally.
        has_ai = any(isinstance(message, AIMessage) for message in state["messages"])
        bound = forced if not has_ai else unforced
        response = bound.invoke(state["messages"])
        assert isinstance(response, BaseMessage)
        return {"messages": [response]}

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
    builder.add_node("tools", ToolNode(tool_list))
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
) -> list[BaseMessage]:
    """Run one ``implement`` step and return its final message list.

    ``opening`` is the Pinned Prefix as a message or message list (Attempt
    Header, then the Target Issue). The first model request carries it with
    ``tool_choice`` forcing the activation call; the activation result is an
    ordinary Exchange Unit handled by the tools node like any other.
    """
    from langchain_core.messages import BaseMessage as _BaseMessage

    if isinstance(opening, _BaseMessage):
        initial: list[BaseMessage] = [opening]
    else:
        initial = list(opening)
    graph = build_implement_step_graph(model, tools, forced_tool=forced_tool)
    result = graph.invoke({"messages": initial})
    assert isinstance(result, dict)
    messages = result["messages"]
    assert isinstance(messages, list)
    return messages
