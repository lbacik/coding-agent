"""The fixed eleven-node Attempt graph, from ``claim`` to ``terminal_writes``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (one fixed graph
whatever the Workflow says; no merge node; no framework checkpointer — the
Run Ledger is the only durable state), §6 (the nodes in order), §14
("Recording before publishing": ``terminal_writes`` records the Terminal
Outcome before it performs anything) and §17 (the ledger tables).

MVP row ids: C-GRF-1, L3-SEL-11, L3-CLR-19, L3-CLR-20, L3-IMP-9,
L3-IMP-11, L3-ERR-2, P-RDY-1, P-RDY-2, P-RDY-3, C-RDY-1, C-RDY-2, C-WSP-1,
L2-12, L2-13, L2-14.

Every node whose behaviour belongs to a later ticket or slice
(``baseline``, ``gate``, ``delivery_snapshot``, ``final_checks``,
``publication_gate``, ``delivery_intent``, ``publish``) is a pass-through
until that slice fills it; the graph's shape never changes. ``readiness``
is filled by this slice: it rebuilds the workspace, pins the profile at the
Base Revision as the Validation Contract, and resolves every Readiness Fact
from the profile and the Target Issue alone. A refusal sets the Terminal
Outcome here and every later node passes through, so the Attempt reaches
``terminal_writes`` with no model call.
Runtime context (meters, the tracker port, Criterion Evidence) reaches nodes
through ``RunnableConfig``, never through graph state. An unclassified
exception anywhere in the graph ends the Attempt ``failed`` — permanent, not
transient — and the outcome is still recorded.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from coding_agent.ledger.store import RunLedger
from coding_agent.profile.services import ServiceProber, SocketServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix


def _default_service_prober() -> ServiceProber:
    return SocketServiceProber()
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.graph.context import AttemptContext
from coding_agent.runtime.graph.step import (
    has_report_completion,
    run_implement_step,
)
from coding_agent.runtime.graph.tools import build_implement_step_tools
from coding_agent.runtime.readiness import (
    ReadinessRefusal,
    default_toolchain_matrix,
    resolve_readiness,
)
from coding_agent.tracker.port import TrackerPort

ATTEMPT_NODE_ORDER = (
    "claim",
    "readiness",
    "baseline",
    "run_step",
    "gate",
    "delivery_snapshot",
    "final_checks",
    "publication_gate",
    "delivery_intent",
    "publish",
    "terminal_writes",
)
"""The fixed node order of contract §6. Asserted structurally, not by reading."""

DELIVERED = "delivered"
FAILED = "failed"


class AttemptState(TypedDict, total=False):
    """The Attempt graph's data plane only.

    Ids, the fetched issue and the outcome under construction travel here.
    Meters, Criterion Evidence, review records and the tracker port must never
    appear in this state — they reach nodes through ``AttemptContext``. The
    structural test asserts the annotation names carry none of them.
    """

    attempt_id: str
    issue_number: int
    attempt_number: int
    issue_title: str
    issue_body: str
    base_revision: str
    base_branch: str
    seam_set: list[str]
    acceptance_criteria: list[dict[str, str]]
    outcome: str
    reason: str


@dataclass(frozen=True)
class AttemptResult:
    """The externally observable end of an Attempt: outcome and reason."""

    attempt_id: str
    outcome: str
    reason: str


def claim(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§5: open the Attempt in the ledger, then read the Target Issue back."""
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    context.ledger.open_attempt(
        attempt_id,
        effective_configuration=context.effective_configuration,
    )
    issue = context.tracker.fetch_issue(state["issue_number"])
    context.ledger.set_attempt_state(attempt_id, "claim-confirmed")
    return {"issue_title": issue.title, "issue_body": issue.body}


