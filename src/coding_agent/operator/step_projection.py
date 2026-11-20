"""The step-workflow operator-contract projection (contract §19, S9).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §19 (the
operator projection) and §4.5 (Attempt Archives). The native vocabulary is
producer-owned, so every operator-contract change for step workflows lives
in this one slice, coordinating with
[Spec: coding-agent serves operator contract major 1 as a producer](https://github.com/lbacik/coding-agent/issues/245).

This module is pure: it maps Run Ledger step records (typed
``StepExecutionRecord`` / ``StepEffectRecord`` / ``StepTransitionRecord``
from the ledger reader, or plain mappings for tests) onto the §19
projection. The wiring — the graph nodes emitting ``native.`` step events,
the archive files, the listener/show dispatch — lives beside it; this
module decides the values.

Matrix row ids: OPC-1 … OPC-6.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from coding_agent.runtime.ceilings import (
    ORIGINS,
    OriginCell,
    origin_cell_from_readable,
)

if TYPE_CHECKING:
    from coding_agent.ledger.store import StepEffectRecord, StepExecutionRecord, StepTransitionRecord

TERMINAL_OUTCOMES = frozenset(
    {
        "completed",
        "blocked",
        "unfinished",
        "failed",
        "withdrawn",
        "drift",
        "interrupted",
        "internal-failure",
    }
)
"""The closed Terminal Outcome set (contract §15.1)."""

ATTENTION_OUTCOMES = frozenset(
    {"failed", "unfinished", "blocked", "interrupted", "internal-failure"}
)
"""Outcomes whose ``attention_required`` is always true (contract §19).

