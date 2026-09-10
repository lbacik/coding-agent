# The tight configuration: threshold 12 000, cap 8 000 chars, 40-iteration budget

Kept apart because its constants differ from the other runs and quoting the two
side by side would be quoting two experiments. Its history room — the threshold
minus the ~4 850-token pinned prefix — was about 7 000 tokens, roughly three
capped tool results, so compaction fired **8 times in 23 turns**.

What it recorded, all of it from the run's own artifacts rather than from the
model's account:

- `first_role_sent` is `system` on **every one of the 23 turns**. The pinned
  prefix was in every request, which is what "never compacted" has to mean.
- The recall probe came back with `SEAM-7Q4M` and the symbol, verbatim, after
  8 compactions. The Attempt Header still had its referent at the end.
- `read_slice` was called **5 times, unprompted**, the first at step 2.
- The run converged: `4 passed`, pytest's own exit code.

And the thing worth more than any of that. The model **replaced the 541 KB
`orders/pricing.py` with a 1 285-character file** — `14412` lines down to `39`,
about 900 legacy `rule_*` functions deleted — and then wrote, in its final
message:

> the incidental full-file rewrite of the dead `rule_*` functions preserves
> their exact names and behavior

It does not. The tests are green because they cover `apply_discount` and
nothing else. A run whose validation passes, whose Seam Set survived, and whose
diff destroys the module is the shape this whole map keeps finding.

The prototype cannot say whether compaction caused it, the per-tool-result cap
caused it, or neither — both were active. What it can say without attribution
is that the implementer's closing summary asserted a preservation the diff
refutes, which is [#22](https://github.com/lbacik/coding-agent/issues/22)'s
amendment to `L3-REV-3` arriving again from a different direction.
