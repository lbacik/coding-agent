---
status: accepted
---

# Runtime-owned validation authenticates the invocation, not the code it executes

"Verified" means the runtime itself ran the Validation Contract's commands, as pinned at the Base Revision, in an environment without credentials, and parsed their results itself. It does not mean the candidate code those commands executed was honest, and it does not make Criterion Evidence, Validation Evidence, the Run Ledger or the Transcript tamper-resistant against that code. The MVP assumes a cooperative model and says so. It records an indirect write as an Execution Footprint instead of containing it, and it leaves the human review of the pull request as the last trust boundary. Decided in [What write history must an Attempt retain, and what does runtime-owned validation guarantee?](https://github.com/lbacik/coding-agent/issues/27).

## Context

[ADR 0014](./0014-completion-is-declared-and-substantiated-never-inferred.md) made the runtime the owner of the evidence: only `test_targeted` writes Criterion Evidence, and a test the model runs any other way counts for nothing. Read on its own, that sentence suggests more than it delivers.

[#25](https://github.com/lbacik/coding-agent/issues/25) observed an implementer that changed `orders/pricing.py` without ever calling `write_file` on it. It wrote a test file whose module level performed the edit, called the targeted-test tool, which imported the file, and then replaced the test with a clean one. The final diff was correct and minimal and carried no trace of how it was made. Nothing in it was adversarial.

What makes this possible is not a tool the model holds but where project code runs. Validation commands execute candidate code as the Worker's own user, on the state volume. That code can write the workspace, the per-Attempt evidence directory (the JUnit file Criterion Evidence is parsed from included), the Run Ledger and the Transcript. Credentials are already kept out of its environment (contract §18). Removing tools does not change this: `write_file` plus `test_targeted` is already arbitrary execution, so adding a shell would neither widen nor narrow it.

## Decision

- **What the runtime guarantees.** The commands come from the Validation Contract, never from the model. The runtime invokes them, strips credentials from their environment, kills their whole process group on timeout, and parses their exit codes and JUnit results itself. A green `final_checks` must leave a clean tree.
- **What it does not guarantee.** That executed candidate code (tests, test configuration, production code) did not skip tests, alter their results or rewrite their evidence files. That the evidence, the Run Ledger and the Transcript are intact against such code. That the code had no network effects. The contract states these limits explicitly.
- **Indirect writes are recorded, not prevented.** After every tool call that executes project code (today `test_targeted` only), the runtime records the Attempt's Execution Footprint for that call: the workspace paths the call changed, with content digests before and after. It goes to the Run Ledger, to the tool result the model sees, and, bounded, to the pull-request body. It gates nothing: it is neither an input to the Implementation Outcome nor to the Publication Gate.
- **The Transcript is the record of what the model asked for.** The contract guarantees it as durable per-Attempt state. Together with the Execution Footprint and the Delivery Snapshot, it is what a reader of the handoff can reconstruct an Attempt from.
- **Isolation is deferred.** Running validation commands under a separate identity or container, with the state volume out of reach, is recorded in contract §19.

## Considered options

**Isolate validation commands from runtime state now.** Rejected for the MVP. It is a change to the image and the process model, and no current guarantee depends on it once the contract stops implying one.

**Keep a full history of every workspace state.** Rejected. A tree snapshot after every tool call stores a great deal and adds little: the content of the model's own writes is already in the Transcript, and the question #25 left open was *which* files executed code touched, which the footprint answers.

**Reject or block indirect writes outside test paths.** Rejected. The #25 fix was correct, and generated snapshots and fixtures are legitimate indirect writes. Under a cooperative model a block punishes the honest case and cannot stop the dishonest one.

**Track writes per write tool.** Rejected. It records only the channel the model used deliberately, which is exactly the one that missed #25, and it would break again the moment another execution tool appears.

## Consequences

- **The footprint watches the git-visible tree only.** Tracked files plus untracked files that are not gitignored — the same set as the final-checks dirty-tree rule — so `__pycache__`, `.pytest_cache` and the like never appear. **The footprint cannot see** content of intermediate states, writes outside the workspace, writes to gitignored paths, or a change undone within the same call. Against code that sets out to hide its effects it proves nothing.
- **Generic Sub-agents are unaffected.** They hold no tool that executes project code, so no footprint arises in them. Their transcripts and report auditability belong to [Design: model-invoked Sub-agents replace hard-coded fan-out inside Workflow Steps](https://github.com/lbacik/coding-agent/issues/193).
- **The decision is independent of a shell.** Should one be added ([#196](https://github.com/lbacik/coding-agent/issues/196)), it is one more tool that executes project code and records a footprint the same way.
- **Reversing this** means isolating validation commands from the state volume. That is the point at which "verified" could start to mean tamper-resistant.
