## What is asked for

`shout(name)` should accept an optional number of exclamation marks.

## Acceptance criteria

- [ ] `shout(name, exclamations=1)` ends the shouted greeting with exactly `exclamations` exclamation
  marks: `shout("world", exclamations=3)` returns `"HELLO, WORLD!!!"`.
- [ ] `shout` keeps its current result when `exclamations` is not given: `shout("world")` returns
  `"HELLO, WORLD!"`.
- [ ] `exclamations` must be an integer between 1 and 5 inclusive. Any other integer raises
  `ValueError` whose message names the argument.
- [ ] `greet` keeps its current signature and behaviour. The change is made in `shout`, not in
  `greet`.

## Out of scope

Configurable greeting words, localisation and any other punctuation. This change adds one
optional argument to `shout` and nothing else.
