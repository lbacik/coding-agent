"""One Sub-agent group member: admission, run, stop/ceiling observation, ending.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8
(Sub-agents: one per ``spawn_subagent`` call), §9.8 (queued/started/ended
ledger events) and §11 (meters, admission with holds), ADR 0020 (consecutive
calls form one concurrent group) and ADR 0024 (a stopped Sub-agent discards
its in-flight request at the next stop point).

One member's lifecycle used to spread across four modules — the spawn group
runner admitting every member up front, a default-subagent helper returning a
wide positional tuple, the conversation runner taking a long keyword surface,
and the shared model-request lifecycle. This module owns the whole lifecycle
of one member through :class:`SubagentMember`:

* admission — validation, the single toolset build, the estimate, the first
  hold (admitted in call order by the group, so a later member sees earlier
  members' holds, LIM-7);
* run — the bounded read-only tool loop on the Pinned Model through the
  shared model-request lifecycle, charging billable tool-call batches before
  they run, polling a single stop predicate at every stop point;
* ending — a single rich outcome plus the ``subagent_ended`` ledger payload
  derived from the result object by the pure
  :func:`subagent_ended_payload`, never re-threaded tuples.

The member holds no shared mutable state across members: tools, record and
anchoring state are per member, and ordering is preserved by slot index, not
execution order. Members must tolerate concurrent execution (ADR 0020); the
spawn group admits in call order and may run admitted members in any order.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.observed_identity import (
    PinViolation,
    gateway_identity_of,
    last_reported_model,
    observed_response_model,
)
from coding_agent.provider.pin_witness import PinWitness
from coding_agent.runtime.ceilings import OriginCell, response_usage
from coding_agent.runtime.conversation_context import ConversationContextState
from coding_agent.runtime.graph.agent_model_request import (
    ModelRequestEnding,
    _flush_attempt_usage,
    _is_gateway_for,
    _prices_for,
    run_model_request,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_ENDED_KIND,
    SUBAGENT_QUEUED_KIND,
    SUBAGENT_STARTED_KIND,
    SubagentRecord,
    SubagentResult,
    build_subagent_tools,
    estimate_spawn_admission,
    run_subagent,
    subagent_tool_result_text,
    validate_spawn_args,
)
from coding_agent.runtime.transcript import fork_subagent_conversation

if TYPE_CHECKING:
    from coding_agent.runtime.graph.agent_step import AgentStepDeps
    from coding_agent.runtime.step_limits import AdmissionHold, LimitFact
    from coding_agent.workflow.schema import WorkflowStep


@dataclass
class SubagentMemberOutcome:
    """One admitted member's end: caller-facing text plus meter facts."""

    text: str
    """The caller-facing tool result joining the Exchange Unit in call order."""
    report: str | None
    """The accepted report, or ``None`` where the member made none."""
    ok: bool
    """Whether the member reported (the step tally's ``subagent_reports``)."""
    billable_tool_calls: int
    """The member's billable tool-call total for the step tally."""
    limit_fact: LimitFact | None
    """The tool-call crossing fact, or ``None`` (ends the caller on settle)."""


