---
status: accepted
---

# An interrupted publication is finished, not repeated

At the Publication Gate, before the first delivery write, the Attempt records a **Delivery Intent** in the Run Ledger: its id, branch, head commit, the gate's verdict, whether the pull request is a draft, and the writes that verdict calls for. Any resumption that finds an Intent re-enters the **delivery sequence** and never the implementation. Decided in [Define implementation sequence and v1 acceptance scenarios](https://github.com/lbacik/coding-agent/issues/8).

Three earlier decisions disagreed at exactly this point, and the disagreement was invisible until they were read side by side:

- [Define LangGraph execution and model-provider contracts](https://github.com/lbacik/coding-agent/issues/5) §9 routes an exhausted node retry to a **new Attempt**.
- [Define agent GitHub identity and write authorization](https://github.com/lbacik/coding-agent/issues/9) scenario E assumes that new Attempt will **find the existing pull request** on the same branch and no-op.
- [Define verified pull request delivery and recovery](https://github.com/lbacik/coding-agent/issues/7) §2 then gave every Attempt its **own branch**, and §4 of [Define issue lifecycle and documentation readiness](https://github.com/lbacik/coding-agent/issues/4) excludes from selection any issue carrying an open agent-authored delivery pull request.

Together those make the assumed recovery impossible. A new Attempt gets a new branch, so it cannot find the previous Attempt's pull request; and it would not be selected in the first place, because that pull request is open. An Attempt that died between `open_pull_request` and `comment_delivery` would leave an open pull request with no explanation, stale labels, a still-assigned machine account, and nothing in the system able to reach it.

"Repeat the implementation as a new Attempt" and "finish publishing this Attempt" are not interchangeable operations, and the design had been using one word for both.

## Considered options

**Let a new Attempt adopt the previous one's pull request.** Rejected: it requires either a shared branch per issue, which reintroduces force-push and a moving Base Revision, or a search across branches for an adoptable pull request — and the selection guard would have to learn an exception, which weakens the guard everywhere else to fix one window.

**Relax the open-pull-request selection guard.** Rejected for the same reason from the other side. That guard is what stops the agent piling a second delivery onto an issue a human is already reviewing. Making it conditional on a state the agent itself wrote is how a guard stops being one.

**Accept the orphan and let a human clean it up.** Rejected: the failure is silent. The pull request looks like a normal delivery, and the missing comment and stale labels are only legible to someone who already knows this failure mode exists.

**Make the whole publication one write.** Not available. Opening a pull request, commenting, relabelling and unassigning are four API calls, and the one-external-write-per-node rule exists because there is no transaction across them.

## Consequences

**The point of no return has a location.** [Define verified pull request delivery and recovery](https://github.com/lbacik/coding-agent/issues/7) §8 named the Publication Gate, but two write nodes sit between the gate's judgement and the open pull request, and the older rule requiring a Gate before every external write still applied inside that window. Now the rule is positional and checkable: a stop signal arriving before the Intent is written diverts the Attempt; one arriving after it does not.

**A second process loss no longer fails an Attempt that has already committed to delivering.** T14 sends a twice-lost Attempt to `failed`; T14a exempts the case where an Intent exists, and runs a bounded completion path with no model and no tool loop. The Attempt reaches the outcome the Intent named, which is also the outcome the human is owed an explanation of.

**The Intent does not authorise re-creating anything.** It records what was decided, not what is owed unconditionally. A pull request a human has closed is a reversal, not an absence, and the remote verification rule still governs: only absence authorises a re-write. An Intent whose pull request has been closed routes to the cooperative stop path.

**One more durable record to keep correct.** The Intent is written before the first delivery write and lives beside the write receipts, so a Run Ledger loss takes both — which is why every write kind also has a remote check.

Reversing this means answering the question it was created for: what finishes an Attempt that died with its pull request open and its issue silent.
