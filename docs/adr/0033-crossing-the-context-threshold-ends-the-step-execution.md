---
status: accepted
supersedes: 0011-the-pinned-prefix-is-a-region-not-a-rule.md
---

# Crossing the Context Threshold ends the Step Execution

The main conversation of a Step Execution never outgrows its pin's Context Threshold. Before a request whose Conversation Context ([ADR 0031](./0031-the-conversation-context-is-anchored-on-provider-reported-input.md)) would exceed the Usable Threshold — `context-threshold-tokens` less the code-owned safety margin and request overhead — the request is not sent and the Step Execution ends `limit-exceeded` with the limit fact `{"scope": "step", "reason": "context-threshold"}`, which also carries the crossing Conversation Context and the Usable Threshold; a `context_threshold` Run Ledger event records the same pair. The Workflow routes it like any other limit. A Sub-agent that reaches the threshold still ends without a report.

The runtime has no in-step continuation: it never opens a second main conversation inside a Step Execution, never asks the model to summarise its own work, and never evicts or rewrites turns of a conversation.

## Why

- **One mechanism per concern.** An in-step continuation spent tokens, cost and wall clock on a turn the Workflow never saw, and decided on its own when a step's work moved to a fresh conversation. Running out of room is already a Workflow concern: a `limit-exceeded` Route to an AI-assisted Handoff step, or to a Hard Handoff, states in the configuration what happens next, under an allowance the operator set.
- **The operator controls the point.** `context-threshold-tokens` is the single place that decides when a step stops growing. Setting it to 200,000 means the step hands its work to its `limit-exceeded` Route at about 198,000 tokens of context, with nothing hidden in between.
- **Less state.** No snapshot artifact, no snapshot schema, no handoff counter, no handoff output cap and no third Usage Origin to record, restore and report.

## Consequences

- **Configuration is unchanged.** `context-window-tokens`, `context-threshold-tokens` and `output-cap-tokens` keep their meaning and validation, including `context-threshold-tokens + output-cap-tokens ≤ context-window-tokens` and the Admission floor (`effective-tokens` ≥ Usable Threshold + `output-cap-tokens`, and the matching `cost-usd` floor). The code-owned constants are the safety margin (1,000) and request overhead (1,000) only.
- **A step that needs more context than the threshold allows** ends `limit-exceeded`; a Workflow that wants the work continued routes to a step that hands it over, and a later Attempt or Workflow continues by policy.
- **Usage Origins are `main` and `subagent`.** Until [agent-operator-contract](https://github.com/lbacik/agent-operator-contract)'s coding-agent mapping (§16) changes, the operator status keeps publishing a `handoff` cell that is always zero, `continuations` as zero and `native.handoff_threshold_tokens` as the configured Context Threshold, so a 1.1 consumer reads it unchanged. Changing that mapping is a follow-up in the operator contract.
- **[ADR 0011](./0011-the-pinned-prefix-is-a-region-not-a-rule.md)'s eviction is gone with it.** Its structural guarantees — an intact Pinned Prefix and whole Exchange Units within a conversation — remain in force.
