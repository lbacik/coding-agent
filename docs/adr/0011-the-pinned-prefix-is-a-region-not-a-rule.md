---
status: accepted
---

# The pinned prefix is a region eviction cannot address, and compaction never rewrites a turn

When the implementer's conversation crosses the compaction threshold, the Worker **evicts whole Exchange Units from the oldest end** and never summarises, rewrites or partially drops anything. The Attempt Header and the injected skill files are not protected by a rule the compactor obeys; they live in a **Pinned Prefix** the eviction code has no access to. Decided in [Can the Attempt Header and skill instructions survive a filling context, and does anyone read a slice of a capped tool result?](https://github.com/lbacik/coding-agent/issues/25).

## The observation

Two policies were run over the identical task, workspace, toolset and budget, differing only in how the conversation was compacted.

**KEEP** put the pinned material outside the evictable region and evicted whole units. Across 23 turns and 8 compactions on the Anthropic pin, the first message sent was the system message on **every single turn**, and the recall probe at the end returned the Seam Set identifier and its symbol verbatim. On the OpenAI pin, where the assistant turn carries the `reasoning` block [ADR 0010](0010-the-assistant-turn-is-carried-back-verbatim.md) requires be sent back, eviction of whole units was accepted by the provider without complaint.

**TAIL** kept one flat message list and truncated it to its last N. It failed twice, and the two failures are the whole reason this is an ADR:

- At the first compaction the **system message was dropped, and the request succeeded.** The provider accepted it, the model answered, the loop continued. The Attempt Header — and with it the Seam Set that answers `tdd`'s *"confirm them with the user"* — was simply gone, and nothing anywhere said so.
- At the second the cut landed mid-unit and orphaned a tool result, and **that** the provider refused outright: `400 … unexpected tool_use_id found in tool_result blocks`.

So the loud half of the naive policy is the harmless one. The half that costs correctness is the half that says nothing.

Whether the cut lands mid-unit is a coin flip on where the boundary happens to fall — checked without a model, `keep=6` aligns cleanly and `keep=7` orphans. Intermittent is worse than always: a bug that appears in half the runs is a bug that gets attributed to the model.

## Why this is an ADR and not a comment in the compactor

Because since [ADR 0008](0008-the-tdd-seam-gate-is-a-readiness-fact.md) the Attempt Header carries the **Seam Set**, and the Seam Set is the referent of an instruction in upstream text we are forbidden to edit ([ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md)). Losing it mid-node leaves the model reading *"confirm them with the user"* with nothing behind it, and [#22](https://github.com/lbacik/coding-agent/issues/22) established exactly what happens then: all three arms wrote tests at an unconfirmed seam and **not one of them said so**.

That is this map's recurring failure — an invariant whose violation leaves no trace — and it gets the same treatment as [#20](https://github.com/lbacik/coding-agent/issues/20)'s reviewer collapse, [#22](https://github.com/lbacik/coding-agent/issues/22)'s seam and [#24](https://github.com/lbacik/coding-agent/issues/24)'s dropped reasoning block: held by construction, not by obedience.

## The rule, in four parts

**1. The Pinned Prefix is a region, not a policy.** The conversation is two parts: a Pinned Prefix the eviction code cannot address, and a history of Exchange Units it can. "Never compacted" is then a fact about which list a message is in, checkable by reading the eviction function's arguments, rather than a rule some future compactor has to keep remembering.

**2. Eviction only; no summarisation, ever.** [ADR 0010](0010-the-assistant-turn-is-carried-back-verbatim.md) already forbids rebuilding an assistant turn, and a summariser rebuilds many at once. Beyond the provider requirement, a summariser is another model call whose faithfulness cannot be checked from its own output — the same shape as every failure above. Dropping a turn is honest; paraphrasing one is a claim.

**3. The eviction grain is the Exchange Unit.** An assistant turn and every tool result answering it move together or not at all. Splitting them is a malformed request on Anthropic, and on the Responses API it breaks the chain the provider expects.

**4. An oversized Pinned Prefix refuses to open the Attempt.** If the prefix alone crosses the threshold, no eviction helps: every remaining byte is one we promised not to drop. The Worker refuses rather than quietly dropping part of it — the same choice [ADR 0009](0009-provider-capability-is-established-by-attempting.md) made at startup, and for the same reason: nothing has been claimed yet, so there is nothing to strand.

## What this is not

It is not a size problem. `implement`, `tdd` and `codebase-design` are **10 428 bytes of `SKILL.md` between them** — about 2.6K tokens, or roughly 4 850 with the Attempt Header and the Target Issue — against a 200K window. Part 4 above is a guard on an invariant, not a mitigation for a scenario anyone expects to meet.

Nor is the prefix expensive to re-send. With a cache breakpoint on it, the Anthropic run billed **104 121 of its 249 654 input tokens as cache reads**, the prefix hitting cache on essentially every turn after the first; the OpenAI run, caching on its own terms, read **108 839 of 118 285**. [#24](https://github.com/lbacik/coding-agent/issues/24) measured zero cache hits without a breakpoint and predicted roughly a tenth of the rate with one. That prediction is the reason part 1 is affordable.