@dataclass
class SubagentMember:
    """One spawn-group member from admission to its ending.

    The group constructs members in call order before any member runs
    (:meth:`admit` owns validation, the single toolset build and the first
    hold), then runs each admitted member through :meth:`run`. Invalid and
    refused spawns never run: :meth:`admit` records their outcome on the
    returned instance as :attr:`early_outcome`, and :meth:`run` returns it
    without further events. Members that never start (the stop observed
    before their run) settle through :meth:`discard_before_start` with no
    queued/started/ended events — a group-only concern, no member instance
    runs.
    """

    call_id: str
    description: str
    instructions: str
    step_id: str
    seq: int
    early_outcome: SubagentMemberOutcome | None = None
    """The outcome where admission settled the spawn without running it."""
    tools: tuple[BaseTool, ...] | None = None
    """The member's toolset, built once at admission and reused for the run."""
    record: SubagentRecord | None = None
    """The ``report_subagent`` exit record shared with the built toolset."""
    hold: AdmissionHold | None = None
    """The spawn-time first hold, open until the first request or discard."""

    @property
    def hold_id(self) -> str | None:
        """This member's spawn-time hold id, if one is held."""
        return self.hold.hold_id if self.hold is not None else None
    member_state: ConversationContextState = field(
        default_factory=ConversationContextState
    )
    """This member's anchoring state: peaks survive a pin violation."""

    @classmethod
    def admit(
        cls,
        deps: AgentStepDeps,
        step: WorkflowStep,
        seq: int,
        call_id: str,
        description: object,
        instructions: object,
        record_event: Callable[[str, str, dict[str, object]], None],
    ) -> SubagentMember:
        """Admit one spawn call in group call order, owning its first hold.

        Validation failures start nothing and write no ledger event. A
        refused admission emits the ``queued`` (refused) and ``ended``
        (``refused``) events and settles nothing — the member never runs.
        An admitted member holds its first hold open so later admissions in
        the same pass see it in force (LIM-7); its toolset is built once
        here and reused by :meth:`run`, never rebuilt.
        """
        error = validate_spawn_args(description, instructions)
        if error is not None:
            return cls(
                call_id=call_id,
                description=str(description),
                instructions=str(instructions),
                step_id=step.id,
                seq=seq,
                early_outcome=SubagentMemberOutcome(
                    text=error,
                    report=None,
                    ok=False,
                    billable_tool_calls=0,
                    limit_fact=None,
                ),
            )
        assert isinstance(description, str) and isinstance(instructions, str)
        record = SubagentRecord(description=description)
        tools = build_subagent_tools(
            deps.workspace,
            record,
            artifact_store=deps.artifact_store,
            base_revision=deps.attempt.base_revision,
            change_artifact_id=f"read-change-{call_id}",
            base_tip=deps.attempt.base_tip,
        )
        estimate = estimate_spawn_admission(instructions, tools)
        fact, hold = deps.budgets.admit_hold(
            step.id,
            estimate,
            seq=seq,
            output_cap=deps.output_cap,
            origin="subagent",
        )
        if fact is not None:
            record_event(
                SUBAGENT_QUEUED_KIND,
                f"sub-agent refused {call_id!r}",
                {
                    "attempt_id": deps.attempt.attempt_id,
                    "step_id": step.id,
                    "seq": seq,
                    "call_id": call_id,
                    "description": description,
                },
            )
            record_event(
                SUBAGENT_ENDED_KIND,
                f"sub-agent ended {call_id!r}",
                refused_subagent_ended_payload(
                    attempt_id=deps.attempt.attempt_id,
                    step_id=step.id,
                    seq=seq,
                    call_id=call_id,
                ),
            )
            return cls(
                call_id=call_id,
                description=description,
                instructions=instructions,
                step_id=step.id,
                seq=seq,
                early_outcome=SubagentMemberOutcome(
                    text=(
                        f"Sub-agent {description!r}: limit-exceeded "
                        f"({fact.scope}). The request was not sent."
                    ),
                    report=None,
                    ok=False,
                    billable_tool_calls=0,
                    limit_fact=None,
                ),
            )
        assert hold is not None
        return cls(
            call_id=call_id,
            description=description,
            instructions=instructions,
            step_id=step.id,
            seq=seq,
            tools=tools,
            record=record,
            hold=hold,
        )

    def discard_before_start(self, deps: AgentStepDeps) -> None:
        """Settle an admitted member that never starts (stop observed).

        The spawn-time hold settles empty — the member spent nothing — and
        no queued/started/ended event is recorded. No-op where there is no
        open hold (invalid, refused, or already run members).
        """
        if self.hold is not None:
            self.hold.close()
        self.hold = None

    def run(
        self,
        deps: AgentStepDeps,
        step: WorkflowStep,
        record_event: Callable[[str, str, dict[str, object]], None],
        *,
        request_model: Callable[
            [object, list[BaseMessage], list[BaseTool]],
            BaseMessage | ModelRequestEnding,
        ]
        | None = None,
    ) -> SubagentMemberOutcome:
        """Run this admitted member to its ending and settle it.

        Emits the member's own ``queued``/``started``/``ended`` events,
        settles its holds, and returns the single rich outcome — no
        caller-side tuple unpacking. ``request_model`` injects the
        model-request lifecycle (the scripted seam tests drive the member
        through); production leaves it ``None`` so every round and every
        provider retry runs through the shared lifecycle under its own
        hold. The only stop input threaded to the conversation runner is
        the single stop predicate: stop points are wired by construction.
        """
        if self.early_outcome is not None:
            return self.early_outcome
        assert self.tools is not None and self.record is not None
        assert self.hold is not None
        record_event(
            SUBAGENT_QUEUED_KIND,
            f"sub-agent queued {self.call_id!r}",
            {
                "attempt_id": deps.attempt.attempt_id,
                "step_id": self.step_id,
                "seq": self.seq,
                "call_id": self.call_id,
                "description": self.description,
            },
        )
        record_event(
            SUBAGENT_STARTED_KIND,
            f"sub-agent started {self.call_id!r}",
            {
                "attempt_id": deps.attempt.attempt_id,
                "step_id": self.step_id,
                "seq": self.seq,
                "call_id": self.call_id,
                "description": self.description,
            },
        )
        before_cell = deps.budgets.step_by_origin(self.step_id)["subagent"]
        member_started = time.monotonic()
        first_hold: AdmissionHold | None = self.hold
        self.hold = None
        witness = PinWitness(
            record_event=record_event,
            attempt_id=deps.attempt.attempt_id,
            pin=deps.pin,
            step_id=self.step_id,
            seq=self.seq,
            call_id=self.call_id,
        )
        response_models: list[str | None] = []

        def _note_response(response: BaseMessage) -> None:
            # The shared lifecycle already asserted the response against the
            # pin and recorded its identity: only the observed model is
            # collected here, for the ``subagent_ended`` answering model.
            if deps.pin.is_gateway:
                response_models.append(gateway_identity_of(response).model_group)
            else:
                response_models.append(observed_response_model(response))

        def _default_request_model(
            bound_model: object,
            request_messages: list[BaseMessage],
            request_tools: list[BaseTool],
        ) -> BaseMessage | ModelRequestEnding:
            nonlocal first_hold
            this_hold = first_hold
            first_hold = None
            return run_model_request(
                deps,
                witness,
                self.step_id,
                self.seq,
                bound_model,
                request_messages,
                request_tools,
                first_hold=this_hold,
                record_main_context=False,
                origin="subagent",
                context_state=self.member_state,
            )

        def _charge_tool_calls(count: int) -> LimitFact | None:
            fact = deps.budgets.note_tool_calls(
                self.step_id, count, origin="subagent"
            )
            _flush_attempt_usage(deps)
            return fact

        try:
            result = run_subagent(
                fork_subagent_conversation(deps.model, self.call_id, self.description),
                description=self.description,
                instructions=self.instructions,
                call_id=self.call_id,
                workspace=deps.workspace,
                artifact_store=deps.artifact_store,
                redactions=deps.redactions,
                base_revision=deps.attempt.base_revision,
                base_tip=deps.attempt.base_tip,
                price=_prices_for(deps),
                is_gateway=_is_gateway_for(deps),
                on_response=_note_response,
                is_stopped=deps.is_stopped,
                request_model=request_model
                if request_model is not None
                else _default_request_model,
                context_window=deps.context_window,
                charge_tool_calls=_charge_tool_calls,
                context_state=self.member_state,
                tool_set=self.tools,
                shared_record=self.record,
            )
        except PinViolation as exc:
            # A pin-violating member ends with no report: its own spend
            # already settled from the carried response — or, where the
            # violation was observed on a failed request, charged at its
            # hold's worst case — by the shared lifecycle before raising.
            # The leftover hold settles here only where it somehow stayed
            # open; the violation propagates, halting the Worker.
            if first_hold is not None:
                if exc.response is None:
                    first_hold.charge(origin="subagent")
                else:
                    first_hold.settle(
                        response_usage(
                            exc.response,
                            _prices_for(deps),
                            is_gateway=_is_gateway_for(deps),
                        ),
                        self.step_id,
                        origin="subagent",
                        responses=1,
                    )
            first_hold = None
            member_cell = (
                deps.budgets.step_by_origin(self.step_id)["subagent"] - before_cell
            )
            record_event(
                SUBAGENT_ENDED_KIND,
                f"sub-agent ended {self.call_id!r}",
                pin_violation_subagent_ended_payload(
                    attempt_id=deps.attempt.attempt_id,
                    step_id=self.step_id,
                    seq=self.seq,
                    call_id=self.call_id,
                    observed=exc.observed,
                    member_cell=member_cell,
                    duration_seconds=time.monotonic() - member_started,
                    peak=self.member_state.peak,
                ),
            )
            raise
        # The context guard can end the member before its first request: no
        # request ran, so the spawn-time hold is still open. Settle it empty
        # — the member spent nothing — so the ended cell reads all zero. The
        # same settle covers the injected request-model seam, which never
        # touches holds.
        if first_hold is not None:
            first_hold.close()
        member_cell = deps.budgets.step_by_origin(self.step_id)["subagent"] - before_cell
        record_event(
            SUBAGENT_ENDED_KIND,
            f"sub-agent ended {self.call_id!r}",
            subagent_ended_payload(
                attempt_id=deps.attempt.attempt_id,
                step_id=self.step_id,
                seq=self.seq,
                call_id=self.call_id,
                result=result,
                response_models=response_models,
                member_cell=member_cell,
                usable_threshold=(
                    deps.context_window.usable_threshold
                    if deps.context_window is not None
                    else None
                ),
            ),
        )
        return SubagentMemberOutcome(
            text=subagent_tool_result_text(result),
            report=result.report,
            ok=result.status == "reported",
            billable_tool_calls=result.billable_tool_calls,
            limit_fact=result.limit_fact,
        )


