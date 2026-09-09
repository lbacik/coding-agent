Fixed point `base` resolves; diff is non-empty (2 files). Spec resolved from the commit's originating issue (provided in full). Standards source: `CODING_STANDARDS.md`, plus the smell baseline. Both axes ran in parallel.

## Standards

**Hard violations of documented standards**

1. **Rule 1 (Money is `Decimal`, never `float`)** — breached in both changed functions. `line_total`: `factor = 1.0 - (float(discount_percent) / 100.0)` and `float(gross) * factor`. `receipt_total`: `discounted = round(float(total) * factor, 2)`. The rule explicitly covers "no factor applied to one", so the `factor` computation is itself a breach, not just the multiplication. The `Decimal(str(...))` round-trip does not repair the precision already lost.
2. **Rule 2 (Rounding is explicit; `round()` banned on monetary values)** — breached twice: `round(float(gross) * factor, 2)` and `round(float(total) * factor, 2)`. The repo's own `to_cents` helper is now bypassed in both places, and `round()` is half-to-even, so results diverge from `ROUND_HALF_UP` at exact halves.
3. **Rule 3 (Every public function fully annotated)** — `discount_percent=0` is unannotated in both `line_total` and `receipt_total`; `discount_history()` has neither parameter nor return annotation.
4. **Rule 4 (No module-level mutable state)** — `_DISCOUNT_LOG = []` is a module-level list mutated at runtime by `_DISCOUNT_LOG.append(...)`. `discount_history` also hands the live list to callers, so external code can mutate it too.
5. **Rule 5 (Invalid input raises `ValueError` naming the argument)** — no validation of `discount_percent`. `receipt_total(lines, 150)` silently returns a negative total; a non-integer or negative percent is silently accepted. The existing `quantity` guard shows the expected shape.

**Baseline smells (judgement calls)**

- **Duplicated Code** — the factor shape appears in both functions: `factor = 1.0 - (float(discount_percent) / 100.0)`. One discount calculation, extracted, would serve both (and give the validation a single home).
- **Primitive Obsession** — `discount_percent` as a bare number carries an invariant (whole number, 0–100) that nothing enforces; a small `Percentage` type would make rule 5 unavoidable.
- **Speculative Generality** — `apply_loyalty_points` and `discount_history` add capability no requirement asks for; both should be deleted rather than annotated.
- **Divergent Change** — the module now changes for three unrelated reasons: receipt arithmetic, loyalty redemption, and discount audit logging.

*Findings: 5 hard violations, 4 judgement calls.*

## Spec

**(a) Requirements missing or partial**

- AC2: *"`discount_percent` is a whole number between 0 and 100 inclusive. Anything outside that range raises `ValueError` naming the argument."* Entirely absent — there is no range check, no integer check, and no `ValueError`. `receipt_total(lines, 150)` returns a negative Decimal.
- AC3: *"The discounted total is rounded to two decimal places using the project's existing `to_cents` helper."* Partial at best — the code rounds to two places, but via `round(float(...), 2)`, not `to_cents`. The named helper is no longer called by either changed function.

**(b) Behaviour not asked for (scope creep)**

- AC4: *"`line_total` keeps its current signature. The discount is a property of the receipt, not of a line."* Violated directly — `line_total` gained a `discount_percent=0` parameter and applies it. This is the requirement the spec bolded pre-emptively.
- Out of scope: *"Loyalty points, stored discount history, and any form of discount reporting."* All three shipped: `apply_loyalty_points`, `_DISCOUNT_LOG`, and `discount_history`.
- Out of scope: *"This change adds one optional argument and nothing else."* The diff adds two optional arguments, two public functions, and a module-level accumulator.

**(c) Implemented but looks wrong**

- AC1: *"applies `discount_percent` to the summed total. `discount_percent` defaults to `0`."* The default and the happy path are right, and the new test (`10.00` at 10% → `9.00`) passes, but the arithmetic runs through `float`, so results are not reliably exact at the cent and half-cent cases round the wrong way relative to the rest of the module.
- AC5: *"No other public function changes behaviour."* Broken twice over. `line_total` changed signature *and* its rounding path (previously `to_cents`, now `round(float(...))`), so its output can differ at exact halves even when no discount is passed. `receipt_total` also now mutates process-global state as a side effect of being called.
- `discount_history` returns the mutable `_DISCOUNT_LOG` by reference, so any caller can corrupt the log — a defect in a function that shouldn't exist.

*Findings: 2 missing/partial, 3 scope-creep, 3 wrong.*

---

**Summary:** Standards — 9 findings (5 hard, 4 judgement); worst: money and discount factors computed in `float` and rounded with `round()`, breaching rules 1 and 2 and bypassing `to_cents`. Spec — 8 findings; worst: AC4 explicitly forbade changing `line_total`'s signature, and the diff changes it *and* its rounding behaviour, which also breaks AC5.