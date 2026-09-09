## Standards

Standards source: `CODING_STANDARDS.md` (5 rules). All findings in `src/receipts/__init__.py` unless noted.

### Hard violations (documented standards)

**1. Rule 1 — "Money is `Decimal`, never `float`. No monetary value, and no factor applied to one, may pass through a binary floating point type at any point."** Breached four times, both in `line_total` and `receipt_total`:

```python
factor = 1.0 - (float(discount_percent) / 100.0)
return Decimal(str(round(float(gross) * factor, 2)))
```
```python
discounted = round(float(total) * factor, 2)
```

Both the money (`gross`, `total`) and the factor go through `float`. The `Decimal(str(...))` round-trip does not repair this — the precision is already lost. The rule's "or any factor applied to one" clause names this exact construct.

**2. Rule 2 — "The built-in `round()` is banned on monetary values."** `round(float(gross) * factor, 2)` and `round(float(total) * factor, 2)`. The compliant `to_cents` helper still exists directly below and is now bypassed by both changed functions.

**3. Rule 3 — "Every public function is fully annotated."** Three misses: `discount_percent=0` is unannotated in `line_total` and in `receipt_total`; `def discount_history():` has neither params nor a return type.

**4. Rule 4 — "No module-level mutable state ... may not hold a list, dict or set that anything mutates at runtime."** `_DISCOUNT_LOG = []`, mutated by `_DISCOUNT_LOG.append(...)` in `receipt_total`. `discount_history()` compounds it by returning the live list by reference, so callers can mutate it too.

**5. Rule 5 — "Invalid input raises `ValueError` with a message naming the offending argument."** `discount_percent` is never validated. `receipt_total(lines, 150)` silently returns a negative total; `receipt_total(lines, -10)` silently marks up. `apply_loyalty_points` likewise accepts negative `points`. Contrast the existing, compliant `quantity < 0` check two lines above.

### Judgement calls (smell baseline)

- **Duplicated Code.** The `factor = 1.0 - (float(...) / 100.0)` → multiply → round shape appears verbatim in both `line_total` and `receipt_total`. Extract one discount helper (ideally a `Decimal` one, per rule 1) and call it from both.
- **Speculative Generality.** `apply_loyalty_points` and `discount_history` are unreferenced and untested. Also `line_total`'s new `discount_percent` is dead: `receipt_total` calls `line_total(unit_price, quantity)` without it, so no caller can reach it. Inline back until a need appears.
- **Divergent Change.** The module now absorbs three unrelated concerns in one commit: discounting, loyalty redemption, and audit logging.
- **Primitive Obsession.** A bare number for `discount_percent` invites the unvalidated 0–100 confusion in rule 5 above; a small `Percentage` type would carry its own bounds check.

### Note

The new test passes only by numeric coincidence: `Decimal(str(9.0))` is `Decimal("9.0")`, which compares equal to `Decimal("9.00")` while no longer being cent-scaled. It would not catch the float drift on inputs like `Decimal("0.35")`.

**Standards axis: 9 findings (5 hard violations, 4 judgement calls).** Worst: the Rule 1/Rule 2 float-and-`round()` path through the money calculations, which silently corrupts every discounted total.