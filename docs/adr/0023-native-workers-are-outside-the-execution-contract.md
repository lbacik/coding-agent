---
status: accepted
---

# Native workers are outside the execution contract

The Worker drives only a Pinned Model whose tool loop the runtime itself owns. A native worker is a third-party coding agent that runs its own model and tool loop, such as Codex CLI or Claude Code. It is not integrated in the selected scope, and nothing claims that one satisfies the execution contract. The owner chose this over a single guarantee level and over named compliance classes. Decided in [Does the execution contract hold across worker adapters, or do native workers leave its scope?](https://github.com/lbacik/coding-agent/issues/68).

## Context

The execution contract rests on three things the runtime imposes because it owns the loop:

- a forced first tool call, `activate_implement` through provider-level `tool_choice`;
- an evidence directory the model cannot write, so Criterion Evidence is the runtime's record and not the model's account;
- `report_completion` as the only non-exceptional exit, so completion is declared and substantiated, never inferred ([ADR 0014](./0014-completion-is-declared-and-substantiated-never-inferred.md)).

[Research #67](https://github.com/lbacik/coding-agent/blob/9d408bc3968ee6004d9aaa20d690aeedee0f0f89/docs/research/2026-09-19-issue-67-native-worker-control-surface.md) inspected `codex-cli 0.155.1` and `claude 2.1.278`. Only the second guarantee survives on both, and on both it has to be configured because each worker fails open by default:

- **Forced first tool call:** no mechanism on either worker. A host can deny every other tool's execution, which constrains effects and not the model's first decision.
- **Declared vs silent completion:** neither worker's own reporting distinguishes them. A `Stop` hook can refuse a silent stop. Claude Code overrides the hook after 8 consecutive blocks. Codex has no turn ceiling, so the host would have to impose a wall-clock bound.

The parity reference, `simple-coding-agent`, ran a native worker. The MVP has already replaced its exit, its review entry point and its open tool surface with runtime-owned equivalents (contract §20).

## Decision

- **Only adapters for a model whose tool loop the runtime owns are in scope.** No native-worker adapter is built, and the Workflow Configuration offers no way to select one.
- **No compliance class is defined.** No Implementation Outcome, Completion Claim, Criterion Evidence or completed Review is ever produced from a native worker's own loop.
- **The contract and the acceptance rows are unchanged.** That includes the MVP rows and the retained v1 L3-* rows. The MVP runtime contract states the scope in §1. Neither contract adds a requirement, and no row is added, changed or retired.
- **Research #67 is not revalidated.** It stays the record of what the versions it inspected exposed. It is evidence for this decision and qualifies no worker.

## Considered options

**One guarantee level: a worker that cannot meet the contract is unsupported.** Rejected. It keeps the full contract as the bar, so it leads to the same result today: #67 excludes both inspected workers. It would also imply a standing admission path. A new worker version would invite revalidation and an adapter design even though nothing in the product needs one.

**Named compliance classes.** Rejected. A weaker class would need definitions for:

- which Implementation Outcomes stay decidable without a forced activation;
- how a hook-refused silent stop, capped by the product or by a host clock, substantiates declared completion under ADR 0014;
- what that means for Criterion Evidence, the completed Review, auditability, limits and lifecycle.

Each answer would put a second, weaker meaning on vocabulary the contract uses with one meaning, and every guarantee would depend on vendor hook semantics that change between releases.

## Consequences

- **Reopening this needs a new ADR that supersedes this one.** That ADR must revalidate #67 against the worker versions it actually considers and state each guarantee in terms the host can substantiate. Any production integration is then a separate implementation handoff.
- **"Native worker" is not a domain term.** The glossary lists it under **Worker**'s _Avoid_ and says that a native worker is neither a Worker nor a Pinned Model.
- **Sub-agents are unaffected.** A Sub-agent ([#193](https://github.com/lbacik/coding-agent/issues/193), [ADR 0020](./0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)) is a conversation inside the runtime-owned tool loop, not a native worker.
