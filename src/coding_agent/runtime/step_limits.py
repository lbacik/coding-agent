"""Step-workflow limits: four meters, Attempt Ceiling, allowances and admission.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §11 (meters,
Attempt Ceiling and Step Allowances, Reserved Steps and the Work Pool,
admission with holds, the output cap, tool-call counting, deadlines,
operations still running after the ceiling, the ``limit-exceeded`` fact and
its precedence).

Slice S5 of ``docs/plan/step-workflow-implementation-plan.md``.

Matrix row ids: LIM-1 … LIM-17.

The four meters are cost (USD, per token bucket from the pinned prices),
effective tokens (cache reads excluded), tool calls and wall clock. There is
no model-request counter; repetition is bounded by Execution Ceilings (§8.3).
Usage is recorded durably as each response arrives; admission is checked
*before* every model request against the worst-case bound, which is held
while the request is in flight and settled with the actual usage on
response (§11.4).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import (
    ORIGINS,
    OriginCell,
    UsageOrigin,
    UsageTotals,
    origin_cell_from_readable,
)
from coding_agent.workflow.schema import METERS, AttemptCeiling, WorkflowStep, Workflow

Clock = Callable[[], float]
"""A wall-clock source returning seconds (``time.monotonic`` by default)."""

DEFAULT_CODE_OWNED_COMMAND_TIMEOUT_SECONDS = 600.0
"""Code-owned per-command timeout (§11.5, §10)."""

DEFAULT_CODE_OWNED_MODEL_TIMEOUT_SECONDS = 300.0
"""Code-owned per-model-request timeout (§11.5)."""


LimitScope = str
"""One of ``step``, ``work-pool``, ``reserve``, ``attempt-ceiling``."""

LimitMoment = str
"""One of ``before-request``, ``tool-call``, ``deadline``, ``entry-refusal``."""

BEFORE_REQUEST: LimitMoment = "before-request"
TOOL_CALL_MOMENT: LimitMoment = "tool-call"
DEADLINE_MOMENT: LimitMoment = "deadline"
ENTRY_REFUSAL: LimitMoment = "entry-refusal"

STEP_SCOPE: LimitScope = "step"
WORK_POOL_SCOPE: LimitScope = "work-pool"
RESERVE_SCOPE: LimitScope = "reserve"
ATTEMPT_SCOPE: LimitScope = "attempt-ceiling"

_SCOPE_PRECEDENCE: dict[str, int] = {
    ATTEMPT_SCOPE: 0,
    WORK_POOL_SCOPE: 1,
    RESERVE_SCOPE: 1,
    STEP_SCOPE: 2,
    "execution-ceiling": 3,
}
"""Primary-reason order (§11.7): process interruption outranks everything and
is decided outside this module; among limits the Attempt Ceiling beats the
Work Pool / reserve, which beats the Step Allowance, which beats the
Execution Ceiling."""


@dataclass(frozen=True)
class LimitFact:
    """One ``limit-exceeded`` fact (§11.7).

    ``scope`` is the primary scope (precedence order above); ``dimensions``
    names every meter crossed; ``limit`` carries the crossed limit value per
    crossed meter; ``spend`` carries the spend per crossed meter;
    ``moment`` is one of the four admission moments.
    """

    scope: str
    dimensions: tuple[str, ...]
    limit: dict[str, float]
    spend: dict[str, float]
    moment: str

    def to_dict(self) -> dict[str, object]:
        """The JSON-stable record stored on the Step Execution (§13.2)."""
        return {
            "scope": self.scope,
            "dimensions": list(self.dimensions),
            "limit": dict(self.limit),
            "spend": dict(self.spend),
            "moment": self.moment,
        }


def reserved_allowances(workflow: Workflow) -> dict[str, dict[str, float]]:
    """The complete allowance of every Reserved Step, per meter (§11.3)."""
    return {
        step_id: dict(step.limits)
        for step_id, step in workflow.steps.items()
        if step.reserved
    }


def work_pool(workflow: Workflow) -> dict[str, float]:
    """The Work Pool: Attempt Ceiling minus Σ Reserved allowances (§11.3)."""
    ceiling = workflow.ceiling.by_meter()
    reserved = reserved_allowances(workflow)
    pool: dict[str, float] = {}
    for meter in METERS:
        pool[meter] = ceiling[meter] - sum(
            allow.get(meter, 0.0) for allow in reserved.values()
        )
    return pool


def is_reserved_step(workflow: Workflow, step_id: str) -> bool:
    """Whether ``step_id`` draws only on its own allowance (§11.3)."""
    step = workflow.steps.get(step_id)
    return step is not None and step.reserved


def step_allowance(step: WorkflowStep) -> dict[str, float] | None:
    """The step's Step Allowance, or ``None`` where it declares none (§11.2)."""
    if not step.limits:
        return None
    return dict(step.limits)


