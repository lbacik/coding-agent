---
status: accepted
---

# Durable Intake and Operator Commands serialise with the Claim and with finalization, and never write lifecycle state

Intake (`open`/`closed` plus the pending `stop`) and every Operator Command
live in the Run Ledger's SQLite file, in separate tables owned by a separate
module (`operator.commands`), and serialise with the Attempt lifecycle at
exactly two points: the Claim boundary and finalization. Commands gate only
the decision to open a Claim and never write lifecycle state: no command
writes an Attempt row, an outcome or an effect. Decided in
[Operator drain, un-drain and command lookup](https://github.com/lbacik/coding-agent/issues/249),
adoption step 4 of the operator contract ([parent](https://github.com/lbacik/coding-agent/issues/245)).

## Context

The operator contract (major 1, §6.2, §7.5, §8) requires durable commands:
a committed command is stored on the state volume for the volume's lifetime
and never deleted; exit 0 means durably recorded; every command reaches a
final acknowledgement (`completed`, `superseded` or `not_fulfilled`); and
`stop` serialises with the Claim boundary — committed before it, no Claim
opens. Intake persists across restarts; a fresh volume starts open.

The previous slice (#248) put instance metadata in the ledger file with
separate tables and its own connection, and served `identity` and a minimal
`status` from an in-process listener. This slice adds the `stop`/`resume`
drain and the `command` lookup on the same foundation.

## Decision

- **Same file, separate tables, separate module.** `operator_intake` (one
  row: `state`, the pending `stop`'s request ID and sequence, `updated_at`;
  absent means open) and `operator_commands` (one row per committed
  command keyed by request ID with an autoincrement `sequence`, `kind`,
  canonical `arguments` JSON, `payload_digest`, `repository`,
  `acknowledgement`, `detail`, timestamps; rows never deleted) live in the
  Run Ledger's SQLite file. Both are created with `CREATE TABLE IF NOT
  EXISTS`, cover fresh volumes only, and have no migration. All SQL for
  them sits in `operator/commands.py`; `ledger/store.py` and
  `operator/store.py` only call `ensure_operator_tables`. Volumes created
  by the previous slice (#248) lack the pending-stop columns, so
  `ensure_operator_tables` tolerates them with `ALTER TABLE ... ADD
  COLUMN`: this is restart tolerance, not a migration — no backfill, no
  versioning, and fresh volumes carry the full schema from creation. The restart keeps
  the recorded state: the Intake row is inserted `ON CONFLICT DO NOTHING`,
  so a fresh volume starts open and a restart preserves closed Intake.
- **Two serialisation points, both through SQLite's write lock.** The
  Intake check and the commit of a new Attempt row happen in one
  `BEGIN IMMEDIATE` write transaction (`open_attempt_if_intake_open`): a
  `stop` committed before the boundary prevents the Claim from opening
  (nothing assigned, nothing written), while one committed after stays
  `accepted`. The transition of an Attempt to `finished` or `released`
  completes a pending `stop` in the same write transaction
  (`set_attempt_state`), so the drain holds across the normal loop, the
  infrastructure-failure path, the Claim read-back release and every
  recovery settling path. The listener and the Worker thread each use
  their own connection; the listener's per-request connection carries a
  10 s busy timeout, so a mutation that cannot commit within 10 s answers
  `internal` with `stored: false`.
- **Commands never write lifecycle state.** `commit_stop`/`commit_resume`
  write only the Intake row and command rows. Commands gate only the
  decision to open a Claim: `stop` never interrupts model work, never
  diverts an Attempt at the Publication Gate and never acts as SIGTERM.
  `resume` only reopens Intake and supersedes a pending `stop`; it never
  starts a process or resumes interrupted work.
- **Startup reconciliation.** Every `accepted` command an earlier process
  left is resolved against the ledger before the first Claim boundary:
  one whose Attempt is already final becomes `completed`; one whose
  Attempt is still unfinished stays pending, so the drain still holds.
  Nothing is left `accepted` forever across a restart.
- **SIGTERM is unchanged** and never writes durable Intake: the
  process-local shutdown flag stops intake at once, while the durable
  row is untouched by signals and by `docker stop`.

## Considered options

**A separate SQLite file for commands.** Rejected. One file keeps the two
serialisation points in one write transaction: the Intake check with the
Attempt insert, and the pending-stop completion with finalization. Two
files would need a two-phase dance across locks and could leave a stop
`accepted` after its Attempt finalised.

**Serialising through a mutex or a queue instead of the write lock.**
Rejected. The Worker thread and the listener thread already meet only in
SQLite; an in-process lock would not survive the process boundary the
contract's restart scenarios require (stop, then crash, then restart).

**Completing the stop at outcome recording.** Rejected. The contract is
explicit: recording an outcome is not enough — the command becomes
`completed` only when the Attempt reaches its final lifecycle state, so
completion rides on `finished`/`released`, the same states recovery
settles to.

## Consequences

- `status` reports `draining` (Intake closed, Attempt active) and `paused`
  (Intake closed, nothing active) alongside `idle`/`active`, and lists
  `recent_commands` (at most 20, newest last).
- Idempotency is on the canonical payload (`kind` plus `arguments`;
  `--expect-repo` is not part of it): the same ID with the same payload
  replays the stored record, a different payload mismatches, and a wrong
  repository stores nothing.
- `sequence` increases strictly in commit order because all commits hold
  the one write lock.
