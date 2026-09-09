Implementation complete. Full suite: **12 passed**.

## What I did

Built test-first at one seam — the public `receipt_total` function — in vertical slices: discount applied → range validation → whole-number validation, each red before green.

**`src/receipts/__init__.py`** — `receipt_total(lines, discount_percent=0)`:
- Sums lines as before, then scales by `Decimal(100 - discount_percent) / Decimal(100)` and passes the result through the existing `to_cents` helper.
- Rejects non-integers and out-of-range values with `ValueError` messages naming `discount_percent`.

**`tests/test_receipts.py`** — added coverage for the discount, half-up rounding of the discounted total (`10.05` less 10% = `9.045` → `9.05`, a case that would give `9.04` under banker's rounding, so the test has teeth), the inclusive `0` and `100` boundaries, and both rejection paths.

## Against the ticket and `CODING_STANDARDS.md`

All five acceptance criteria hold: `line_total`'s signature is untouched, `to_cents` does the rounding, and `receipt_total(lines)` with no argument is byte-for-byte the old behaviour. On standards: the discount factor is built from int arithmetic into `Decimal`, so no monetary value or factor touches a binary float; rounding stays delegated to `to_cents`; both new parameters and the return are annotated; no module-level mutable state was added (an earlier `_PERCENT` constant was inlined during review); invalid input raises `ValueError` naming the argument rather than clamping.

Two judgement calls worth flagging for you:

1. **Non-`int` inputs raise `ValueError`, not `TypeError`.** The ticket and standard 5 both say `ValueError`, so I followed them, though `TypeError` is the more idiomatic Python choice for a wrong type. This also means `Decimal("10")` — whole in value but not an `int` — is rejected. Deliberate, since standard 1 bans float factors and the annotation says `int`, but it is stricter than a literal reading of "whole number".
2. **Two tests passed without new implementation code** (half-up rounding and the boundaries), because earlier slices already satisfied them. I kept them as pins on acceptance criteria 2 and 3 rather than deleting them.

## Not done

**I could not commit.** My available tools here are file read/write, directory listing, `run_tests`, and `git_diff`/`git_status` — there is no shell or commit tool, so the work is left uncommitted in the working tree on `work`. Likewise I could not run a type checker: `pyproject.toml` lists only `pytest` in its dev group, and no mypy/pyright config exists, so annotation correctness is unverified beyond review by eye. The `# type: ignore[arg-type]` in the deliberately-wrong-type test is there for a checker that isn't currently configured.