---
status: accepted
---

# Workflow policy is separate from runtime mechanics

> First published as ADR 0026 on the map's documentation branch and renumbered because ADR 0026 on `main` records [Operator drain, un-drain and command lookup](https://github.com/lbacik/coding-agent/issues/249).

The configured Workflow graph and its activated skills describe process policy, including implementation, review and the requirements for progress, completion and publication. The Worker provides scenario-independent execution and retains authorization, exclusive Claim, cancellation, limits, factual execution records, durable effects and deterministic cleanup. Detailed decisions are held in [What may a Workflow configure, and which guarantees remain mandatory in the runtime?](https://github.com/lbacik/coding-agent/issues/279), part of [Design: declarative step workflows compiled from YAML to LangGraph](https://github.com/lbacik/coding-agent/issues/276).

This is an accepted design boundary for the replacement runtime, not a claim that the current code implements it. Retain the names **Workflow Step** and **Step Result**. Issue and pull-request input types and label filters are configured independently of the scenario; runtime validation of declared results remains separate from model-authored business claims. Configuration and operator instructions remain operator-owned, read at startup and fixed in the Effective Configuration at Claim.

## Earlier decisions

- Amend ADR 0016: the graph is no longer confined to ordered implementation steps or a fixed process skeleton. Retain operator ownership and per-Attempt pinning.
- Amend ADR 0014: retain explicit reporting and the distinction between claims and runtime-produced evidence. Its implementation-specific completion schema, criterion obligations and publication policy are not universal runtime rules.
- Supersede ADR 0021's review-specific activation hook, positional attribution and mandatory review gate in the replacement design. Review is expressed through configuration and/or skills; activating `code-review` has no implicit review lifecycle side effects.
- The process-specific ordering and readiness requirements in ADRs 0002 and 0008 must be expressed by the relevant Workflow/skill rather than imposed on every scenario. This does not remove the ability to record revision identity or collect evidence.
- Retain ADR 0023's runtime-owned model/tool loop. ADRs 0003 and 0017 remain the durable-state baseline; this boundary does not decide graph replay, model resumption or disposal of unfinished Attempts (since decided in [ADR 0028](./0028-every-external-write-is-an-intent-settled-once.md)). ADR 0013's Context-Window Handoff remains distinct from a configured Handoff role pending the limits decision.

Exact Step Result schemas, model scope, generic operation catalog, input Claim/Fingerprint behavior, routing, budgets, terminal categories and recovery remain child decisions of the map. Approval, merge and other additional GitHub operations are not implicitly authorized or required. No production implementation, old-YAML converter or dual execution engine is introduced here.
