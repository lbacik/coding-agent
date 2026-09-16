---
status: accepted
supersedes: 0011-the-pinned-prefix-is-a-region-not-a-rule.md
---

# Context-window crossings use a durable handoff

The oldest-Exchange-Unit eviction policy is replaced by a bounded handoff into a fresh work node. Decided in [issue #44](https://github.com/lbacik/coding-agent/issues/44), after the implementation-ready [planning map #40](https://github.com/lbacik/coding-agent/issues/40) was completed.

## Decision

Each **Pinned Model** has a `ContextWindowConfig` containing a deterministic estimated context threshold, request overhead, safety margin and a separate handoff output-token cap. The estimate includes the Pinned Prefix, whole Exchange Units and bound tool schemas. Provider-reported usage is recorded separately and never substituted for the local control signal.

Before a work-node request would exceed the usable threshold, that node stops. A dedicated handoff node receives the current explicit context and may make exactly one model invocation, without tools. Its response must be one valid versioned **Continuation Snapshot** containing:

- the Attempt id and Base Revision;
- the current branch and commit;
- changed files;
- commands and their results;
- available Validation Evidence;
- unresolved questions; and
- the next intended action.

The snapshot is redacted before persistence, stored as an Attempt artifact, and addressed by its SHA-256 digest. A `handoff` Run Ledger event records that digest, artifact reference, snapshot version, context estimate, handoff cap and provider-reported handoff usage. The same usage is flushed through the Attempt-wide token and cost ledger before the event is recorded.

The next work node receives only the intact Pinned Prefix and the snapshot. It never receives old Exchange Units, a reconstructed assistant turn or provider-specific reasoning from the previous node. Within each node, the existing verbatim assistant-turn rule and whole-Exchange-Unit invariant remain unchanged.

On restart, a recorded digest is verified against its artifact and reused. A malformed, incomplete, over-limit, unpersistable or unverifiable snapshot produces a redacted failure artifact and ends the Attempt as `failed` / `handoff-failure`; no continuation is started. A Pinned Prefix that cannot fit with request overhead is refused before model work begins.

## Why this replaces eviction

Eviction preserves a conversation by silently deleting evidence that later turns may need, and repeated threshold crossings can repeatedly rebill the fixed prefix. Handoff makes the boundary explicit and durable. The new node has a bounded, provider-neutral state contract, while the old node's opaque provider blocks remain local to the node that received them rather than being guessed at during reconstruction.

## Consequences

The Run Ledger and Attempt report expose handoff count, snapshot digest/version, estimated context, handoff limit, provider-reported input/output usage and cumulative Attempt-wide usage for measurement. A snapshot is an artifact, not a temporary file, so the digest is the idempotency key across process loss.

The handoff itself consumes the Attempt's existing budgets. It cannot open a second budget, use a fallback model, call tools, or turn an invalid partial answer into a continuation. A failed handoff is therefore a terminal Attempt classification and is not eligible for deterministic delivery finalization.

The old `evict_oldest` helper remains only as compatibility support for callers from the pre-handoff S3 slice; production Attempts use the context-window configuration and handoff path.

