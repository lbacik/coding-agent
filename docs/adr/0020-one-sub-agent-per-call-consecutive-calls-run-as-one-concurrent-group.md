---
status: accepted
---

# One Sub-agent per call; consecutive calls in a turn run as one concurrent group

> **Replacement design amendment:** [What is the execution and data contract of one Workflow Step?](https://github.com/lbacik/coding-agent/issues/280#issuecomment-6798402008) offers Sub-agents only to an `agent` step that declares the `subagents` tool group; one Sub-agent per call, the concurrent group, the fixed read-only toolset and the Pinned Model are retained. [How do step limits, Attempt budgets and AI-assisted Handoff compose?](https://github.com/lbacik/coding-agent/issues/282#issuecomment-6798892866) counts a Sub-agent's spend against the calling step and the Attempt rather than Attempt-wide meters alone, admits each concurrent request individually, and keeps `max-parallel-subagents` a Workflow-level setting. The text below records the existing MVP design.

A Workflow Step's model starts a Sub-agent with `spawn_subagent(description, instructions)`, one Sub-agent per call. Parallelism comes from several such calls in one assistant turn: the runtime runs each unbroken run of consecutive `spawn_subagent` calls as one concurrent group, up to `workflows.<id>.limits.max_parallel_subagents`, queueing the excess in call order. Every other tool call in the turn is a barrier, and results join the Exchange Unit in call order. Decided in [What is the tool contract for model-invoked Sub-agents?](https://github.com/lbacik/coding-agent/issues/65).

## Context

Upstream skills are written against a tool that starts one sub-agent per call: `code-review` says to "spawn both sub-agents in parallel". [ADR 0007](./0007-upstream-skill-text-is-answered-never-rewritten.md) answers upstream text rather than rewriting it, so the tool should let that sentence be followed as written. Until now every tool call in a turn ran sequentially in `run_tools`.

## Decision

- **Shape.** One call, one Sub-agent. The input is a short `description` (for the Run Ledger and the Transcript) and the model's `instructions`; there is no toolset, budget or model parameter. The toolset is fixed, the meters are Attempt-wide and the model is the Pinned Model.
- **Concurrency is a property of the turn, not of the tool.** Consecutive `spawn_subagent` calls form one group; any other call (a write, a command, `test_targeted`, an activation, an exit) ends the group and runs after it. No workspace write therefore runs while a Sub-agent reads the tree, and the caller is suspended until its group ends, so Sub-agents from different turns never overlap.
- **Read-only, with the change visible.** The fixed toolset is the workspace file readers, `read_result_slice`, a runtime-owned `read_change` (commit list and diff against the Base Revision, covering what the Delivery Snapshot would commit, bounded with a pointer to the full text) and the Sub-agent's own `report_subagent` exit. Git still runs only inside the runtime.

## Considered options

**A batch tool, `run_subagents([...])`.** Concurrency would stay inside one tool and `run_tools` would stay sequential. Rejected: the model would have to translate "spawn both in parallel" into a single batched call, an interpretation the upstream text never asks for, and the one-call-per-agent shape is the one upstream skills assume.

**Run the other tools first and all spawns afterwards, or refuse mixed turns.** Rejected: the first reorders what the model emitted, the second refuses a turn the model has every reason to produce.

**Have the model paste the change into `instructions`, or have the runtime always attach it.** Rejected: pasting collides with the instructions bound and the capping of shell output, and always attaching it adds context nobody asked for. `read_change` generalises what contract §10 computes for reviewer seats today, on demand.

## Consequences

- `run_tools` is no longer strictly sequential. Reversing this means either a batch tool or giving up parallel Sub-agents.
- `read_change` widens the "fixed read-only file set" of the Sub-agent map's Notes by one runtime-owned reader.
- Each Sub-agent's queueing, start and end are Run Ledger events, which is what makes the limit observable: at limit `1` no two Sub-agent intervals overlap.