def ceiling_by_meter(ceiling: AttemptCeiling) -> dict[str, float]:
    """The Attempt Ceiling as meter name to allowance."""
    return ceiling.by_meter()


def _meter_spend(
    totals: UsageTotals, elapsed_seconds: float, meter: str
) -> float:
    if meter == "cost-usd":
        return totals.cost_usd
    if meter == "effective-tokens":
        return float(totals.effective_tokens)
    if meter == "tool-calls":
        return float(totals.tool_calls)
    if meter == "wall-clock-minutes":
        return elapsed_seconds / 60.0
    raise ValueError(f"unknown meter {meter!r}")


def _meter_held(held_tokens: int, held_cost: float, meter: str) -> float:
    if meter == "cost-usd":
        return held_cost
    if meter == "effective-tokens":
        return float(held_tokens)
    if meter == "tool-calls":
        return 0.0
    if meter == "wall-clock-minutes":
        return 0.0
    raise ValueError(f"unknown meter {meter!r}")


def worst_case_bound(
    estimate_tokens: int,
    output_cap: int,
    prices: TokenPrices | None,
    *,
    is_gateway: bool = False,
) -> tuple[int, float]:
    """The admission bound for one model request (§11.4).

    The local context estimate priced as uncached input, plus the request's
    output cap. Returns ``(bound_tokens, bound_cost_usd)``: the token bound
    counts against the ``effective-tokens`` meter, the dollar bound against
    ``cost-usd``. Where no price is bound (the unit-test seam) the dollar
    bound is zero but the token bound still applies.

    A gateway pin prices its context estimate at max(input, cache-write)
    (step-workflow contract §11.4), so Admission and settlement agree: the
    estimate may include an unreported cache write. Direct pins price the
    estimate at the plain input rate, unchanged.
    """
    bound_tokens = max(0, estimate_tokens) + max(0, output_cap)
    if prices is None:
        return bound_tokens, 0.0
    input_rate = (
        max(prices.input, prices.cache_write) if is_gateway else prices.input
    )
    bound_cost = (
        max(0, estimate_tokens) / 1_000_000 * input_rate
        + max(0, output_cap) / 1_000_000 * prices.output
    )
    return bound_tokens, bound_cost


def effective_model_timeout(
    remaining_wall_seconds: float, code_owned_seconds: float
) -> float:
    """One model request's timeout: min(code-owned, remaining) (§11.5)."""
    if remaining_wall_seconds <= 0:
        return 0.0
    return min(code_owned_seconds, remaining_wall_seconds)


def effective_command_timeout(
    remaining_wall_seconds: float, code_owned_seconds: float
) -> float:
    """One command's timeout: min(code-owned, remaining) (§11.5)."""
    if remaining_wall_seconds <= 0:
        return 0.0
    return min(code_owned_seconds, remaining_wall_seconds)


@dataclass
class _HeldRequest:
    hold_id: str
    step_id: str
    bound_tokens: int
    bound_cost_usd: float
    bound_tools: int = 0
    origin: UsageOrigin = "main"


def _normalize_origin(origin: object) -> UsageOrigin:
    """One origin, or ``main`` where the caller passed an unknown value."""
    if origin in ORIGINS:
        return str(origin)
    return "main"


