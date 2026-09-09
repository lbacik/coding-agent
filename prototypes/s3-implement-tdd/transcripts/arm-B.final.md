Full suite green (9 passed). Self-review against the repo's standards, since I have no `/code-review` skill available in this step:

| Standard | Status |
|---|---|
| 1. Money is `Decimal`, never `float` | ✅ `retained` is built from `Decimal(100)` and an `int`; floats are rejected before they can touch a monetary value |
| 2. Explicit rounding, no `round()` | ✅ goes through the existing `to_cents` (criterion 3); `test_receipt_total_rounds_half_cent_up` fails if anyone swaps in `round()` |
| 3. Public functions fully annotated | ✅ `discount_percent: int = 0`, return `Decimal` |
| 4. No module-level mutable state | ✅ unchanged |
| 5. `ValueError` naming the argument, never clamp | ✅ both messages name `discount_percent`; tests assert on the name via `match=` |

Acceptance criteria 1–5 are all covered, including 4 and 5: `line_total` and `to_cents` are untouched, and the diff adds exactly one optional argument. Nothing from the out-of-scope list crept in.

## Notes and judgement calls

- **"Whole number" is enforced, not just the range.** Criterion 2's wording is about the range, but `Decimal("10.5")` or `10.5` would otherwise sail through the bounds check and then get folded into a money calculation — `Decimal(10.5)` accepts a float silently, which breaks standard 1. So a fractional percent raises `ValueError` too. Flagging it as an interpretation in case you read criterion 2 more narrowly.
- **`True` is still accepted as 1%.** `bool` is a subclass of `int`, so `isinstance` lets it through. I left it: no test demanded it and the loop says don't add speculative behaviour. It's a type confusion, not a money-safety hole. Easy to tighten if you want it rejected.
- **One test was green on arrival.** `test_receipt_total_rounds_half_cent_up` passed the moment I wrote it, because the first slice already routed the discounted total through `to_cents`. It never had a red phase — it's a regression guard for criterion 3 rather than a driver of design. Every other new test went red first for the right reason.

## Two things I could not do

- **No commit.** My tools here are file read/write, directory listing, `run_tests`, and `git_diff`/`git_status` — there's no shell or commit tool, so the work is left uncommitted in the working tree on `work`. You'll need to commit it. Note that `src/receipts/__pycache__/` and `tests/__pycache__/` are untracked and there's no `.gitignore` in the repo; don't sweep those in.
- **No typechecking.** The dev dependency group is `pytest` only — no mypy or pyright is available, so "run typechecking regularly" was not possible. I kept annotations complete by hand, and marked the deliberately ill-typed call in `test_receipt_total_rejects_fractional_discount` with `# type: ignore[arg-type]` so it won't trip a checker if one is added later. That annotation is unverified.