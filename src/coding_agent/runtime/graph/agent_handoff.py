"""One Context-Window Handoff in an `agent` Step Execution.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (one
conversation per Step Execution, the Context-Window Handoff) and §11
(meters and the ``limit-exceeded`` fact).

Part of #487 (slice 4 of 4). The agent loop performs every
Context-Window Handoff through :func:`perform_handoff`, which owns steps
1–6 in the order the loop ran them — the code-owned handoff-count check,
the producer turn with its snapshot validation and persistence, the handoff
count and digest update, the ``NATIVE_HANDOFF`` attempt event, and the
fresh Step opening with its forked continuation conversation. The swallowed
``except Exception: pass`` wrappers move as they are; #526 tracks fixing
them.

The threshold check (``estimate > threshold``) stays in the loop. The loop
passes a callable composing the fresh Step opening, so this module knows
nothing about step inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.observed_identity import PinViolation
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.context_handoff import (
    MAX_HANDOFFS_PER_STEP,
    SNAPSHOT_SIZE_LIMIT_BYTES,
    ContinuationSnapshot,
    ConversationContextState,
    HandoffFailure,
    handoff_event_payload,
    measure_conversation_context,
    parse_snapshot_response,
)
from coding_agent.runtime.graph.agent_model_request import (
    ModelRequestEnding,
    _flush_attempt_usage,
    run_model_request,
)
from coding_agent.runtime.transcript import (
    fork_conversation,
    handoff_turn_conversation,
    work_conversation,
)
from coding_agent.workflow.schema import WorkflowStep

if TYPE_CHECKING:
    from coding_agent.provider.pin_witness import PinWitness
    from coding_agent.runtime.graph.agent_step import (
        AgentStepDeps,
        AgentStepOutcome,
        ExecutionTally,
    )


def build_handoff_instruction(
    *,
    attempt_id: str,
    base_revision: str,
    branch: str,
    commit: str,
) -> str:
    """The handoff turn's instruction, appended to the current conversation.

    The handoff turn sends the current main-conversation messages (the Pinned
    Prefix and the whole Exchange Units so far) followed by this instruction,
    with no tool schemas and no tools bound (§9.9, ADR 0013). The instruction
    names the snapshot contract and the Attempt facts the conversation alone
    may not pin down, so the model can compose a valid Continuation Snapshot
    from the conversation above it.
    """
    return (
        "The conversation above is the whole current work node. Produce exactly "
        "one Continuation Snapshot as a single JSON object with these required "
        "fields: version (1), attempt_id, base_revision, branch, commit, "
        "changed_files (list of strings), commands (list of {command, result} "
        "objects), validation_evidence, unresolved_questions (list of strings), "
        "next_intended_action (non-empty string). Return only the JSON object, "
        "optionally fenced as ```json. Carry work facts only, never runtime "
        "policy state. "
        f"Attempt: {attempt_id}; base revision: {base_revision}; "
        f"branch: {branch}; commit: {commit}."
    )


def produce_handoff_snapshot(
    deps: AgentStepDeps,
    tally: ExecutionTally,
    step: WorkflowStep,
    seq: int,
    messages: list[BaseMessage],
    witness: PinWitness,
) -> ContinuationSnapshot | AgentStepOutcome:
    """Run one bounded handoff turn and return its Continuation Snapshot (§9.9).

    The Worker's real wiring (``worker/attempt_run``) binds this as every
    agent Step Execution's :data:`HandoffProducer`. The turn sends the current
    main-conversation messages plus the handoff instruction on the Attempt's
    one Pinned Model with no tools bound and the pin's handoff output cap sent
    as the request's max output tokens. Each try, provider retries included,
    passes Admission with its own worst-case bound (the handoff request's
    Conversation Context plus the handoff cap), holds it, and settles the
    hold with the response's provider-reported usage — a response without
    complete usage settles at the hold's worst case — exactly like any other
    model request (§11.4). The turn reads the main conversation's anchoring
    state but never moves it, and it never records into the main-conversation
    context figures or cache-hit counters. The answering-model assertion
    applies to the handoff response as to any other (§9.1).

    Returns the parsed Continuation Snapshot, or the ``AgentStepOutcome``
    ending the Step Execution where Admission refuses the turn (moment
    ``before-request``) or its settled spend crosses a limit. A handoff
    request whose own Conversation Context plus the handoff cap exceeds the
    threshold raises :class:`HandoffFailure` before anything is sent: overflow
    is a failed handoff, and the Step Execution ends ``error``.
    """
    window = deps.context_window
    if window is None:
        raise HandoffFailure(
            "the handoff turn is not configured: no context-window policy"
        )
    threshold = window.threshold_tokens
    cap = window.handoff_token_cap
    branch = (
        deps.attempt_branch
        or deps.attempt.head_branch
        or deps.attempt.base_branch
        or ""
    )
    handoff_messages = [
        *messages,
        HumanMessage(
            content=build_handoff_instruction(
                attempt_id=deps.attempt.attempt_id,
                base_revision=deps.attempt.base_revision,
                branch=branch,
                commit=deps.attempt.current_revision,
            )
        ),
    ]
    turn_context = measure_conversation_context(
        handoff_messages, [], tally.context_state
    )
    if turn_context.value + cap > threshold:
        raise HandoffFailure(
            "the handoff request does not fit the context threshold "
            f"(estimate {turn_context.value} + handoff cap {cap} exceeds "
            f"threshold {threshold}); refusing to continue"
        )
    handoff_model = fork_conversation(
        deps.model, handoff_turn_conversation(step.id, seq, tally.handoffs + 1)
    ).bind_tools([])

    def _count_model_call() -> None:
        tally.model_calls += 1

    result = run_model_request(
        deps,
        witness,
        step.id,
        seq,
        handoff_model,
        handoff_messages,
        [],
        record_main_context=False,
        on_model_call=_count_model_call,
        handoffs=tally.handoffs,
        output_cap=cap,
        context_state=tally.context_state,
    )
    if isinstance(result, ModelRequestEnding):
        if result.kind == "deadline":
            return tally.deadline(result.detail)
        assert result.fact is not None
        return tally.limit(result.fact, result.detail)
    return parse_snapshot_response(result.content, redactions=deps.redactions)


def perform_handoff(
    deps: AgentStepDeps,
    tally: ExecutionTally,
    step: WorkflowStep,
    seq: int,
    messages: list[BaseMessage],
    tools: Sequence[BaseTool],
    fresh_opening: Callable[[], list[BaseMessage]],
    witness: PinWitness,
) -> tuple[list[BaseMessage], InvokableToolModel] | AgentStepOutcome:
    """Perform one Context-Window Handoff or end the Step Execution.

    Owns the whole handoff in the loop's order: the code-owned handoff-count
    check, the producer turn (a missing producer, an over-limit snapshot or
    a persistence failure ends the step through ``tally.handoff_failed``),
    the handoff count and snapshot-digest update, the ``handoff`` Run Ledger
    event carrying the crossing Conversation Context (§13.2), the
    ``NATIVE_HANDOFF`` attempt event (swallowed as the loop swallowed it),
    the anchoring-state reset for the new conversation, and the fresh Step
    opening carrying the Continuation Snapshot, forked under its own
    continuation conversation and bound to the step's tools.

    The producer settles the handoff turn's spend itself through the step's
    budgets as part of the admitted request: this function charges no spend
    of its own. A ``PinViolation`` from the handoff turn propagates exactly
    as a main-conversation violation does — it never becomes a failed
    handoff.

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
        produced = producer(deps, tally, step, seq, list(messages), witness)
        if not isinstance(produced, ContinuationSnapshot):
            return produced
        snapshot = produced
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
    except PinViolation:
        raise
    except Exception as exc:
        return tally.handoff_failed(exc)
    tally.handoffs += 1
    # The crossing Conversation Context belongs to the old conversation:
    # measure it before the reset and record the `handoff` event with the
    # value (and basis) that crossed the threshold (§13.2).
    crossing = measure_conversation_context(
        list(messages), list(tools), tally.context_state
    )
    totals = deps.budgets.attempt_totals()
    window = deps.context_window
    tally.record_event(
        "handoff",
        f"handoff {tally.handoffs} in {step.id}#{seq}",
        handoff_event_payload(
            snapshot=snapshot,
            node_cap_tokens=window.threshold_tokens if window is not None else 0,
            provider_input_tokens=totals.input_tokens,
            provider_output_tokens=totals.output_tokens,
            cumulative_tokens=totals.tokens,
            cumulative_effective_tokens=totals.effective_tokens,
            cumulative_cost_usd=totals.cost_usd,
            cumulative_tool_calls=totals.tool_calls,
            conversation_context=crossing,
            usable_threshold=(
                window.usable_threshold if window is not None else None
            ),
        ),
    )
    # A handoff starts a new conversation: the fresh continuation
    # node has settled nothing yet, so the used figure drops while
    # the Attempt-wide peak survives. The flush below carries the
    # reset; the next loop top records the fresh estimate. The anchoring
    # state resets too, so the first request after the handoff measures
    # `estimated`.
    tally.context_state = ConversationContextState()
    deps.budgets.reset_main_context(
        work_conversation(step.id, seq, tally.handoffs)
    )
    _flush_attempt_usage(deps)
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
