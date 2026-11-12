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

    ``response_models`` scripts what the "provider" reports: the observed
    identity of each queued response, in order (``None`` where that
    response reports none). A provided entry sets (or, for ``None``,
    clears) the response metadata's ``model`` key on a copy of the queued
    message, so the ledger records scripted evidence, never the pin key.
    Omitted, every queued message goes out exactly as given.
    """

    def __init__(
        self,
        outcomes: list[AIMessage],
        *,
        response_models: Sequence[str | None] | None = None,
    ) -> None:
        if response_models is not None:
            if len(response_models) != len(outcomes):
                raise ValueError(
                    "response_models must name one observed identity "
                    f"per queued response, got {len(response_models)} "
                    f"for {len(outcomes)} outcomes"
                )
            stamped: list[AIMessage] = []
            for message, observed in zip(outcomes, response_models):
                metadata = dict(message.response_metadata or {})
                if observed is None:
                    metadata.pop("model", None)
                    metadata.pop("model_name", None)
                else:
                    metadata["model"] = observed
                stamped.append(
                    message.model_copy(update={"response_metadata": metadata})
                )
            outcomes = stamped
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
