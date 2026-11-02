"""A scripted model for the M1 primary test seam.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (the step's model
loop) and §1 (the model's toolset holds no GitHub mutation).

MVP row ids: C-GRF-1, L3-SEL-11, L3-CLR-19, L3-IMP-9.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from conftest import FakeChatModel


class ScriptedModel(FakeChatModel):
    """The existing ``FakeChatModel`` grown into a step-loop driver.

    Pops one queued ``AIMessage`` per ``invoke`` and records every message
    list it was called with (inherited). ``bind_tools`` additionally records
    the bound tool names, so tests can assert structurally — on the bytes
    actually handed to the model — that no label, comment, assignment,
    issue-edit or pull-request tool was ever bound.
    """

    def __init__(self, outcomes: list[AIMessage]) -> None:
        super().__init__(outcomes)
        self.bound_tool_names: list[str] = []
        self.tool_choices: list[str | None] = []
        self.bind_history: list[tuple[str | None, list[str]]] = []

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> ScriptedModel:
        names = [tool.name for tool in tools]
        self.bound_tool_names = names
        self.tool_choices.append(tool_choice)
        self.bind_history.append((tool_choice, list(names)))
        return self
