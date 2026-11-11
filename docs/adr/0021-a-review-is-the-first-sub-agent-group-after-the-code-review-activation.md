---
status: accepted
---

# A review is the first Sub-agent group after the code-review activation, on the snapshot tree

> **Superseded for the replacement design:** [ADR 0027](./0027-workflow-policy-is-separate-from-runtime-mechanics.md) removes review-specific runtime recognition, activation side effects and the mandatory review gate. Review becomes Workflow/skill policy. The text below remains a record of the existing MVP implementation, not a requirement on the replacement runtime.

Once `code-review` runs on generic Sub-agents, the runtime no longer runs the reviewers, so it can no longer recognise a review by running one. `activate_code_review` stays a recognisable anchor instead. It passes the in-tool Gate, then commits and pushes the review snapshot X ([ADR 0002](./0002-commit-and-push-before-review.md)), records X and returns the skill body verbatim. The **Review** it opens is the first concurrent group of Sub-agents ([ADR 0020](./0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)) its caller starts after it, in the same work node, provided the working tree at the start of that group equals X. A Review is completed when that group has at least one Sub-agent and every one of them made a Sub-agent Report. This supersedes the hard-coded two-seat fan-out of [#62](https://github.com/lbacik/coding-agent/issues/62) and contract §10, as decided in [What review evidence does the runtime own once code-review runs on generic Sub-agents?](https://github.com/lbacik/coding-agent/issues/194).

## Context

#71 and #73 made a completed review a condition of every pull request, and the runtime could enforce that because it ran both reviewer seats itself. [ADR 0020](./0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md) gave the model a generic `spawn_subagent` with no role field, so the runtime cannot tell which Sub-agent Reports were a review.

## Decision

- **The evidence is what the runtime itself produced:** the activation, the pushed X, the tree at the start of the group, each Sub-agent's end state, and its report verbatim. The runtime knows no review axes and counts no seats.
- **Attribution is positional and checked against the tree.** Reads and commands between the activation and the group do not matter. A changed tree does, and so do a later group or a work-node boundary (step end or Context-Window Handoff): any of them leaves that activation without a Review. A retry is a new activation, which costs no new commit while the tree is unchanged.
- **The review status keeps its four values:** `not-invoked`, `incomplete`, `fresh` and `stale`. `fresh` and `stale` compare the X of the latest completed Review with the delivered head. The review gate (`fresh` or `stale`) stays a Publication Gate condition, and findings never block. The one-time return of a Completion Claim without a completed Review (P-DEL-3) stays, and the return names the cause.
- **The limit of three invocations per Attempt is removed.** Spend is bounded by the Attempt-wide meters alone, because a limit on activations would not limit Sub-agents.
- **The Attempt Header states the rule.** The activation result stays the skill body verbatim ([ADR 0007](./0007-upstream-skill-text-is-answered-never-rewritten.md)).

## Considered options

**No review evidence at all.** Review would become entirely the model's business, with no status and no gate. Rejected: it drops the #71 guarantee when an honest weaker form is available.

**A role field on `spawn_subagent`.** Rejected: it puts review knowledge back into a generic tool, and a role the model declares is no stronger evidence than the order of its calls.

**Attribute every Sub-agent until the next activation or tree change.** Rejected: it complicates membership and adds no evidence that a fresh activation cannot give.

**Require at least, or exactly, two reports.** Rejected: it hard-codes the structure of the `code-review` skill into the runtime, which is what this change removes. Whether anything guards against a fan-out that never happened is left to [What remains of the verbatim Role Block guarantee when the model writes reviewer instructions?](https://github.com/lbacik/coding-agent/issues/195).

## Consequences

- The review gate guarantees that recorded Sub-agents reviewed a pushed tree and that each of them reported. It does not guarantee what the model told them to do.
- A model that edits the tree between the activation and its reviewers loses the Review. It learns this from the Attempt Header and, at the latest, from the P-DEL-3 return.
- The pull request shows each Sub-agent's `description` and report in place of the Standards and Spec headings. The `instructions` are kept only in the Run Ledger's `review_status` event.
