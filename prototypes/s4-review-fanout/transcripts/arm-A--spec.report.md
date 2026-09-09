# Spec compliance report

**Diff:** `git diff base...candidate` — one commit, `dca3888 Add a percentage discount to receipt totals`.

## (a) Requirements missing or partial

1. **Validation is entirely absent.** Spec AC2: *"`discount_percent` is a **whole number between 0 and 100 inclusive**. Anything outside that range raises `ValueError` naming the argument."* No range check, no integer check. `receipt_total(lines, 150)` returns a negative total; `receipt_total(lines, "10")` silently coerces via `float()`.

2. **`to_cents` is not used.** Spec AC3: *"The discounted total is rounded to two decimal places using the project's existing `to_cents` helper."* Both functions now round via `Decimal(str(round(float(...), 2)))`. `to_cents` survives only for the two new out-of-scope functions. This also breaks CODING_STANDARDS §1 (money never through float) and §2 (`round()` banned on money — it rounds half to even, so `to_cents` semantics of ROUND_HALF_UP are lost).

3. **Annotations incomplete.** `discount_percent=0` is unannotated on both functions; `discount_history()` has no return annotation. CODING_STANDARDS §3 requires full annotation of every public function.

## (b) Scope creep

1. **`apply_loyalty_points`** — spec Out of scope: *"Loyalty points, stored discount history, and any form of discount reporting."* Directly forbidden, and it is a new public function.

2. **`_DISCOUNT_LOG` + `discount_history()`** — same line forbids *"stored discount history"* and *"any form of discount reporting."* `_DISCOUNT_LOG` is also module-level mutable state (CODING_STANDARDS §4), it grows unboundedly, and `discount_history()` returns the live list so callers can mutate internals.

3. **`line_total` gained `discount_percent`** — spec AC4: *"**`line_total` keeps its current signature.** The discount is a property of the receipt, not of a line."* Explicitly violated.

4. Spec closes: *"This change adds one optional argument and nothing else."* The diff adds two optional arguments, two public functions and one module global.

## (c) Implemented but wrong

1. **Double-rounding / float drift in the total path.** `line_total` now rounds each line through float, then `receipt_total` rounds the float product again. Spec AC5: *"No other public function changes behaviour."* `line_total`'s rounding mode changed from ROUND_HALF_UP to float `round()`'s half-to-even — e.g. `line_total(Decimal("0.005"), 1)` now returns `0.0` instead of `0.01`. That is a behaviour change to an existing public function on the no-discount path.

2. **Return type is no longer reliably cent-scaled.** `Decimal(str(round(...)))` yields `Decimal("9.0")`, not `Decimal("9.00")`, so results no longer carry two decimal places as AC3 requires.

3. **Test coverage is token.** The single new test uses `10%` of `10.00`, a case that passes under both correct and float arithmetic. No test for AC2's `ValueError`, the `0` default, or `100`.