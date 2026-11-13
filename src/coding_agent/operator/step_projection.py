"""The step-workflow operator-contract projection (contract §19, S9).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §19 (the
operator projection) and §4.5 (Attempt Archives). The native vocabulary is
producer-owned, so every operator-contract change for step workflows lives
in this one slice, coordinating with
[Spec: coding-agent serves operator contract major 1 as a producer](https://github.com/lbacik/coding-agent/issues/245).

This module is pure: it maps Run Ledger rows (passed in as plain mappings
so the operator listener keeps its fresh-connection reads and the tests
stay fast) onto the §19 projection. The wiring — the graph nodes emitting
``native.`` step events, the archive files, the listener/show dispatch —
lives beside it; this module decides the values.

Matrix row ids: OPC-1 … OPC-6.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

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


def is_step_attempt_id(attempt_id: str) -> bool:
    """Whether ``attempt_id`` is a step-workflow id (``<workflow>#<N>/<n>``)."""
    from coding_agent.operator.attempts import is_step_attempt_id as _is_step

    return _is_step(attempt_id)


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
    executions: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """The first ``limit_fact`` committed on any Step Execution, if any."""
    for execution in executions:
        fact = execution.get("limit_fact")
        if isinstance(fact, Mapping) and fact:
            return fact
    return None


def _workflow_end_reason(
    transitions: Sequence[Mapping[str, Any]],
) -> str:
    """The recorded Workflow End as an outcome reason (§8.2, §15.1)."""
    for transition in reversed(list(transitions)):
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
    executions: Sequence[Mapping[str, Any]] | None = None,
    transitions: Sequence[Mapping[str, Any]] | None = None,
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
    step_effects: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int, int], str]:
    """The ``(step, execution, ordinal)`` keys holding a receipt, by remote id."""
    receipts: dict[tuple[str, int, int], str] = {}
    for effect in step_effects:
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


def _execution_url(execution: Mapping[str, Any]) -> str | None:
    """The pull-request URL an ``open-pull-request`` execution recorded."""
    for source in (execution.get("payload"), execution.get("facts")):
        if not isinstance(source, Mapping):
            continue
        for key in ("pull_url", "pull_request_url"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _execution_number(execution: Mapping[str, Any]) -> int | None:
    """The pull-request number an execution recorded, if any."""
    for source in (execution.get("payload"), execution.get("facts")):
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
    step_effects: Sequence[Mapping[str, Any]] | None = None,
    executions: Sequence[Mapping[str, Any]] | None = None,
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
    for execution in reversed(list(executions or [])):
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
    step_effects: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Whether any effect still waits for its receipt or settled aside (§19).

    An intent key ``(step, execution, ordinal)`` without a matching receipt
    is unsettled; a ``not-performed`` row (a write the recovery rules
    settled aside, §14.2) counts the same way. Either forces
    ``attention_required`` true whatever the Terminal Outcome.
    """
    intents: set[tuple[str, int, int]] = set()
    receipts: set[tuple[str, int, int]] = set()
    for effect in step_effects or []:
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
    step_effects: Sequence[Mapping[str, Any]] | None = None,
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
    executions: Sequence[Mapping[str, Any]] | None,
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
    for execution in executions or []:
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
    executions: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """The Attempt spend summed over every Step Execution's usage (§11.1).

    Cost and effective tokens (falling back to raw tokens) accumulate over
    the executions that metered them; tool calls read ``tool_calls`` (or
    the agent-step ``tool_calls_meter``); wall clock accumulates only where
    an execution recorded it, and stays ``None`` otherwise. Operation steps
    meter nothing, so a pure-operation Attempt simply spent nothing yet.
    """
    cost = 0.0
    effective_tokens = 0
    tool_calls = 0
    wall_clock: float | None = None
    for execution in executions or []:
        usage = execution.get("usage")
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
    return {
        "cost_usd": cost,
        "effective_tokens": effective_tokens,
        "tool_calls": tool_calls,
        "wall_clock_seconds": wall_clock,
    }


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
    executions: Sequence[Mapping[str, Any]],
    step_effects: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
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
