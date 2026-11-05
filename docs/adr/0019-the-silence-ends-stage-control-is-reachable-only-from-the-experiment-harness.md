---
status: accepted
---

# The silence-ends-the-stage control is reachable only from the experiment harness

Decided in the triage of [What is the cost of asking for completion?](https://github.com/lbacik/coding-agent/issues/69):
the pre-contract "silence ends the stage" behaviour runs against the current runtime only as the
control arm of a side-by-side experiment, and only the experiment harness may select it.

## Context

The step subgraph's exit rule is fixed by contract §8 and
[ADR 0014](./0014-completion-is-declared-and-substantiated-never-inferred.md): `report_completion` is
the only non-exceptional exit, and a response with no tool calls draws up to two deterministic silence
continuations per work node. The question behind #69 is what that rule costs — whether the
continuations buy verified completions or only spend the Attempt's meters. Answering it means running
the pre-contract behaviour (the first tool-less response ends the stage, no continuation, no
`report_completion` on the tool surface) against the current runtime and comparing per-run evidence
side by side.

The risk is leakage: if the control were selectable from production configuration — a Workflow
Configuration field, a CLI flag, an environment variable — a deployer could run it by accident, and a
misread of the comparison could rewrite contract §8 by configuration. The control must therefore be a
value the production entry paths cannot construct.

## Decision

- **The seam is a policy value.** The step subgraph takes a `StepExitPolicy`: `contract` (today's §8
  behaviour, the default, unchanged) or `silence_ends_stage` (the control: `report_completion` off
  the model's tool surface, the first response with no tool calls ends the implementation stage, no
  silence continuation). Everything else stays identical between arms.
- **Only the experiment harness constructs the control value.** No Workflow Configuration field, CLI
  flag or environment variable selects it. The `agent implement` entry path and Workflow
  Configuration loading never construct it; the control constant lives in the runtime seam module and
  the harness module, and production code paths pass no policy (which is the contract default).
- **The harness is outside `agent implement`.** It is driven by a protocol file (arms, fixtures,
  runs per cell, declared run order, per-Attempt and overall spend caps, oracle commands) and runs
  Attempts sequentially, refusing to start a run whose preconditions or spend cap fail, running each
  fixture's oracle after the Attempt ends against the captured tree.

## Considered options

**A configuration flag (`exit_policy: silence-ends-stage` in Workflow Configuration).**
Rejected: it would make the control a production-selectable behaviour, exactly the leakage this ADR
refuses. The comparison needs the control to exist without becoming deployable.

**A CLI flag (`--exit-policy`) on `agent implement`.** Rejected for the same reason: the production
entry path must not name the control at all.

**Reimplementing the old behaviour as a separate graph.** Rejected: a fork would drift from the
runtime it is meant to measure. The policy value keeps one graph, one toolset (minus the absent
exit), one ledger — so the only intended tool-schema difference is the absent `report_completion`.

## Consequences

- Production behaviour, contract §8 and the review mechanism are unchanged; existing tests, including
  the L3-IMP-24/25/26 silence rows, pass with the default policy.
- The per-run record pins the policy-relevant facts (arm, tool-schema digest) so a reader can verify
  the arms differed only as intended.
- Reversing this means deleting the control value and the harness; the seam (a defaulted parameter)
  stays harmless.
