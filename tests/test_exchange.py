from __future__ import annotations

import inspect

from langchain_core.messages import AIMessage, ToolMessage

from coding_agent.implement.exchange import ExchangeUnit, evict_oldest, flatten


def _unit(tag: str, *, n_results: int = 1) -> ExchangeUnit:
    assistant = AIMessage(content=f"turn {tag}", tool_calls=[])
    results = tuple(
        ToolMessage(content=f"result {tag}.{i}", tool_call_id=f"{tag}-{i}")
        for i in range(n_results)
    )
    return ExchangeUnit(assistant=assistant, results=results)


# --- flatten ------------------------------------------------------------


def test_flatten_lays_out_each_units_assistant_then_its_results_in_order() -> None:
    a = _unit("a", n_results=2)
    b = _unit("b", n_results=1)

    messages = flatten([a, b])

    assert messages == (a.assistant, *a.results, b.assistant, *b.results)


def test_flatten_of_no_units_is_empty() -> None:
    assert flatten([]) == ()


# --- evict_oldest: L3-IMP-4, L3-IMP-12 -----------------------------------


def test_evict_oldest_keeps_everything_when_already_under_budget() -> None:
    units = [_unit("a"), _unit("b"), _unit("c")]

    result = evict_oldest(units, estimate=lambda u: 1, budget_tokens=10)

    assert result == tuple(units)


def test_evict_oldest_drops_from_the_oldest_end_until_under_budget() -> None:
    units = [_unit("a"), _unit("b"), _unit("c"), _unit("d")]

    # Each unit estimates as 1 token; a budget of 2 should evict the two
    # oldest and keep the two most recent, in their original order.
    result = evict_oldest(units, estimate=lambda u: 1, budget_tokens=2)

    assert result == (units[2], units[3])


def test_evict_oldest_never_evicts_the_last_remaining_unit_even_over_budget() -> None:
    units = [_unit("a"), _unit("b")]

    # No budget could ever fit even one unit; the most recent must survive
    # regardless, since it is what the next request answers.
    result = evict_oldest(units, estimate=lambda u: 1000, budget_tokens=0)

    assert result == (units[1],)


def test_evict_oldest_of_no_units_is_empty() -> None:
    assert evict_oldest([], estimate=lambda u: 1, budget_tokens=0) == ()


def test_evict_oldest_never_splits_a_unit() -> None:
    """Every surviving element is a whole `ExchangeUnit` — the assistant
    turn and every one of its tool results move together or not at all,
    by construction (there is no way to return part of one)."""
    units = [_unit("a", n_results=3), _unit("b", n_results=2)]

    result = evict_oldest(units, estimate=lambda u: 5, budget_tokens=6)

    assert result == (units[1],)
    assert len(result[0].results) == 2


# --- L3-IMP-4: structural assertion ---------------------------------------


def test_evict_oldest_signature_cannot_receive_the_pinned_prefix() -> None:
    """`L3-IMP-4`: asserted on `evict_oldest`'s own signature, not on its
    behaviour — there is no parameter through which a Pinned Prefix (or
    any of its messages) could reach this function, so it cannot be
    evicted by a bug here even in principle."""
    params = inspect.signature(evict_oldest).parameters
    assert set(params) == {"units", "estimate", "budget_tokens"}
    for name in ("prefix", "pinned_prefix", "opening_messages"):
        assert name not in params
