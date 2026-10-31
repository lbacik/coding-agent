---
status: accepted
---

# Recovery finishes a recorded outcome from the Run Ledger, never from a framework checkpoint

A restarted Worker recovers every unfinished Attempt from the Run Ledger alone. It re-enters the fixed Attempt graph at a named node, with state rebuilt from the ledger, and no model call is made. The MVP uses no LangGraph checkpointer and no Lease. Every Terminal Outcome is recorded in the ledger, together with its rendered writes, before the first terminal write. Recovery finishes a recorded outcome and never reinterprets it. Decided in [How do Attempts recover and publish without duplicating GitHub effects?](https://github.com/lbacik/coding-agent/issues/74), part of the planning map [Design: YAML-configured LangGraph MVP with simple-coding-agent parity](https://github.com/lbacik/coding-agent/issues/70).

## Context

The MVP runtime is a LangGraph graph, and LangGraph's own answer to restart is a checkpointer that resumes a thread from its last saved state. The parity contract ([#71](https://github.com/lbacik/coding-agent/issues/71)) has already deferred model resumption (v1 T13/T14). Before the Delivery Intent, an interrupted Attempt is not resumed: it ends `failed`/`interrupted` with its pushed work kept. After the Intent, publication completes as itself ([ADR 0003](./0003-delivery-intent.md)). What remains to recover is therefore finishing writes, not continuing work.

The parity reference shows what happens when recovery decides the outcome again from a phase marker. `simple-coding-agent` maps any checkpoint in its PUSHING or PUBLISHING phase that has no handoff note to a complete Attempt. Attempts that ended red also pass through those phases. A crash during their release therefore makes the restart open a pull request for incomplete work. The result comment says otherwise, because its marker deduplication keeps the original text.

## Decision

- **One durable authority.** The Run Ledger, a single SQLite file on the state volume, holds every Attempt's lifecycle state, its pinned Effective Configuration, its recorded Terminal Outcome and the intent and receipt of each external write.
- **No framework checkpoint.** Graph state is ephemeral. Recovery derives the entry state of the node it enters (`terminal_writes`, or `publish` when a Delivery Intent exists) from the ledger.
- **No Lease.** One Worker holds an exclusive lock on its state volume. Once it holds the lock, every open Attempt it finds was orphaned by a dead process, so a heartbeat would add nothing.
- **The outcome is recorded before it is published.** Every Terminal Outcome, not only a delivery, is written with its reason, rendered comment and pull-request bodies and the list of writes it calls for, before the first of those writes. A restart completes the remaining writes of that outcome. Only an Attempt with no recorded outcome is given one at recovery: `failed`/`interrupted`, or `abandoned` when a human has closed the issue or removed the Selection Label in the meantime.
- **Recovery uses pinned values only.** Repository, label names, base branch, Agent Identity and rendered bodies come from the ledger, whatever the current Workflow Configuration says. A missing or unreadable pinned record is a startup refusal, never a fallback to the current configuration.

## Considered options

**A LangGraph checkpointer per Attempt (`thread_id = attempt_id`).** Rejected for the MVP. Nothing is resumed mid-node, so a checkpoint would only duplicate state the ledger must hold anyway, and it could never be the authority on external effects. Two sources of truth that can disagree after a crash are worse than one. A checkpointer becomes worth having again if model resumption (T13/T14) returns; every assistant turn already round-trips through its serializer ([#73](https://github.com/lbacik/coding-agent/issues/73)).

**Re-deriving the outcome at recovery from the phase reached.** Rejected because of the reference defect above. A phase says where the process was, not what the Attempt had decided.

**A heartbeat Lease (v1 contract).** Deferred. It distinguishes a live Worker from a dead one only when more than one process can reach the same Attempts, and worker pools are out of scope for the map.

## Consequences

- **Recovery cannot spend model budget or change code.** It pushes already-made local commits fast-forward only, and performs GitHub writes. Nothing else.
- **Any outcome can be interrupted and still reach GitHub exactly once.** This covers `failed-limit`, readiness refusals and `delivered` alike, because each one's comment and labels are written from the recorded outcome rather than recomputed.
- **The ledger is a single point of loss.** Remote lookups by Attempt Marker still prevent a duplicate pull request or terminal comment after a ledger loss. An Agent Identity assignment without a ledger record is indistinguishable from the owner's own ([ADR 0015](./0015-the-claim-is-an-assignment-of-the-agent-identity.md)), so releasing it after a ledger loss is a runbook step, not a guarantee.
- **Reversing this** means deciding again which store answers "did this write land" when the two disagree.
