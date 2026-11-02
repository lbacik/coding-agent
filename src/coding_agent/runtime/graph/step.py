"""The M1 step subgraph: one ``implement`` step's model loop.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (the step
subgraph's only external write is the M3 review-snapshot push, so M1 writes
nothing) and §8 (one Workflow Step's conversation with its model, its tools
and its exit; the final step ends with ``report_completion``).

MVP row ids: L3-SEL-11, L3-CLR-19, L3-IMP-9, C-SKL-1.

The subgraph is ``open → model ⇄ tools → exit`` with no checkpointer: the
model node appends one response, the tools node executes its tool calls, and
the loop ends when the model calls ``report_completion`` (or makes no tool
call at all — silence continuations belong to M2). The caller reads the
completion back out of the final message list; graph state is not the
Attempt's state.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from coding_agent.provider.pinned_model import InvokableToolModel


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
    model: InvokableToolModel, tools: tuple[BaseTool, ...] | list[BaseTool],
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the ``open → model ⇄ tools → exit`` subgraph (no checkpointer)."""
    bound = model.bind_tools(list(tools))

    def open_step(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = (state, config)
        return {"messages": []}

    def call_model(state: StepState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        _ = config
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
    builder.add_node("tools", ToolNode(list(tools)))
    builder.add_edge(START, "open")
    builder.add_edge("open", "model")
    builder.add_conditional_edges("model", route_after_model, {"tools": "tools", "exit": END})
    builder.add_edge("tools", "model")
    return builder.compile(checkpointer=None)


def run_implement_step(
    model: InvokableToolModel,
    tools: tuple[BaseTool, ...] | list[BaseTool],
    opening: HumanMessage,
) -> list[BaseMessage]:
    """Run one ``implement`` step and return its final message list."""
    graph = build_implement_step_graph(model, tools)
    result = graph.invoke({"messages": [opening]})
    assert isinstance(result, dict)
    messages = result["messages"]
    assert isinstance(messages, list)
    return messages