def subagent_ended_payload(
    *,
    attempt_id: str,
    step_id: str,
    seq: int,
    call_id: str,
    result: SubagentResult,
    response_models: list[str | None],
    member_cell: OriginCell,
    usable_threshold: int | None,
) -> dict[str, object]:
    """Derive one ``subagent_ended`` payload from the member's result.

    A pure function of the rich result object — no positional tuple
    re-threading, no ledger or budget reads — unit-testable in isolation
    from execution. The answering model is the last reported observed
    model; the peak, provider error, compared context and context causes
    travel only where the ending sets them.
    """
    payload: dict[str, object] = {
        "attempt_id": attempt_id,
        "step_id": step_id,
        "seq": seq,
        "call_id": call_id,
        "reported": result.status == "reported",
        "answering_model": last_reported_model(response_models),
        "response_models": list(response_models),
        "ending": result.reason,
        "duration_seconds": result.duration_seconds,
        "usage": member_cell.as_readable(),
    }
    if result.peak_context is not None:
        payload["conversation_context_peak"] = result.peak_context.to_record()
    if result.provider_error is not None:
        payload["provider_error"] = result.provider_error
    if result.end_context is not None:
        payload["conversation_context"] = result.end_context.to_record()
        if usable_threshold is not None:
            payload["usable_threshold"] = usable_threshold
    if result.reason == "context-window":
        if result.context_cause is not None:
            payload["context_cause"] = result.context_cause
        if result.overflow_signal is not None:
            payload["overflow_signal"] = result.overflow_signal
    return payload