@dataclass
class AttemptBudgets:
    """The Attempt's live meter state: spend, holds and admission (§11).

    One instance per Attempt run, shared across the Workflow graph's nodes
    and across concurrent Sub-agent admissions under one lock. Spend is
    authoritative here; the Run Ledger mirrors holds and per-execution usage
    durably (§13.2) so a crash charges every still-open hold its worst case
    (LIM-8).
    """

    ceiling: AttemptCeiling
    workflow: Workflow
    prices: TokenPrices | None = None
    output_cap: int = 0
    attempt_id: str = ""
    is_gateway: bool = False
    """Whether the Attempt's pin reaches its model through a Model Gateway.

    A gateway pin's Admission bound prices its context estimate at
    max(input, cache-write) (§11.4); direct pins price at the input rate.
    """
    opened_at: float = field(default_factory=time.monotonic)
    clock: Clock = field(default_factory=lambda: time.monotonic)
    code_owned_command_timeout: float = DEFAULT_CODE_OWNED_COMMAND_TIMEOUT_SECONDS
    code_owned_model_timeout: float = DEFAULT_CODE_OWNED_MODEL_TIMEOUT_SECONDS
    _attempt: UsageTotals = field(default_factory=UsageTotals)
    _per_step: dict[str, UsageTotals] = field(default_factory=dict)
    _origin_cells: dict[tuple[str, str], OriginCell] = field(default_factory=dict)
    _origins_enabled: bool = field(default=True)
    _held: dict[str, _HeldRequest] = field(default_factory=dict)
    _step_seconds: dict[str, float] = field(default_factory=dict)
    _context_used: int | None = None
    _context_peak: int | None = None
    _context_estimate: int | None = None
    _context_estimate_basis: str = "estimated"
    _context_conversation: str | None = None
    _main_cache_read: int = 0
    _main_cache_input: int = 0
    _main_cache_missing: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def note_main_context_estimate(
        self, estimate: int, conversation: str, *, basis: str = "estimated"
    ) -> None:
        """The step loop's latest Conversation Context for the main conversation.

        The value the context-threshold check compares against the Usable
        Threshold:
        the provider-anchored Conversation Context where a measured response
        anchors it (``basis="anchored"``), else the chars-based estimate of
        the next request (``basis="estimated"``). Only the main conversation
        records here — Sub-agent admissions never call this.
        """
        with self._lock:
            self._context_estimate = estimate
            self._context_estimate_basis = (
                basis if basis in ("anchored", "estimated") else "estimated"
            )
            self._context_conversation = conversation

    def note_main_context_used(self, used: int | None, conversation: str) -> None:
        """One settled main-conversation response's context size, if measured.

        ``used`` is ``input_tokens + output_tokens`` of the settled response,
        or ``None`` where the response carried no usable ``usage_metadata`` —
        unknown is never zero, so a missing reading clears the figure instead
        of reporting one. The peak only moves on a measured value and stays
        Attempt-wide across Step Executions. Only the main conversation records
        here — Sub-agent responses never call this.
        """
        with self._lock:
            self._context_conversation = conversation
            if used is None:
                self._context_used = None
                return
            self._context_used = used
            if self._context_peak is None or used > self._context_peak:
                self._context_peak = used

    def note_main_cache_hit(
        self, cache_read_tokens: int, input_tokens: int, conversation: str
    ) -> None:
        """Fold one settled main-conversation response's cache buckets in.

        ``cache_read_tokens`` over ``input_tokens`` is the response's share
        of the Attempt's main-conversation ``cache_hit_rate``. Only the main
        loop records here — never Sub-agent admissions — so the counters stay
        the main conversation's own.
        """
        with self._lock:
            self._context_conversation = conversation
            self._main_cache_read += max(0, cache_read_tokens)
            self._main_cache_input += max(0, input_tokens)

    def note_main_cache_missing(self, conversation: str) -> None:
        """Count one settled main-conversation response without usage.

        Any main response without usable ``usage_metadata`` keeps the
        ``cache_hit_rate`` ``unavailable``: a ratio over partial data is
        never ``measured``. Sub-agent responses without usage never call
        this — the count is main-conversation only.
        """
        with self._lock:
            self._context_conversation = conversation
            self._main_cache_missing += 1

    def reset_main_context(self, conversation: str) -> None:
        """A fresh main conversation: a new Step Execution.

        ``used`` and the estimate drop — the new conversation has settled
        nothing yet — while the Attempt-wide peak survives. The next flush
        after this reads ``unavailable`` until the loop estimates and a
        response settles. The main-conversation cache counters never reset
        here: ``cache_hit_rate`` sums every Step Execution's settled
        responses, so only the used figure drops.
        """
        with self._lock:
            self._context_conversation = conversation
            self._context_used = None
            self._context_estimate = None

    def main_context_snapshot(self) -> dict[str, Any]:
        """The main conversation's context for the Attempt usage flush.

        Always present with ``None`` for unknown values so the operator
        snapshot reports ``unavailable`` instead of inventing a number. The
        cache counters (``cache_read`` / ``cache_input`` /
        ``responses_without_usage``) travel next to the context values so
        ``status`` serves prepared state without reading the Transcript.
        """
        with self._lock:
            return {
                "used": self._context_used,
                "peak": self._context_peak,
                "estimate": self._context_estimate,
                "estimate_basis": self._context_estimate_basis,
                "conversation": self._context_conversation,
                "cache_read": self._main_cache_read,
                "cache_input": self._main_cache_input,
                "responses_without_usage": self._main_cache_missing,
            }

    def restore_main_context(self, context: Mapping[str, Any] | None) -> None:
        """Resume the main-conversation context from the last flush (§11, C-LIM-1).

        A Worker restart rebuilds the live context the same way it rebuilds
        the spend meter: the peak survives, the cache counters keep
        accumulating, and the next flush continues instead of restarting
        from unknown.
        """
        if not isinstance(context, Mapping):
            return
        with self._lock:
            used = context.get("used")
            peak = context.get("peak")
            estimate = context.get("estimate")
            conversation = context.get("conversation")
            self._context_used = (
                used if isinstance(used, int) and not isinstance(used, bool) else None
            )
            self._context_peak = (
                peak if isinstance(peak, int) and not isinstance(peak, bool) else None
            )
            self._context_estimate = (
                estimate
                if isinstance(estimate, int) and not isinstance(estimate, bool)
                else None
            )
            basis = context.get("estimate_basis")
            self._context_estimate_basis = (
                basis if basis in ("anchored", "estimated") else "estimated"
            )
            self._context_conversation = (
                conversation if isinstance(conversation, str) else None
            )
            self._main_cache_read = _restore_counter(context.get("cache_read"))
            self._main_cache_input = _restore_counter(context.get("cache_input"))
            self._main_cache_missing = _restore_counter(
                context.get("responses_without_usage")
            )

    @property
    def pool(self) -> dict[str, float]:
        """The Work Pool for this Attempt's Workflow (§11.3)."""
        return work_pool(self.workflow)

    @property
    def reserved(self) -> dict[str, dict[str, float]]:
        """Every Reserved Step's allowance (§11.3)."""
        return reserved_allowances(self.workflow)

    def elapsed_seconds(self) -> float:
        """Attempt wall clock from opening, including operation time (§11.6)."""
        return max(0.0, self.clock() - self.opened_at)

    def step_seconds(self, step_id: str) -> float:
        """The step's own wall clock: summed Step Execution durations (§11.2)."""
        with self._lock:
            return self._step_seconds.get(step_id, 0.0)

    def add_step_seconds(self, step_id: str, seconds: float) -> float:
        """Fold one finished Step Execution's duration into its step (§11.2)."""
        with self._lock:
            total = self._step_seconds.get(step_id, 0.0) + max(0.0, seconds)
            self._step_seconds[step_id] = total
            return total

    def open_hold_ids(self) -> tuple[str, ...]:
        """Every still-held admission hold, for crash charging (LIM-8)."""
        with self._lock:
            return tuple(self._held.keys())

    def attempt_totals(self) -> UsageTotals:
        """Cumulative Attempt spend, excluding in-flight holds."""
        with self._lock:
            return self._attempt

    def origins_enabled(self) -> bool:
        """Whether per-origin cells are tracked (never restarted from zero)."""
        with self._lock:
            return self._origins_enabled

    def step_by_origin(self, step_id: str) -> dict[str, OriginCell]:
        """One step's cumulative spend per origin, zeros where nothing spent."""
        with self._lock:
            cells: dict[str, OriginCell] = {}
            for origin in ORIGINS:
                cells[origin] = self._origin_cells.get(
                    (step_id, origin), OriginCell()
                )
            return cells

    def attempt_by_origin(self) -> dict[str, OriginCell] | None:
        """The Attempt spend per origin, or ``None`` where not recorded.

        ``None`` where the last flush carried no ``by_origin`` (an archived
        ledger): per-origin figures then stay ``unavailable`` and later
        flushes carry none — ``by_origin`` is never restarted from zero
        against a restored total.
        """
        with self._lock:
            if not self._origins_enabled:
                return None
            summed: dict[str, OriginCell] = {origin: OriginCell() for origin in ORIGINS}
            for ( _step_id, origin), cell in self._origin_cells.items():
                if origin in summed:
                    summed[origin] = summed[origin] + cell
            return summed

    def restore_attempt_spend(
        self, totals: UsageTotals, by_origin: Mapping[str, OriginCell] | None = None
    ) -> None:
        """Resume the Attempt meter from the last ledger flush (§11, C-LIM-1).

        A Worker restart rebuilds live budgets from the durable usage
        flush instead of restarting from zero, so the next flush keeps
        accumulating rather than overwriting the settled spend. ``usage``
        and ``by_origin`` restore together: where the last flush has no
        ``by_origin``, per-origin tracking disables and later flushes carry
        none.
        """
        with self._lock:
            self._attempt = totals
            if by_origin is None:
                self._origins_enabled = False
                self._origin_cells = {}
                self._per_step = {}
                return
            self._origins_enabled = True
            restored: dict[tuple[str, str], OriginCell] = {}
            # The flush has no per-step data: only the Attempt sum restores,
            # kept under the empty step key so step deltas still sum to it.
            for origin in ORIGINS:
                cell = by_origin.get(origin)
                if isinstance(cell, OriginCell):
                    restored[("", origin)] = cell
            self._origin_cells = restored
            self._per_step = {}

    def step_totals(self, step_id: str) -> UsageTotals:
        """Cumulative spend of every Step Execution of ``step_id`` (§11.2)."""
        with self._lock:
            return self._per_step.get(step_id, UsageTotals())

    def _add_origin_cell(
        self, step_id: str, origin: UsageOrigin, delta: OriginCell
    ) -> None:
        """Fold one origin delta into the step × origin meter, if enabled."""
        if not self._origins_enabled:
            return
        key = (step_id, _normalize_origin(origin))
        self._origin_cells[key] = self._origin_cells.get(key, OriginCell()) + delta

    @staticmethod
    def _cell_from_totals(
        actual: UsageTotals, *, responses: int, responses_without_usage: int
    ) -> OriginCell:
        """One origin delta from settled bucket totals plus response counts."""
        return OriginCell(
            input_tokens=actual.input_tokens,
            cache_read_tokens=actual.cache_read_tokens,
            cache_write_tokens=actual.cache_write_tokens,
            output_tokens=actual.output_tokens,
            cost_input_usd=actual.cost_input_usd,
            cost_output_usd=actual.cost_output_usd,
            cost_cache_read_usd=actual.cost_cache_read_usd,
            cost_cache_write_usd=actual.cost_cache_write_usd,
            effective_tokens=actual.effective_tokens,
            tool_calls=actual.tool_calls,
            responses=responses,
            responses_without_usage=responses_without_usage,
        )

    def held_totals(self) -> tuple[int, float]:
        """The in-flight worst case currently held (tokens, dollars)."""
        with self._lock:
            tokens = sum(held.bound_tokens for held in self._held.values())
            cost = sum(held.bound_cost_usd for held in self._held.values())
            return tokens, cost

    def _pool_spent(self, step_id: str) -> tuple[UsageTotals, float]:
        """Spend counting against the Work Pool (non-reserved steps only)."""
        with self._lock:
            total = UsageTotals()
            for sid, totals in self._per_step.items():
                if not is_reserved_step(self.workflow, sid):
                    total = total + totals
            _ = step_id
            return total, self.elapsed_seconds()

    def _scope_limits_and_spent(
        self, step_id: str
    ) -> list[tuple[str, dict[str, float], UsageTotals, float]]:
        """Every scope constraining ``step_id``: (scope, limit, spent, elapsed).

        A Reserved Step draws only on its own allowance; every other agent
        step draws on the shared Work Pool (§11.3). Every step additionally
        draws on the Attempt Ceiling (§11.2, §11.6).
        """
        scopes: list[tuple[str, dict[str, float], UsageTotals, float]] = []
        elapsed = self.elapsed_seconds()
        step = self.workflow.steps.get(step_id)
        reserved = step is not None and step.reserved
        if reserved:
            assert step is not None
            scopes.append(
                (RESERVE_SCOPE, dict(step.limits), self.step_totals(step_id), elapsed)
            )
        else:
            pool_spent, _ = self._pool_spent(step_id)
            scopes.append((WORK_POOL_SCOPE, self.pool, pool_spent, elapsed))
            if step is not None and step.limits:
                scopes.append(
                    (STEP_SCOPE, dict(step.limits), self.step_totals(step_id), elapsed)
                )
        scopes.append(
            (ATTEMPT_SCOPE, self.ceiling.by_meter(), self.attempt_totals(), elapsed)
        )
        return scopes

    def _crossed(
        self,
        step_id: str,
        *,
        extra_tokens: int = 0,
        extra_cost: float = 0.0,
        extra_tools: int = 0,
        extra_seconds: float = 0.0,
        moment: str,
    ) -> LimitFact | None:
        """The primary fact where ``extra_*`` would cross any scope, else None.

        ``extra_*`` is the prospective spend (the worst-case bound for
        admission, ``1`` tool for tool-call counting, the elapsed overrun for
        deadlines). Every crossed meter in every crossed scope is collected;
        the primary scope follows §11.7 precedence and the fact carries every
        crossed dimension with that scope's limit and the current spend.
        """
        held_tokens, held_cost = self.held_totals()
        attempt_elapsed = self.elapsed_seconds()
        candidates: list[tuple[str, str, float, float]] = []
        for scope, limit, spent, _ in self._scope_limits_and_spent(step_id):
            # Step wall clock (§11.2) is the sum of the step's own Step
            # Execution durations; every other scope runs on the Attempt
            # clock from opening (§11.6).
            if scope in (STEP_SCOPE, RESERVE_SCOPE):
                scope_elapsed = self.step_seconds(step_id)
                scope_spent_elapsed = self.step_seconds(step_id)
            else:
                scope_elapsed = attempt_elapsed
                scope_spent_elapsed = attempt_elapsed
            for meter in METERS:
                allowance = float(limit.get(meter, float("inf")))
                if allowance == float("inf"):
                    continue
                used = _meter_spend(spent, scope_elapsed + extra_seconds, meter)
                held = _meter_held(held_tokens, held_cost, meter)
                if meter == "effective-tokens":
                    used += extra_tokens + held
                elif meter == "cost-usd":
                    used += extra_cost + held
                elif meter == "tool-calls":
                    used += extra_tools + held
                elif meter == "wall-clock-minutes":
                    if scope == WORK_POOL_SCOPE and not is_reserved_step(
                        self.workflow, step_id
                    ):
                        allowance = self._non_reserved_deadline_minutes()
                    if scope == STEP_SCOPE:
                        step = self.workflow.steps.get(step_id)
                        if step is not None and "wall-clock-minutes" not in step.limits:
                            continue
                if used > allowance:
                    spend_now = _meter_spend(spent, scope_spent_elapsed, meter)
                    candidates.append((scope, meter, allowance, spend_now))
        if not candidates:
            return None
        candidates.sort(key=lambda item: _SCOPE_PRECEDENCE.get(item[0], 99))
        primary = candidates[0][0]
        dimensions = tuple(sorted({meter for _, meter, _, _ in candidates}))
        limit_map = {meter: lim for scope, meter, lim, _ in candidates if scope == primary}
        for _, meter, lim, _ in candidates:
            if meter in dimensions and meter not in limit_map:
                limit_map[meter] = lim
        spend_map = {meter: spent for _, meter, _, spent in candidates if meter in dimensions}
        return LimitFact(
            scope=primary,
            dimensions=dimensions,
            limit=limit_map,
            spend=spend_map,
            moment=moment,
        )

    def _non_reserved_deadline_minutes(self) -> float:
        """Non-reserved model work stops at the deadline minus reserves (§11.3)."""
        ceiling = self.ceiling.by_meter()["wall-clock-minutes"]
        reserved_minutes = sum(
            allow.get("wall-clock-minutes", 0.0) for allow in self.reserved.values()
        )
        return ceiling - reserved_minutes

    def check_entry(self, step_id: str, *, is_operation: bool = False) -> LimitFact | None:
        """Refuse an ``agent`` step whose budgets are already spent (§11.6).

        Operations still execute under their own code-owned timeouts, so a
        YAML Hard Handoff runs after the ceiling (LIM-11). Returns the
        ``entry-refusal`` fact, or ``None`` where the step may open.
        """
        if is_operation:
            return None
        step = self.workflow.steps.get(step_id)
        if step is None or step.agent is None:
            return None
        return self._crossed(step_id, moment=ENTRY_REFUSAL)

    def admit_request(
        self,
        step_id: str,
        estimate_tokens: int,
        *,
        output_cap: int | None = None,
        origin: UsageOrigin = "main",
    ) -> tuple[LimitFact | None, str | None]:
        """Admit one model request, holding its worst case (§11.4, LIM-6).

        Returns ``(None, hold_id)`` where the bound fits every applicable
        remaining allowance, pool or reserve and the Attempt Ceiling;
        otherwise the ``before-request`` fact and no hold — the request is
        not sent and the Step Execution ends ``limit-exceeded``.

        ``output_cap`` is this one request's output cap for its bound (the
        Attempt's cap where omitted). ``origin`` is the request's Usage Origin, always
        known; provider retries count under the origin of the request they
        retry.
        """
        bound_tokens, bound_cost = worst_case_bound(
            estimate_tokens,
            self.output_cap if output_cap is None else output_cap,
            self.prices,
            is_gateway=self.is_gateway,
        )
        fact = self._crossed(
            step_id,
            extra_tokens=bound_tokens,
            extra_cost=bound_cost,
            moment=BEFORE_REQUEST,
        )
        if fact is not None:
            return fact, None
        hold_id = f"hold-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._held[hold_id] = _HeldRequest(
                hold_id=hold_id,
                step_id=step_id,
                bound_tokens=bound_tokens,
                bound_cost_usd=bound_cost,
                origin=_normalize_origin(origin),
            )
        return None, hold_id

    def hold_bound(self, hold_id: str) -> tuple[int, float] | None:
        """The worst-case bound one open hold carries, if still held."""
        with self._lock:
            held = self._held.get(hold_id)
            if held is None:
                return None
            return held.bound_tokens, held.bound_cost_usd

    def settle_hold(
        self,
        hold_id: str,
        step_id: str,
        actual: UsageTotals,
        *,
        origin: UsageOrigin | None = None,
        responses: int = 0,
        responses_without_usage: int = 0,
    ) -> UsageTotals:
        """Settle a hold with the actual usage on response (§11.4)."""
        with self._lock:
            held = self._held.pop(hold_id, None)
            resolved = step_id or (held.step_id if held is not None else "")
            resolved_origin: UsageOrigin = _normalize_origin(
                origin if origin is not None else (held.origin if held is not None else "main")
            )
            self._attempt = self._attempt + actual
            if resolved:
                self._per_step[resolved] = (
                    self._per_step.get(resolved, UsageTotals()) + actual
                )
            if responses or responses_without_usage or (
                actual.input_tokens
                or actual.output_tokens
                or actual.cache_read_tokens
                or actual.cache_write_tokens
                or actual.effective_tokens
                or actual.cost_usd
                or actual.tool_calls
            ):
                self._add_origin_cell(
                    resolved,
                    resolved_origin,
                    self._cell_from_totals(
                        actual,
                        responses=responses,
                        responses_without_usage=responses_without_usage,
                    ),
                )
            return self._attempt

    def charge_hold(
        self, hold_id: str, *, origin: UsageOrigin | None = None
    ) -> UsageTotals:
        """Charge a hold's admitted worst case (LIM-8).

        A response without usage (successful or failed), and a hold still
        open after a crash, are each charged their admitted worst case
        (§11.4, §13.2). The charge goes to the origin of the request, booked
        in the input bucket (tokens and ``cost_input``), and counted in that
        origin's responses without usage.
        """
        with self._lock:
            held = self._held.pop(hold_id, None)
            if held is None:
                return self._attempt
            resolved_origin: UsageOrigin = _normalize_origin(
                origin if origin is not None else held.origin
            )
            worst = UsageTotals(
                tokens=held.bound_tokens,
                effective_tokens=held.bound_tokens,
                cost_usd=held.bound_cost_usd,
                input_tokens=held.bound_tokens,
                output_tokens=0,
                cost_input_usd=held.bound_cost_usd,
            )
            self._attempt = self._attempt + worst
            self._per_step[held.step_id] = (
                self._per_step.get(held.step_id, UsageTotals()) + worst
            )
            self._add_origin_cell(
                held.step_id,
                resolved_origin,
                OriginCell(
                    input_tokens=held.bound_tokens,
                    cost_input_usd=held.bound_cost_usd,
                    effective_tokens=held.bound_tokens,
                    responses=1,
                    responses_without_usage=1,
                ),
            )
            return self._attempt

    def note_tool_calls(
        self, step_id: str, count: int = 1, *, origin: UsageOrigin = "main"
    ) -> LimitFact | None:
        """Count requested tool calls; refuse past the limit (§11.5, LIM-9).

        Calls are counted when requested. Where the count crosses any
        applicable limit the calls are not executed and the Step Execution
        ends ``limit-exceeded`` with moment ``tool-call`` — but the requested
        calls still count toward the spend the fact reports. A Sub-agent's
        own tool calls carry origin ``subagent``; the caller's
        ``spawn_subagent`` call carries ``main``.
        """
        with self._lock:
            delta = UsageTotals(tool_calls=count)
            self._attempt = self._attempt + delta
            self._per_step[step_id] = self._per_step.get(step_id, UsageTotals()) + delta
            self._add_origin_cell(
                step_id, _normalize_origin(origin), OriginCell(tool_calls=count)
            )
        return self._crossed_after_spend(step_id, moment=TOOL_CALL_MOMENT)

    def add_spend(
        self,
        step_id: str,
        delta: UsageTotals,
        *,
        origin: UsageOrigin = "main",
        responses: int = 0,
        responses_without_usage: int = 0,
    ) -> None:
        """Fold one settled response (or Sub-agent spend) in (§11.2)."""
        with self._lock:
            self._attempt = self._attempt + delta
            self._per_step[step_id] = self._per_step.get(step_id, UsageTotals()) + delta
            if responses or responses_without_usage or (
                delta.input_tokens
                or delta.output_tokens
                or delta.cache_read_tokens
                or delta.cache_write_tokens
                or delta.effective_tokens
                or delta.cost_usd
                or delta.tool_calls
            ):
                self._add_origin_cell(
                    step_id,
                    _normalize_origin(origin),
                    self._cell_from_totals(
                        delta,
                        responses=responses,
                        responses_without_usage=responses_without_usage,
                    ),
                )

    def _crossed_after_spend(self, step_id: str, *, moment: str) -> LimitFact | None:
        return self._crossed(step_id, moment=moment)

    def check_after_spend(self, step_id: str, *, moment: str = BEFORE_REQUEST) -> LimitFact | None:
        """Where settled spend crossed any applicable limit, the primary fact."""
        return self._crossed(step_id, moment=moment)

    def remaining_tool_calls(self, step_id: str) -> int:
        """Remaining billable tool calls for ``step_id`` (§11.5).

        The minimum remaining ``tool-calls`` room over every scope that
        constrains the step: its Step Allowance where one is declared, the
        Work Pool for non-reserved steps or the step's own reserve for
        Reserved Steps, and the Attempt Ceiling. A scope declaring no
        ``tool-calls`` allowance does not constrain the count; the Attempt
        Ceiling always declares it (§11.2), so the bound is finite.
        """
        candidates: list[float] = []
        for _scope, limit, spent, _ in self._scope_limits_and_spent(step_id):
            if "tool-calls" not in limit:
                continue
            allowance = float(limit["tool-calls"])
            candidates.append(max(0.0, allowance - float(spent.tool_calls)))
        if not candidates:
            return 0
        return int(min(candidates))

    def remaining_wall_seconds(self, step_id: str) -> float:
        """Remaining model-work time for ``step_id`` in seconds (§11.5).

        The minimum over the applicable scopes; non-reserved steps stop at
        the Attempt deadline minus the reserved minutes (§11.3, LIM-16).
        """
        attempt_elapsed = self.elapsed_seconds()
        candidates: list[float] = []
        for scope, limit, _spent, _ in self._scope_limits_and_spent(step_id):
            if "wall-clock-minutes" not in limit:
                continue
            allowance = float(limit["wall-clock-minutes"])
            if scope == WORK_POOL_SCOPE and not is_reserved_step(self.workflow, step_id):
                allowance = self._non_reserved_deadline_minutes()
            if scope in (STEP_SCOPE, RESERVE_SCOPE):
                elapsed = self.step_seconds(step_id)
            else:
                elapsed = attempt_elapsed
            candidates.append(max(0.0, allowance * 60.0 - elapsed))
        if not candidates:
            return float("inf")
        return min(candidates)

    def model_timeout_for(self, step_id: str) -> float:
        """One model request's timeout for ``step_id`` (§11.5, LIM-10)."""
        return effective_model_timeout(
            self.remaining_wall_seconds(step_id), self.code_owned_model_timeout
        )

    def command_timeout_for(self, step_id: str) -> float:
        """One command's timeout for ``step_id`` (§11.5, LIM-10)."""
        return effective_command_timeout(
            self.remaining_wall_seconds(step_id), self.code_owned_command_timeout
        )


