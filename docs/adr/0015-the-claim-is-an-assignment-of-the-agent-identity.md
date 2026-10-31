---
status: accepted
---

# The Claim is an assignment of the Agent Identity

The Worker makes its Claim visible by assigning the Agent Identity to the Target Issue and reading the issue back; there is no `agent-running` label. This supersedes the consequence "Assignment leaves the contract" of [ADR 0005](./0005-agent-identity-is-the-repository-owner.md) and nothing else in it. Decided in [What is the MVP parity contract, and which v1 guarantees does it supersede?](https://github.com/lbacik/coding-agent/issues/71), part of the planning map [Design: YAML-configured LangGraph MVP with simple-coding-agent parity](https://github.com/lbacik/coding-agent/issues/70).

## Context

ADR 0005 removed assignment from the contract because the Agent Identity is the repository owner's own account: assigning it is indistinguishable from the owner assigning themselves, and unassigning at a Terminal Outcome could strip the owner's own assignment. A label, `agent-running`, carried the human-visible claim instead.

The MVP targets behavioural parity with the tested `simple-coding-agent`, which claims by assigning the token's account and verifying the assignment after a short settle delay. The owner chose that native GitHub signal over a label.

## Decision

- **Claim:** assign the Agent Identity, then read the issue back. If any other assignee is present, or any selection guard no longer passes, the Worker removes only its own assignment and moves to the next candidate.
- **Eligibility:** an issue with any assignee is not eligible, as before.
- **Whose assignment it is:** the Run Ledger decides whether an assignment of the Agent Identity belongs to the Worker. If no Attempt is open for it, it is a human's assignment, and the issue is excluded and left untouched.
- **Ending an Attempt:** at a Terminal Outcome the Worker removes only an assignment it recorded itself.
- **No claim comment:** the assignment and the terminal comment, which carries the Attempt Marker, are the visible record.
- **No orphaned Claim:** a process loss must never leave an issue assigned with no Attempt that can be finished or released. The mechanism belongs to [How do Attempts recover and publish without duplicating GitHub effects?](https://github.com/lbacik/coding-agent/issues/74).

## Considered options

**Keep `agent-running` (ADR 0005 as written).** Rejected. It is a second, project-specific vocabulary for a fact GitHub already has a native field for, and it departs from the parity reference for no observable gain.

**Assignment and label together.** Rejected as redundant. Two signals for one fact can disagree after a partial write.

## Consequences

- **The GitHub UI no longer tells the owner who assigned an issue.** The Run Ledger and the terminal comment answer that.
- **Self-assignment is how the owner keeps an issue away from the Worker.** An owner who assigns themselves to an issue has excluded it.
- **The reason for removing assignment disappears with the shared identity.** Moving to a dedicated identity ([#11](https://github.com/lbacik/coding-agent/issues/11)) makes the assignment unambiguous on its own.