def refused_subagent_ended_payload(
    *,
    attempt_id: str,
    step_id: str,
    seq: int,
    call_id: str,
) -> dict[str, object]:
    """Derive the ``subagent_ended`` payload for a refused member.

    Pure: a refused member never runs — zero duration, an all-zero usage
    cell, no peak — and the spawn group records it without a member run.
    """
    return {
        "attempt_id": attempt_id,
        "step_id": step_id,
        "seq": seq,
        "call_id": call_id,
        "reported": False,
        "answering_model": None,
        "response_models": [],
        "ending": "refused",
        "duration_seconds": 0.0,
        "usage": OriginCell().as_readable(),
    }


def pin_violation_subagent_ended_payload(
    *,
    attempt_id: str,
    step_id: str,
    seq: int,
    call_id: str,
    observed: str | None,
    member_cell: OriginCell,
    duration_seconds: float,
    peak: Any,
) -> dict[str, object]:
    """Derive the ``subagent_ended`` payload for a pin-violating member.

    Pure: the violation propagates instead of returning a result, so the
    payload carries the observed identity directly, with the member's
    settled cell and peak.
    """
    payload: dict[str, object] = {
        "attempt_id": attempt_id,
        "step_id": step_id,
        "seq": seq,
        "call_id": call_id,
        "reported": False,
        "answering_model": observed,
        "response_models": [observed],
        "ending": "pin-violation",
        "duration_seconds": duration_seconds,
        "usage": member_cell.as_readable(),
    }
    if peak is not None:
        payload["conversation_context_peak"] = peak.to_record()
    return payload
