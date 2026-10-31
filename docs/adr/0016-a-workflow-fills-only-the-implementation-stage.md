---
status: accepted
---

# A Workflow fills only the implementation stage, and its configuration is the operator's alone

The operator's Workflow Configuration (YAML) declares named Workflows of ordered Workflow Steps, and the Worker builds the graph from it. A Workflow fills the implementation stage of an Attempt and nothing else. The Workflow Configuration is read only from the Worker's own filesystem at startup and never from the Target Repository. Decided in [What does workflow YAML configure, and what does each ordered step guarantee?](https://github.com/lbacik/coding-agent/issues/72), part of the planning map [Design: YAML-configured LangGraph MVP with simple-coding-agent parity](https://github.com/lbacik/coding-agent/issues/70).

## Context

The MVP replaces a fixed `implement` entry point with operator-configurable Workflows. Without an explicit boundary, configuration tends to grow until it can change anything. Typical examples are skipping the baseline, adding a step after the final checks, publishing on a weaker verdict, or letting the Target Project steer the model.

The parity reference, `simple-coding-agent`, has no configuration file. It does, however, read `model_instructions` from a profile in the Target Repository and append them to the model's starting prompt. Anyone who can push to the default branch can therefore instruct the model.

The guarantees decided so far all sit outside the implementation stage:

- selection guards;
- Claim by assignment (ADR 0015);
- Readiness refusals;
- the review gate;
- the Publication Gate verdicts;
- Delivery Intent (ADR 0003);
- runtime-owned evidence (ADR 0014).

## Decision

### The fixed skeleton

Selection, Claim, Readiness, baseline, Delivery Snapshot, final checks, the Publication Gate and the terminal writes are a fixed skeleton owned by the Worker.

- **YAML cannot reshape it.** It cannot add, remove or reorder these nodes.
- **YAML cannot change the guarantees.** It cannot change their verdicts or outcome mapping.

### What a Workflow can set

A Workflow supplies only the ordered Workflow Steps of the implementation stage. Each step is either:

- the Skill Bundle member `implement`, the sole top-level skill in the MVP; or
- an operator `prompt_file`, delivered verbatim through a forced activation call.

Every step boundary is a Gate.

### Selection

A Workflow names exactly one Selection Label and may add label filters. Filters can only narrow the fixed selection guards, never remove them.

### Where configuration comes from

The Workflow Configuration and its prompt files are trusted operator input.

- **The Worker's filesystem only.** They are read from there, and never from the Target Repository.
- **Read once.** They are read once, at startup.
- **Pinned per Attempt.** At Claim they are pinned as the Attempt's Effective Configuration.
- **No instruction channel from the Target Project.** No Target Project content reaches the model as instruction. The Project Profile stays limited to bootstrap, test and check facts.

## Considered options

**YAML as a general graph.** In this option YAML would declare edges, conditions and nodes anywhere in the Attempt. Rejected for two reasons:

- every guarantee above would become a property of each operator's file instead of the Worker;
- the parity contract would no longer be checkable once for all deployments.

User-defined branching is already out of scope for the map.

**Workflow configuration in the Target Repository,** or a repository overlay on the operator's file. Rejected. A push to the default branch, or a pull request that edits it, would change what the model is told for every later Attempt. The Selection Label exists precisely so that only a human authorises work.

**Repository guidance appended to the prompt,** the reference's `model_instructions`. Rejected for the same reason. Parity covers observable behaviour, not this channel.

## Consequences

- **Changing the skeleton means changing the Worker.** Any new node in the skeleton, such as a post-check step or a different publication policy, is a code change with its own decision. Operators cannot opt into it.
- **The parity contract is checked once for all deployments.** The P-* acceptance scenarios apply to every Workflow, not only the parity one.
- **Project-specific guidance is the operator's job.** It reaches the model only through a `prompt_file` the operator owns. A repository maintainer who wants the agent to follow house rules asks the operator.
- **Config edits apply to the next Attempt.** Editing the Workflow Configuration or a prompt file takes effect only after a restart and never changes an Attempt already open, recovery included.
