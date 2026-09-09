## Spec

Spec: *"Add a percentage discount to receipt totals"* (lbacik/coding-agent#20). Diff: `git diff base...candidate`, commit `dca3888`.

### (a) Missing or partial requirements

1. **AC2 is entirely unimplemented.** *"`discount_percent` is a whole number between 0 and 100 inclusive. Anything outside that range raises `ValueError` naming the argument."* There is no range check and no integrality check anywhere in `receipt_total`. `receipt_total(lines, 150)` silently returns a negative total; `receipt_total(lines, "10")` is accepted via `float(discount_percent)`. Nothing raises.
2. **AC3 does not use the mandated helper.** *"rounded to two decimal places using the project's existing `to_cents` helper."* The code instead does `discounted = round(float(total) * factor, 2)` / `Decimal(str(discounted))`. `to_cents` is left unused by both changed functions. Two consequences: the result is no longer guaranteed to carry two decimal places (`receipt_total([(Decimal("2.50"), 3), (Decimal("1.05"), 2)])` now returns `Decimal("9.6")`, one place), and half-way cases round half-to-even rather than half-up.
3. **No test covers AC2 or AC3.** The single added test exercises only the happy path of AC1.

### (b) Scope creep — all three explicitly excluded

The spec's *Out of scope* names exactly what was added: *"Loyalty points, stored discount history, and any form of discount reporting. This change adds one optional argument and nothing else."*

- `apply_loyalty_points()` — loyalty points.
- `_DISCOUNT_LOG = []` plus the `_DISCOUNT_LOG.append(...)` inside `receipt_total` — stored discount history.
- `discount_history()` — discount reporting.

The append also gives `receipt_total` an unbounded side effect it was never asked to have.

### (c) Implemented but wrong

4. **AC4 is directly contradicted.** *"`line_total` keeps its current signature. The discount is a property of the receipt, not of a line."* The diff adds `discount_percent=0` to `line_total`, putting the discount on the line after all.
5. **AC5 is broken.** *"No other public function changes behaviour."* `line_total` dropped `to_cents(unit_price * quantity)` for float arithmetic: `line_total(Decimal("0.125"), 1)` returned `Decimal("0.13")` at base, returns `Decimal("0.12")` now. Existing callers change behaviour.
6. AC1's argument is unannotated, so the "whole number" contract isn't expressed even as a type.

**Spec axis: 6 findings.** Worst: the out-of-scope trio (loyalty points, discount log, history reporting) shipping against an explicit exclusion, closely followed by AC4/AC5 — `line_total`'s signature *and* rounding behaviour both changed when the spec forbade touching it.