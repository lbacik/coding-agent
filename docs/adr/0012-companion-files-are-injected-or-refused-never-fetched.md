---
status: accepted
---

# A companion file is injected whole or it is unavailable, and no tool fetches one

The implementer's Pinned Prefix carries `tdd`'s two companion files verbatim alongside the three `SKILL.md` files. `codebase-design`'s two are neither injected nor reachable. The `read_skill_resource` tool is **removed**, because with those two decisions made it has nothing left in the Skill Bundle to serve. Decided in [Do the implementer's companion files get pre-loaded, offered at the moment of need, or left unused?](https://github.com/lbacik/coding-agent/issues/28).

## The observation this rests on

[#22](https://github.com/lbacik/coding-agent/issues/22) found that **no arm followed `tdd`'s `[tests.md](tests.md)` or `[mocking.md](mocking.md)` by any route**, so the substance of those files silently did not apply. It could not say why, and concluded only that non-use was the realistic outcome.

[#25](https://github.com/lbacik/coding-agent/issues/25) supplies the why from the opposite result. `read_slice` was called **unprompted in every run**, the first within two turns, 5–15 times per run — the same shape of offer, taken every time. The difference is **where the offer sits**: a companion file is a link inside a skill the model read once at the top of the conversation, while a truncation notice names its artifact *inside the result the model is reading, at the moment it wants more of it*. A companion file has no such moment. There is no event to hang an offer on, so "offer it at the moment of need" is not a third option; it is the link route with extra machinery.

That leaves pre-load or accept non-use, and the choice is made per file by reading what the file says.

## Why `tdd`'s two are injected

`tests.md` (2 214 B) and `mocking.md` (1 481 B) are **3 695 bytes together**, about 900 tokens on top of the ~4 850 [ADR 0011](0011-the-pinned-prefix-is-a-region-not-a-rule.md) measured, and they hit cache with the rest of the prefix. Cost is not what decides this.

What decides it is that `mocking.md` carries substance `tdd/SKILL.md` does not: the mock-at-system-boundaries list, dependency injection, and SDK-style interfaces over generic fetchers. `SKILL.md` names *"mocks internal collaborators"* as an anti-pattern and never says where the boundary is. A file whose substance is load-bearing and which nothing ever reads is not a file in the bundle; it is a fiction that the build verifies as present once per image.

`tests.md` overlaps `SKILL.md`'s prose more heavily — it is largely worked examples of the same three anti-patterns — but it grounds the tautological-test rule in a concrete before-and-after, which is the anti-pattern a model is most likely to commit and least likely to notice. At 2 214 bytes it is not worth a separate ruling.

**Every example in both files is TypeScript and jest**, while two of the three supported languages are not. The principles are language-agnostic and the idioms are not, so the Attempt Header says which is which rather than the Worker editing the files ([ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md)). Injecting only for TypeScript targets was rejected: it weakens the Python and PHP implementer in exactly the substance that carries across languages, and it makes the Pinned Prefix vary in size by target, so `L3-IMP-13`'s budget stops being one number.

## Why `codebase-design`'s two are refused

Not budget — hazard, and it is decided by reading them.

`DESIGN-IT-TWICE.md` is a human-in-the-loop session document with delegation in it: *"Show this to the user, then immediately proceed to Step 2"* and *"Spawn 3+ sub-agents in parallel."* An Attempt has neither a user nor a delegation tool. [#22](https://github.com/lbacik/coding-agent/issues/22) established what a model does with an instruction it cannot reach — every arm answered an unreachable `/code-review` by reviewing its own work, including the arm that had no activation tool and was therefore never refused anything. Injected, this file invites the implementer to run a design tournament inside its own head, inside a bounded tool loop under a 60-minute ceiling, on a ticket that asked for a bug fix.

`DEEPENING.md` says *"Old unit tests on shallow modules become waste once tests at the deepened module's interface exist; delete them."* [#25](https://github.com/lbacik/coding-agent/issues/25) already produced a run that **deleted 14 401 lines** while reporting that the rewrite *"preserves their exact names and behavior"*. Handing an unsupervised implementer a licence to delete tests is the single instruction in the bundle that most directly enables the failure the S4 fan-out exists to catch.

Refusing costs little, because `codebase-design` is by its own description *"a reference to consult, not a session to run"*, and the whole of its load-bearing content — the module / interface / depth / seam / adapter / leverage / locality glossary — is in `SKILL.md`, which is injected. The two files stay installed in the image, because S1b's verification requires every companion to be present and that check is not being weakened; they are simply never put in front of a model.

## Why the tool goes rather than merely going unused

`implement` and `code-review` carry **no companion files at all**. With `tdd`'s two injected and `codebase-design`'s two refused, `read_skill_resource` has nothing in the Skill Bundle left to serve.

Deleting it is not tidiness. This project holds its most important guarantees **structurally, on the toolset** — `L3-IMP-9`'s *"no such tool exists"*, `L3-REV-2`'s read-only reviewers. If a tool remains that resolves an arbitrary path relative to a skill's own directory, then the refusal above becomes a **rule** the tool must keep obeying — a filter someone can widen, forget, or get wrong — rather than a **region** nothing can reach. That is precisely the distinction [ADR 0011](0011-the-pinned-prefix-is-a-region-not-a-rule.md) drew for the Pinned Prefix and decided the same way. No tool, no path, nothing to filter.

This **supersedes** the arrangement settled in [#5](https://github.com/lbacik/coding-agent/issues/5) and [#7](https://github.com/lbacik/coding-agent/issues/7), where companion files loaded on demand through a single `read_skill_resource(skill, path)` resolved relative to the skill's directory. `L3-IMP-2` is retired with it, on the precedent [#24](https://github.com/lbacik/coding-agent/issues/24) set in retiring `L1-2`: a row whose mechanism the contract no longer has cannot be anything but vacuous. `L3-IMP-3` is untouched — `codebase-design` remains activatable only nested from `tdd`, which is the activation registry and a different mechanism entirely.

## The build gate this obliges

Both halves of the decision were made by **reading what four specific files say**. If the `mattpocock/skills` pin advances and one of them is reworded, renamed, or joined by a third, that reading silently expires and nobody is watching — and the failure mode is the one this map keeps meeting, an invariant whose violation leaves no trace.

So `agent verify-skill-bundle`, which already fails the build on a *missing* companion, gains a committed manifest of the exact companion set with a **digest of each file's contents**, and fails the build on an unexpected companion, a missing one, or a changed one. A pin bump touching any of the four **should** break the build, whitespace included — the same cost [ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md) accepted for `code-review`'s §3 and §4, for the same reason.

Note what is *not* digested: nothing outside the four companion files. Injection is verbatim and whole, so a reworded `SKILL.md` flows through correctly on its own; it is the *decision to inject or refuse* that has an expiry date, not the text.
