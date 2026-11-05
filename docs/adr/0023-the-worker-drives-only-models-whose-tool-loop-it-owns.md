---
status: accepted
---

# The Worker drives only models whose tool loop it owns

A Pinned Model is always a model the Worker calls through a provider adapter, inside a tool loop the runtime itself runs. A third-party coding agent that runs its own model and tool loop, such as Codex CLI or Claude Code, is out of scope. Issue #68 calls such an agent a "native worker". No adapter exists for one, and nothing claims that one satisfies the runtime contract. The owner chose this over a single guarantee level and over named compliance classes. Decided in [Does the execution contract hold across worker adapters, or do native workers leave its scope?](https://github.com/lbacik/coding-agent/issues/68).

## Context

The runtime contract rests on three things the runtime imposes because it owns the loop:

- a forced first tool call, `activate_implement` through provider-level `tool_choice`;
- an evidence directory the model cannot write, so Criterion Evidence is the runtime's record and not the model's account;
- `report_completion` as the only non-exceptional exit, so completion is declared and substantiated, never inferred ([ADR 0014](./0014-completion-is-declared-and-substantiated-never-inferred.md)).

[Research #67](https://github.com/lbacik/coding-agent/blob/9d408bc3968ee6004d9aaa20d690aeedee0f0f89/docs/research/2026-09-19-issue-67-native-worker-control-surface.md) inspected `codex-cli 0.155.1` and `claude 2.1.278`. Only the second of the three is available on both, and only once configured, because both agents fail open by default:

- **Forced first tool call:** neither agent has a mechanism for it. A host can deny the execution of every other tool, but that constrains effects, not the model's first decision. Claude Code's `EndConversation` cannot be denied at all.
- **Declared vs silent completion:** neither agent's own reporting distinguishes them. A host could rebuild the distinction only by joining its own receipt of the tool call to the termination event. A `Stop` hook can refuse a silent stop, but Claude Code overrides the hook after 8 consecutive blocks. Codex has no turn ceiling, so the host would have to impose a wall-clock bound.

The parity reference, `simple-coding-agent`, ran such an agent. The MVP has already replaced its exit, its review entry point and its open tool surface with runtime-owned equivalents (contract §20).

## Decision

- **Only adapters for a model whose tool loop the runtime owns are in scope.** The Workflow Configuration's (provider, model) table offers nothing else.
- **No compliance class is defined.** No Implementation Outcome, Completion Claim, Criterion Evidence or completed Review is ever produced from an agent's own loop.
- **No requirement or acceptance row changes.** This covers both the MVP rows and the retained v1 L3-* rows. The MVP runtime contract records the scope in §1 and adds no requirement.
- **Research #67 is not revalidated.** It stays the record of what the versions it inspected exposed. It is evidence for this decision and qualifies no agent.

## Considered options

**One guarantee level: an agent that cannot meet the contract is unsupported.** Rejected. It keeps the full contract as the bar, so today it reaches the same result: #67 excludes both inspected agents. But it implies a standing admission path, in which every new agent version invites revalidation and an adapter design that nothing in the product needs.

**Named compliance classes.** Rejected. A weaker class would have to define several things:

- which Implementation Outcomes stay decidable without a forced activation;
- how a host substantiates declared completion under ADR 0014 when silent stops are refused by a hook capped by the product or by a host clock;
- what the class means for Criterion Evidence, the completed Review, auditability, limits and lifecycle.

Each answer would give a second, weaker meaning to vocabulary the contract uses with one meaning. Every guarantee would also rest on vendor hook semantics that change between releases.

## Consequences

- **Reopening this needs a new ADR that supersedes this one.** That ADR must revalidate #67 against the agent versions it actually considers and state each guarantee in terms the host can substantiate. Any production integration then gets its own implementation handoff.
- **"Native worker" is not a domain term.** It would collide with **Worker**, so the glossary lists it under that entry's _Avoid_. The **Worker** entry states that such an agent is neither a Worker nor a Pinned Model.
- **Sub-agents are unaffected.** A Sub-agent ([ADR 0020](./0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md)) is a conversation inside the runtime-owned tool loop, not a separate agent with a loop of its own.
