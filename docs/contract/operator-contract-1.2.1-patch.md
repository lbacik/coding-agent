# Operator contract patch `v1.2.1`: coding-agent mapping (record for issue #615)

Parent: [lbacik/coding-agent#605](https://github.com/lbacik/coding-agent/issues/605).
Contract issue: [lbacik/agent-operator-contract#49](https://github.com/lbacik/agent-operator-contract/issues/49).
Producer adoption: [lbacik/coding-agent#616](https://github.com/lbacik/coding-agent/issues/616)
(blocked by this issue).

## What this record is

The informative coding-agent mapping in §16 of the shared
`lbacik/agent-operator-contract` repository still describes the removed
Context-Window Handoff (commit `82591a8`, ADR 0033): Handoff Threshold,
`native.handoff_threshold_tokens`, `continuations` as Context-Window
Handoffs, and a `handoff` usage cell. The producer still publishes those
legacy figures for 1.1 compatibility and waits for this mapping change
before dropping them.

This file records the prepared patch release for producer adoption. The
change is docs-only plus the distribution version bump: the wire contract
(schemas, envelope, operations) and the minor version are unchanged, so the
release is `v1.2.1`, not a new minor. The contract-side branch carrying the
edit is `agent/issue-49` in `lbacik/agent-operator-contract`; publication
(push, merge, tag, release) needs push rights the agent container does not
have, so a human (or the driving process) publishes it, then #616 adopts
the tag on the producer side.

Intended tag: **`v1.2.1`**.

## Prepared change 1: normative §9.2 wording (non-restrictive)

`docs/spec.md`, context-figure definitions. Ending the Step Execution
counts as stopping, so the parenthetical must not limit how an engine stops
growing the conversation:

- Before: `**`context_threshold`:** the main conversation's context size at
  which the engine stops growing it (by a handoff or a compaction); the
  value compared with `context_used`.`
- After: `**`context_threshold`:** the main conversation's context size at
  which the engine stops growing it; the value compared with `context_used`.`

No schema, enum, basis or operation changes. §7.2 keeps `handoff` in the
open set of Usage Origins (other backends use it); only the coding-agent
note below stops reporting it.

## Prepared change 2: §16 coding-agent note (informative, replacement text)

The `context_window`, Budget, `cost_usd`, `subagents_*`, upgrade-history,
`turns`, Attempt Archive, capabilities and no-legacy-grammar bullets are
unchanged. The replaced bullets read, in full:

- `context_threshold`: `configured`, the current agent Step Execution's
  selected Usable Threshold: the step's soft Context Threshold where the
  step configures one, else its hard or pin-default Context Threshold, less
  the safety margin and the request overhead. Threshold figures use
  configured tokens. Where a step configures both soft and hard thresholds,
  `native.context_hard_usable_threshold_tokens` (tokens, `configured`) also
  reports the hard Usable Threshold.
- `context_used`: `estimated`, the exact next main-request Conversation
  Context the same Step Execution compares with its selected Usable
  Threshold. Its `reason` is "anchored on provider-reported input" when the
  value is the last provider-reported input of the conversation plus a local
  estimate of what was appended since, and "local estimate, no measured
  response yet" before the first measured response. Sub-agent conversations
  have their own Conversation Context and are excluded; a Sub-agent's
  results contribute to a main request only when returned to it. The current
  pair reads `unavailable` until the receiver of an inherited conversation
  evaluates its own opening request, and is omitted during operations and
  finalisation phases. Each Step Execution starts a new conversation, so the
  value restarts with it.
- `context_peak`: `estimated`, the Attempt-wide peak of `context_used`,
  kept across Step Executions and conversations, including refused requests.
- `native.context_estimate_tokens` is removed in 1.1.
  `native.handoff_threshold_tokens` and `continuations` are not reported.
- `budget.by_origin`: `main` and `subagent` cells only. The AI-assisted
  Handoff receiver's main requests and tools project under `main` once,
  attributed to its Step Execution. Per-bucket costs, response counts and
  per-step spend stay in the Run Ledger and the export. `cache_hit_rate` is
  derived from the `main` cell. The legacy `handoff` cell is not reported.
- Pickup counts for the current opted-in Attempt read as native count
  figures when known: pickups used with basis `measured` and the pickup
  limit with basis `configured`.
- Native Contract Events (redacted, `native.` prefix, consumers MAY ignore)
  project recorded facts: an actual runtime threshold crossing with its
  compared value, basis and applicable Usable Thresholds; the AI-assisted
  Handoff result or refusal; entry to Hard Handoff; verified Round Handoff
  readiness; and pickup exhaustion. An ordinary Step Result that happens to
  use a reserved spelling emits no crossing event. Readiness is reported
  only after the terminal comment, Pickup Label and assignment release are
  confirmed, including after replay. The durable exhaustion and
  human-reauthorisation diagnostic stays visible during idle Intake, after
  restart and on an independent Worker.
- coding-agent produces this mapping from the release that closes
  [lbacik/coding-agent#616](https://github.com/lbacik/coding-agent/issues/616);
  earlier releases report the 1.2 mapping published at `v1.2.0`, which keeps
  a `handoff` cell that is always zero, a `continuations` figure that is a
  measured zero and `native.handoff_threshold_tokens` for 1.1 compatibility.

## Prepared change 3: version bump (one version line)

- `pyproject.toml`: `version = "1.2.0"` → `version = "1.2.1"`, plus
  `uv lock` for the updated lockfile. Tag `v1.2.1` must equal the
  distribution version (CI `package` job); minor stays `2`, so
  `tests/test_version.py` (`test_distribution_version_names_the_contract_version`)
  keeps passing.

## Publication (human/driver: push rights required)

1. Push `agent/issue-49` in `lbacik/agent-operator-contract`, open the pull
   request against `main`, merge it.
2. Tag the merge commit `v1.2.1` and publish the GitHub release
   (`gh release create v1.2.1 --target <merge-sha> --title v1.2.1 --notes ...`).
   CI runs on the tag (`ruff format --check`, `ruff check`, `mypy`,
   `pytest`, producer suite, package checks).
3. Close [lbacik/agent-operator-contract#49](https://github.com/lbacik/agent-operator-contract/issues/49)
   with the release note.

## Producer adoption (issue #616, after the tag lands)

- Bump the dev dependency
  (`agent-operator-contract @ git+https://github.com/lbacik/agent-operator-contract@v1.2.1`),
  `uv lock`, `src/coding_agent/conformance/__init__.py` (`CONTRACT_PIN`),
  related docstrings, `CONTEXT.md` pinned revision and affected fixtures.
- Drop the legacy `handoff` cell, `continuations` and
  `native.handoff_threshold_tokens` projections; project the selected Usable
  Thresholds, exact compared `context_used`, Attempt-wide peak, receiver
  attribution, pickup counts and native events per this mapping.

## Acceptance trace (issue #615)

- Selected Usable Threshold, optional hard threshold, exact compared context
  with estimated basis/reason, phase availability and Attempt-wide peak:
  `context_threshold`, `context_used` and `context_peak` bullets above.
- `main`/`subagent` origins, receiver attribution, pickup counts, native
  events, and removal of the legacy cell/figures: `budget.by_origin`,
  pickup-count and native-events bullets above.
- Wire contract and minor version kept: schemas untouched, §9.2 fix is
  non-restrictive wording only, release is a patch; tag recorded here as
  `v1.2.1` for #616.
