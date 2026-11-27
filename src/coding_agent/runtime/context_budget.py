"""The Context Budget: one Usable Threshold, one pricing rule (pure ints).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §§3, 9.9, 11.4
and ADRs 0031, 0032, 0033. Every Usable Threshold is a configured token bound
less the code-owned safety margin and request overhead; every Admission hold
and every admission floor prices its context tokens at the gateway
``max(input, cache-write)`` rule (direct pins at the plain input rate) plus
the request's output cap.

This module owns the three rules once — the margin rule, the two-view
precedence (soft-first selection, hard-first guard) and the pricing rule —
as pure ints-in/ints-out functions. The Step Execution loop, the Sub-agent
loop (through the shared model-request lifecycle), the operator snapshot and
the workflow validator (plus step limits) call it instead of restating the
rules. Callers keep their own records: ledger events, snapshot structs,
validator diagnostics, tally outcomes and routes never enter this module, so
the workflow validator can depend on it without the reverse (ADR 0027).

Two views, deliberately not unified: selection answers which Usable
Threshold is in force (soft where the step configures soft, else hard where
it opts in, else the requesting conversation pin's default), while the guard
answers which bound one compared request crosses (hard evaluated first, then
soft, then the pin default). A soft-configured step without hard is treated
defensively as soft-selected; validation requires hard alongside soft.
Crossing is strict at every bound: equality passes and the request is sent.
"""

from __future__ import annotations

from typing import Literal, overload

ThresholdCrossing = Literal["hard", "soft", "pin"]
"""Which bound one compared request crosses: the hard, soft or pin guard."""


@overload
def usable_from_raw(
    raw_tokens: int,
    safety_margin_tokens: int,
    request_overhead_tokens: int,
) -> int: ...