A SIGTERM-ended live Attempt records ``interrupted`` with reason
``shutdown`` (§12, matrix row REC-25): it is covered here, not by a
separate rule.
"""

OPEN_PULL_REQUEST_RESULTS = frozenset({"opened", "already-open"})
"""Step Results of ``open-pull-request`` that count as delivery (§10.5)."""

_UNAVAILABLE = "unavailable"
"""A figure the ledger carries but this Attempt simply has not produced yet."""


def _coerce_execution(execution: Any) -> Mapping[str, Any]:
    """One Step Execution as a mapping, from a dict row or typed record."""
    if isinstance(execution, Mapping):
        return execution
    return {
        "step_id": getattr(execution, "step_id", None),
        "seq": getattr(execution, "seq", None),
        "state": getattr(execution, "state", None),
        "result": getattr(execution, "result", None),
        "payload": getattr(execution, "payload", None),
        "facts": getattr(execution, "facts", None),
        "usage": getattr(execution, "usage", None),
        "limit_fact": getattr(execution, "limit_fact", None),
        "entry_from_step": getattr(execution, "entry_from_step", None),
        "entry_result": getattr(execution, "entry_result", None),
    }


def _coerce_effect(effect: Any) -> Mapping[str, Any]:
    """One step effect as a mapping, from a dict row or typed record."""
    if isinstance(effect, Mapping):
        return effect
    return {
        "step_id": getattr(effect, "step_id", None),
        "execution_number": getattr(effect, "execution_number", None),
        "ordinal": getattr(effect, "ordinal", 0),
        "status": getattr(effect, "status", None),
        "remote_id": getattr(effect, "remote_id", None),
    }


def _coerce_transition(transition: Any) -> Mapping[str, Any]:
    """One step transition as a mapping, from a dict row or typed record."""
    if isinstance(transition, Mapping):
        return transition
    return {
        "kind": getattr(transition, "kind", None),
        "from_step": getattr(transition, "from_step", None),
        "result": getattr(transition, "result", None),
        "to_step": getattr(transition, "to_step", None),
        "to_end": getattr(transition, "to_end", None),
        "reason": getattr(transition, "reason", None),
    }


def describe_limit_fact(fact: Mapping[str, Any]) -> str:
    """One line describing a ``limit-exceeded`` fact (contract §11.7).

    The fact records scope, every dimension crossed, the limit, the spend
    and the moment; the description names the scope, the dimensions and the
    moment so the operator sees which ceiling fired and when.
    """
    scope = fact.get("scope", "unknown")
    dimensions = fact.get("dimensions", fact.get("dimension", []))
    if isinstance(dimensions, str):
        dimensions = [dimensions]
    moment = fact.get("moment")
    detail = f"{scope} limit exceeded"
    if isinstance(dimensions, Sequence) and not isinstance(dimensions, str):
        names = [str(item) for item in dimensions if str(item)]
        if names:
            detail += f" ({', '.join(names)})"
    if moment is not None and str(moment):
        detail += f" at {moment}"
    return detail


def _first_limit_fact(
    executions: Sequence[Any],
) -> Mapping[str, Any] | None:
    """The first ``limit_fact`` committed on any Step Execution, if any."""
    for raw in executions:
        execution = _coerce_execution(raw)
        fact = execution.get("limit_fact")
        if isinstance(fact, Mapping) and fact:
            return fact
    return None


def _workflow_end_reason(
    transitions: Sequence[Any],
) -> str:
    """The recorded Workflow End as an outcome reason (§8.2, §15.1)."""
    coerced = [_coerce_transition(item) for item in transitions]
    for transition in reversed(coerced):
        if transition.get("kind") == "end" and transition.get("to_end") is not None:
            from_step = transition.get("from_step") or "?"
            result = transition.get("result") or "?"
            end = transition.get("to_end")
            return f"ended at end {end} after result {result} of step {from_step}"
    return ""


def outcome_for(
    *,
    outcome: str | None,
    reason: str,
    executions: Sequence[Any] | None = None,
    transitions: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """The §19 outcome projection: Terminal Outcome plus reason (OPC-1).

    ``outcome.native`` is the Terminal Outcome itself. ``outcome.reason``
    prefers the limit (or error) fact when one occurred — the first
    ``limit_fact`` committed on any Step Execution — and otherwise is the
    Workflow End with the step and Step Result that reached it (§15.1).
    """
    executions = list(executions or [])
    transitions = list(transitions or [])
    limit_fact = _first_limit_fact(executions)
    if limit_fact is not None:
        reason_text = describe_limit_fact(limit_fact)
    elif reason:
        reason_text = reason
    else:
        reason_text = _workflow_end_reason(transitions)
    return {"native": outcome, "reason": reason_text}


def _effect_receipts(
    step_effects: Sequence[Any],
) -> dict[tuple[str, int, int], str]:
    """The ``(step, execution, ordinal)`` keys holding a receipt, by remote id."""
    receipts: dict[tuple[str, int, int], str] = {}
    for raw in step_effects:
        effect = _coerce_effect(raw)
        if effect.get("status") != "receipt":
            continue
        step_id = effect.get("step_id")
        execution_number = effect.get("execution_number")
        ordinal = effect.get("ordinal", 0)
        remote_id = effect.get("remote_id")
        if (
            isinstance(step_id, str)
            and isinstance(execution_number, bool) is False
            and isinstance(execution_number, int)
            and isinstance(ordinal, bool) is False
            and isinstance(ordinal, int)
            and remote_id is not None
        ):
            receipts[(step_id, execution_number, ordinal)] = str(remote_id)
    return receipts


def _execution_url(execution: Any) -> str | None:
    """The pull-request URL an ``open-pull-request`` execution recorded."""
    coerced = _coerce_execution(execution)
    for source in (coerced.get("payload"), coerced.get("facts")):
        if not isinstance(source, Mapping):
            continue
        for key in ("pull_url", "pull_request_url"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _execution_number(execution: Any) -> int | None:
    """The pull-request number an execution recorded, if any."""
    coerced = _coerce_execution(execution)
    for source in (coerced.get("payload"), coerced.get("facts")):
        if not isinstance(source, Mapping):
            continue
        for key in ("pull_number", "pull_request_number"):
            value = source.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if value is not None:
                try:
                    return int(str(value))
                except (ValueError, TypeError):
                    continue
    return None


def delivery_for(
    *,
    step_effects: Sequence[Any] | None = None,
    executions: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    """The §19 delivery projection: the created pull request, or null (OPC-2).

    Only a pull request **created** by the Attempt counts, from the
    ``open-pull-request`` receipt: the number comes from the receipt's
    remote id and the URL from the execution facts. A published review or
    a comment has no field in operator-contract major 1. An ``opened``
    execution without its receipt is unsettled, not delivered: this
    returns ``None`` and the attention rule flags it.
    """
    receipts = _effect_receipts(list(step_effects or []))
    for raw_execution in reversed(list(executions or [])):
        execution = _coerce_execution(raw_execution)
        if execution.get("result") not in OPEN_PULL_REQUEST_RESULTS:
            continue
        step_id = execution.get("step_id")
        seq = execution.get("seq")
        if not isinstance(step_id, str) or not isinstance(seq, int):
            continue
        if isinstance(seq, bool):
            continue
        candidates = sorted(
            key for key in receipts if key[0] == step_id and key[1] == seq
        )
        if not candidates:
            continue
        remote_id = receipts[candidates[0]]
        number: int | None = None
        try:
            number = int(remote_id)
        except (ValueError, TypeError):
            number = _execution_number(execution)
        return {"number": number, "url": _execution_url(execution)}
    return None


def has_unsettled_or_not_performed(
    step_effects: Sequence[Any] | None,
) -> bool:
    """Whether any effect still waits for its receipt or settled aside (§19).

    An intent key ``(step, execution, ordinal)`` without a matching receipt
    is unsettled; a ``not-performed`` row (a write the recovery rules
    settled aside, §14.2) counts the same way. Either forces
    ``attention_required`` true whatever the Terminal Outcome.
    """
    intents: set[tuple[str, int, int]] = set()
    receipts: set[tuple[str, int, int]] = set()
    for raw_effect in step_effects or []:
        effect = _coerce_effect(raw_effect)
        step_id = effect.get("step_id")
        execution_number = effect.get("execution_number")
        ordinal = effect.get("ordinal", 0)
        if not isinstance(step_id, str) or not isinstance(execution_number, int):
            continue
        if isinstance(execution_number, bool) or isinstance(ordinal, bool):
            continue
        if not isinstance(ordinal, int):
            continue
        key = (step_id, execution_number, ordinal)
        status = effect.get("status")
        if status == "not-performed":
            return True
        if status == "receipt":
            receipts.add(key)
        elif status == "intent":
            intents.add(key)
    return any(key not in receipts for key in intents)


def attention_required_for(
    *,
    outcome: str | None,
    step_effects: Sequence[Any] | None = None,
) -> bool:
    """The §19 attention rule (OPC-3).

    True for ``failed``, ``unfinished``, ``blocked``, ``interrupted`` (a
    SIGTERM-ended ``shutdown`` included) and ``internal-failure``, and
    whenever an effect is unsettled or ``not-performed``; false for
    ``completed``, ``withdrawn`` and ``drift``. A released Claim (no
    outcome) never requires attention.
    """
    if outcome in ATTENTION_OUTCOMES:
        return True
    if has_unsettled_or_not_performed(step_effects):
        return True
    return False


def native_phase_for(
    executions: Sequence[Any] | None,
    *,
    live_phase: str | None = None,
) -> str | None:
    """The native phase: the step id plus the Step Execution number (OPC-4).

    The live phase the graph node reports (``<step>#<seq>``) wins while the
    Attempt runs; otherwise the latest Step Execution decides. ``None``
    where no Step Execution has opened yet.
    """
    if live_phase:
        return live_phase
    latest: Mapping[str, Any] | None = None
    for raw_execution in executions or []:
        execution = _coerce_execution(raw_execution)
        step_id = execution.get("step_id")
        seq = execution.get("seq")
        if isinstance(step_id, str) and isinstance(seq, int) and not isinstance(
            seq, bool
        ):
            latest = execution
    if latest is None:
        return None
    return f"{latest['step_id']}#{latest['seq']}"


def _as_float(value: Any) -> float | None:
    """``value`` as a float where it is a real number, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    """``value`` as an int where it is a real number, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def ceiling_from_effective_configuration(
    effective_configuration: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The Attempt Ceiling from the pinned Effective Configuration (§11.2).

    Reads the pinned ``workflow.ceiling`` (``cost_usd``, ``tokens``,
    ``tool_calls``, ``wall_clock_minutes`` → seconds), falling back to the
    legacy ``workflow.limits`` shape rows from older releases carry; a
    missing or non-numeric meter reads as ``None`` rather than zero so an
    absent ceiling is never mistaken for a spent one.
    """
    effective_configuration = effective_configuration or {}
    workflow = effective_configuration.get("workflow")
    workflow_map: Mapping[str, Any] = (
        workflow if isinstance(workflow, Mapping) else {}
    )
    raw_ceiling: Any = workflow_map.get("ceiling")
    if not isinstance(raw_ceiling, Mapping):
        raw_ceiling = workflow_map.get("limits")
    limits: Mapping[str, Any] = (
        raw_ceiling if isinstance(raw_ceiling, Mapping) else {}
    )
    wall_minutes = _as_float(limits.get("wall_clock_minutes"))
    return {
        "cost_usd": _as_float(limits.get("cost_usd")),
        "tokens": _as_int(limits.get("tokens")),
        "tool_calls": _as_int(limits.get("tool_calls")),
        "wall_clock_seconds": (
            wall_minutes * 60.0 if wall_minutes is not None else None
        ),
    }


def spend_from_executions(
    executions: Sequence[Any] | None,
) -> dict[str, Any]:
    """The Attempt spend summed over every Step Execution's usage (§11.1).

    Cost and effective tokens (falling back to raw tokens) accumulate over
    the executions that metered them; tool calls read ``tool_calls`` (or
    the agent-step ``tool_calls_meter``); wall clock accumulates only where
    an execution recorded it, and stays ``None`` otherwise. Operation steps
    meter nothing, so a pure-operation Attempt simply spent nothing yet.

    ``by_origin`` sums each origin's cell over the executions that recorded
    one. A contributing record without ``by_origin`` — an archived ledger —
    leaves every per-origin figure ``None`` ("not recorded"): summing gives
    ``None`` per origin if any contributing record lacks it, so old spend is
    never assigned to ``main`` and a missing key is never read as 0.
    """
    cost = 0.0
    effective_tokens = 0
    tool_calls = 0
    tokens = 0
    input_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    has_tokens = False
    has_input = False
    has_cache_read = False
    has_cache_write = False
    wall_clock: float | None = None
    origin_sums: dict[str, OriginCell] = {origin: OriginCell() for origin in ORIGINS}
    origin_missing: dict[str, bool] = {origin: False for origin in ORIGINS}
    for raw_execution in executions or []:
        usage = _coerce_execution(raw_execution).get("usage")
        if not isinstance(usage, Mapping):
            continue
        cost_value = _as_float(usage.get("cost_usd"))
        if cost_value is not None:
            cost += cost_value
        tokens_value = _as_int(usage.get("effective_tokens"))
        if tokens_value is None:
            tokens_value = _as_int(usage.get("tokens"))
        if tokens_value is not None:
            effective_tokens += tokens_value
        raw_value = _as_int(usage.get("tokens"))
        if raw_value is not None:
            tokens += raw_value
            has_tokens = True
        input_value = _as_int(usage.get("input_tokens"))
        if input_value is not None:
            input_tokens += input_value
            has_input = True
        read_value = _as_int(usage.get("cache_read_tokens"))
        if read_value is not None:
            cache_read_tokens += read_value
            has_cache_read = True
        write_value = _as_int(usage.get("cache_write_tokens"))
        if write_value is not None:
            cache_write_tokens += write_value
            has_cache_write = True
        calls_value = _as_int(usage.get("tool_calls"))
        if calls_value is None:
            calls_value = _as_int(usage.get("tool_calls_meter"))
        if calls_value is not None:
            tool_calls += calls_value
        wall_value = _as_float(
            usage.get("wall_clock_seconds", usage.get("elapsed_seconds"))
        )
        if wall_value is not None:
            wall_clock = (wall_clock or 0.0) + wall_value
        raw_by_origin = usage.get("by_origin")
        if not isinstance(raw_by_origin, Mapping):
            for origin in ORIGINS:
                origin_missing[origin] = True
            continue
        for origin in ORIGINS:
            cell_raw = raw_by_origin.get(origin)
            if not isinstance(cell_raw, Mapping):
                origin_missing[origin] = True
                continue
            origin_sums[origin] = origin_sums[origin] + origin_cell_from_readable(
                cell_raw
            )
    spend: dict[str, Any] = {
        "cost_usd": cost,
        "effective_tokens": effective_tokens,
        "tool_calls": tool_calls,
        "wall_clock_seconds": wall_clock,
    }
    if has_tokens:
        spend["tokens"] = tokens
    if has_input:
        spend["input_tokens"] = input_tokens
    if has_cache_read:
        spend["cache_read_tokens"] = cache_read_tokens
    if has_cache_write:
        spend["cache_write_tokens"] = cache_write_tokens
    spend["by_origin"] = {
        origin: (None if origin_missing[origin] else origin_sums[origin].as_readable())
        for origin in ORIGINS
    }
    return spend


def budget_for(
    *,
    ceiling: Mapping[str, Any],
    spend: Mapping[str, Any],
) -> dict[str, Any]:
    """The §19 budget projection: the four meters, used against configured (OPC-5).

    Limits come from the Attempt Ceiling; usage from the executions. A
    ``None`` side reads as ``unavailable`` — never as zero — so the
    operator never mistakes an unmetered Attempt for a free one.
    """

    def _meter(name: str) -> dict[str, Any]:
        used = spend.get(name)
        # The Attempt Ceiling configures ``tokens``; the projection reports
        # effective tokens, so that ceiling binds the effective meter (§11.2,
        # §19). Every other meter shares its name on both sides.
        configured = ceiling.get("tokens" if name == "effective_tokens" else name)
        return {
            "used": used if isinstance(used, (int, float)) else _UNAVAILABLE,
            "configured": (
                configured
                if isinstance(configured, (int, float)) and not isinstance(
                    configured, bool
                )
                else _UNAVAILABLE
            ),
        }

    return {
        "cost_usd": _meter("cost_usd"),
        "effective_tokens": _meter("effective_tokens"),
        "tool_calls": _meter("tool_calls"),
        "wall_clock_seconds": _meter("wall_clock_seconds"),
    }


def capabilities_for(input_type: str | None) -> list[str]:
    """The §19 capabilities for one Worker's input type (OPC-6).

    ``next-issue`` is advertised only by ``issue`` Workers (the default —
    the MVP shape every existing test pins); a ``pull-request`` Worker
    answers ``unsupported``. ``handoff now`` stays ``unsupported`` on both:
    it is never advertised, so it needs no entry here.
    """
    if input_type == "pull-request":
        return ["stop-after"]
    return ["next-issue", "stop-after"]


_NATIVE_PHASE_RE = re.compile(r".+#[0-9]+")


def is_native_phase(value: Any) -> bool:
    """Whether ``value`` has the native phase shape ``<step>#<seq>`` (OPC-4).

    The live view still carries MVP node names (``claim``, ``run_step``)
    before the first Step Execution opens; only a ``<step>#<seq>`` value
    may reach ``status`` verbatim, otherwise the phase falls back to the
    latest Step Execution (``None`` where none has opened yet).
    """
    return isinstance(value, str) and _NATIVE_PHASE_RE.fullmatch(value) is not None


def project_attempt(
    *,
    state: str,
    recorded: tuple[str, str] | None,
    executions: Sequence[Any],
    step_effects: Sequence[Any],
    transitions: Sequence[Any],
    effective_configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """The shared §19 projection core for one step-workflow Attempt (S9).

    Both the ``status`` last-finished builder and the ``attempts show``
    reader project through here so the outcome, delivery, attention,
    budget, limits and phase stay identical: the ceiling comes from the
    pinned Effective Configuration, the spend from the Step Executions,
    delivery from the ``open-pull-request`` receipt, and the outcome
    prefers a limit fact over the recorded reason (§19, OPC-1 … OPC-5).
    A released Claim (or an Attempt without a recorded outcome) carries
    no outcome and never requires attention.
    """
    ceiling = ceiling_from_effective_configuration(effective_configuration)
    spend = spend_from_executions(executions)
    budget = budget_for(ceiling=ceiling, spend=spend)
    delivery = delivery_for(step_effects=step_effects, executions=executions)
    if state == "released" or recorded is None:
        outcome: dict[str, Any] | None = None
        attention_required = False
    else:
        native, stored_reason = recorded
        reason_text = str(
            outcome_for(
                outcome=native,
                reason=stored_reason,
                executions=executions,
                transitions=transitions,
            )["reason"]
        )
        outcome = {"native": native, "reason": reason_text}
        attention_required = attention_required_for(
            outcome=native, step_effects=step_effects
        )
    return {
        "outcome": outcome,
        "delivery": delivery,
        "attention_required": attention_required,
        "budget": {
            "raw_tokens": _UNAVAILABLE,
            "effective_tokens": budget["effective_tokens"]["used"],
            "enforced_tokens": "effective",
        },
        "limits": {
            "effective_tokens": budget["effective_tokens"],
            "cost_usd": budget["cost_usd"],
            "wall_clock_seconds": budget["wall_clock_seconds"],
            "tool_calls": budget["tool_calls"],
        },
        "phase": native_phase_for(executions),
    }
