# Runbook: Run Ledger loss

> **Status:** this runbook describes the step-workflow runtime of the
> [step workflow runtime contract](../contract/step-workflow-runtime-contract.md)
> once it ships. Until the cutover slice switches the Worker to that
> runtime, the MVP runtime is unchanged, and the
> [previous version of this runbook](https://github.com/lbacik/coding-agent/blob/86f1759f5f0e6846cdce782cbec5dd82152c4a3d/docs/runbooks/ledger-loss.md)
> applies to it.

Ledger loss — the state volume's `run-ledger.sqlite3` deleted, corrupted
beyond repair, or replaced by an empty file — is **outside the runtime's
guarantees**. A restarted Worker settles and finishes what the Run Ledger
records, and nothing else ([ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md));
with the ledger gone there is no Attempt to finish, only the remote effects
the lost Attempts left behind. This runbook says what still holds, what does
not, and how a human returns the Worker to a clean state. It links the
decisions rather than restating them.

## What still holds

- **No lost write is repeated.** Every external write carries its Effect
  Intent key in its Attempt Marker
  ([ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)),
  so a landed write stays identifiable on GitHub by that key. With the
  ledger gone the Worker holds no Effect Intent to settle, so it never
  re-sends a lost Attempt's write, and a later Attempt, numbered past the
  lost ones (below), writes under keys of its own.
- **Attempt numbering continues.** An Attempt is identified as
  `<workflow>#<N>/<n>`, with `n` counted per Workflow and input. After a
  ledger loss, `n` is rebuilt from what survives on GitHub: the Attempt
  branches `agent/<workflow>/<issue>/<n>-*` and the Attempt Markers, such
  as those on Claim Comments and terminal comments
  ([#286 resolution](https://github.com/lbacik/coding-agent/issues/286#issuecomment-6799233346),
  point 13). The next Attempt is therefore not numbered `1` again, and its
  branch does not collide with a lost Attempt's surviving branch.
- **The volume lock still serialises Workers.** Loss of the ledger does not
  lose the lock: a second Worker on the same state volume is still refused.
- **A fresh ledger is a valid ledger.** An empty file behaves like a first
  start: it holds no unfinished Attempt, so there is nothing to recover and
  no legacy Attempt to refuse startup on
  ([#283 resolution](https://github.com/lbacik/coding-agent/issues/283#issuecomment-6799526314),
  point 17).

## What does not hold

- **A surviving Claim is orphaned.** A Claim is the Agent Identity's
  assignment plus a Claim Comment
  ([ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md)).
  A lost Attempt never published its terminal comment, so its Claim Comment
  stays in force, and its input, which still has an assignee, is not
  eligible for any Workflow. Recovery releases only a Claim whose intent the
  ledger recorded, so only a human releases this one; see below.
- **Recorded outcomes and unsettled effects are gone.** The Terminal
  Outcome, its rendered terminal comment and every Effect Intent and Effect
  Receipt lived in the ledger
  ([ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)).
  After a loss nothing settles a write that was in flight, nothing posts a
  missing terminal comment, and nothing re-decides an outcome. An input
  whose work was pushed but never commented on needs a human to read the
  branch and decide. Step Artifacts, Execution Summaries and Transcripts left
  on the state volume were referenced from the ledger and are not an
  authority of their own; nothing reads them back.
- **Usage, budgets and Step Execution records are gone.** Nothing of a lost
  Attempt's spend, Routes or progress events is recovered.

## How a human releases an orphaned Claim

The Agent Identity is the repository owner's own account
([ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md)), so its
assignment alone is indistinguishable from the owner assigning themselves;
the Claim Comment's Attempt Marker is what identifies a Worker's Claim.
After a ledger loss a surviving Claim is the human's to release
([#286 resolution](https://github.com/lbacik/coding-agent/issues/286#issuecomment-6799233346),
point 12):

1. List the open issues and pull requests that carry a Claim Comment of the
   Agent Identity and no terminal comment for the same Attempt.
2. For each input that should go back to the queue, **remove the Agent
   Identity's assignment and delete the Claim Comment** through the GitHub
   page or the API. Do not add or remove any label: the Selection Label is
   the human's authorisation to run, and its presence still authorises.
3. Restart the Worker. It claims the input under the current configuration
   as a new Attempt numbered past the lost ones; the runtime does not
   continue the lost Attempt's work
   ([#285 resolution](https://github.com/lbacik/coding-agent/issues/285#issuecomment-6800378643),
   point 11).

Do not hand-edit a replacement ledger: a forged Attempt record with a
guessed Effective Configuration is exactly what the refusal to start on a
missing or unreadable pinned record exists to reject
([ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md)).