@overload
def usable_from_raw(
    raw_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None: ...


def usable_from_raw(
    raw_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None:
    """One Usable Threshold: the configured bound less margin and overhead.

    The one margin rule every caller shares. ``None`` where any input is
    missing (no policy, or no context-window config bound); otherwise the
    plain subtraction, even where non-positive — positivity is the floor's
    own refusal (``None``/no floor), never the guard's: an opted-in step
    keeps its hard guard however small the bound validates to.
    """
    if raw_tokens is None:
        return None
    if safety_margin_tokens is None:
        return None
    if request_overhead_tokens is None:
        return None
    return raw_tokens - safety_margin_tokens - request_overhead_tokens


@overload
def hard_usable_threshold(
    hard_tokens: int,
    safety_margin_tokens: int,
    request_overhead_tokens: int,
) -> int: ...


@overload
def hard_usable_threshold(
    hard_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None: ...


def hard_usable_threshold(
    hard_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None:
    """The step's own Hard Usable Threshold, where the step opts in (§3).

    The policy's ``hard-tokens`` less the code-owned safety margin and request
    overhead. ``None`` where any input is missing (no policy, or no
    context-window config bound).
    """
    return usable_from_raw(
        hard_tokens, safety_margin_tokens, request_overhead_tokens
    )


@overload
def soft_usable_threshold(
    soft_tokens: int,
    safety_margin_tokens: int,
    request_overhead_tokens: int,
) -> int: ...


@overload
def soft_usable_threshold(
    soft_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None: ...


def soft_usable_threshold(
    soft_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None:
    """The step's own Soft Usable Threshold, where the step configures it (§3).

    The policy's ``soft-tokens`` less the code-owned safety margin and request
    overhead. ``None`` where the step configures no soft bound (hard-only
    opted-in steps and legacy steps alike) or no context-window config is
    bound.
    """
    if soft_tokens is None:
        return None
    return usable_from_raw(
        soft_tokens, safety_margin_tokens, request_overhead_tokens
    )


@overload
def pin_usable_threshold(
    threshold_tokens: int,
    safety_margin_tokens: int,
    request_overhead_tokens: int,
) -> int: ...


@overload
def pin_usable_threshold(
    threshold_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None: ...


def pin_usable_threshold(
    threshold_tokens: int | float | None,
    safety_margin_tokens: int | float | None,
    request_overhead_tokens: int | float | None,
) -> int | float | None:
    """The requesting pin's default Usable Threshold (§16).

    The pin's Context Threshold less the safety margin and the request
    overhead. ``None`` where unpinned.
    """
    return usable_from_raw(
        threshold_tokens, safety_margin_tokens, request_overhead_tokens
    )


@overload
def select_usable_threshold(
    soft_usable: int | None,
    hard_usable: int | None,
    pin_usable: int | None,
) -> int | None: ...


@overload
def select_usable_threshold(
    soft_usable: int | float | None,
    hard_usable: int | float | None,
    pin_usable: int | float | None,
) -> int | float | None: ...


def select_usable_threshold(
    soft_usable: int | float | None,
    hard_usable: int | float | None,
    pin_usable: int | float | None,
) -> int | float | None:
    """The Usable Threshold in force: soft-first selection.

    The step's soft Usable Threshold where configured, else its hard Usable
    Threshold where it opts in, else the requesting conversation pin's
    default. Soft-without-hard resolves defensively to soft-selected
    (validation requires hard alongside soft).
    """
    if soft_usable is not None:
        return soft_usable
    if hard_usable is not None:
        return hard_usable
    return pin_usable


def threshold_outcome(
    compared_value: int | float,
    *,
    hard_usable: int | float | None,
    soft_usable: int | float | None,
    pin_usable: int | float | None,
) -> ThresholdCrossing | None:
    """Which bound one compared request crosses, if any (hard-first guard).

    On an opted-in step (``hard_usable`` bound) the hard guard is evaluated
    first — a request strictly over the hard bound selects ``"hard"`` even
    where it also crosses the soft bound — then the soft guard; otherwise
    the pin-default guard applies. Crossing is strict at every bound:
    equality passes. ``None`` where the request passes every bound,
    including where no bound is configured.
    """
    if hard_usable is not None:
        if compared_value > hard_usable:
            return "hard"
        if soft_usable is not None and compared_value > soft_usable:
            return "soft"
        return None
    if pin_usable is not None and compared_value > pin_usable:
        return "pin"
    return None


def effective_input_price(
    input_price: float, cache_write_price: float, *, is_gateway: bool
) -> float:
    """The input price one Admission bound prices its context tokens at.

    A gateway pin prices at ``max(input, cache-write)`` so Admission and
    settlement agree where a response carries an unreported cache write; a
    direct pin prices at the plain input rate, unchanged.
    """
    if is_gateway:
        return max(input_price, cache_write_price)
    return input_price


def priced_bound(
    context_tokens: int,
    output_cap: int,
    *,
    input_price: float,
    output_price: float,
    cache_write_price: float,
    is_gateway: bool,
) -> tuple[int, float]:
    """One Admission bound: ``(bound_tokens, bound_cost_usd)`` (§11.4).

    The single pricing core behind the Admission hold (evaluated at the
    request estimate) and the admission floor (the same function evaluated
    at the maximum: the Usable Threshold in force plus the output cap). The
    context tokens price as uncached input — a gateway pin at
    ``max(input, cache-write)`` so Admission and settlement agree where a
    response carries an unreported cache write, a direct pin at the plain
    input rate, unchanged — plus the request's output cap. Prices are USD
    per million tokens, so the dollar bound divides by one million. Token
    counts clamp at zero: a bound is never negative.
    """
    bound_tokens = max(0, context_tokens) + max(0, output_cap)
    effective_input = effective_input_price(
        input_price, cache_write_price, is_gateway=is_gateway
    )
    bound_cost = (
        max(0, context_tokens) / 1_000_000 * effective_input
        + max(0, output_cap) / 1_000_000 * output_price
    )
    return bound_tokens, bound_cost


def admission_hold(
    estimate_tokens: int,
    output_cap: int,
    *,
    input_price: float,
    output_price: float,
    cache_write_price: float,
    is_gateway: bool,
) -> tuple[int, float]:
    """The Admission hold for one request: the pricing core at the estimate."""
    return priced_bound(
        estimate_tokens,
        output_cap,
        input_price=input_price,
        output_price=output_price,
        cache_write_price=cache_write_price,
        is_gateway=is_gateway,
    )


def admission_floor(
    usable_threshold: int,
    output_cap: int,
    *,
    input_price: float,
    output_price: float,
    cache_write_price: float,
    is_gateway: bool,
) -> tuple[int, float] | None:
    """The admission floor: the same pricing core at the maximum.

    The largest admissible input carries ``usable_threshold`` context tokens
    plus the pin's full output cap. ``None`` where ``usable_threshold`` is
    non-positive: non-positive usable values yield no floor.
    """
    if usable_threshold <= 0:
        return None
    return priced_bound(
        usable_threshold,
        output_cap,
        input_price=input_price,
        output_price=output_price,
        cache_write_price=cache_write_price,
        is_gateway=is_gateway,
    )


def code_owned_margins() -> tuple[int, int]:
    """The code-owned safety margin and request overhead (ADR 0032).

    Imported lazily so the workflow validator — which depends on this
    module, never the reverse — stays free of the provider-adapter import
    at load time.
    """
    from coding_agent.provider.pinned_model import (
        REQUEST_OVERHEAD_TOKENS,
        SAFETY_MARGIN_TOKENS,
    )

    return SAFETY_MARGIN_TOKENS, REQUEST_OVERHEAD_TOKENS
