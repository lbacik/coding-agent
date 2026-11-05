## What is asked for

`greet` should tolerate names with stray whitespace around them, and refuse a name that is
empty.

## Acceptance criteria

- [ ] `greet` strips leading and trailing whitespace from `name` and keeps whitespace inside it:
  `greet("  Ada Lovelace ")` returns `"Hello, Ada Lovelace!"`.
- [ ] `greet` raises `ValueError` whose message names the `name` argument when `name` is empty or
  contains only whitespace.
- [ ] `shout` gets both behaviours through `greet`: `shout("  ada ")` returns `"HELLO, ADA!"`.

## Out of scope

Normalising inner whitespace, changing letter case in `greet`, and any new argument.
