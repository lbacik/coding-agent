# Standards Review — `dca3888 Add a percentage discount to receipt totals`

Diff: `git diff base...candidate`. Sources: `CODING_STANDARDS.md`; smell baseline.

## Hard violations (documented standards)

**`src/receipts/__init__.py` — `line_total`**

```python
factor = 1.0 - (float(discount_percent) / 100.0)
return Decimal(str(round(float(gross) * factor, 2)))
```

1. **Rule 1 (Money is `Decimal`, never `float`)** — violated twice: `float(gross)` puts a monetary value through binary float, and `factor` is a float factor applied to money. The rule explicitly extends to "any factor applied to one."
2. **Rule 2 (Rounding is explicit)** — `round(..., 2)` is the banned built-in on a monetary value; must be `to_cents` / `.quantize(Decimal("0.01"), ROUND_HALF_UP)`. The `Decimal(str(...))` round-trip does not repair the already-lost precision.
3. **Rule 3 (full annotation)** — `discount_percent=0` has no type annotation on a public function.

**`src/receipts/__init__.py` — `receipt_total`**

```python
factor = 1.0 - (float(discount_percent) / 100.0)
discounted = round(float(total) * factor, 2)
_DISCOUNT_LOG.append((discount_percent, discounted))
return Decimal(str(discounted))
```

4. **Rules 1 and 2** — same float factor, `float(total)`, and banned `round()`.
5. **Rule 3** — `discount_percent=0` unannotated.
6. **Rule 4 (no module-level mutable state)** — `_DISCOUNT_LOG = []` is a module list mutated at runtime by `.append(...)`; `discount_history()` hands the mutable list out.
7. **Rule 3** — `def discount_history():` has neither parameter-free annotation nor a return type.
8. **Rule 5 (invalid input raises `ValueError`)** — no validation of `discount_percent`. A value of `150` silently produces a negative total and `-50` silently inflates it; the rule forbids silent clamping/sentinels, and by extension requires a `ValueError` naming `discount_percent`.

Note also that `line_total`'s new `discount_percent` parameter is never passed by `receipt_total` (which calls `line_total(unit_price, quantity)`), so line-level discounting is dead on arrival.

## Judgement calls (baseline smells)

- **Duplicated Code** — the discount shape `factor = 1.0 - (float(discount_percent) / 100.0)` followed by a rounded multiply appears in both `line_total` and `receipt_total`. Extract one `apply_discount(amount: Decimal, discount_percent: Decimal) -> Decimal` and call it from both.
- **Primitive Obsession** — `discount_percent` as a bare number for a domain concept with an invariant (0–100). A small `Percentage` type would carry the validation Rule 5 demands.
- **Divergent Change / Speculative Generality** — `apply_loyalty_points` and `discount_history` are unrelated to "add a percentage discount"; loyalty redemption is unused and untested, and the history accessor exists only to expose the state Rule 4 bans. Delete both until a real need shows.
- **Mysterious Name** — `factor` says nothing of what it scales; `discount_multiplier` would.