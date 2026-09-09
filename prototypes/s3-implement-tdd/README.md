# THROWAWAY prototype — `/implement` with `/tdd` nested

This directory answers [issue #22](https://github.com/lbacik/coding-agent/issues/22)
and nothing else. It is **not** the S3 node, it writes no production code, and
it adds nothing to the graph. It lives only on the branch
`prototype/s3-implement-tdd`; `main` keeps the decision, not the harness.

It is the sibling of [`prototypes/s4-review-fanout`](https://github.com/lbacik/coding-agent/tree/prototype/s4-review-fanout/prototypes/s4-review-fanout),
which answered the same underlying unknown from the reviewer's side.

## The question

Driven from our own bounded tool loop with explicit activation, does
`/implement` with `/tdd` nested actually run — both present in the activation
registry, neither activating twice, companion files served through
`read_skill_resource`?

## The three arms

| Arm | What the implementer receives | Why |
| --- | --- | --- |
| **A** | `implement/SKILL.md`, plus `activate_skill` whose description names the one skill this step may activate (`tdd`) | What the contract requires |
| **B** | `implement/SKILL.md` **and** `tdd/SKILL.md`, both injected up front, and **no activation tool at all** | The control that separates *"the model would not ask"* from *"asking would not have changed the run"* |
| **C** | `implement/SKILL.md`, plus `activate_skill` that takes **any** name and says nothing about what exists | Maps what the model reaches for unprompted — question 5's discovery half |

Arm B is the control that earns its place. If arm A never activates `tdd`, that
alone does not say whether explicit activation is broken: the run might have
looked the same either way. B holds the skill's *content* constant and removes
only the act of asking for it, so the two arms disagree on exactly one thing.

Every arm runs over an identical fresh clone of the same base revision, with an
identical budget (30 iterations) and an identical toolset apart from the
activation tools that define the arm.

## What the toolset deliberately lacks

- **No commit tool.** `implement/SKILL.md` ends *"Commit your work to the
  current branch"*, and [ADR 0002](../../docs/adr/0002-commit-and-push-before-review.md)
  puts the commit before review, where the graph owns it. Question 6 asks how
  the conflict presents, so the model must be able to *reach* for a commit and
  miss.
- **No typechecker**, though the skill says to run one regularly. A second,
  low-stakes instance of "the skill says do X and there is no X", to compare
  against the high-stakes one below.
- **No `code-review`.** It belongs to a node that does not exist yet while the
  implementer runs. `activate_skill("code-review")` is refused — this is
  `L3-REV-3`'s structural assertion meeting an actual model (question 3).
- **No way to ask a user anything.** `tdd` says *"No test is written at an
  unconfirmed seam"* and there is nobody to confirm with. The ticket says to
  **record** what the model does and not to design the gate here, so the
  harness offers no gate to design.

Each of these shows up in the record the same way: an `unknown_tool_calls`
entry, or a refusal from the registry.

## The registry, probed without a model

`registry.py` holds the activation rules on their own, and running it directly
checks all nine of them for free — including the refusals a model might never
happen to trigger:

```sh
python3 registry.py .scratch/bundle
```

## The skills go in verbatim

`mattpocock/skills@3cca18b368ae95cdbdebbff572ccafa662551015`, the same pin the
image's Skill Bundle uses. [ADR 0007](../../docs/adr/0007-upstream-skill-text-is-answered-never-rewritten.md)
binds this prototype: no normalisation, no trimming — least of all of the
`/code-review` line, which is the one thing question 3 exists to observe.

## The task

Made in the sandbox repository, so this needs neither S2 nor S3:

- Base revision: `prototype/s4-review-base` on `lbacik/coding-agent-sandbox` — a
  small `receipts` package with a real `CODING_STANDARDS.md` and three tests
- Spec: [sandbox issue #5](https://github.com/lbacik/coding-agent-sandbox/issues/5),
  an optional percentage discount on `receipt_total`

The base does **not** contain the discount, so this is real work with a real
seam to agree on. It is the same pair `s4-review-fanout` reviewed, from the
other end, which keeps the two prototypes comparable.

## Running it

```sh
./run.sh                 # all three arms
./run.sh --arms A        # one of them
```

Needs `ANTHROPIC_API_KEY` in the environment (or in the repository's `.env`).

## What is recorded

Per arm, in `transcripts/arm-<A|B|C>.json`:

1. **`activations`** — every activation attempt, granted or refused, with the
   reason and the step. This is `L3-IMP-1` and `L3-IMP-3` as observed rather
   than asserted.
2. **`resource_reads`** — every `read_skill_resource` call, the path the model
   asked for and the path it resolved to inside the skill's own directory
   (`L3-IMP-2`), so a mapping the model gets wrong is visible as a mapping.
3. **`unknown_tool_calls`** — every reach for a tool that is not there: the
   commit, the typechecker, the missing user.
4. **`test_runs` and `red_before_green`** — the loop's own fingerprint, taken
   from what actually ran rather than from what the model said it did.
5. **`claimed_activation_signals`** — the implementer's version of arm C in
   `s4-review-fanout`: text that claims a skill was applied, to be read against
   the registry, which knows whether it was.
6. **`seam_signals`**, **`commit_signals`**, **`review_signals`** — questions 6,
   3 and the collision the ticket forbids resolving.
7. **`arm-<X>.diff`** — the work itself, so the run can be judged on whether it
   produced the change at all.

## What it found

Explicit activation works, and works without being told what to activate: arm C,
whose tool named no skills at all, still turned *"Use /tdd where possible"* into
`activate_skill("tdd")` and then ran a real red→green loop — real because
`red_before_green` is read off the pytest exit codes, not off what the model
said it did. All three arms produced a correct, standards-respecting change.

What the run did **not** produce is any exercise of `read_skill_resource`. No
arm followed `[tests.md](tests.md)`, by that tool or any other, so `L3-IMP-2`
has a mechanically-checked registry rule behind it and no observed model
behaviour. Same for `codebase-design`, which nothing reached for.

And in every arm, including the one with no activation tool at all, the model
answered the unreachable `/code-review` line by **reviewing its own work** and
saying so. It also described the seam as *agreed* when nobody had agreed it.
The full answer is the resolution comment on
[issue #22](https://github.com/lbacik/coding-agent/issues/22).
