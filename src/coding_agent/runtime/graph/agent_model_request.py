"""One model request in an `agent` Step Execution, wall-clock check to response.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §9 (one
conversation per Step Execution) and §11 (meters, admission with holds and
the ``limit-exceeded`` fact).

Part of #487 (slice 3 of 4). A pure refactor: the agent loop makes each
model request through :func:`request_model_response`, which maps the shared
:func:`run_model_request` lifecycle — the wall-clock check, the admission
hold, the invoke with its retry loop, the per-request timeout, the
model-call count, the identity and effort pin checks, the hold settlement,
the recorded ``model_response`` event and the after-spend limit check — in
exactly the order the loop runs them. The Sub-agent loop runs every one of
its requests through the same lifecycle, mapping the ending to its own
outcome. In particular the after-spend limit return happens **after** the
``model_response`` event is recorded (#527).

Tool-call metering after the response (``note_tool_calls``, and the check
that bars tools when no wall-clock time remains) stays in the loop. It
belongs to the turn, not the request.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_core.messages import AIMessage, BaseMessage, UsageMetadata
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import BudgetSnapshot
from coding_agent.provider.observed_identity import (
    check_direct_pin,
    check_gateway_error_exc,
    check_reported_effort,
)
from coding_agent.provider.pin_witness import PinWitness
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import (
    UsageOrigin,
    UsageTotals,
    response_has_complete_usage,
    response_has_unreported_gateway_cache_write,
    response_usage,
    usage_breakdown,
)
from coding_agent.runtime.context_handoff import (
    ConversationContext,
    ConversationContextState,
    measure_conversation_context,
)
from coding_agent.runtime.transcript import work_conversation

if TYPE_CHECKING:
    from coding_agent.runtime.graph.agent_step import (
        AgentStepDeps,
        AgentStepOutcome,
        ExecutionTally,
    )
    from coding_agent.runtime.redaction import Redactable
    from coding_agent.runtime.step_limits import LimitFact


def _measure(
    messages: Sequence[BaseMessage],
    tools: Sequence[BaseTool],
    state: ConversationContextState | None = None,
) -> ConversationContext:
    """The Conversation Context of the next request: the one compared measure.

    Anchored on the conversation's last provider-reported ``input_tokens``
    where a measured response anchors ``state``, else today's ``bytes/4`` of
    the whole request (basis ``estimated``). No caller prices ``bytes/4`` of
    the whole request directly any more — every context decision and Admission
    hold reads this measure.
    """
    return measure_conversation_context(list(messages), list(tools), state)


def _usable_threshold_for(deps: AgentStepDeps) -> int | None:
    """The Usable Threshold in force for this Step Execution, if configured."""
    window = deps.context_window
    return window.usable_threshold if window is not None else None


def _prices_for(deps: AgentStepDeps) -> TokenPrices:
    return deps.prices


def _output_cap_for(deps: AgentStepDeps) -> int:
    return deps.output_cap


def _ledger_hold(deps: AgentStepDeps, step_id: str, seq: int, hold_id: str) -> None:
    bound = deps.budgets.hold_bound(hold_id)
    if bound is None:
        return
    bound_tokens, bound_cost = bound
    try:
        deps.ledger.record_admission_hold(
            deps.budgets.attempt_id or deps.attempt.attempt_id,
            step_id,
            seq,
            hold_id,
            bound_tokens=bound_tokens,
            bound_cost_usd=bound_cost,
        )
    except Exception:
        pass


def _ledger_settle(deps: AgentStepDeps, hold_id: str, *, charged: bool = False) -> None:
    try:
        if charged:
            deps.ledger.settle_admission_hold(hold_id, status="charged")
        else:
            deps.ledger.settle_admission_hold(hold_id)
    except Exception:
        pass


def _flush_attempt_usage(deps: AgentStepDeps) -> None:
    """Flush the Attempt meter to the Run Ledger (contract §11, C-LIM-1).

    Called after every settled model response and every counted tool call,
    so ``current_attempt`` reports spend while a step is still open. The
    flush carries the Attempt totals (the restore source, not per-step
    totals) plus the Attempt wall clock, the main conversation's context
    (``used`` / ``peak`` / ``estimate`` / ``conversation``) with its cache
    counters (``cache_read`` / ``cache_input`` / ``responses_without_usage``)
    for the operator snapshot, and ``by_origin`` (``main`` / ``subagent`` /
    ``handoff``) where per-origin tracking is enabled; the origins sum to
    ``usage``. Where the last flush carried no ``by_origin`` (an archived
    ledger) later flushes carry none. A Worker restart reads the last flush
    back from the ledger file. The context and the cache counters are
    main-conversation only — a Sub-agent flush carries the last
    main-conversation values forward unchanged.
    Best effort like every
    other ledger write on this path.
    """
    try:
        deps.ledger.record_usage(
            deps.attempt.attempt_id,
            BudgetSnapshot(
                elapsed_seconds=deps.budgets.elapsed_seconds(),
                usage=deps.budgets.attempt_totals(),
                context=deps.budgets.main_context_snapshot(),
                by_origin=deps.budgets.attempt_by_origin(),
            ),
            detail="attempt usage flush",
        )
    except Exception:
        pass


def _is_gateway_for(deps: AgentStepDeps) -> bool:
    """Whether this Step Execution runs behind a Model Gateway (§11.4).

    The deps flag carries the pin kind; the budgets flag agrees — the
    Supervisor builds both from the same pin. Either flag suffices, so a
    caller that threads only one still settles gateway responses at the
    max rate.
    """
    if deps.is_gateway:
        return True
    if deps.budgets.is_gateway:
        return True
    return False


def _record_unreported_cache_write(deps: AgentStepDeps) -> None:
    """Count one gateway response with an unreported cache write (§11.4).

    The response keeps its reported token buckets while its uncached
    remainder prices at max(input, cache-write); the per-Attempt durable
    counter is the ledger marker the Status Snapshot reads back as
    ``upper-bound``. Best effort like every other ledger write on this path.
    """
    try:
        deps.ledger.record_response_with_unreported_cache_write(
            deps.attempt.attempt_id
        )
    except Exception:
        pass


def _record_missing_usage(deps: AgentStepDeps) -> None:
    """Count one response that settled at its worst case for the snapshot (§11).

    The per-Attempt durable counter marks the Attempt's token and cost
    figures ``upper-bound`` in the Status Snapshot: they include a
    worst-case charge, so they are a ceiling on the true spend, never a
    measured zero. Best effort like every other ledger write on this path.
    """
    try:
        deps.ledger.record_response_without_usage(deps.attempt.attempt_id)
    except Exception:
        pass


def _note_activity(deps: AgentStepDeps) -> None:
    """Record one settled model response on the live view, never failing.

    Best-effort like every other live-view report: a missing or raising
    hook never fails the Step Execution it describes.
    """
    callback = deps.on_activity
    if callback is None:
        return
    try:
        callback()
    except Exception:
        pass


def _settle_response_spend(
    deps: AgentStepDeps,
    step_id: str,
    hold_id: str,
    response: BaseMessage,
    *,
    origin: UsageOrigin = "main",
) -> None:
    """Settle one responded request's admission hold with its actual spend.

    A successful response carrying usage metadata settles with its meter
    delta (tool calls are counted when requested, never twice, §11) and
    counts one response under the request's origin; a response without
    usage metadata settles no spend but still counts one response — one
    without usage — under the request's origin, so the origin response
    count stays equal to the tally's ``model_calls`` (§11.1). Only a
    failed request is charged its worst case (§11.4).
    """
    if isinstance(getattr(response, "usage_metadata", None), dict):
        delta = response_usage(response, _prices_for(deps))
        deps.budgets.settle_hold(
            hold_id, step_id, replace(delta, tool_calls=0), origin=origin, responses=1
        )
    else:
        deps.budgets.settle_hold(
            hold_id,
            step_id,
            UsageTotals(),
            origin=origin,
            responses=1,
            responses_without_usage=1,
        )
    _ledger_settle(deps, hold_id)
    _note_activity(deps)
    _flush_attempt_usage(deps)


def _note_main_used(
    deps: AgentStepDeps, step_id: str, seq: int, handoffs: int, response: BaseMessage
) -> None:
    """Record one settled main-conversation response's context size, if measured.

    ``input_tokens + output_tokens`` of the settled response is the
    conversation's size after that response; a response without complete
    usage metadata clears the figure to ``unavailable`` (unknown is never
    zero, and the snapshot never falls back to the estimate). Only the main
    loop calls this — never the handoff turn, never Sub-agents.
    """
    used: int | None = None
    if response_has_complete_usage(response):
        raw = getattr(response, "usage_metadata", None)
        metadata = cast(UsageMetadata, raw) if isinstance(raw, dict) else None
        breakdown = usage_breakdown(metadata)
        used = breakdown.input_tokens + breakdown.output_tokens
    deps.budgets.note_main_context_used(
        used, work_conversation(step_id, seq, handoffs)
    )


def _note_main_cache(
    deps: AgentStepDeps, step_id: str, seq: int, handoffs: int, response: BaseMessage
) -> None:
    """Fold one settled main-conversation response into the cache counters.

    A response carrying complete usage metadata contributes its
    ``cache_read_tokens`` over ``input_tokens`` to the Attempt's
    main-conversation ``cache_hit_rate``; a response without usable usage
    metadata counts as missing instead — a ratio over partial data is never
    ``measured``. Only the main loop calls this, next to
    :func:`_note_main_used`: never the handoff turn, never Sub-agents, and
    never a pin-violating response (which settles through
    ``_settle_response_spend`` and ends the Attempt without recording).
    """
    conversation = work_conversation(step_id, seq, handoffs)
    if response_has_complete_usage(response):
        raw = getattr(response, "usage_metadata", None)
        metadata = cast(UsageMetadata, raw) if isinstance(raw, dict) else None
        breakdown = usage_breakdown(metadata)
        deps.budgets.note_main_cache_hit(
            breakdown.cache_read_tokens, breakdown.input_tokens, conversation
        )
    else:
        deps.budgets.note_main_cache_missing(conversation)


@dataclass(frozen=True)
class ModelRequestEnding:
    """One model request's non-response ending, mapped by the caller.

    The step loop maps an ending to its own :class:`AgentStepOutcome`
    (``limit`` through the tally's ``limit``, ``deadline`` through the
    tally's ``deadline``); a Sub-agent maps either kind to its
    ``no-report`` / ``ceiling`` end. The ``limit`` fact is the crossed
    ``before-request`` fact or the crossed post-spend fact; a ``deadline``
    carries no fact — the step loop builds it from the spend as today.
    """

    kind: Literal["limit", "deadline"]
    fact: LimitFact | None
    detail: str


PROVIDER_FAILURE_SUMMARY_KEY = "provider_failure"
"""The Execution Summary key marking a provider-failed Step Execution (§9.6).

The marker is the durable fact the poll loop reads to count a routed
``error`` toward the halt-after-three (contract §4.4): unlike the Terminal
Outcome reason it survives routing, and unlike process memory it survives
a restart, the same way recorded Step Execution facts do.
"""


def provider_failure_status(exc: BaseException) -> int | None:
    """The HTTP status a provider failure surfaced, where there is one (§9.6).

    Provider SDKs mix their own exception into the ``ModelError`` they
    raise, so a real failure usually carries ``status_code``; scripted
    failures and failures without one fall back to the status the
    ``ModelError`` subclass names (400, 401, 403, 404, 429). Server-side
    (``ModelAPIError``), transport (``ModelConnectionError``,
    ``ModelTimeoutError``) and context-overflow failures name no single
    status and report ``None``.
    """
    from langchain_core.exceptions import (
        ModelAuthenticationError as _Auth,
    )
    from langchain_core.exceptions import (
        ModelInvalidRequestError as _Invalid,
    )
    from langchain_core.exceptions import (
        ModelNotFoundError as _NotFound,
    )
    from langchain_core.exceptions import (
        ModelPermissionDeniedError as _Permission,
    )
    from langchain_core.exceptions import (
        ModelRateLimitError as _RateLimit,
    )

    for attribute in ("status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    for cls, status in (
        (_Invalid, 400),
        (_Auth, 401),
        (_Permission, 403),
        (_NotFound, 404),
        (_RateLimit, 429),
    ):
        if isinstance(exc, cls):
            return status
    return None


def provider_failure_label(exc: BaseException) -> str:
    """One provider failure as ``Class`` or ``Class (status N)`` (§9.6).

    The class name and the status carry no secret; callers redact the
    surrounding text, never this label.
    """
    status = provider_failure_status(exc)
    if status is None:
        return type(exc).__name__
    return f"{type(exc).__name__} (status {status})"


def _model_window_for(deps: AgentStepDeps) -> int | None:
    """The declared Model Context Window in force, if configured (§9.6)."""
    window = deps.context_window
    if window is None:
        return None
    return window.threshold_tokens


def _overflow_context_of(exc: BaseException) -> Any | None:
    return getattr(exc, "_overflow_context", None)


def _overflow_value_of(exc: BaseException) -> int | None:
    """The compared Conversation Context value, however the raise carried it."""
    context = _overflow_context_of(exc)
    if context is not None:
        value = getattr(context, "value", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    carried = getattr(exc, "conversation_value", None)
    if isinstance(carried, int) and not isinstance(carried, bool):
        return carried
    return None


def _overflow_basis_of(exc: BaseException) -> str | None:
    """The compared Conversation Context basis, however the raise carried it."""
    context = _overflow_context_of(exc)
    if context is not None:
        basis = getattr(context, "basis", None)
        if isinstance(basis, str):
            return basis
    carried = getattr(exc, "conversation_basis", None)
    if isinstance(carried, str):
        return carried
    return None


def provider_failure_summary(exc: BaseException) -> dict[str, object]:
    """The Execution Summary marker for a failed model request (§9.6).

    Names the provider error class and the HTTP status where there is
    one; the ``context_overflow`` flag tells the halt-after-three apart —
    a context overflow ends the Step Execution ``error`` but never counts
    toward the halt. A Context Overflow also names the signal (``refused``
    or ``cut-short``), the Conversation Context sent (value and basis) and
    the declared Model Context Window.
    """
    from langchain_core.exceptions import ContextOverflowError as _Overflow

    summary: dict[str, object] = {
        "error": type(exc).__name__,
        "status": provider_failure_status(exc),
        "context_overflow": isinstance(exc, _Overflow),
    }
    if isinstance(exc, _Overflow):
        from coding_agent.runtime.context_overflow import overflow_signal_of

        summary["signal"] = overflow_signal_of(exc)
        context = _overflow_context_of(exc)
        if context is not None:
            summary["conversation_context"] = context.to_record()
        else:
            value = _overflow_value_of(exc)
            basis = _overflow_basis_of(exc)
            if value is not None and basis is not None:
                summary["conversation_context"] = {
                    "value": value,
                    "basis": basis,
                    "anchor_input_tokens": None,
                    "anchor_request_ref": None,
                    "appended_estimate_tokens": 0,
                }
        usable = getattr(exc, "_overflow_usable", None)
        if isinstance(usable, int):
            summary["usable_threshold"] = usable
        window = getattr(exc, "_overflow_window", None)
        if isinstance(window, int):
            summary["model_context_window"] = window
    return summary


def provider_failure_detail(exc: BaseException, redactions: Redactable = None) -> str:
    """The Step Execution detail for a failed model request (§9.6).

    Names the provider error class and the HTTP status where there is
    one, then the failure's own message. A Context Overflow names the
    overflow, the signal, the Conversation Context sent (value and basis)
    and the declared Model Context Window. The message may carry provider
    response bodies, so it passes through the Attempt's redaction
    registry before it reaches the detail.
    """
    from coding_agent.runtime.redaction import registry_from

    message = registry_from(redactions).redact(str(exc)).strip()
    from langchain_core.exceptions import ContextOverflowError as _Overflow

    if isinstance(exc, _Overflow):
        from coding_agent.runtime.context_overflow import overflow_signal_of

        signal = overflow_signal_of(exc)
        head = f"Context Overflow (signal {signal}): the model request failed: {provider_failure_label(exc)}"
        parts = [head]
        context = _overflow_context_of(exc)
        if context is not None:
            parts.append(
                f"Conversation Context sent {context.value} tokens "
                f"(basis {context.basis})"
            )
        else:
            value = _overflow_value_of(exc)
            basis = _overflow_basis_of(exc)
            if value is not None and basis is not None:
                parts.append(
                    f"Conversation Context sent {value} tokens (basis {basis})"
                )
        window = getattr(exc, "_overflow_window", None)
        if isinstance(window, int):
            parts.append(f"Model Context Window {window} tokens")
        else:
            parts.append("Model Context Window unavailable")
        if message:
            parts.append(message)
        return ": ".join(parts)
    head = f"the model request failed: {provider_failure_label(exc)}"
    if message:
        return f"{head}: {message}"
    return head


def _invoke_with_cap(
    bound_model: object, messages: list[BaseMessage], output_cap: int
) -> BaseMessage:
    invoke = cast(Any, getattr(bound_model, "invoke"))
    if output_cap > 0:
        try:
            return cast(BaseMessage, invoke(messages, max_tokens=output_cap))
        except TypeError:
            # Compatibility shim for models whose `invoke` takes only the
            # messages (strictly-unary test fakes): the turn still goes out,
            # uncapped, but its spend stays bounded — the Admission hold
            # already reserves estimate + cap as the worst case, the
            # provider-reported usage settles actuals, and the after-spend
            # check catches crossings. The production capability wrappers
            # accept and forward `max_tokens`, so the cap reaches the
            # provider there.
            pass
    return cast(BaseMessage, invoke(messages))


def run_model_request(
    deps: AgentStepDeps,
    witness: PinWitness,
    step_id: str,
    seq: int,
    bound_model: object,
    messages: list[BaseMessage],
    tools: Sequence[BaseTool],
    *,
    first_hold_id: str | None = None,
    record_main_context: bool = True,
    on_model_call: Callable[[], None] | None = None,
    handoffs: int = 0,
    output_cap: int | None = None,
    origin: UsageOrigin = "main",
    context_state: ConversationContextState | None = None,
) -> BaseMessage | ModelRequestEnding:
    """Run one model request to its accepted response or its ending.

    Owns the whole life of one model request, in the step loop's order: the
    wall-clock check, the admission hold, the invoke with its retry loop,
    the per-request timeout, the model-call count, the identity and effort
    pin checks, the hold settlement, the recorded ``model_response`` event
    and the after-spend limit check.

    Both the Step Execution loop (through :func:`request_model_response`)
    and the Sub-agent loop run every request — each round and each provider
    retry — through this function, so Admission and the worst-case charge
    apply to each try exactly once (§11.4, ADR 0029). The first Sub-agent
    request runs under its spawn-time hold (``first_hold_id``): the spawn
    group admitted it up front in call order, so it is reused as is — never
    admitted twice and never recorded twice. The bounded Context-Window
    Handoff turn runs through this function too, with ``record_main_context``
    off, ``origin`` carrying ``handoff`` and ``output_cap`` carrying the
    handoff output cap, so its spend settles under the handoff origin while
    the main-conversation context figures and cache-hit counters stay
    untouched. Provider retries count under the origin of the request they
    retry. A caller that maps the ending
    (:class:`ModelRequestEnding`) to its own outcome decides what an
    admission refusal, a crossed limit or an exhausted wall clock ends.

    ``messages`` is appended to exactly as the loop appended to it: the
    accepted response is appended before it is returned, and a response
    whose spend crossed a limit is appended and recorded before the ending
    is returned. A
    ``PinViolation`` and a non-retryable ``ModelError`` propagate exactly as
    they did from the loop.
    """
    request_cap = _output_cap_for(deps) if output_cap is None else output_cap
    hold_id: str | None = first_hold_id
    # The Conversation Context this request is compared with: measured once
    # up front so the Admission hold, the retry admissions and the recorded
    # ``model_response`` event all carry the same compared value — including
    # a first request running under its spawn-time hold.
    compared = _measure(messages, tools, context_state)
    usable_threshold = _usable_threshold_for(deps)
    anchor_count = len(messages)
    if deps.budgets.remaining_wall_seconds(step_id) <= 0:
        if hold_id is not None:
            deps.budgets.settle_hold(
                hold_id, step_id, UsageTotals(), origin=origin
            )
            _ledger_settle(deps, hold_id)
            _flush_attempt_usage(deps)
        return ModelRequestEnding(
            kind="deadline",
            fact=None,
            detail="the remaining wall-clock time is exhausted",
        )
    if hold_id is None:
        admit_fact, hold_id = deps.budgets.admit_request(
            step_id, compared.value, output_cap=request_cap, origin=origin
        )
        if admit_fact is not None:
            return ModelRequestEnding(
                kind="limit",
                fact=admit_fact,
                detail=(
                    "the worst-case bound exceeds the "
                    f"{admit_fact.scope} limit"
                ),
            )
        assert hold_id is not None
        _ledger_hold(deps, step_id, seq, hold_id)
    timeout = deps.budgets.model_timeout_for(step_id)
    started = deps.budgets.clock()
    from langchain_core.exceptions import ModelError as _ModelError

    response: BaseMessage | None = None
    attempts = 0
    pending_charge: str | None = hold_id
    while attempts < 3:
        attempts += 1
        try:
            response = _invoke_with_cap(bound_model, messages, request_cap)
            break
        except _ModelError as exc:
            failed_hold = pending_charge
            if pending_charge is not None:
                deps.budgets.charge_hold(pending_charge, origin=origin)
                _ledger_settle(deps, pending_charge, charged=True)
                _flush_attempt_usage(deps)
                pending_charge = None
            if deps.pin.is_gateway:
                # A failed gateway request carrying retry or fallback
                # evidence above 0 fails closed (§9.1): the hold is
                # already charged at worst case above, the evidence
                # is recorded, and the Attempt ends through the §12
                # finalisation — never retried, never classified by
                # status. An error without such evidence is not a
                # violation and is classified by its status as today.
                if check_gateway_error_exc(deps.pin, exc).violated:
                    raise witness.violation_from_error(
                        exc, context=compared, usable_threshold=usable_threshold
                    ) from exc
            if not exc.is_retryable or attempts >= 3:
                from langchain_core.exceptions import (
                    ContextOverflowError as _Overflow,
                )

                if isinstance(exc, _Overflow):
                    setattr(exc, "_overflow_context", compared)
                    setattr(exc, "_overflow_usable", usable_threshold)
                    setattr(exc, "_overflow_window", _model_window_for(deps))
                    # A refused request settles at its hold's worst case and
                    # records the Context Overflow fact with the hold it
                    # charged (§11.4, §13.2). The hold settlement above is
                    # already charged; this records the fact.
                    try:
                        witness.record_refused_overflow(
                            exc,
                            context=compared,
                            usable_threshold=usable_threshold,
                            hold_id=failed_hold,
                            model_context_window=_model_window_for(deps),
                        )
                    except Exception:
                        pass
                raise
            compared = _measure(messages, tools, context_state)
            retry_fact, retry_hold = deps.budgets.admit_request(
                step_id, compared.value, output_cap=request_cap, origin=origin
            )
            if retry_fact is not None:
                return ModelRequestEnding(
                    kind="limit",
                    fact=retry_fact,
                    detail=(
                        "the retry bound exceeds the "
                        f"{retry_fact.scope} limit"
                    ),
                )
            assert retry_hold is not None
            _ledger_hold(deps, step_id, seq, retry_hold)
            pending_charge = retry_hold
            hold_id = retry_hold
            continue
    assert response is not None
    assert isinstance(response, BaseMessage)
    if timeout != float("inf"):
        elapsed = deps.budgets.clock() - started
        if elapsed >= timeout:
            if pending_charge is not None:
                deps.budgets.charge_hold(pending_charge, origin=origin)
                _ledger_settle(deps, pending_charge, charged=True)
                _flush_attempt_usage(deps)
            return ModelRequestEnding(
                kind="deadline",
                fact=None,
                detail="the model request ran into the remaining time",
            )
    if on_model_call is not None:
        on_model_call()
    # The violating response is never used: it is charged, its
    # identity and the violation are recorded, and the Attempt
    # ends through §12 finalisation — never retried, never
    # carried back to the model (§9.1).
    if check_direct_pin(deps.pin, response).violated:
        if pending_charge is not None:
            _settle_response_spend(
                deps, step_id, pending_charge, response, origin=origin
            )
        if deps.pin.is_gateway and response_has_unreported_gateway_cache_write(
            response, is_gateway=True
        ):
            _record_unreported_cache_write(deps)
        raise witness.violation_from_response(
            response, context=compared, usable_threshold=usable_threshold
        )
    # A reported effort that differs from the pinned effort is
    # a pin violation under the same reason (§9.1): the
    # response is discarded, its usage charged, and the
    # Attempt ends through §12 finalisation — never retried,
    # never carried back to the model.
    if check_reported_effort(deps.pin, response).violated:
        if pending_charge is not None:
            _settle_response_spend(
                deps, step_id, pending_charge, response, origin=origin
            )
        raise witness.violation_from_response(
            response, context=compared, usable_threshold=usable_threshold
        )
    from coding_agent.runtime.context_overflow import (
        ResponseCutShortByContext,
        cut_short_of,
        is_context_window_cut_short,
    )

    cut_short = cut_short_of(response)
    cutting_context = is_context_window_cut_short(response)
    if pending_charge is not None:
        if record_main_context:
            _note_main_used(deps, step_id, seq, handoffs, response)
            _note_main_cache(deps, step_id, seq, handoffs, response)
            if context_state is not None:
                context_state.note_response(
                    response,
                    anchor_count,
                    f"{work_conversation(step_id, seq, handoffs)}"
                    f"#response-{witness.response_index}",
                )
        if response_has_complete_usage(response):
            gateway = _is_gateway_for(deps)
            delta = response_usage(
                response, _prices_for(deps), is_gateway=gateway
            )
            if gateway and response_has_unreported_gateway_cache_write(
                response, is_gateway=True
            ):
                _record_unreported_cache_write(deps)
            # Tool calls are counted when requested (note_tool_calls
            # below, LIM-9): the provider usage settles tokens and cost
            # only, so one turn is never charged twice (LIM-13).
            meter_delta = replace(delta, tool_calls=0)
            deps.budgets.settle_hold(
                pending_charge, step_id, meter_delta, origin=origin, responses=1
            )
            _ledger_settle(deps, pending_charge)
            _note_activity(deps)
            _flush_attempt_usage(deps)
        else:
            # A response without usage — or with partial usage — settles
            # at its Admission hold's worst case, whether it succeeded or
            # failed (§11.4). Missing usage is not a pin violation: the
            # caller continues and the hold settles as ``charged``.
            deps.budgets.charge_hold(pending_charge, origin=origin)
            _ledger_settle(deps, pending_charge, charged=True)
            _record_missing_usage(deps)
            _note_activity(deps)
            _flush_attempt_usage(deps)
        if cutting_context:
            # A response cut short by the window settles with its usage
            # (above) and is a Context Overflow — never bounded correction
            # (§9.5). The ``model_response`` records the cut-short kind and
            # the raise carries the compared context for the caller's
            # detail and Execution Summary.
            witness.record_success(
                response,
                context=compared,
                usable_threshold=usable_threshold,
                cut_short="context-window",
            )
            window = _model_window_for(deps)
            raise ResponseCutShortByContext(
                "the response was cut short by the context window",
                response=response,
                conversation_value=compared.value,
                conversation_basis=compared.basis,
                usable_threshold=usable_threshold,
                model_context_window=window,
                context=compared,
            )
        crossed = deps.budgets.check_after_spend(step_id)
        if crossed is not None:
            messages.append(response)
            witness.record_success(
                response,
                context=compared,
                usable_threshold=usable_threshold,
                cut_short=cut_short,
            )
            return ModelRequestEnding(
                kind="limit",
                fact=crossed,
                detail=f"the spend crossed the {crossed.scope} limit",
            )
    elif cutting_context:
        # No open hold (a Sub-agent's spawn-time hold settled elsewhere):
        # still classify the cut-short response as a Context Overflow.
        witness.record_success(
            response,
            context=compared,
            usable_threshold=usable_threshold,
            cut_short="context-window",
        )
        window = _model_window_for(deps)
        raise ResponseCutShortByContext(
            "the response was cut short by the context window",
            response=response,
            conversation_value=compared.value,
            conversation_basis=compared.basis,
            usable_threshold=usable_threshold,
            model_context_window=window,
            context=compared,
        )
    messages.append(response)
    witness.record_success(
        response,
        context=compared,
        usable_threshold=usable_threshold,
        cut_short=cut_short,
    )
    return response


def request_model_response(
    deps: AgentStepDeps,
    tally: ExecutionTally,
    witness: PinWitness,
    step_id: str,
    seq: int,
    bound_model: object,
    messages: list[BaseMessage],
    tools: Sequence[BaseTool],
) -> BaseMessage | AgentStepOutcome:
    """Run one Step Execution model request to its response or its outcome.

    The step loop's mapping over :func:`run_model_request`: an ending
    becomes the tally's ``limit`` or ``deadline`` outcome, an accepted
    response returns as is. A non-retryable ``ModelError``, or a retryable
    one whose three tries are exhausted, ends the Step Execution
    ``error`` through the tally's ``provider_error`` (§9.6) — the shared
    lifecycle already charged the failed request's hold at its worst case
    (§11.4) before raising. A ``PinViolation`` propagates untouched: it is
    raised before this classification and never becomes ``error`` (§12).
    ``messages`` is appended to by the shared lifecycle, exactly as the
    loop appended to it.
    """
    from langchain_core.exceptions import ModelError as _ModelError

    def _count_model_call() -> None:
        tally.model_calls += 1

    try:
        result = run_model_request(
            deps,
            witness,
            step_id,
            seq,
            bound_model,
            messages,
            tools,
            record_main_context=True,
            on_model_call=_count_model_call,
            handoffs=tally.handoffs,
            context_state=tally.context_state,
        )
    except _ModelError as exc:
        return tally.provider_error(
            provider_failure_detail(exc, deps.redactions),
            provider_failure_summary(exc),
        )
    if isinstance(result, ModelRequestEnding):
        if result.kind == "deadline":
            return tally.deadline(result.detail)
        assert result.fact is not None
        return tally.limit(result.fact, result.detail)
    return result
