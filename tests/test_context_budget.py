"""The Context Budget: one margin rule, one precedence, one pricing rule.

Table tests at the module interface (``coding_agent.runtime.context_budget``):
precedence × margins × pricing (gateway vs direct) × equality-at-bound ×
None-propagation × estimate-vs-maximum equivalence. The loop tests keep one
crossing per kind plus recording intact; the arithmetic lives here.
"""

from __future__ import annotations

import pytest

from coding_agent.runtime.context_budget import (
    admission_floor,
    admission_hold,
    effective_input_price,
    priced_bound,
    select_usable_threshold,
    threshold_outcome,
    usable_from_raw,
)

SAFETY = 1_000
OVERHEAD = 1_000


def test_usable_from_raw_subtracts_margin_and_overhead() -> None:
    """The one margin rule: raw less safety less overhead."""
    assert usable_from_raw(200_000, SAFETY, OVERHEAD) == 198_000


def test_usable_from_raw_propagates_none() -> None:
    """No policy or no window config bound reads as no Usable Threshold."""
    assert usable_from_raw(None, SAFETY, OVERHEAD) is None
    assert usable_from_raw(200_000, None, OVERHEAD) is None
    assert usable_from_raw(200_000, SAFETY, None) is None


def test_usable_from_raw_keeps_non_positive_for_the_floor() -> None:
    """The subtraction is pure: non-positive usable values reach the floor.

    The floor refuses them (``None``/no floor); the guard keeps its own
    bound however small it validates to.
    """
    assert usable_from_raw(2_000, SAFETY, OVERHEAD) == 0
    assert usable_from_raw(1_500, SAFETY, OVERHEAD) == -500


@pytest.mark.parametrize(
    ("soft", "hard", "pin", "expected"),
    [
        (190_000, 198_000, 198_000, 190_000),  # soft configured: soft wins
        (None, 198_000, 198_000, 198_000),  # hard-only opt-in: hard wins
        (None, None, 198_000, 198_000),  # legacy step: pin default
        (190_000, None, 198_000, 190_000),  # soft-without-hard: soft-selected
        (None, None, None, None),  # nothing bound: no threshold
    ],
)
def test_select_usable_threshold_soft_first(
    soft: int | None, hard: int | None, pin: int | None, expected: int | None
) -> None:
    """Selection is soft-first: soft, else hard, else the pin default."""
    assert select_usable_threshold(soft, hard, pin) == expected


@pytest.mark.parametrize(
    ("value", "hard", "soft", "pin", "expected"),
    [
        # Hard-first: a request over both bounds selects the Hard Handoff.
        (199_000, 198_000, 190_000, 198_000, "hard"),
        # Over soft but at or below hard: the soft continuation.
        (195_000, 198_000, 190_000, 198_000, "soft"),
        (191_000, 198_000, 190_000, 198_000, "soft"),
        # Equality passes at each bound: at the hard bound the hard guard
        # passes but the soft guard still fires; at the soft bound all pass.
        (198_000, 198_000, 190_000, 198_000, "soft"),
        (190_000, 198_000, 190_000, 198_000, None),
        (198_000, None, None, 198_000, None),
        # Below every bound: the request is sent.
        (100, 198_000, 190_000, 198_000, None),
        (100, None, None, 198_000, None),
        # Pin guard on steps without a policy.
        (199_000, None, None, 198_000, "pin"),
        # No bound configured: nothing to cross.
        (10**9, None, None, None, None),
        # An opted-in step replaces the pin guard: over the pin default but
        # under both step bounds still passes.
        (198_500, 199_000, 198_600, 198_000, None),
        # Soft-without-hard still guards soft-first behind hard's absence:
        # with no hard bound the pin guard applies, never soft.
        (191_000, None, 190_000, 198_000, None),
    ],
)
def test_threshold_outcome_hard_first(
    value: int,
    hard: int | None,
    soft: int | None,
    pin: int | None,
    expected: str | None,
) -> None:
    """The guard is hard-first, then soft, then pin; strict ``>`` at each."""
    assert (
        threshold_outcome(
            value, hard_usable=hard, soft_usable=soft, pin_usable=pin
        )
        == expected
    )


def test_effective_input_price_gateway_takes_max() -> None:
    """A gateway pin prices at max(input, cache-write); direct at input."""
    assert (
        effective_input_price(2.0, 5.0, is_gateway=True)
        == 5.0
    )
    assert (
        effective_input_price(5.0, 2.0, is_gateway=True)
        == 5.0
    )
    assert (
        effective_input_price(2.0, 5.0, is_gateway=False)
        == 2.0
    )


def test_priced_bound_direct_prices_plain_input() -> None:
    """Direct pins price the estimate at the plain input rate, unchanged."""
    tokens, cost = priced_bound(
        198_000,
        1_000,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=False,
    )
    assert tokens == 199_000
    assert cost == pytest.approx(198_000 / 1_000_000 * 2.0 + 1_000 / 1_000_000 * 8.0)


def test_priced_bound_gateway_prices_max_input_cache_write() -> None:
    """Gateway pins price the estimate at max(input, cache-write)."""
    tokens, cost = priced_bound(
        198_000,
        1_000,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=True,
    )
    assert tokens == 199_000
    assert cost == pytest.approx(198_000 / 1_000_000 * 5.0 + 1_000 / 1_000_000 * 8.0)


def test_priced_bound_clamps_negative_counts() -> None:
    """A bound is never negative: token counts clamp at zero."""
    assert priced_bound(-10, -5, input_price=2.0, output_price=8.0, cache_write_price=5.0, is_gateway=True) == (0, 0.0)
    assert priced_bound(-10, -5, input_price=2.0, output_price=8.0, cache_write_price=5.0, is_gateway=False) == (0, 0.0)


@pytest.mark.parametrize("is_gateway", [False, True])
def test_hold_and_floor_agree_at_the_same_count(is_gateway: bool) -> None:
    """Hold-vs-floor equivalence: one core at the estimate and the maximum.

    The Admission hold (at the request estimate) and the admission floor (at
    the maximum: the Usable Threshold plus the output cap) are the same
    function, for gateway and direct pins alike.
    """
    estimate = 150_000
    usable = 198_000
    cap = 1_000
    assert admission_hold(
        estimate,
        cap,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=is_gateway,
    ) == priced_bound(
        estimate,
        cap,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=is_gateway,
    )
    assert admission_floor(
        usable,
        cap,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=is_gateway,
    ) == priced_bound(
        usable,
        cap,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=is_gateway,
    )
    # The floor is the hold evaluated at the maximum.
    assert admission_floor(
        usable,
        cap,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=is_gateway,
    ) == admission_hold(
        usable,
        cap,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=is_gateway,
    )


def test_hold_floor_equivalence_gateway_and_direct_diverge_only_by_rate() -> None:
    """Gateway and direct floors share tokens and differ only by input rate."""
    gateway = admission_floor(
        198_000,
        1_000,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=True,
    )
    direct = admission_floor(
        198_000,
        1_000,
        input_price=2.0,
        output_price=8.0,
        cache_write_price=5.0,
        is_gateway=False,
    )
    assert gateway[0] == direct[0] == 199_000
    assert gateway[1] == pytest.approx(
        198_000 / 1_000_000 * 5.0 + 1_000 / 1_000_000 * 8.0
    )
    assert direct[1] == pytest.approx(
        198_000 / 1_000_000 * 2.0 + 1_000 / 1_000_000 * 8.0
    )
