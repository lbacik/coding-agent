"""Runtime context for Attempt graph nodes, carried outside graph state.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 ("Runtime context
stays out of graph state: Attempt-wide meters, Criterion Evidence, review
records and the tracker port are passed to nodes as runtime context, not
carried in graph state") and §6 (the Attempt graph).

MVP row ids: C-GRF-1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.tracker.port import TrackerPort


@dataclass(frozen=True)
class AttemptContext:
    """Everything a node needs that must not live in graph state.

    Meters, Criterion Evidence (M2), review records (M3) and the tracker
    port travel here, inside ``RunnableConfig["configurable"]``, while the
    graph state carries only the Attempt's small data-plane fields (ids, the
    fetched issue, the outcome under construction). ``ledger`` is the real
    Run Ledger file handle, not a copy: recording the Terminal Outcome and
    reading it back for recovery go through this object.
    """

    tracker: TrackerPort
    ledger: RunLedger
    model: InvokableToolModel
    workspace: Path
    effective_configuration: Mapping[str, object]

    @classmethod
    def from_config(cls, config: RunnableConfig | None) -> AttemptContext:
        """Recover the context the caller passed to ``graph.invoke``."""
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        context = configurable.get("attempt_context")
        assert isinstance(context, AttemptContext), (
            "the Attempt graph requires an AttemptContext under "
            "config['configurable']['attempt_context']"
        )
        return context

    def to_configurable(self) -> dict[str, object]:
        """Wrap this context for ``graph.invoke(state, config=...)``."""
        return {"attempt_context": self}
