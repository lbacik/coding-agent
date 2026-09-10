# THROWAWAY prototype — the pinned prefix under a filling context

This directory answers [issue #25](https://github.com/lbacik/coding-agent/issues/25)
and nothing else. It is **not** the S3 node, it writes no production code, and
it adds nothing to the graph. It lives only on the branch
`prototype/s3-context-pinning`; `main` keeps the decision, not the harness.

It is the third of this map's prototypes, after
[`s3-implement-tdd`](https://github.com/lbacik/coding-agent/tree/prototype/s3-implement-tdd/prototypes/s3-implement-tdd)
(#22) and
[`s3-provider-contract`](https://github.com/lbacik/coding-agent/tree/prototype/s3-provider-contract/prototypes/s3-provider-contract)
(#24), and it inherits from both: #22's rule that a verdict comes from the test
runner's exit code and never from the model's account of itself, and #24's
finding that the OpenAI pin runs on the Responses API, where the assistant turn
carries a `reasoning` block the provider expects to see again.

## The question

`L3-IMP-4` says skill instructions and the Attempt Header are **never compacted
or summarised**. Since [ADR 0008](../../docs/adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)
that is a correctness claim, not a housekeeping one: the Header carries the Seam
Set, which is the referent answering `tdd`'s *"confirm them with the user"*. If
it goes, the model is reading an instruction that points at nothing — and #22
established what happens then, with all three arms writing tests at an
unconfirmed seam and none of them saying so.

`L3-IMP-5` says an oversized tool result becomes head + tail + a pointer, and
the model **can read a chosen slice**. Nobody has watched a model want one.

## One measurement taken before any code was written

The pinned material is not big. `implement/SKILL.md` is 433 bytes, `tdd` 3 549,
`codebase-design` 6 446 — **10 428 bytes of skill, about 2.6K tokens**, or 19 340
with every companion file. Against a 200K window the whole pinned prefix is
around 1.5%. The ticket's third question — what happens when the pinned material
alone approaches the window — is therefore not a scenario that occurs; it needs
a guard, not a prototype. The guard is `PinnedMaterialTooLarge` in
[`context.py`](context.py), and the rule that it refuses rather than dropping
part of the prefix is checked without spending a model call.

## The two arms

| Arm | How the conversation is compacted | Why |
| --- | --- | --- |
| **KEEP** | The pinned prefix lives outside the evictable region; eviction drops **whole exchange units** from the oldest end. | The candidate design. "Never compacted" becomes a property of the data structure rather than a rule some future compactor has to keep obeying. |
| **TAIL** | One flat message list, truncated to its last `N`. | The control. It is the loop everybody writes first, and it is the only way to see what the structural version is actually buying. |

The arms differ in **one** thing. Same task, same generated workspace, same
toolset, same budget, same threshold, same pin. Both run on **both pins**,
because compaction is history rewriting by another name and
[ADR 0010](../../docs/adr/0010-the-assistant-turn-is-carried-back-verbatim.md)
says the Responses turn does not survive being rebuilt.

`TAIL`'s failure is not hypothetical and is not left to luck to demonstrate:
`context.py` asserts both halves of it without a model — the system message is
dropped once the tail is shorter than the history, and a tool result is orphaned
from its calling turn **whenever the cut lands mid-unit**, which is a coin flip
on where the boundary falls. Intermittent is worse than always: it makes the
bug look like the model's fault.

## The threshold is set low on purpose

[Contract §5](../../docs/contract/v1-runtime-contract.md) makes the compaction
threshold a tuning constant, so this runs at **12 000 tokens** rather than at a
real window. The machinery under test is identical and it costs a hundredth as
much. The trigger is driven by the **provider's own count of the previous
prompt**, not by a local estimate, because #24 measured the two pins counting an
identical prompt at 6 818 and 4 414 tokens — one estimate is two budgets, which
is why §5 already says a token ceiling is one set per Pinned Model.

## The slice question is asked without a hint

`orders/pricing.py` in the generated workspace is ~541 KB and
`apply_discount` — the one symbol the Seam Set names — sits at character
270 206, dead centre, reachable by neither the head nor the tail of a capped
result. So the **motive** to read a slice is real. The **prompt** is absent:
`read_slice` is in the toolset with an ordinary docstring, the truncation notice
names the artifact, and nothing anywhere tells the model to use it. Wrong routes
stay open — it can rewrite the file wholesale, or append a shadowing definition.
That is the honest form of the question #22 asked about `read_skill_resource`
and answered with *never called by any arm*.

## The recall probe

The run ends with one extra turn asking the model to state the Seam Set
identifier back. `SEAM-7Q4M` appears nowhere but the Attempt Header, so
recalling it is evidence the Header was still in the request — and failing to
recall it, without saying so, is the silent failure this ticket exists to rule
out. It is asked **after** the work is finished so it cannot steer the run it
is measuring.

## What the toolset deliberately lacks

- **No commit tool.** [ADR 0002](../../docs/adr/0002-commit-and-push-before-review.md)
  puts the commit after this node; #22 already recorded how the conflict presents.
- **No search or grep.** Not to force the slice — `run_tests` and a wholesale
  rewrite are both still open — but so that reading the middle of a large file
  is a thing the model has to decide to do rather than a thing it falls into.
- **No way to ask a user anything.** The Attempt Header is the confirmation, per
  ADR 0008. Whether the model accepts it as one is part of what is being watched.

## Running it

```sh
SCRIPT=context.py ./run.sh     # the compaction rules, no model calls, free
./run.sh                       # both arms, both pins
./run.sh --arms KEEP --providers openai
```

Keys come from the repository `.env`. Transcripts, the per-turn measured prompt
size, the compaction event log, every truncation and every slice read land in
[`transcripts/`](transcripts).

---

## What it found

| Arm | Pin | Threshold | Turns | Compactions | Slice reads | Seam recalled | pytest | Provider |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KEEP | anthropic | 12 000 | 23 | 8 | 5 | yes | `4 passed` | clean |
| KEEP | openai | 20 000 | 18 | 0 | 3 | yes | `1 passed` | clean |
| KEEP | openai | 6 000 | 30 | **14** | 6 | yes | `1 passed` | clean |
| TAIL | anthropic | 20 000 | 14 | 2 | 8 | **no** | `no tests` | **400** |
| TAIL | openai | 20 000 | 23 | 0 | 15 | yes | `1 passed` | clean |

`TAIL`/openai never crossed its threshold, so it says nothing about the trap; it
is in the table because leaving it out would imply a result it does not have.
The OpenAI pin counts the same prompt lower than Anthropic (#24: 6 818 vs
4 414), which is why the 6 000 row exists at all.

### 1. The pinned prefix survives, and the recall probe says so

`first_role_sent` is `system` on **every turn of every KEEP run**, including the
one that compacted 14 times, and every recall probe came back with `SEAM-7Q4M`
and the symbol. The structural claim holds — but the run is corroboration, not
the assertion, for the reason the next paragraph gives.

### 2. The two halves of the naive policy behave oppositely

`TAIL`/anthropic is the whole argument for [ADR 0011](../../docs/adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md):

- **Step 13** dropped the system message. `first_role_sent` became `human` and
  **the request succeeded** — 17 920 tokens counted, model answered, loop
  continued. One entire turn ran with no Attempt Header, and nothing said so.
- **Step 14** orphaned a tool result, and *that* the provider refused:
  `400 … unexpected tool_use_id found in tool_result blocks`.

The loud failure is the harmless one. Losing the Seam Set is the silent one.

### 3. Whole-unit eviction survives the Responses reasoning chain

The open worry from [ADR 0010](../../docs/adr/0010-the-assistant-turn-is-carried-back-verbatim.md)
was that compaction is history rewriting by another name. Fourteen compactions
on the OpenAI pin, zero provider errors: **evicting whole units is not
rewriting**, and the distinction the ADR draws holds on the wire.

### 4. `L3-IMP-5` is not vacuous — the pointer is the affordance

`read_slice` was called **unprompted in every single run**, the first within two
turns, 5–15 times per run, with ranges the model chose. This is the opposite of
[#22](https://github.com/lbacik/coding-agent/issues/22)'s `read_skill_resource`,
which no arm ever called. The difference is where the offer sits: a companion
file is named in a skill the model read once, while the truncation notice names
the artifact **in the result the model is reading, at the moment it wants more**.

### 5. What none of this was asked to find

Three runs converged to a green suite. Two of them are wrong.

- **KEEP/anthropic** replaced the 541 KB `pricing.py` with 1 285 characters —
  `14412` lines to `39`, ~900 legacy functions deleted — and reported that the
  rewrite *"preserves their exact names and behavior"*. It does not.
- **KEEP/openai at 6 000** never touched `apply_discount`. It added an
  `orders/__init__.py` that imports the module and **rebinds the symbol at
  import time**, so the Seam Set's own function still returns the wrong number
  to anyone who imports it directly — and the replacement silently drops the
  `round`.
- **KEEP/openai at 20 000**, the only run that never compacted, made the exact
  one-line change and nothing else.

n=1 per cell, so this prototype cannot claim compaction *causes* worse work.
What it can say is that a green validation is not evidence of a correct diff,
that the implementer's closing summary asserted a preservation the diff refutes,
and that both facts arrived here independently of the questions asked.

### 6. `run_tests` is an arbitrary code execution channel

KEEP/openai at 20 000 fixed `pricing.py` **without ever calling `write_file` on
it**. It wrote a "test" whose module level was:

```python
pricing = Path("orders/pricing.py")
pricing.write_text(pricing.read_text().replace(
    "return round(price * discount, 2)",
    "return round(price * (1.0 - discount), 2)"))
```

then ran pytest — which imported the file and performed the edit — then replaced
the test file with a clean version. The final diff is correct and shows no trace
of how it was made.

This is not a compaction finding and it is bigger than one. Every guarantee this
project holds *structurally on the toolset* — `L3-IMP-9`'s "no such tool
exists", `L3-REV-2`'s read-only reviewers — is only as strong as the absence of
a tool that runs project code. A test runner is such a tool, and the implementer
must have one. It became [#27](https://github.com/lbacik/coding-agent/issues/27).
