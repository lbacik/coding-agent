---
status: accepted
supersedes: 0003-delivery-intent.md
---

# Every external write is an intent settled once, and a restart never enters the Workflow graph

In the replacement runtime every external write, from the Claim to the last terminal label, is preceded by its own durable **Effect Intent** in the Run Ledger and closed by an **Effect Receipt**. A restarted Worker settles the intent that was in flight and finishes the Attempt's lifecycle; it never runs a Workflow Step, neither an agent nor a Runtime Operation. Decided in [How are step transitions, terminal outcomes and external effects recorded and recovered?](https://github.com/lbacik/coding-agent/issues/283), part of [Design: declarative step workflows compiled from YAML to LangGraph](https://github.com/lbacik/coding-agent/issues/276).

This is an accepted decision for the replacement design, not a description of the current code.

## Context

ADR 0003 placed one point of no return per Attempt, the Delivery Intent, in front of a fixed publication sequence, and ADR 0017 let recovery re-enter that fixed graph at a named node. A configured Workflow has no fixed publication sequence: it may push, open a pull request, publish a review and post comments, several times and in any order the author routes, and many Attempts publish nothing at all. The current code also protects only pushes and the pull request with intents; comments, labels and assignments are looked up only during recovery.

## Decision

- **One intent per write.** Each external write records, after its Gate and immediately before the write, an Effect Intent keyed by Attempt, Workflow Step, Step Execution sequence number and write ordinal, holding the exact rendered payload and the expected target state (the revision to push, the head a compare-and-swap expects, the input it addresses). The key travels in the write's Attempt Marker. A stop signal arriving after the intent does not divert that one write.
- **Settle, never repeat blindly.** On restart an intent without a receipt is looked up remotely by its key. If the write landed, the receipt is recorded. If it is absent, the operation's factual preconditions (active Claim, no withdrawal, revision or head compare-and-swap) are checked again: when they hold the write is performed once, otherwise the intent is recorded as not performed with its reason. Something a human closed or removed is a reversal, never an absence, and is not re-created.
- **A restart never enters the graph.** After settling, the Attempt ends outside the Workflow: with `interrupted`, or `withdrawn` when the input was withdrawn meanwhile. The same lifecycle finalisation ends cancellation, Drift, withdrawal and internal runtime failure. No model call is made, the dirty tree is neither committed nor pushed, and the workspace is kept locally.
- **The outcome is still recorded before it is published.** The Terminal Outcome, the rendered terminal comment and the list of terminal writes are recorded once, immutably, after all in-flight effects have been settled and before the first terminal write; each terminal write then has its own intent and receipt.

## Relation to earlier decisions

- **Supersedes ADR 0003** for the replacement design: there is no Attempt-level Delivery Intent. Each write is its own point of no return; a withdrawal after a publication landed does not undo it, and later writes pass their own Gates.
- **Amends ADR 0017:** retained are the Run Ledger as the single authority, no framework checkpointer, no Lease, the outcome recorded before it is published, and recovery from pinned values only. Replaced is recovery's re-entry into a fixed Attempt graph at a named node.

## Considered options

**Deterministic continuation of operation steps after a restart.** Rejected: it would replay the graph by the back door and run operations against GitHub state of unknown age, which ADR 0027 did not authorise and which a single settle-and-finish rule makes unnecessary.

**Keep a single Attempt-level Delivery Intent.** Rejected: a configured Workflow may publish several times or not at all, so one marker cannot say which writes are owed.

## Consequences

- An interrupted Attempt may leave a pull request, review or comment that its Workflow meant to follow with further steps. The runtime never reverts it; the terminal comment lists every landed and not-performed effect, so the gap is visible.
- Every write kind needs a remote lookup by key, including comments, labels and assignments.
- Reversing this means deciding again which store answers "did this write land" and what a restart may still run.
