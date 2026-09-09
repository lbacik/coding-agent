# THROWAWAY prototype — the `/code-review` fan-out

This directory answers [issue #20](https://github.com/lbacik/coding-agent/issues/20)
and nothing else. It is **not** the S4 adapter, it writes no production code,
and it adds no node to the graph. It lives only on the branch
`prototype/s4-review-fanout`; `main` keeps the decision, not the harness.

## The question

Given one candidate diff, does driving `/code-review` as two isolated reviewer
conversations produce two independent reports — or does a reviewer try to spawn
another pair, and does review re-enter inside the implementer?

## The two arms

| Arm | What each reviewer receives | Why |
| --- | --- | --- |
| **A** | Only its own role's instructions, sliced out of the skill by `roles.py` | What [the contract](../../docs/contract/v1-runtime-contract.md) requires |
| **B** | The whole `SKILL.md`, still addressed as "you are the *role* reviewer" | The first control |
| **C** | The whole `SKILL.md`, **no role assigned at all** | The control that actually controls — see below |

Arm B turned out not to control anything: it behaved like arm A, so its
silence proved nothing about isolation. The suspect was the sentence *"you are
the standards reviewer"*, which takes the orchestrator's seat away before the
skill can ask for it. Arm C removes that sentence and hands the skill over
exactly as a naive adapter would, from the seat where step 4 says to spawn
both sub-agents in parallel. That is the arm that found something.

Every arm runs over the identical candidate, with the identical read-only
toolset (`read_file`, `list_dir`, `git_diff`, `git_log`, `git_show`) and the
identical bounded tool loop.

## The candidate

Made by hand in the sandbox repository, so this needs neither S2 nor S3:

- Base revision: `prototype/s4-review-base` on `lbacik/coding-agent-sandbox`
- Candidate: `prototype/s4-review-candidate` on the same repository
- Spec: [sandbox issue #5](https://github.com/lbacik/coding-agent-sandbox/issues/5)

The candidate carries something for each axis: documented-standard breaches and
a duplicated-code smell for Standards, a missing requirement and unrequested
behaviour for Spec, and a rounding defect both axes could reasonably notice —
that last one deliberately, to see whether the two reports collapse into each
other.

## The skill is read at the pinned commit

`mattpocock/skills@3cca18b368ae95cdbdebbff572ccafa662551015` — the same pin the
image's Skill Bundle uses, so the Skill Bundle of S1b is not a prerequisite
either.

## Running it

```sh
./run.sh                 # arms A and B
./run.sh --arms C        # the control that matters
```

Needs `ANTHROPIC_API_KEY` in the environment (or in the repository's `.env`).
It clones the sandbox, fetches the pinned skill, and runs all four
conversations, writing transcripts to `transcripts/`.

To see the role split on its own, without spending anything:

```sh
python3 roles.py <path to the pinned SKILL.md>
```

## What is recorded

Per conversation, in `transcripts/arm-<A|B>--<role>.json`:

1. **`unknown_tool_calls`** — a reviewer reaching for a tool its toolset does
   not carry. This is the shape a fan-out attempt takes under an
   explicit-activation harness.
2. **`fanout_signals_*`** — sentences that instruct, request or describe a
   further fan-out, whether or not a tool call followed.
3. **`reentry_signals_in_report`** — the reviewer assuming it may edit the code
   it is reviewing, which is what L3-REV-3 has to rule out structurally.
4. The full transcript, so the two reports can be read side by side and judged
   separately useful or not.

## What it found

No arm attempted a fan-out, because no delegation tool existed to attempt it
with. What arm C did instead was **collapse**: one conversation reviewed both
axes, wrote both sections, aggregated them per the skill's step 5, and opened
with the sentence *"Both axes ran in parallel."* Nothing ran in parallel. The
report is shaped exactly like a correct two-axis report and is indistinguishable
from one by inspection.

So the danger the plan named — recursion — is not the danger. Silent collapse
is, and the thing that prevents it is structural (two conversations, one
toolset without delegation in it), never the wording of a prompt. The full
answer is the resolution comment on
[issue #20](https://github.com/lbacik/coding-agent/issues/20).
