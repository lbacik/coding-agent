"""One Context-Window Handoff in an `agent` Step Execution.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (one
conversation per Step Execution, the Context-Window Handoff) and §11
(meters and the ``limit-exceeded`` fact).

Part of #487 (slice 4 of 4). A pure refactor: the agent loop performs every
Context-Window Handoff through :func:`perform_handoff`, which owns steps
1–6 in the order the loop ran them — the code-owned handoff-count check,
the producer turn with its snapshot validation and persistence, the handoff
count and digest update, the ``NATIVE_HANDOFF`` attempt event, the snapshot
spend with its after-spend limit check, and the fresh Step opening with its
forked continuation conversation. The swallowed ``except Exception: pass``
wrappers move as they are; #526 tracks fixing them.

The threshold check (``estimate > threshold``) stays in the loop. The loop
passes a callable composing the fresh Step opening, so this module knows
nothing about step inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.context_handoff import (
    MAX_HANDOFFS_PER_STEP,
    SNAPSHOT_SIZE_LIMIT_BYTES,
    ContinuationSnapshot,
)
from coding_agent.runtime.graph.agent_model_request import _flush_attempt_usage
from coding_agent.runtime.transcript import fork_conversation, work_conversation
from coding_agent.workflow.schema import WorkflowStep

if TYPE_CHECKING:
    from coding_agent.runtime.graph.agent_step import (
        AgentStepDeps,
        AgentStepOutcome,
        ExecutionTally,
    )


def perform_handoff(
    deps: AgentStepDeps,
    tally: ExecutionTally,
    step: WorkflowStep,
    seq: int,
    messages: list[BaseMessage],
    tools: Sequence[BaseTool],
    fresh_opening: Callable[[], list[BaseMessage]],
) -> tuple[list[BaseMessage], InvokableToolModel] | AgentStepOutcome:
    """Perform one Context-Window Handoff or end the Step Execution.

    Owns the whole handoff in the loop's order: the code-owned handoff-count
    check, the producer turn (a missing producer, an over-limit snapshot or
    a persistence failure ends the step through ``tally.handoff_failed``),
    the handoff count and snapshot-digest update, the ``NATIVE_HANDOFF``
    attempt event (swallowed as the loop swallowed it), the snapshot spend
    with its after-spend limit check (a crossed limit ends the step through
    ``tally.limit``), and the fresh Step opening carrying the Continuation
    Snapshot note, forked under its own continuation conversation and bound
    to the step's tools.

    Returns the new messages with the newly bound model, or the
    :class:`AgentStepOutcome` ending the Step Execution. The caller does
    ``if isinstance(result, AgentStepOutcome): return result``.
    """
    if tally.handoffs >= MAX_HANDOFFS_PER_STEP:
        return tally.handoff_count()
    producer = deps.handoff_producer
    try:
        if producer is None:
            raise ValueError("no handoff producer: cannot continue over threshold")
        snapshot = producer(list(messages))
        assert isinstance(snapshot, ContinuationSnapshot)
        snapshot_bytes = len(snapshot.to_json().encode("utf-8"))
        if snapshot_bytes > SNAPSHOT_SIZE_LIMIT_BYTES:
            raise ValueError(
                "continuation snapshot exceeds "
                f"{SNAPSHOT_SIZE_LIMIT_BYTES} bytes "
                f"({snapshot_bytes}); refusing to continue"
            )
        try:
            deps.artifact_store.store(
                f"continuation-snapshots/{snapshot.digest}.json",
                snapshot.to_json(),
            )
        except Exception as exc:
            raise ValueError(f"could not persist snapshot: {exc}") from exc
    except Exception as exc:
        return tally.handoff_failed(exc)
    tally.handoffs += 1
    # A handoff starts a new conversation: the fresh continuation
    # node has settled nothing yet, so the used figure drops while
    # the Attempt-wide peak survives. The flush below carries the
    # reset; the next loop top records the fresh estimate.
    deps.budgets.reset_main_context(
        work_conversation(step.id, seq, tally.handoffs)
    )
    tally.snapshot_digest = snapshot.digest
    try:
        from coding_agent.operator.archive import NATIVE_HANDOFF

        deps.ledger.emit_attempt_event(
            deps.attempt.attempt_id,
            NATIVE_HANDOFF,
            f"handoff {tally.handoffs} in {step.id}#{seq}",
        )
    except Exception:
        pass
    try:
        snap_tokens = max(1, len(snapshot.to_json().encode("utf-8")) // 4)
        snap_price = deps.budgets.prices
        snap_cost = snap_tokens / 1_000_000 * snap_price.input if snap_price is not None else 0.0
        deps.budgets.add_spend(
            step.id,
            UsageTotals(
                tokens=snap_tokens,
                effective_tokens=snap_tokens,
                cost_usd=snap_cost,
                input_tokens=snap_tokens,
            ),
        )
    except Exception as exc:
        return tally.handoff_failed(exc)
    _flush_attempt_usage(deps)
    try:
        after_handoff = deps.budgets.check_after_spend(step.id)
    except Exception as exc:
        return tally.handoff_failed(exc)
    if after_handoff is not None:
        try:
            return tally.limit(
                after_handoff,
                f"the handoff spend crossed the {after_handoff.scope} limit",
            )
        except Exception as exc:
            return tally.handoff_failed(exc)
    fresh = fresh_opening()
    snapshot_note = HumanMessage(
        content=f"# Continuation Snapshot\n\n{snapshot.to_json()}"
    )
    fresh.append(snapshot_note)
    new_messages = fresh
    # Each fresh node after a Context-Window Handoff records under
    # its own ``step/<step-id>/<n>/continuation/<k>`` conversation
    # (§13.3) while sharing the one file sequence.
    caller_model = fork_conversation(
        deps.model, work_conversation(step.id, seq, tally.handoffs)
    )
    bound_model = caller_model.bind_tools(list(tools))
    return new_messages, bound_model
