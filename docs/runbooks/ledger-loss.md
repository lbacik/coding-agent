# Runbook: Run Ledger loss

Ledger loss — the state volume's `run-ledger.sqlite3` deleted, corrupted
beyond repair, or replaced by an empty file — is **outside the MVP's
guarantees**. Recovery ([contract §17](../contract/mvp-runtime-contract.md#17-recovery))
rebuilds Attempt state from the Run Ledger alone; with the ledger gone
there is no Attempt to recover, only the remote effects the lost Attempts
left behind. This runbook says what still holds, what does not, and how a
human returns the Worker to a clean state. It links the contract rather
than restating it.

## What still holds

- **No duplicate pull request or terminal comment.** Every write is looked
  up remotely before it is performed ([contract §13](../contract/mvp-runtime-contract.md#13-external-writes)):
  a pull request is found by head branch plus Attempt Marker, a terminal
  comment by the full paginated comment list filtered to the Agent
  Identity's id and the marker. A restarted Worker re-checks the same
  lookups, so a surviving remote effect is adopted as the receipt, never
  duplicated.
- **The volume lock still serialises Workers.** Loss of the ledger does not
  lose the lock: a second Worker on the same state volume is still refused
  ([contract §3](../contract/mvp-runtime-contract.md#3-the-workers-lifecycle)).
- **A fresh ledger is a valid ledger.** The tables are created with
  `CREATE TABLE IF NOT EXISTS` and there is no schema version
  ([contract §17](../contract/mvp-runtime-contract.md#17-recovery)), so an
  empty file behaves exactly like a first start.

## What does not hold

- **An Agent Identity assignment with no ledger record is the owner's own**
  ([ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md),
  [contract §17](../contract/mvp-runtime-contract.md#17-recovery)). The
  Worker never removes it, and the issue stays ineligible for Selection.
  Only a human releases it; see below.
- **Attempt numbering restarts.** The next Attempt number is one more than
  the highest `n` the ledger holds ([contract §5](../contract/mvp-runtime-contract.md#5-claim)).
  After a loss the ledger holds none, so the next Attempt reuses
  `#<issue>/1` and its branch `agent/<issue>/1-<slug>` may collide with the
  lost Attempt's surviving branch. The Worker never deletes a branch
  ([contract §13](../contract/mvp-runtime-contract.md#13-external-writes));
  rename or delete the orphaned branch first if the names would collide.
- **Recorded outcomes are gone.** The terminal comment and pull-request
  bodies were rendered once and stored in the ledger
  ([contract §14](../contract/mvp-runtime-contract.md#14-terminal-outcomes));
  after a loss nothing re-sends them, and nothing re-decides them. An
  issue whose work was pushed but never commented on needs a human to read
  the branch and decide.
- **Usage, budgets and progress events are gone.** Meters restart from
  zero; a Continuation Snapshot published in a terminal comment still
  round-trips through the comment lookup
  ([contract §16](../contract/mvp-runtime-contract.md#16-continuation)).

## How a human releases an orphaned assignment

The Agent Identity is the repository owner's own account
([contract §18](../contract/mvp-runtime-contract.md#18-identity-and-authorization)),
so its assignment is indistinguishable from the owner assigning themselves.
After a ledger loss every surviving assignment is therefore the owner's to
release ([contract §17](../contract/mvp-runtime-contract.md#17-recovery)):

1. List the issues assigned to the owner's account that the Worker had
   claimed (Selection Label present, `ready-for-human` absent, no terminal
   comment carrying an Attempt Marker).
2. For each issue that should go back to the queue, **remove the owner's
   assignment** through the issue page or the API. Do not add or remove
   any label: the Selection Label is the human's authorisation to run
   ([contract §18](../contract/mvp-runtime-contract.md#18-identity-and-authorization)),
   and it is still valid.
3. Where a surviving `agent/<issue>/<n>-<slug>` branch would collide with
   the next Attempt's branch, rename or delete it first.
4. Restart the Worker. It claims the issue as `#<issue>/1` under the
   current configuration — a new Attempt, not a Continuation
   ([contract §16](../contract/mvp-runtime-contract.md#16-continuation)).

Do not hand-edit a replacement ledger: a forged `attempts` row with a
guessed Effective Configuration is exactly what the startup refusal
(REC-CFG-2) exists to reject.
