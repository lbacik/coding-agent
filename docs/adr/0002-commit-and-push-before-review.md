---
status: accepted
---

# The candidate is committed and pushed before anything reviews it

An Attempt commits its work as a **Delivery Snapshot** and pushes that commit to its `agent/<issue>/<n>-<slug>` branch *before* validation runs and before either reviewer is invoked. The branch therefore exists, and is visible in the Target Repository, while the work is still unjudged and possibly red. Decided in [Define verified pull request delivery and recovery](https://github.com/lbacik/coding-agent/issues/7).

Two independent facts force it, and neither is negotiable on its own:

- `/code-review` resolves a nonempty `git diff <base>...HEAD` and the commit list behind it. Uncommitted edits are simply absent from that comparison, so an uncommitted candidate is reviewed as though it did not exist. Established in [Establish upstream implement skill integration requirements](https://github.com/lbacik/coding-agent/issues/2).
- The workspace is reconstructible and never authoritative: after a process loss it is re-created from the remote, so the only durable anchor an Attempt has is a commit reachable from the *pushed* branch. Established in [Define LangGraph execution and model-provider contracts](https://github.com/lbacik/coding-agent/issues/5) §13.4.

A commit satisfies the first. Only a push satisfies the second.

## Considered options

**Adapt the review contract to read the working tree.** Compare the Base Revision with the complete candidate tree, untracked additions included. Rejected: it requires an adapter around `/code-review` or a maintained fork, and it buys a divergence from upstream that has to be re-justified at every upstream bump — a permanent cost to avoid one commit.

**Materialise the candidate in a separate review worktree or branch.** Both reviewers see a committed `HEAD` while the working branch keeps `/implement`'s stated ordering. Rejected: it introduces a second identity for the same tree, and every question about recovery then has to be asked twice, once per identity. The ordering it preserves is a preference of the upstream prose, not a property anything depends on.

**Commit before review, but push only at delivery.** The reviewers are satisfied and no branch appears early. Rejected: a process loss then destroys the whole Attempt back to Claim, and because the Attempt's wall clock is measured from the Run Ledger rather than from process start, the resumed Attempt re-implements from zero against a clock that has already run down. It makes the Automatic Retry Budget a formality.

## Consequences

**A branch in the repository is not a claim about quality.** Only a pull request is. This has to be written down, because the natural reading of a pushed `agent/` branch is that something stood behind it, and here nothing does yet: validation has not run.

**One process loss costs at most one phase of an Attempt** — implementation, or validation-and-review — never the whole Attempt. That bound is the whole point of pushing early, and it is the property to check first if the node order is ever rearranged.

**A remediation round produces a second commit, and the branch is not squashed.** What the fix changed stays legible to the reviewer who asked for it.

**Failed Attempts leave branches behind.** The agent never deletes one: deletion is asymmetric, and whether a failed Attempt's work is worth anything is a judgement for a human rather than for the Attempt that failed. Selection excludes an issue on an open agent-authored pull request, never on the existence of a branch, so the accumulation costs nothing but namespace.

Reversing this decision means choosing one of the rejected options above, and the second fact — the durable anchor — survives all of them. Any replacement must still say how much implementation work a single process loss may destroy.
