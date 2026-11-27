---
status: accepted
---

# The Issue Tracker and the Code Host are reached only through their ports

Part of [#663](https://github.com/lbacik/coding-agent/issues/663). The domain
language and the step-workflow runtime contract assume GitHub: `CONTEXT.md`
defines the **Target Repository** as "the one GitHub repository",
[ADR 0005](0005-agent-identity-is-the-repository-owner.md) makes the Agent
Identity the repository owner, and contract
[§18](../contract/step-workflow-runtime-contract.md#18-identity-and-authorization)
and the S0a runbook assume a fine-grained personal access token. The
refactor slices around this issue moved code without deciding anything —
provider-neutral failure types, the frozen `CodeHostProfile`, the
`CodeHostStartupPort` with transport owned in the adapter — but moving code
cannot answer the questions below. A Jira adapter has no spec until they are
answered.

## Context

A future Worker may read its inputs from one system and push its work to
another: issues tracked in Jira, code hosted on GitHub. Today the two
coincide, so every rule names GitHub once and means both. The split needs
names before it needs code, because the contract, the configuration shape
and the credential discipline all hang off the names.

Naming: **"Forge" was considered and is rejected.** `docs/ops/workflow-configuration.md`
([External builders: the Agent Forge sequence](../ops/workflow-configuration.md#external-builders-the-agent-forge-sequence)),
`README.md` and the `Dockerfile` already use "Agent Forge" and "Forge
adapter" for the external image builder. One word for two concepts in one
repository is a defect (the same reason `Gate` is not called a checkpoint).
The split is therefore named **Issue Tracker** and **Code Host**, and each
side is reached only through its port: the tracker through
`StepTrackerPort` (`src/coding_agent/inputs/port.py`), the host through its
startup and runtime ports, with transport owned in the adapter in both cases.

## Decision

1. **Terms.** `CONTEXT.md` gains **Issue Tracker** — the system that holds
   Workflow Inputs, selection labels and comments — and **Code Host** — the
   system that holds the repository, branches, diffs and pull-request refs.
   The **Target Repository** is redefined against the Code Host: the Code
   Host repository, named by the Workflow Configuration, that the Worker
   pushes to and opens pull requests against. Where the tracker is the Code
   Host itself — the GitHub arrangement, and the default wherever
   `target.tracker` is omitted — it is also the repository whose issues or
   pull requests the Worker may select and work on. The Target Issue's
   provenance moves to the Tracker. No other glossary term changes meaning.

2. **Selection-Label authorisation outside GitHub.** The contract keeps its
   `LabeledEvent`-shaped authorisation surface (actor id, actor login, actor
   type, timestamp): whatever the tracker, the Claim read-back tests one
   event's actor. What changes is where the event comes from and which rule
   tests it. On GitHub nothing changes: the latest `labeled` timeline event
   for the Selection Label, tested under the detected identity mode (§5.1).
   On Jira the adapter sources the event from the issue's `labels`-field
   changelog entry, falling back to the relevant status-transition actor
   only where labels are automation-set. The shared/dedicated
   repository-owner identity modes are retired for non-GitHub trackers —
   Jira has no repository-owner identity — so only the dedicated-mode rule
   applies there: the actor is neither the agent account nor a
   bot/automation actor. No YAML key selects the mode, on any tracker; the
   existing principle is retained.

3. **Closing on delivery.** The default is link-by-key only in the
   pull-request body — no implicit transition. A ready pull request on
   another tracker carries the tracker's key link (Jira: the issue key),
   never an instruction to the tracker to change state. Any Jira state
   transition is an explicit, separately-gated Runtime Operation recorded as
   a new terminal-write effect kind under
   [ADR 0028](0028-every-external-write-is-an-intent-settled-once.md)
   (Effect Intent settled once, with receipt lookup), never an implicit side
   effect of delivery. Transitions the tracker's own development integration
   performs on its own (Jira development-integration auto-transitions) are
   that external system's business, not the Worker's.

4. **Input identity.** One Workflow Configuration pins exactly one Jira
   project, so the adapter maps `PROJ-N` to integer `N` and the Attempt Id
   grammar (`<workflow>#<N>/<n>`, §6.3) is unchanged: numbers are still
   plain integers, branches and markers keep working, and ledger-loss
   numbering rebuilds as today. Cross-project work means a second Worker,
   consistent with one-Worker-one-repository. Opaque input refs, with a
   ledger and an Attempt Id migration, are explicitly deferred, not built:
   they would change the Attempt Id grammar, every Attempt Marker, and the
   ledger-loss rebuild, for a generality no planned deployment needs.

5. **Accounts and credentials.** Two systems mean two Agent Identities — a
   tracker account and a code-host account — and two secrets
   (`target.tracker.token-env`, `target.code_host.token-env`). Each gets its
   own startup checks: an identity probe for the numeric id, a permission
   check on its own side, and a token-kind check analogous to the §18 rule
   where the platform issues distinguishable token kinds, plus rate-limit
   headroom on the side whose quota selection consumes. Both secrets are
   registered in the single existing redaction registry — the §18 choke
   point, still exactly one — filtered from logs, tool arguments and
   results, ledger records and child-process environments; git uses the
   code-host credential through the credential helper. The GitHub
   single-credential arrangement (ADR 0005) is unchanged: where tracker and
   host coincide, one identity and one secret remain.

6. **Configuration shape.** `target.tracker` / `target.code_host` in the
   Workflow Configuration, with an omitted `tracker` meaning issues come
   from the Code Host. `base-branch` and `profile` stay at `target` level:
   they describe the code side. A `jira` tracker entry pins its project
   (exactly one per configuration, decision 4) beside its credential name.
   The structural `target` change follows the repo's own precedent: the
   Workflow Configuration moves to `version: 5` — a new
   `workflow-v5.schema.json` alongside v4, the old version refused with a
   migration diagnostic naming the moves, no converter and no dual engine —
   and the Effective Configuration record moves with it. Strict parsing is
   retained. The operator guide and contract §§2/5/6/14/18 describe the new
   shape. The v5 code migration itself is a build follow-up; this ADR
   decides the shape so that follow-up has a spec to implement.

## Considered options

- **"Forge" / "Forge adapter" for the split.** Rejected: the Agent Forge
  collision above. The guide's external-builder sequence keeps the word.
- **A Jira status transition as the primary authorisation signal.**
  Rejected: where labels are automation-set the transition actor is the
  automation, not the human. The `labels` changelog entry is primary; the
  transition actor is a fallback for exactly that case.
- **Applying the shared-identity rule to Jira via a configured owner.**
  Rejected: there is no repository-owner identity to compare against, and a
  YAML-selected mode would reintroduce the knob #288 point 1 removed. The
  dedicated-only rule needs no comparison and no knob.
- **Implicit Jira transition on delivery (or `Closes PROJ-N` semantics).**
  Rejected: it would make delivery decide tracker state, against the
  Workflow-policy/runtime-mechanics split
  ([ADR 0027](0027-workflow-policy-is-separate-from-runtime-mechanics.md))
  and the one-intent-per-write rule (ADR 0028). Link-by-key is the default;
  a transition is a gated operation the Workflow routes to.
- **Opaque input refs with a ledger and Attempt Id migration.** Rejected
  (deferred): breaks the Attempt Id grammar, markers, branches and
  ledger-loss rebuild for unneeded generality. One project per
  configuration keeps integers.
- **One shared credential for tracker and host.** Rejected: different
  systems, different accounts, different permission surfaces. Two
  identities, one redaction registry.
- **A required `tracker` block.** Rejected: omitted-means-host keeps every
  existing file meaningful through the migration and states the common
  case once.

## Consequences

- **`StepTrackerPort` is one port implementation's contract, not the
  default.** Its `LabeledEvent` shape becomes the cross-tracker
  authorisation surface; `fetch_repository_owner_id` and identity-mode
  detection stay GitHub-specific, with the dedicated-only rule for other
  trackers. `GitHubStepTracker` behaviour is unchanged.
- **The pure authorisation logic** (`detect_identity_mode`,
  `label_authorised`) extends to the dedicated-only rule for non-GitHub
  trackers; no mode knob enters the YAML.
- **Delivery never transitions.** A future Jira transition operation joins
  the catalog as a terminal-write effect with its own intent and receipt;
  until it exists, link-by-key is the whole behaviour.
- **Attempt identity stays integer.** The Jira adapter owns the
  `PROJ-N ↔ N` mapping inside its pinned project; nothing else learns
  about keys.
- **Two-credential discipline** (per-identity startup checks, joint
  redaction-registry membership) holds wherever tracker and host differ;
  the GitHub arrangement keeps one of each.
- **No Jira adapter code is written under this ADR.** It is design and
  contract only; the v5 migration and the adapters are build follow-ups
  with their own acceptance rows.