def build_budgets(
    workflow: Workflow,
    *,
    prices: TokenPrices | None = None,
    output_cap: int = 0,
    attempt_id: str = "",
    opened_at: float | None = None,
    clock: Clock | None = None,
    is_gateway: bool = False,
) -> AttemptBudgets:
    """One Attempt's live budgets for ``workflow`` (§11.2–§11.3)."""
    resolved_clock: Clock = clock if clock is not None else time.monotonic
    return AttemptBudgets(
        ceiling=workflow.ceiling,
        workflow=workflow,
        prices=prices,
        output_cap=output_cap,
        attempt_id=attempt_id,
        opened_at=resolved_clock() if opened_at is None else opened_at,
        clock=resolved_clock,
        is_gateway=is_gateway,
    )


def attempt_opened_at_now() -> datetime:
    """The Attempt opening timestamp (§11.6: wall clock runs from opening)."""
    return datetime.now(timezone.utc)


def _restore_counter(value: Any) -> int:
    """A restored main-conversation cache counter, or zero where absent.

    Ledgers flushed before the counters travelled in the payload carry no
    such key: after a restart they restart from zero rather than inventing
    a number.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _as_count(value: Any) -> int:
    """A readable token count, or zero where the payload lacks it."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _as_dollars(value: Any) -> float:
    """A readable dollar amount, or zero where the payload lacks it."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def usage_totals_from_readable(usage: Mapping[str, Any]) -> UsageTotals:
    """Rebuild Attempt spend from a flushed usage payload (§11, C-LIM-1)."""
    return UsageTotals(
        tokens=_as_count(usage.get("tokens")),
        effective_tokens=_as_count(usage.get("effective_tokens")),
        cost_usd=_as_dollars(usage.get("cost_usd")),
        tool_calls=_as_count(usage.get("tool_calls")),
        input_tokens=_as_count(usage.get("input_tokens")),
        output_tokens=_as_count(usage.get("output_tokens")),
        cache_read_tokens=_as_count(usage.get("cache_read_tokens")),
        cache_write_tokens=_as_count(usage.get("cache_write_tokens")),
        cost_input_usd=_as_dollars(usage.get("cost_input_usd")),
        cost_output_usd=_as_dollars(usage.get("cost_output_usd")),
        cost_cache_read_usd=_as_dollars(usage.get("cost_cache_read_usd")),
        cost_cache_write_usd=_as_dollars(usage.get("cost_cache_write_usd")),
    )


def by_origin_from_readable(payload: Mapping[str, Any] | None) -> dict[str, OriginCell] | None:
    """One flush payload's ``by_origin`` cells, or ``None`` where not recorded.

    ``None`` where the payload carries no ``by_origin`` (an archived ledger):
    per-origin figures then read ``unavailable`` with reason ``not recorded``,
    and later flushes carry none. Old spend is never assigned to ``main``.
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("by_origin")
    if not isinstance(raw, Mapping):
        return None
    cells: dict[str, OriginCell] = {}
    for origin in ORIGINS:
        value = raw.get(origin)
        if not isinstance(value, Mapping):
            return None
        cells[origin] = origin_cell_from_readable(value)
    return cells


def restore_budgets_from_usage(
    budgets: AttemptBudgets, payload: Mapping[str, Any] | None
) -> None:
    """Resume ``budgets`` from the last flushed usage payload, if any.

    Both the spend meter and the main-conversation context (so the peak
    survives a Worker restart) come back from the same flush. ``usage``
    and ``by_origin`` restore together: where the last flush has no
    ``by_origin``, later flushes carry none and per-origin figures stay
    ``unavailable``.
    """
    if not isinstance(payload, Mapping):
        return
    context = payload.get("context")
    budgets.restore_main_context(
        context if isinstance(context, Mapping) else None
    )
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return
    budgets.restore_attempt_spend(
        usage_totals_from_readable(raw), by_origin_from_readable(payload)
    )