def readiness(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§7: rebuild the workspace, pin the profile, resolve every Readiness
    Fact — or refuse terminally with no model call.

    A refusal sets the Terminal Outcome here; every later node passes it
    through to ``terminal_writes``. The recorded reason names exactly which
    fact or key is missing or wrong.
    """
    if state.get("outcome"):
        return {}
    context = AttemptContext.from_config(config)
    matrix = (
        context.toolchain_matrix
        if context.toolchain_matrix is not None
        else default_toolchain_matrix()
    )
    service_env: Mapping[str, str] = (
        dict(context.service_env) if context.service_env is not None else dict(os.environ)
    )
    decision = resolve_readiness(
        context.workspace,
        state.get("issue_title") or "",
        state.get("issue_body") or "",
        matrix,
        service_env,
        context.service_prober,
    )
    if isinstance(decision, ReadinessRefusal):
        return {"outcome": FAILED, "reason": decision.reason}
    return {
        "base_revision": decision.base_revision,
        "base_branch": decision.base_branch,
        "seam_set": list(decision.seam_set),
        "acceptance_criteria": [
            {"id": criterion.id, "text": criterion.text}
            for criterion in decision.acceptance_criteria
        ],
    }


def baseline(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§7: pass-through until the baseline slice fills it.

    A Readiness refusal recorded above passes straight through: no command
    runs and no model is called.
    """
    _ = config
    if state.get("outcome"):
        return {}
    return {}


def run_step(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§8: run the single ``implement`` step through its step subgraph.

    A Readiness refusal recorded above passes straight through with no
    model call. Entering with no Seam Set in state is refused as an
    internal invariant violation (L3-IMP-11): ``failed``, non-transient,
    with no model call.
    """
    if state.get("outcome"):
        return {}
    if not state.get("seam_set"):
        return {
            "outcome": FAILED,
            "reason": (
                "missing-readiness-fact: seam-set "
                "(internal invariant violation: implement entered with no Seam Set in state)"
            ),
        }
    context = AttemptContext.from_config(config)
    tools = build_implement_step_tools(context.workspace)
    opening = HumanMessage(
        content=(
            f"Attempt {state['attempt_id']}: implement the Target Issue "
            f"#{state['issue_number']}.\n"
            f"Title: {state.get('issue_title', '')}\n"
            f"Body:\n{state.get('issue_body', '')}\n"
            "Call report_completion when the implementation stage is finished."
        )
    )
    messages = run_implement_step(context.model, tools, opening)
    if has_report_completion(messages):
        return {"outcome": DELIVERED, "reason": "report-completion"}
    return {"outcome": FAILED, "reason": "no-report-completion"}


def gate(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§11: pass-through until the Gates slice fills it (single step: proceed)."""
    _ = (state, config)
    return {}


def delivery_snapshot(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def final_checks(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def publication_gate(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def delivery_intent(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def publish(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§13: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def terminal_writes(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§14: record the Terminal Outcome first, then perform the writes.

    "Recording before publishing" is an ordering inside this node: the
    ``outcomes`` row exists before the first tracker call. Over the fake
    tracker (and the M1 local adapter) the writes perform nothing remotely;
    the fake records them so tests can assert the order.
    """
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    outcome = state.get("outcome") or FAILED
    reason = state.get("reason") or "unknown"
    context.ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason=reason)
    context.tracker.post_terminal_comment(
        state["issue_number"], f"Attempt {attempt_id}: {outcome} ({reason})"
    )
    context.tracker.remove_selection_label(state["issue_number"])
    if outcome != DELIVERED:
        context.tracker.add_ready_for_human(state["issue_number"])
    context.tracker.release_assignment(state["issue_number"])
    return {"outcome": outcome, "reason": reason}


_NODE_IMPLS = {
    "claim": claim,
    "readiness": readiness,
    "baseline": baseline,
    "run_step": run_step,
    "gate": gate,
    "delivery_snapshot": delivery_snapshot,
    "final_checks": final_checks,
    "publication_gate": publication_gate,
    "delivery_intent": delivery_intent,
    "publish": publish,
    "terminal_writes": terminal_writes,
}


def build_attempt_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the fixed Attempt graph: eleven nodes, no merge, no checkpointer."""
    builder = StateGraph(AttemptState)
    for name in ATTEMPT_NODE_ORDER:
        builder.add_node(name, _NODE_IMPLS[name])
    builder.add_edge(START, ATTEMPT_NODE_ORDER[0])
    for previous, following in zip(ATTEMPT_NODE_ORDER, ATTEMPT_NODE_ORDER[1:]):
        builder.add_edge(previous, following)
    builder.add_edge(ATTEMPT_NODE_ORDER[-1], END)
    return builder.compile(checkpointer=None)


def run_attempt(
    *,
    issue_number: int,
    attempt_number: int,
    tracker: TrackerPort,
    ledger: RunLedger,
    model: InvokableToolModel,
    workspace: Path,
    effective_configuration: Mapping[str, object],
    toolchain_matrix: SupportedToolchainMatrix | None = None,
    service_env: Mapping[str, str] | None = None,
    service_prober: ServiceProber | None = None,
) -> AttemptResult:
    """Invoke the Attempt graph in-process and return its Terminal Outcome.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (readiness
    resolves every Readiness Fact before any model call; a refusal ends
    ``failed`` with no model call) and §14 (the Terminal Outcome is recorded
    before the terminal writes).

    MVP row ids: P-RDY-1, P-RDY-2, P-RDY-3, C-RDY-1, C-RDY-2, C-WSP-1,
    L2-12, L2-13, L2-14, L3-CLR-20, L3-IMP-11.

    An unclassified exception anywhere in the graph (L3-ERR-2) ends the
    Attempt ``failed`` — permanent, not transient — and the outcome is still
    recorded in the ledger before this returns.
    """
    attempt_id = f"#{issue_number}/{attempt_number}"
    context = AttemptContext(
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=effective_configuration,
        toolchain_matrix=toolchain_matrix,
        service_env=service_env,
        service_prober=service_prober
        if service_prober is not None
        else _default_service_prober(),
    )
    graph = build_attempt_graph()
    try:
        final = graph.invoke(
            {
                "attempt_id": attempt_id,
                "issue_number": issue_number,
                "attempt_number": attempt_number,
            },
            config={"configurable": context.to_configurable()},
        )
    except Exception:
        try:
            ledger.open_attempt(
                attempt_id, effective_configuration=effective_configuration
            )
        except sqlite3.IntegrityError:
            pass
        recorded = ledger.record_terminal_outcome(
            attempt_id, outcome=FAILED, reason="unclassified-error"
        )
        return AttemptResult(
            attempt_id=attempt_id, outcome=recorded.outcome, reason=recorded.reason
        )
    assert isinstance(final, dict)
    outcome = str(final.get("outcome") or FAILED)
    reason = str(final.get("reason") or "unknown")
    return AttemptResult(attempt_id=attempt_id, outcome=outcome, reason=reason)
