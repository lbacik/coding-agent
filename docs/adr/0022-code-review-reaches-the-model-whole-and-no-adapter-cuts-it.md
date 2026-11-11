---
status: accepted
---

# code-review reaches the model whole, and no adapter cuts it

> **Replacement design amendment:** the skill still reaches the model whole through its activation, and no adapter cuts it. [ADR 0027](./0027-workflow-policy-is-separate-from-runtime-mechanics.md) removes the Review recognition and the review gate this decision leans on (see [ADR 0021](./0021-a-review-is-the-first-sub-agent-group-after-the-code-review-activation.md)): `code-review` triggers no runtime side effect, and Sub-agents carry no review attribution ([#285 resolution](https://github.com/lbacik/coding-agent/issues/285#issuecomment-6800378643)). Pull-request and review bodies come only from a document field of a Step Result ([#284 resolution](https://github.com/lbacik/coding-agent/issues/284#issuecomment-6800063171)), so the disclosure of a Review's Sub-agents in the pull request described below is not a runtime guarantee of the replacement design. The text below records the existing MVP design.

Once `activate_code_review` returns the `code-review` skill body verbatim and the model starts its reviewers with `spawn_subagent` ([ADR 0021](./0021-a-review-is-the-first-sub-agent-group-after-the-code-review-activation.md)), the Worker no longer cuts Role Blocks, authors Seat Assignments or composes Reviewer Briefs. The model writes each reviewer's instructions from the skill's own text. The Attempt Header answers what that text refers to and an Attempt lacks. The runtime does not enforce one Sub-agent per review axis, and it discloses the Sub-agents that actually ran. This narrows [ADR 0007](./0007-upstream-skill-text-is-answered-never-rewritten.md) and reverses the choice of [#73](https://github.com/lbacik/coding-agent/issues/73) that reviewer briefs are not model-written. Decided in [What remains of the verbatim Role Block guarantee when the model writes reviewer instructions?](https://github.com/lbacik/coding-agent/issues/195).

## Context

ADR 0007 governed an adapter that knew where each reviewer axis began and ended inside `code-review/SKILL.md`. Four rules followed from that: verbatim slices, structural anchors, a bounded Seat Assignment and a build-time digest gate over the slices. The adapter no longer exists. The whole skill now reaches the model through its activation, the same way every other Skill Bundle member does.

The [fan-out prototype](https://github.com/lbacik/coding-agent/issues/20) showed what the Seat Assignment guarded against. A conversation with no seat assigned reviewed both axes in one context and then reported a fan-out that never happened, in a report that could not be told apart from a correct one.

## Decision

- **The fan-out that never happened is accepted and disclosed, not enforced.** The Run Ledger records every Sub-agent that ran, and the pull request is rendered from the ledger. A fan-out claimed without happening therefore cannot be presented. What remains possible is a collapsed review: one Sub-agent reviewing both axes, or a review the model did itself followed by a token Sub-agent. The pull request shows that as one Sub-agent with its `description`, so a reader can see it.
- **The Attempt Header answers the references in `code-review`** that an Attempt cannot satisfy, without editing the skill:
  - each sub-agent the skill names is its own `spawn_subagent` call, all of them in one turn;
  - the fixed point is the Base Revision, and there is no user to ask for it;
  - a Sub-agent has no shell, so it sees the change through `read_change` rather than a diff command;
  - the spec is the Target Issue, which a Sub-agent cannot see unless its caller passes it in `instructions`;
  - the pull request renders the reports from the Run Ledger, so the skill's aggregation serves only the model itself.
  
  The answers are phrased against what the skill asks for, not against its sentences, as the answers for `tdd` and `codebase-design` already are.
- **The Seat Assignment is retired.** Its single-axis part has no subject, because the model now writes one set of instructions per axis. Its other part, that a conversation is itself the agent its instructions describe, is carried by the fixed framing every Sub-agent opens with ([ADR 0020](./0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)). Nothing in that framing is specific to review.
- **The Role Block extraction and its digest gate are removed.** This includes the anchors, the slice digests and the refusal to start when extraction fails. No whole-file digest of `code-review/SKILL.md` replaces them.
- **Role Block, Seat Assignment and Reviewer Brief leave the glossary.**

## Considered options

**Require at least two Sub-agents in a Review.** Rejected by ADR 0021 and not reopened. It writes the structure of the `code-review` skill into the runtime.

**Keep a review-specific Seat Assignment and prepend it to each reviewer's `instructions`.** Rejected. To name an axis, the runtime would have to know the axes, and ADR 0021 deliberately leaves it knowing none. Prepending review framing to whichever group follows an activation would also put review knowledge back into a generic tool.

**Keep a whole-file digest of `code-review/SKILL.md` as a pin guard.** Rejected for the reason ADR 0007 gave against whole-file digests. A pin bump changes the digest every time, so a human re-records it without reading anything. The Attempt Header's answers for `tdd` and `codebase-design` carry no digest either. If an upstream change stops the skill from asking for sub-agents, the model starts no group. The P-DEL-3 return and the unmet review gate then make that loud at run time, not silent.

## Consequences

- **What #73 avoided returns, with compensation.** Reviewer instructions are model-written again. In exchange:
  - every Sub-agent's `instructions` are kept verbatim in the Run Ledger's `review_status` event;
  - the Sub-agents of a Review are visible in the pull request;
  - the Attempt Header answers the skill's references;
  - every Sub-agent opens with the same Worker-authored framing.
- **ADR 0007 is narrowed, not reversed.** Parts 2 to 4 (anchors, the Seat Assignment and the digest gate) have no subject left. Part 1 and the rule that upstream text is answered and never rewritten still bind every activation, including `code-review`'s.
- **The residual risk recorded in ADR 0007 has no subject left.** That risk was that the Spec reviewer reads the skill's instruction to skip itself. Whether to skip the Spec axis is now the model's call, and the pull request shows it.
- **Acceptance rows.** G-REV-2 and L3-REV-1 are retired. L3-REV-2 now checks that the activation result is byte-identical to the installed skill body. L2-19 drops the two failure modes ADR 0007 added. Contract §20 drops the line "Reviewer briefs are model-written".
