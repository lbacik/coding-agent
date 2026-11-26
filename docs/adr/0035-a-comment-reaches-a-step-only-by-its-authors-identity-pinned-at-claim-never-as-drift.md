---
status: accepted
---

# A comment reaches a step only by its author's identity, pinned at Claim, never as Drift

An issue Workflow's agent step may declare the input `input.comments`: the **Trusted Comments** of the Target Issue, read back at Claim. A comment is trusted when its author is, by numeric account id, the issue's own author, the Target Repository owner or the Agent Identity. The Worker's own Claim and terminal comments are left out by their Attempt Marker. A Worker `post-comment` publication is kept, reduced to its `document`. The set is pinned at Claim and archived. It is not part of the Fingerprint. A comment added during the Attempt is disclosed in the terminal comment and is never Drift. Comments reach the model only as data, like the title and body, with no instruction or authority channel. This amends §7.1 and §7.4 of the step-workflow runtime contract, which supplied no comments in the first release (#676). The decision records intent: it does not claim that the current code implements it.

## Why

- **The issue author adds no new principal.** The body the issue author wrote already reaches the model as data. Trusting the same person's comments widens nothing. It matches the rule simple-coding-agent already runs.
- **The owner closes the dedicated-identity gap.** Under a shared identity the owner is the Agent Identity, so nothing changes. Under a dedicated identity a maintainer's triage brief, written from their own account on someone else's issue, would otherwise be dropped.
- **The filter for the Worker's own writing is structural.** Under a shared identity the owner and the Agent Identity are one account, so identity cannot tell a human note from a Worker comment. The Attempt Marker can, and it reads in one direction only: an unmarked comment passes as written.
- **A reply must not end the work it answers.** If comments were part of the Fingerprint, a reporter's comment would end a running Attempt `drift`. If they were read live, two steps of one Attempt would see different data, and the Attempt Archive could not show what the model was given.

## Considered options

- **Trust by repository permission** (anyone who may apply the Selection Label, as the deferred **Qualifying Answer** does). Rejected for data: it needs a permission read per author, and it would drop an outside reporter's answer, which is exactly what re-triage needs. Qualifying Answer stays a separate, deferred concept for Worker control flow, not for model data.
- **Trust by login text.** Rejected: logins can be renamed and reused. Numeric ids are what the runtime already uses for the Agent Identity.
- **Comments as part of the Fingerprint, so a new comment is Drift.** Rejected for the reason above.
- **Only the comments after the last terminal comment.** Rejected: a triage Attempt posts its brief and then its terminal comment, so the cut would drop the brief.
- **Declaring comments for the whole Workflow.** Rejected: steps already receive data only through declared inputs, and comments are not part of the input's identity the way the title and body are.

## Consequences

- **Pull-request Workflows are excluded.** `input.comments` is refused statically there and in operation bodies. Pull-request conversation and review comments need their own decision.
- **Size is bounded by the runtime, not by YAML.** There are 8,000 characters per comment and 64,000 in total. When the total is exceeded, the oldest comments are dropped whole, and an omission line names how many and points to the Attempt Archive. The opening frame still goes through Admission and the Conversation Context threshold.
- **A comment edited by someone else stays trusted.** Trust follows the author, as it does for the body, which a maintainer can also edit.
- **A Continuation pins the comments afresh** at its own Claim, so a reporter's answer between rounds reaches it.
- **Workflows that do not declare the input read no comments,** and their Claim, Archive and Run Ledger records are unchanged.
