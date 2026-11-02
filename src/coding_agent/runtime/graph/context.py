"""Runtime context for Attempt graph nodes, carried outside graph state.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 ("Runtime context
stays out of graph state: Attempt-wide meters, Criterion Evidence, review
records and the tracker port are passed to nodes as runtime context, not
carried in graph state"), §6 (the Attempt graph), §7 (readiness resolves
the toolchain matrix and the declared services before any model call;
``baseline`` runs the pinned Validation Contract's commands through the
injected runner into the per-Attempt evidence directory before any model
call) and §11 (the step subgraph's context stays bounded: oversized tool
results are capped with a slice pointer, activation results exempt; the
assistant turn is carried back verbatim; cache breakpoints are applied;
a Pinned Prefix that alone crosses the threshold refuses to open).

MVP row ids: C-GRF-1, L2-12, L2-13, L2-14, P-RDY-4, C-RDY-3, C-RDY-4, L2-1,
L3-IMP-5, L3-IMP-10, L3-IMP-13, L3-IMP-20.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.ledger.store import RunLedger
from coding_agent.profile.services import ServiceProber, SocketServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.completion import CriterionEvidence
from coding_agent.runtime.context_handoff import ContextWindowConfig
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
)
from coding_agent.tracker.port import TrackerPort
from coding_agent.validate.runner import CommandRunner


@dataclass(frozen=True)
class AttemptContext:
    """Everything a node needs that must not live in graph state.

    Meters, Criterion Evidence, review records (M3) and the tracker
    port travel here, inside ``RunnableConfig["configurable"]``, while the
    graph state carries only the Attempt's small data-plane fields (ids, the
    fetched issue, the Readiness Facts, the outcome under construction).
    ``ledger`` is the real Run Ledger file handle, not a copy: recording the
    Terminal Outcome and reading it back for recovery go through this object.
    """

    tracker: TrackerPort
    ledger: RunLedger
    model: InvokableToolModel
    workspace: Path
    effective_configuration: Mapping[str, object]
    skills_dir: Path
    criterion_evidence: CriterionEvidence = field(default_factory=CriterionEvidence)
    """Runtime-owned history of every test the targeted-test tool executed.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (the runtime
    owns the evidence; Criterion Evidence records the identifier and result
    of every test the targeted-test tool executed, as a history across the
    Attempt). Only that tool writes here; tests the model runs through any
    other channel count for nothing.

    MVP row ids: L2-2, L3-IMP-10, L3-IMP-20.
    """

    toolchain_matrix: SupportedToolchainMatrix | None = None
    service_env: Mapping[str, str] | None = None
    service_prober: ServiceProber = field(default_factory=SocketServiceProber)
    command_runner: CommandRunner | None = None
    evidence_dir: Path | None = None
    result_cap_limit: int = DEFAULT_RESULT_CAP_LIMIT
    """One result over this many characters is capped (L3-IMP-5, §11)."""
    artifact_store: ArtifactStore | None = None
    """Where capped full results live; the slice tool reads the same store.

    ``None`` means the step node mints a private in-memory store for the
    Attempt. Production passes a filesystem store under the evidence dir.
    """
    context_window: ContextWindowConfig | None = None
    """The Pinned Model's deterministic context policy (§11, L3-IMP-13).

    ``None`` means no opening refusal is checked — the pre-MVP shape the
    happy-path tests run with. A set value is enforced before any model
    call: a Pinned Prefix (with tool-schema and request overhead) over its
    usable threshold refuses the Attempt with no model call and no partial
    prefix.
    """
    cache_breakpoints: bool = True
    """Tag the outgoing request with Anthropic cache breakpoints (§11).

    Applied to a copy of the wire request only; the stored conversation
    keeps exactly what the provider or a tool produced (ADR 0010).
    """

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
