All green. Self-review of the change (the `code-review` skill isn't activatable in this step, so I reviewed against the repo's `CODING_STANDARDS.md` and the ticket's criteria):

**Against the acceptance criteria**
1. `receipt_total(lines, discount_percent)` applies the discount to the summed total; default `0` — covered by the pre-existing `test_receipt_total_sums_lines` plus `test_receipt_total_applies_discount_percent`.
2. Range `0..100` inclusive, whole numbers only; out-of-range and non-whole inputs raise `ValueError` whose message names `discount_percent`. Ends of the range are pinned by a test (`100` → `0.00`).
3. Rounding goes through the existing `to_cents`; the test uses `0.13 − 50% = 0.065 → 0.07`, a case where half-up disagrees with `round()`'s half-to-even, so the standard's banned-`round()` rule is actually enforced by the test.
4. `line_total` is untouched — signature and body identical in the diff.
5. `to_cents` unchanged; no other public function's behaviour moves.

**Against the coding standards**
- The discount factor is built as `(_PERCENT - discount_percent) / _PERCENT` with `_PERCENT = Decimal(100)`, so no float ever touches the money path. The `isinstance(..., int)` guard is what keeps that true: without it, `receipt_total(lines, 12.5)` raised a bare `TypeError` from inside the arithmetic (I saw exactly that in the red run), which violates standard 5's "raise `ValueError` naming the argument". It also rejects whole-valued floats like `15.0`, deliberately — the annotation says `int` and standard 1 keeps floats away from money factors.
- `_PERCENT` is an immutable module constant, permitted by standard 4.
- Both parameters and the return type are annotated (standard 3).

**Out of scope stayed out**: no loyalty points, no stored history, no reporting — one optional argument and its validation.

**Process notes**
- Worked in vertical slices at the single agreed seam, the public `receipt_total`: discount applied → red/green; range rejection → red (`DID NOT RAISE`) → green; whole-number rejection → red (`TypeError` ×2 and one silent acceptance) → green. The rounding test passed on first run since `to_cents` was already in the path; I kept it as a specification test rather than deleting it, because it's the only thing preventing a future switch to `round()`. Expected values are worked literals from the spec, not recomputations of the implementation.
- Full suite run at the end: 11 passed.
- One caveat: there's **no typechecker configured** in this repo (`pyproject.toml` dev deps are `pytest` only), so I couldn't run mypy — the `# type: ignore[arg-type]` on the deliberately ill-typed test arguments is there for whenever one is added.
- I could **not commit**: this step's toolset has `git_diff`/`git_status` but no command execution or commit tool, so the work is staged in the working tree on `work` for the next step to commit.