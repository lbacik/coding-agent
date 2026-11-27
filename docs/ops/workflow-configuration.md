# Workflow Configuration: operator guide

**Status: describes the running Worker (`version: 4`).** This is an operator
guide to writing the Workflow Configuration YAML for step Workflows. It is
never a second source of truth: behaviour is defined by [the step-workflow
runtime contract](../contract/step-workflow-runtime-contract.md) alone, and
this guide restates only what an operator needs to write a valid file,
linking the contract section wherever a rule is summarised. Where this guide
and the contract disagree, the contract wins.

**Upgrading.** This release runs only `version: 4` step-workflow files; a
`version: 3` file is refused with a migration diagnostic and there is no
converter (`version: 2` keeps its own refusal). Upgrading is a coordinated operator sequence — drain with the
compatible release, back up while stopped, upgrade and re-prepare — described
in [Upgrading: drain, backup and rollback](#upgrading-drain-backup-and-rollback).
The archived `version: 2` and `version: 3` shapes are historical and unsupported.

The complete example is [`examples/workflows.yaml`](../../examples/workflows.yaml):
an `implement` Workflow (issue in, pull request out) and a `review` Workflow
(existing pull request in, review out). [A walk through it](#walk-through-the-example)
closes this guide.

Related: [ADR 0027](../adr/0027-workflow-policy-is-separate-from-runtime-mechanics.md)
(Workflow policy is separate from runtime mechanics) and
[ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)
(every external write is an intent settled once).

## What the file decides

One YAML file declares the Target Repository, the Pinned Model(s) and one or
more Workflows. A Workflow is the whole policy of one scenario: which inputs
it takes, which steps run in which order, what each step may do, how much it
may spend, and how it ends. The runtime knows no scenario: nothing in it
knows that a Workflow "implements" or "reviews"; that lives in the YAML and
in the instructions its steps activate (contract
[§1](../contract/step-workflow-runtime-contract.md#1-layers-and-ownership)).

**One Worker runs one Workflow.** A process reads the file and every prompt
file it names once at startup, runs exactly the Workflow selected with
`--workflow <id>`, and pins that Attempt's
[Effective Configuration](#effective-configuration-and-digest) at Claim.
Two scenarios mean two Workflows and two Workers. Editing the file affects
the next Attempt, never one already open (contract
[§2](../contract/step-workflow-runtime-contract.md#2-workflow-configuration),
[§4](../contract/step-workflow-runtime-contract.md#4-the-workers-lifecycle)).

The file has four required top-level keys, an optional `skills` declaration
and an optional `worker` block. All keys are kebab-case.

```yaml
version: 4
target: {...}      # the Target Repository
models: {...}      # Pinned Model definitions, referenced by id
skills: [...]      # optional Skill Bundle membership (missing or [] = empty bundle)
workflows: {...}   # Workflow id -> Workflow
worker: {...}      # optional: poll interval and shutdown grace
```

## File-level fields

- `version` must be `4`.
- `skills` is the optional configuration-wide Skill Bundle membership: a list
  of `{source, include, ref?}` entries. A missing field and `skills: []` both
  mean an empty bundle. Every `skill:` an agent step names must be declared in
  one entry's `include`; an undeclared skill refuses startup naming the
  instruction path. `source` is an HTTPS Git URL (`https://` or
  `git+https://`, no userinfo, query or fragment); `include` names the exact
  member skills; `ref` optionally pins the entry. The whole declaration always
  counts as the bundle membership, whatever `--workflow` selects.
- `target.kind` is `github`. `target.repository` is `OWNER/REPO`, for example
  `lbacik/coding-agent-sandbox`. `target.base-branch` is the branch the Base
  Revision of an issue Attempt resolves from. `target.token-env` names the
  environment variable holding the GitHub token.
- `models.<id>` pins `provider`, `model`, `endpoint`, `effort`,
  `api-key-env` (the name of the variable holding the key),
  `prices-usd-per-mtok` (`input`, `output`, `cache-read`, `cache-write`),
  the three token keys `context-window-tokens`,
  `context-threshold-tokens` and `output-cap-tokens` (all required
  positive integers), and the optional `max-rounds` round bound (a plain
  integer between 1 and 32; omitted means 8). `max-rounds` bounds every
  Advisor call and Sub-agent run the pin answers: one round is one model
  response plus its tool results, and reaching the bound without a report
  ends the run `no-report` / `round-limit`. It is an additional stop,
  never an allowance — ceilings, allowances and the pin's context
  threshold still end a run first — and the Step Execution loop itself has
  no round bound.
  `input` prices only the uncached remainder of the prompt. `effort` is
  `low`, `medium` or `high`. `provider` must be a known provider and
  `endpoint` must be the one the code-owned provider-to-endpoint table in
  [`provider/pinned_model.py`](../../src/coding_agent/provider/pinned_model.py)
  (`PROVIDER_ENDPOINTS`) lists for it
  (contract
  [§2.1](../contract/step-workflow-runtime-contract.md#21-the-file)),
  which admits one endpoint per provider and carries no token budget;
  `model` is free-form and never checked against a table. This release
  knows two providers:

  | `provider` | `endpoint` |
  | --- | --- |
  | `anthropic` | `messages` |
  | `openai` | `responses` |

  An unknown `provider`, or another endpoint for the provider, refuses
  startup. A model that does not exist, or that answers as another model,
  is refused at startup by the Provider Capability Assertion and the
  per-response identity check, with no Attempt opened.
  `context-window-tokens` is the Model Context Window the request and its
  response share; `context-threshold-tokens + output-cap-tokens` must not
  exceed it, and the threshold must exceed the safety margin plus request
  overhead (1,000 + 1,000). The window, threshold and output cap are
  pinned and covered by the digest. Worked examples (not an exhaustive
  list — any model of a supported provider pins the same three keys):

  | `model` | `context-window-tokens` | `context-threshold-tokens` | `output-cap-tokens` |
  | --- | --- | --- | --- |
  | `claude-sonnet-5` | 1,000,000 | 80,000 | 32,000 |
  | `gpt-5.6-luna` | 1,050,000 | 100,000 | 32,000 |

  The threshold counts provider-measured tokens since the anchored
  measure ([ADR 0031](../adr/0031-the-conversation-context-is-anchored-on-provider-reported-input.md)),
  and the recommendation assumes the anchored measure. Reaching the
  threshold ends the Step Execution `limit-exceeded` (reason
  `context-threshold`), routed like any other limit — for example
  `process` → `ai-handoff` — so the threshold is also where a long-running
  step hands its work to the Workflow's next route. There is no in-step
  continuation into a fresh conversation.
- `workflows.<id>`: see the next section. Every id matches
  `[a-z0-9-]+` (lowercase segments joined by single hyphens).

### Gateway pins (Model Gateway)

A `models` entry may instead pin a Model Gateway (contract
[§2.1](../contract/step-workflow-runtime-contract.md#21-the-file)): the
Worker speaks the gateway's `/v1` Responses endpoint through the same
adapter and still owns the tool loop. Exactly one of `provider` or
`gateway` is present.

```yaml
models:
  gateway-main:
    gateway: {kind: litellm, url: https://llm.example.com}
    model: my-team-model        # the gateway's own name (its L1 alias)
    endpoint: responses
    effort: medium
    api-key-env: LITELLM_PROXY_API_KEY
    context-window-tokens: 1000000
    context-threshold-tokens: 100000
    output-cap-tokens: 32000
    prices-usd-per-mtok: {input: 2.0, output: 10.0, cache-read: 0.20, cache-write: 2.50}
```

What each key means:

- `gateway.kind` is the gateway kind, from a closed code-owned list: today
  only `litellm`. `gateway` takes exactly one of `url` or `url-env`.
  `gateway.url` is the gateway's literal `http` or `https` URL (host,
  optional port and path; no userinfo, query or fragment). It is
  canonicalised (lowercase scheme and host, no trailing `/`).
  `gateway.url-env` names an environment variable holding the gateway URL
  instead of a literal, for one baked package that runs against different
  addresses (for example `url-env: LITELLM_PROXY_URL`). The name follows
  the same rule as `api-key-env`: a variable name, never a value. The
  variable is read once per Worker process at startup, before the Provider
  Capability Assertion, and its value must pass the same rules as a literal
  `url` (same schemes, host, and no-userinfo/query/fragment rules) and is
  canonicalised the same way. An unset, empty or malformed value refuses
  startup with no Attempt opened; the refusal names the variable, never the
  value. A change to the variable takes effect only at restart. The
  resolved canonical URL — not the variable name — is part of the Pinned
  Model's pinned identity, so Attempts against different addresses have
  different Effective Configuration digests.
- `model` is the gateway's own name, free-form and never checked against a
  table: pin the L1 alias the gateway reports in `x-litellm-model-group`.
  Any deployment the gateway spreads that name over counts as the Pinned
  Model, and the pin key is the label `litellm:<model>@responses`.
- `endpoint` is the endpoint the code-owned table admits for the gateway
  kind (`responses` for `litellm`). `effort` is `low`, `medium` or `high` —
  and it must be the effort the gateway reports (see Pinnability limits
  below).
- `api-key-env` names the environment variable holding the gateway key; the
  value never appears in the file. The key must reach the Worker process
  (for Compose, see `compose.gateway-example.yaml`).
- `context-window-tokens`, `context-threshold-tokens` and
  `output-cap-tokens` are the pin's own Model Context Window,
  context-window threshold and output cap — the same three keys a direct
  pin declares. The safety margin (1,000) and request overhead (1,000)
  stay code-owned defaults, and the threshold must
  exceed the safety margin plus request overhead. `agent provider-check
  --config <file> <pin>` reads the pin's prices and output cap from the
  file and sends its probe with `max_tokens` set to that cap, so an
  output cap above the model's maximum is refused there.
- `prices-usd-per-mtok` is the pin's configured prices, the Worker's only
  cost authority: a gateway response header such as
  `x-litellm-response-cost` is never read, not even as evidence.

### Recommended gateway configuration

Configure the gateway so a pinned name has no fallbacks or retries, and set
no key's `metadata.disable_fallbacks: false`. This is a recommendation, not
a guarantee: whatever the gateway is configured to do, the per-response
assertion (contract
[§9.1](../contract/step-workflow-runtime-contract.md#91-conversation-and-model))
holds — a response whose `x-litellm-model-group` differs from the pinned
name, or whose attempted fallbacks or retries read above `0`, is a pin
violation, and the Worker never reads the gateway's configuration. Every
request also carries the per-request suppression (`disable_fallbacks: true`,
`num_retries: 0`); the Worker's own provider retry is then the only retry.

### Pinnability limits

A pin's `effort` must be the effort the gateway reports: where a probe
response reports the effort it ran at, the reported effort must equal the
pinned effort exactly, or startup refuses with no Attempt opened. For
example, Meta `muse-spark-1.3-contributor` behind the gateway always runs
at `high` and says so in the response, so its pin carries `effort: high`;
the same pin with `effort: low` refuses, naming the reported `high`.

A model that refuses a forced `tool_choice` can still be pinned. Meta
models answer one with a 400 naming `tool_choice`: that refusal is the
diagnostic forced probe's recorded outcome, and the gating probe binds
its tool unforced, as every work node does. What refuses the pin is a
text answer (a wrong call, or arguments that do not match) to the gating
probe, an effort mismatch, missing usage, or gateway identity evidence
naming another model. A gateway that accepts an invalid effort still
refuses the pin — where the probe response reports no effort, the same
request sent with a deliberately invalid effort must be refused naming
the effort (contract
[§4](../contract/step-workflow-runtime-contract.md#4-the-workers-lifecycle)).
When a vendor resolves the pinned name to a snapshot, pin the name
the vendor reports.

Each assertion run records one Provider Capability Report per referenced
pin — every check's result (`price`, `tool-call-auto`, the diagnostic
`tool-call-forced`, `usage`, `identity`, `effort`), gating and diagnostic,
in run order, each with that pin's own output cap, effort and gateway
identity. The Worker persists the latest report per pin id in the operator
store whether startup passes or refuses; a refused startup keeps the
reports of the pins checked before the refusal. Read them through
`agentctl status` as `status.provider_capability` — an object from pin id
to that pin's report — live or `--offline`: after a refused startup the
offline read names the refused check and its evidence, and the refusal
diagnostic names the refusing pin's id. **Breaking change:** readers that
parsed `status.provider_capability` as a single report must now index by
pin id; with today's configurations the map holds exactly the Workflow's
own pin.

The gateway path is proved live by the `l1_gateway` rows
(`tests/test_gateway_live.py`, matrix `GW-1` … `GW-5`), which run the same
assertions as the fake-gateway unit tests against a real gateway: they are
gated on `TEST_GATEWAY_URL`, `TEST_GATEWAY_API_KEY` and
`TEST_GATEWAY_MODEL`, never run by default or in CI, and run manually before
each release tag and after each LiteLLM upgrade.
- `worker` is optional. `worker.poll-interval-seconds` (default 60) is the
  idle sleep between selection sweeps; every sweep spends the token
  owner's shared GitHub rate limit. `worker.shutdown-grace-seconds`
  (default 30) is the time the live Attempt has to finalise after SIGTERM;
  keep it below your deployment's stop timeout (see
  [Stopping the Worker](#stopping-the-worker)). Both are recorded in the
  Effective Configuration.

### Instance-supplied Project Profile (`target.profile`)

`target.profile` is optional and covers the whole configuration file,
shared by every Workflow in it. Where absent, the Project Profile comes
from the Target Repository (`docs/agents/project-profile.yml` at the
Base Revision) and nothing changes. Where present, the named file is
the Validation Contract's source for readiness, validation and final
checks, and the repository's own profile is never read for any of them
(contract
[§10.2](../contract/step-workflow-runtime-contract.md#102-check-readiness),
[ADR 0030](../adr/0030-the-workflow-configuration-may-supply-the-project-profile.md)).

```yaml
target:
  kind: github
  repository: lbacik/battles
  base-branch: main
  token-env: GITHUB_TOKEN
  profile: project-profile.yml   # beside workflow.yml, prompts/ and skill-policy.json
```

Path rules are the same as for `prompt:` locators (contract
[§2.8](../contract/step-workflow-runtime-contract.md#28-trust-and-paths)):
relative to the configuration file's directory, refused when it escapes
that directory, a symlink accepted only while it stays inside, decoded
as strict UTF-8. A missing, unreadable, invalid-UTF-8, unparsable, or
escaping file refuses `agent config validate` naming `target.profile`.

`agent config validate` parses the referenced profile with the
schema-2 parser and, where a Supported Toolchain Matrix can be resolved,
asserts its toolchain against the matrix: a broken profile or an
unsupported toolchain fails the derived-image build, not the first
Attempt's preflight. Where no matrix can be resolved the toolchain check
is skipped with a notice and validation still exits 0. At `run` startup
the `project-profile` phase repeats the parse and the toolchain
assertion after the `toolchain-matrix` phase; any failure is a Startup
Refusal naming the phase. Where `target.profile` is absent the behaviour
depends on whether the selected Workflow needs a Validation Contract
(ADR 0037): a Workflow needs one iff any `agent` step declares the
`tests` tool group or any operation step is `check-readiness` or
`run-validation` — nothing else (`shell`, `subagents`, `advisor`,
`commit-and-push`, `update-from-base` and the publishing operations bind
against no contract). A Workflow that needs one reads the Target
Repository's `docs/agents/project-profile.yml` at the base-branch tip
after the `github-checks` phase; a missing, unreadable, unparsable or
schema-violating repository profile is a Startup Refusal naming the
searched path and the revision read. Toolchain and service reachability
are never part of this read: they stay in `check-readiness`, which
reports them as `not-ready`. A Workflow that needs none logs
`skipped (profile from Target Repository)` and runs without a profile.
Where `target.profile` is absent and the Workflow needs a contract,
`agent config validate` prints a notice that the profile will be read
from the Target Repository at startup; it is never a refusal. The
profile bytes join
the Claim's content-addressed blobs and the Effective Configuration
record as `project_profile: {locator, sha256, size_bytes}` (record
`version: 2` is unchanged), so the Attempt Archive pins the contract
that was used and a later edit of the file never reaches an open
Attempt. `check-readiness` records `profile_source:
workflow-configuration` (and `repository_profile_ignored: true` where
the repository still carries its own profile) and keeps the toolchain,
services and `bootstrap` checks.

There is no lifecycle-label setting at file level: the labels applied at the
end of an Attempt are declared per Workflow in
[`outcomes`](#terminal-outcomes-and-end-labels).

### The tracker and the Code Host (decided, not yet in force)

The runtime distinguishes the **Issue Tracker** (holds inputs, selection
labels, comments) from the **Code Host** (holds the repository, branches,
diffs, pull-request refs), each reached only through its port (contract
[§2.11](../contract/step-workflow-runtime-contract.md#211-the-tracker-and-the-code-host-decided-pending-the-version-5-migration),
[ADR 0038](../adr/0038-the-issue-tracker-and-the-code-host-are-reached-only-through-their-ports.md)).
Today both are GitHub and the file above is the whole shape. The decided
`version: 5` shape splits `target` accordingly:

```yaml
version: 5
target:
  code_host: {kind: github, repository: OWNER/REPO, token-env: GITHUB_TOKEN}
  tracker: {kind: jira, project: PROJ, token-env: JIRA_TOKEN}  # omitted = Code Host
  base-branch: main
```

An omitted `tracker` keeps today's meaning: issues come from the Code Host.
A `jira` tracker pins exactly one project per configuration (the adapter
maps `PROJ-N` to integer `N`, so Attempt ids stay `<workflow>#<N>/<n>`);
work in another project is another Worker. Tracker and host credentials are
separate secrets with separate startup checks, both registered in the single
redaction registry. Until the `version: 5` migration lands as a build
slice, this shape is decided but not accepted: `version: 4` files keep
validating exactly as above. The word "Forge" is not used for this split:
Agent Forge names the external image builder
([below](#external-builders-the-agent-forge-sequence)).

## A Workflow

| Key | Required | Meaning |
| --- | --- | --- |
| `model` | yes | The id of one `models` entry: the Attempt's one Pinned Model, used by every step and Sub-agent. A step never overrides it. |
| `input` | yes | Which Workflow Inputs the Worker selects: [Input selection](#input-selection). |
| `limits` | yes | The Attempt Ceiling, all four meters: [Limits](#limits). |
| `max-parallel-subagents` | no | How many Sub-agents of one group may run at once: an integer of at least 1, default 2. |
| `outcomes` | no | End labels per Terminal Outcome: [Terminal Outcomes and end labels](#terminal-outcomes-and-end-labels). |
| `round-handoff` | no | Round Handoff enablement for an `issue` Workflow: [Round Handoff and Continuation](#round-handoff-and-continuation). Omitted disables publication and automatic pickup. |
| `start` | yes | The id of the first step. YAML step order means nothing. |
| `defaults` | no | `max-executions` (default 1) and `routes` for the reserved results `error` and `limit-exceeded` only. There is no `defaults.limits`. |
| `steps` | yes | A map from step id to step: [Steps](#steps). |

## Input selection

A Workflow takes exactly one input type (contract
[§5](../contract/step-workflow-runtime-contract.md#5-workflow-inputs-and-selection)):

```yaml
input:
  type: issue                  # or pull-request
  label: agent:implement       # the Selection Label
  require-all: [...]           # optional narrowing filters
  require-any: [...]
  exclude: [ready-for-human, wontfix]
  skip-open-agent-pr: true     # issue Workflows only
  # drafts: include | exclude  # pull-request Workflows only; default exclude
```

- **Selection Label.** Every Workflow names its own label; applying it is
  the human's request for this scenario. Authorisation is checked, not
  assumed: at Claim the Worker reads who last applied the label and refuses
  the input when that was a bot account. Whether the Agent Identity's own
  account may apply it depends on the identity mode, which the Worker
  detects at startup, not from the YAML: when the token belongs to the
  repository owner (a **shared identity**, today's arrangement), the owner
  labelling is the authorisation and is accepted; with a **dedicated**
  agent account, a label applied by that account is refused (contract
  [§5.1](../contract/step-workflow-runtime-contract.md#51-input-type-and-selection-label),
  [§18](../contract/step-workflow-runtime-contract.md#18-identity-and-authorization)).
  On a future non-GitHub tracker the same event shape is sourced from the
  tracker's own record (a Jira `labels` changelog entry) and only the
  dedicated rule applies, since there is no repository-owner identity.
  The label
  stays on the input for the whole Attempt; a human removing it withdraws
  the request. The Worker always removes it when the Attempt ends, so
  running the scenario again needs a human to apply it again.
- **Filters** narrow, never authorise. The runtime hard-codes no exclusion
  any more: `ready-for-human`, `wontfix` or any other label is excluded only
  if `exclude` names it. The Selection Label cannot also appear in a filter.
- **`skip-open-agent-pr`** (issues only): when `true`, skip an issue that
  has an open pull request authored by the Agent Identity bearing that
  issue's Attempt Marker.
- **`drafts`** (pull requests only): whether draft pull requests are
  eligible; default `exclude`. A draft change during an Attempt is ignored.
- **Mandatory guards** apply whatever the filters say: the input is open,
  the Selection Label is present and authorised, the input has no assignee
  and no open native blocker. A pull request must have its head branch in
  the Target Repository; fork pull requests are never eligible.
- **Ordering:** oldest first, by creation time then number.
- **Exclusivity is global per input:** at most one open Attempt per issue or
  pull request, whatever the Workflow. Another Workflow's Worker skips the
  input while a Claim is in force (contract
  [§6](../contract/step-workflow-runtime-contract.md#6-claim-and-attempt-identity)).

The Claim is the Agent Identity's assignment plus a **Claim Comment** on the
input. The Attempt id is `<workflow>#<input number>/<n>`, for example
`implement#42/1`. A fresh issue Attempt starts at its Base Revision on its
own branch `agent/<workflow>/<issue>/<n>-<slug>`; a Continuation of an
opted-in issue Workflow opens its own new branch at the recorded work commit
and keeps the recorded Base Revision ([Round Handoff and
Continuation](#round-handoff-and-continuation)). A pull-request Attempt works
on the pull request's head (the **Input Head**), with the merge base as its
Base Revision.

**Drift and withdrawal** are checked before every step and every external
write (contract
[§7](../contract/step-workflow-runtime-contract.md#7-pinned-input-state-gates-drift-and-withdrawal)).
Editing the title or body, someone else pushing to the pull request, or
retargeting its base ends the Attempt `drift`. Removing the Selection Label,
closing the input or merging the pull request ends it `withdrawn`. New
commits on the base branch are disclosed, not Drift.

## Round Handoff and Continuation

An opted-in issue Workflow can publish a Round Handoff when it ends
`unfinished` on a publishing Route, and another Worker can pick that offer
up as a Continuation: a new Attempt on the same issue that starts from the
exact published work commit and keeps the recorded Base Revision (contract
[§17](../contract/step-workflow-runtime-contract.md#17-continuation)).
Publication rides on the step-local annotations described under
[Routes](#routes), and the note reaches model work as the
`round-handoff.note` input described under [Agent
steps](#agent-steps).

```yaml
workflows:
  implement:
    input: {type: issue, label: agent:implement}
    round-handoff: {max-automatic-pickups: 2}   # pickup-label defaults to round-finished
```

- **Enablement.** `round-handoff` is an `issue`-only mapping with required
  `max-automatic-pickups` (a positive integer: how many automatic pickups
  one human-authorised series gets) and optional `pickup-label` (a non-empty
  label, default `round-finished`, as in
  [`examples/workflows.yaml`](../../examples/workflows.yaml)). Omitted
  disables both publication and automatic pickup: annotations still validate
  but their Routes keep ordinary `unfinished` semantics.
- **Publishing.** Only an `unfinished` Workflow End Route for `pushed` or
  `no-changes` from a `commit-and-push` step can carry `round-handoff:
  {kind: ai-assisted, note: <step>.<result>.<field>}` or `{kind: hard}`
  (see [Routes](#routes)). On the publishing round the runtime verifies the
  push landed, resolves and bounds the note (`ai-assisted` reads the
  declared `document` Step Artifact; `hard` composes the model-free account
  from Execution Summaries), and publishes the offer in the terminal comment
  as an operator-visible account plus one versioned machine-readable record
  (source Attempt, work commit, named ref, Base Revision, note kind and
  note, Pickup Label, authorisation and counter evidence). A failed push, a
  missing note, missing Claim authorisation evidence or a revision the
  remote cannot serve leaves a `round-handoff: no pickup (<reason>)`
  diagnostic with ordinary end labels instead.
- **Validation** (`agent config validate` refuses startup otherwise):
  `round-handoff` appears only in `issue` Workflows; the Workflow needs at
  least one reachable publishing Route; the optional
  `{optional: round-handoff.note}` input is accepted only on possible first
  agent steps — found by following the effective Routes from `start`
  through Runtime Operations — and an enabled Workflow declares it on
  **every** such step; and the Pickup Label is distinct from every
  Selection Label and every end label in the same file, and from its own
  Workflow's `require-all` / `require-any` / `exclude`.
- **Continuation.** The Pickup Label is tried ahead of the Selection Label:
  an issue carrying it whose newest trusted terminal record for this
  Workflow and issue is a live (non-exhausted) Round Handoff offer is
  claimed as a pickup before model work. The claim pins the recorded Base
  Revision and the record's portable authorisation evidence, and re-checks
  the same source at the Claim read-back — a replaced, stale, invalid or
  exhausted signal stays ineligible, including where the issue carries both
  labels, and these signals are diagnosed before any model work. The
  Continuation opens its new Attempt branch exactly at the published work
  commit, keeps the recorded Base Revision, and starts with fresh
  allowances and a fresh conversation; the note arrives only as the
  declared `round-handoff.note` input (absent on a fresh Attempt). It
  enters the graph at `start`, so `check-readiness` and the rest of the
  Workflow run again; there is no separate entry step for Continuations,
  and no Runtime Operation routes differently for a fresh Attempt and a
  Continuation.

**Operator side.** The terminal comment of a publishing round says what it
offers: `Round Handoff: <attempt> offers pickup under <pickup-label>
(<used>/<limit>)` with the work commit (and its ref), the Base Revision,
the note kind and length, and the machine record below; or, where the
series is spent, `publishes its final round (<used>/<limit>,
pickup-limit-reached); automatic pickup is exhausted` with the note and
revisions but ordinary end labels. A series gets `max-automatic-pickups`
automatic pickups under one human authorisation: the first human-authorised
Attempt fixes the limit and starts the count at zero, and a changed
deployed limit applies to the next human-authorised series, never to the
running one. When a series is exhausted — or when the pickup signal is
otherwise spent — the human re-authorises it by clearing the Pickup Label,
then re-applying the Selection Label, which opens a fresh human-authorised
Attempt and a new series.

## Steps

Every step has **exactly one executor** and the same control keys (contract
[§8](../contract/step-workflow-runtime-contract.md#8-the-workflow-graph)):

```yaml
steps:
  <step-id>:
    agent: {...}             # a model conversation, or
    operation: <name>        # a Runtime Operation, with parameters under:
    with: {...}
    max-executions: 3        # Execution Ceiling (default from `defaults`, else 1)
    limits: {...}            # Step Allowance: agent steps only
    reserved: true           # Reserved Step: agent steps only
    routes: {...}            # required
```

### Routes

`routes` maps every possible result of the step to a target: another step id
or a **Workflow End** `{end: completed|blocked|unfinished|failed}`.

- **Exhaustive.** Every result the step can produce is routed: the results
  an agent step declares or an operation's catalog results, plus the two
  **reserved results** `error` and `limit-exceeded`, which every step can
  produce. Coverage is checked after applying `defaults.routes`.
- **Defaults cover reserved results only.** `defaults.routes` may name only
  `error` and `limit-exceeded`; a step's own Route wins. Business results
  are always routed in the step itself. There is no wildcard.
- **One target per result.** A Route is selected by the result name alone:
  no conditions, expressions, payload predicates or lists of targets. A
  scenario that needs a branch declares distinct result names (for example
  `approved` / `changes-needed`).
- **Reserved results may end only as `unfinished` or `failed`.** Routing
  `error` or `limit-exceeded` directly to `completed` or `blocked` is a
  validation error. Operation results such as `requirement-unmet` and
  `stale-head` are not restricted.
- **The `limit-exceeded` Routes must be acyclic**, self-routes included,
  after defaults. In practice the last step of a Hard Handoff chain must
  override a default `limit-exceeded` that would point at itself.
- **Conversation Inheritance is an opt-in step-target flag.** A step-local
  Route between agent steps may spell its target as
  `{step: <id>, inherit-conversation: true}`: the receiver opens with the
  exact immediate source Step Execution's history (contract §8). Only
  `true` opts in — a scalar step id, an omitted flag and explicit `false`
  all enter fresh, and all three spellings canonicalise identically. The
  flag must be a boolean, both ends must be agent steps, end targets stay
  disjoint (`{end: ...}` never carries it), and `defaults.routes` names
  scalar step ids and Workflow Ends only.
- **Round Handoff annotations are step-local.** An `unfinished` Workflow End
  Route for `pushed` or `no-changes` from an issue Workflow's
  `commit-and-push` step may carry
  `round-handoff: {kind: ai-assisted, note: <step>.<result>.<field>}` or
  `round-handoff: {kind: hard}`. `ai-assisted` requires the note to name a
  declared `document` field proven available on every path to the publishing
  step (the usual required-reference proof); `hard` carries no note. The
  annotation is refused on other results, step targets, other Workflow Ends,
  `defaults.routes` and every pull-request Workflow. While the
  `round-handoff` enablement mapping is omitted the annotation validates but
  the Route keeps ordinary `unfinished` semantics. What a publishing round
  publishes, and how a Continuation picks it up, is under [Round Handoff
  and Continuation](#round-handoff-and-continuation).

```yaml
routes:
  done: {step: handoff, inherit-conversation: true}   # carry the conversation
  error: {end: failed}
  pushed: {end: unfinished, round-handoff: {kind: hard}}   # disabled: ordinary unfinished
```

### Execution Ceiling, loops and retry

`max-executions` bounds how many times a step runs in one Attempt (default
1). A Route into a step that has reached its ceiling is **refused**: no Step
Execution is created, and routing follows **that step's** `limit-exceeded`
Route. This is what ends a loop with no declared success. There is no retry
construct: a retry is a Route back to the same step, bounded by its ceiling
(for example `error: publish` with `max-executions: 2`).

## Agent steps

An `agent` step is one fresh model conversation per Step Execution, with no
message history from earlier steps; the shared workspace is the only
implicit continuity (contract
[§9](../contract/step-workflow-runtime-contract.md#9-agent-steps)).
A Route that opts into Conversation Inheritance (`inherit-conversation:
true`) is the one exception: the receiver opens with the Attempt Header
and Workflow Input, then the source framing and its complete Exchange
Units, then a Worker-authored entry frame carrying the receiver's own
declared inputs, Step Result contract and instructions.

```yaml
agent:
  instructions:
    - skill: implement                 # a Skill Bundle skill
    - prompt: prompts/implement-policy.md   # an operator prompt file
  tools: [workspace-read, workspace-write, shell, tests, subagents]
  inputs:
    - process.done.summary             # <step>.<result>.<field>
    - process.facts                    # <step>.facts: the Execution Summary
    - optional: validate.facts         # may be absent
  results:
    done:
      criteria: {list: evidence-ref}
      summary: document
    blocked:
      reason: document
```

- **`instructions`** is an ordered list of `skill:` (a skill declared in the
  configuration-wide `skills` membership) and `prompt:` (an operator prompt
  file) items, activated together when the conversation opens; the whole
  declared Skill Bundle is also available on demand. Instructions grant no
  authority. Several
  instructions in one step carry no ordering guarantee; a guaranteed
  sequence needs separate steps.
- **`tools`** are denied by default and granted from six groups:

  | Group | Grants |
  | --- | --- |
  | `workspace-read` | File reads, listing, search, the change-versus-base reader. |
  | `workspace-write` | File writes and edits. |
  | `shell` | A credential-free command runner. Its output is never evidence. |
  | `tests` | Targeted tests that record evidence; diagnostic checks. |
  | `subagents` | Sub-agents with fixed read-only tools; their spend counts to the step and the Attempt. |
  | `advisor` | Questions to the step's Advisor, answered on the step's Advisor pin; each call's spend counts to the step and the Attempt at the Advisor pin's prices. |

  The Step Result report tool, skill activation and the capped-result reader
  are always present and cannot be declared. No group gives the model a
  GitHub tool: every GitHub write is a Runtime Operation.
- **`inputs`** name data from the **latest** Step Execution of another step,
  even one from an earlier loop round: a payload field
  `<step>.<result>.<field>`, or `<step>.facts`, that step's **Execution
  Summary** (changed files, commands and results, evidence, the limit
  crossed and the spend; never the transcript). An input that may be absent
  is written `{optional: <ref>}`. A required input must be provably present
  on every path to the step, or startup is refused; declare it optional
  instead. Attempt facts (the input's title and body, the revisions) reach
  every agent step through its opening frame. The special input
  `{optional: round-handoff.note}` is accepted only on possible first agent
  steps of an issue Workflow — found by following the effective Routes from
  `start` through Runtime Operations until an agent step or Workflow End —
  and is refused required, in operation bodies, on other steps and in every
  pull-request Workflow. It is absent on a fresh Attempt and carries the
  published handoff note on a Continuation ([Round Handoff and
  Continuation](#round-handoff-and-continuation)). The special input
  `input.comments` is accepted on any agent step of an issue Workflow,
  required or `{optional: input.comments}` — the input is always present
  (an empty list where the pin held no Trusted Comments), so both validate
  and neither constrains the step's path — and is refused in pull-request
  Workflows and as an operation `with.body` reference. Declaring steps are
  framed the issue's Trusted Comments pinned at Claim, oldest first, each
  capped with a pointer to the full copy in the Attempt Archive: comments
  by the issue author, the Target Repository owner and the Agent Identity
  (by numeric id, never login), with the Worker's own Claim and terminal
  comments excluded by Attempt Marker and `post-comment` publications kept
  as the publishing Workflow's document. Comments posted after Claim are
  never Drift and never reach the Attempt; the terminal comment discloses
  them with links. `input` is reserved and cannot be a step id.
- **`results`** is the closed set of named Step Results the model may
  report, at least one, each with a payload of typed fields: `document`,
  `string`, `integer`, `boolean`, `evidence-ref`, `{list: T}` and
  `{enum: [...]}`. An empty payload is `{}`. Reporting a result is the
  conversation's only exit; a malformed report goes back to the model a
  bounded number of times, then the step ends `error`. An `evidence-ref`
  names a test that must have run through the `tests` tools and passed on
  the current tree. Only `document` fields become Step Artifacts and only
  they can be published. The names `error`, `limit-exceeded`,
  `requirement-unmet` and `stale-head` are reserved and cannot be declared.
- **`context-thresholds`** (agent steps only) is the step's own Hard
  Context Threshold policy: required `hard-tokens`, optional `soft-tokens`.
  On an opted-in step the spelling `hard-context-threshold` is reserved to
  the runtime and cannot be declared; steps without the policy keep
  `hard-context-threshold` as an ordinary business result. Where the policy
  configures `soft-tokens`, the spelling `soft-context-threshold` is
  likewise reserved and cannot be declared — including on a hard-only
  opted-in step, so the spelling can never shadow the runtime result;
  steps without the policy keep both spellings as ordinary business results. The step needs a step-local Route for
  `hard-context-threshold`, and the hard Route stays model-free through
  operations to `unfinished` or `failed`:

```yaml
steps:
  process:
    agent:
      tools: [workspace-read]
      results:
        done: {}
    context-thresholds: {hard-tokens: 40000}
    routes: {done: handoff, hard-context-threshold: handoff}
  handoff:
    operation: commit-and-push
    routes: {pushed: {end: unfinished}, no-changes: {end: unfinished}}
```

  `hard-tokens` must exceed the code-owned safety margin plus request
  overhead and, with the pin's output cap, fit the Model Context Window;
  a step whose next request would cross the hard Usable Threshold reports
  `hard-context-threshold` before any hold or provider call.

  A policy with `soft-tokens` adds the Soft Context Threshold: `soft-tokens`
  must sit strictly between the safety margin plus request overhead and
  `hard-tokens`, and the step needs a step-local inheriting Route for
  `soft-context-threshold` into a Reserved agent receiver with an explicit
  hard-only policy and all four Step Allowance meters. A request strictly
  over the Soft Usable Threshold but at or below the hard one reports
  `soft-context-threshold` — the hard guard keeps precedence where both
  bounds are crossed — and the receiver continues the working conversation
  from the inherited history under its own allowance. The receiver's
  `hard-context-threshold`, `error` and `limit-exceeded` Routes stay
  model-free through operations to `unfinished` or `failed`:

```yaml
steps:
  process:
    agent:
      tools: [workspace-read]
      results:
        done: {}
    context-thresholds: {hard-tokens: 40000, soft-tokens: 20000}
    routes:
      done: {end: completed}
      hard-context-threshold: {end: unfinished}
      soft-context-threshold: {step: scribe, inherit-conversation: true}
  scribe:
    agent:
      tools: [workspace-read]
      results:
        note: {summary: document}
    reserved: true
    limits: {cost-usd: 2, effective-tokens: 200000, tool-calls: 100, wall-clock-minutes: 10}
    context-thresholds: {hard-tokens: 40000}
    routes:
      note: {end: completed}
      hard-context-threshold: {end: unfinished}
```

  The receiver's full inherited request is admitted against its own
  remaining allowance and the Attempt Ceiling after the Work Pool is
  exhausted, keeping the deadline, the full output cap and cumulative
  charging; its own floor uses its hard bound like any other step, while
  the soft source's floor uses its Soft Usable Threshold.

- **`advisor`** (agent steps only) declares the step's Advisor: the pin
  that answers its questions and the operator prompt the Advisor opens
  with. It is valid exactly with the `advisor` tool group — each without
  the other is refused — and carries `prompt` (required, one operator
  prompt file) and `model` (optional, a `models` id; without it the
  Advisor uses the Workflow's Pinned Model):

```yaml
models:
  sonnet: {provider: anthropic, model: claude-sonnet-5, ...}   # cheap, does the long work
  sage: {provider: anthropic, model: sage-model, ...}          # stronger, answers questions

workflows:
  implement:
    model: sonnet
    steps:
      triage:
        agent:
          tools: [workspace-read, advisor]
          advisor: {prompt: prompts/triage-advisor.md, model: sage}
          results:
            done: {fix: document}
```

  Each `ask_advisor(question)` call opens one fresh read-only Advisor
  conversation on the step's Advisor pin — framing, the Attempt facts,
  the input, the caller's declared inputs, the `advisor.prompt` bytes,
  then the question — and returns its report (or its ending) as the tool
  result. The Advisor binds the Sub-agent read-only tools plus its own
  `report_advice` exit, runs at most the answering pin's `max-rounds`
  bound (default 8, at most 32), and counts against the
  calling step's allowance and the Attempt Ceiling at the Advisor pin's
  prices. The runtime never checks whether the caller follows an answer.

  Ask like a triage round: send a whole round of questions in one call
  (calls in one turn run one at a time, in order), and quote earlier
  questions and answers in the next round — an Advisor starts fresh every
  call and remembers nothing between them.

## Runtime Operations

An `operation` step runs code, never a model. Its results are fixed by the
catalog; the YAML only routes them (contract
[§10](../contract/step-workflow-runtime-contract.md#10-runtime-operations)).

| Operation | Input type | What it does | Results | `with` keys |
| --- | --- | --- | --- | --- |
| `check-readiness` | both | Parses the Project Profile, checks toolchain and services, runs `bootstrap`. | `ready` / `not-ready` (a failed bootstrap is `error`) | — |
| `run-validation` | both | Runs the pinned Validation Contract commands on the current tree. | `passed` / `failed` | — |
| `commit-and-push` | both | Commits the workspace and pushes: to the Attempt branch for an issue, as a compare-and-swap to the pull request's head for a pull request. | `pushed` / `no-changes`, plus `stale-head` in `pull-request` Workflows | `requires` |
| `open-pull-request` | `issue` only | Opens a pull request from the pushed revision. Its title is a fixed runtime template over the issue title. | `opened` / `already-open` | `body` (required), `draft`, `requires` |
| `publish-review` | `pull-request` only | Publishes a COMMENT review against the current Input Head. | `published` / `stale-head` | `body` (required), `requires` |
| `post-comment` | both | Posts a comment on the input. | `posted` | `body` (required), `requires` |

Every operation can also end `error` and `limit-exceeded`, and any operation
given `with.requires` can also end `requirement-unmet`.

- **`with.body`** is a reference to a `document` field of an agent step's
  result, under the same static proof as a required input. An Execution
  Summary (`<step>.facts`) cannot be published, and there is no facts-mode
  comment: the runtime's terminal comment already reports the Attempt facts.
- **`with.requires: [run-validation: passed]`** is the only requirable
  evidence in the first release. It must hold for the revision being
  published, so policy cannot be bypassed by graph ordering; evidence from
  the base tree never satisfies it.
- **`with.draft`** opens the pull request as a draft.
- **`stale-head`** covers only the race between the last check and the
  write; a head moved earlier is Drift.
- A pull-request title cannot be set: `with.title` is rejected.

**What the runtime adds to a published body** (contract
[§10.5](../contract/step-workflow-runtime-contract.md#105-open-pull-request),
[§14.4](../contract/step-workflow-runtime-contract.md#144-the-attempt-marker-rate-limits-and-refusals)):

- a ready pull request gets `Closes #<issue>`, so merging it closes the
  issue; a draft gets the non-closing `Part of #<issue>`;
- every pull request, review and comment gets a provenance footer after
  the `document` (Attempt id, Workflow id, publishing step, Effective
  Configuration digest, and the Base Revision or the reviewed head) and
  the Attempt Marker;
- validation evidence is **not** added: if reviewers should see it, the
  Workflow's `document` must carry it;
- a body too large for GitHub has only its `document` truncated, with a
  marker pointing to the full Step Artifact in the Attempt Archive.

**The `document` is published verbatim.** The runtime does not rewrite
what the model wrote. A closing keyword the model puts in a pull-request
`document` (`Fixes #12`, `Closes #7`), even in a draft's, takes effect when
the pull request merges; whoever reviews the pull request owns catching it.

## Limits

Four meters are counted: `cost-usd`, `effective-tokens` (effective tokens, excluding
cache reads), `tool-calls` and `wall-clock-minutes` (contract
[§11](../contract/step-workflow-runtime-contract.md#11-limits)). A `limits`
block naming `tokens` is refused as an unknown key.

- **Attempt Ceiling** (Workflow `limits`, mandatory, all four meters): the
  whole Attempt, from its opening, including Sub-agents and operation
  time.
- **Step Allowance** (step `limits`, optional, agent steps only, any subset
  of meters): accumulates over **all** Step Executions of that step in the
  Attempt; re-entering a step never buys fresh budget. Operation steps are
  bounded by code-owned timeouts and cannot declare `limits`.
- **Reserved Step** (`reserved: true`, agent steps only): must declare all
  four meters. Its allowance is carved out of the Attempt Ceiling, and it
  draws only on that allowance. This is how an AI-assisted Handoff keeps a
  protected budget.
- **Work Pool** = Attempt Ceiling − the sum of all Reserved Step allowances,
  per meter. Every non-reserved step shares only the Work Pool. Validation
  requires the reserves to stay below the ceiling in every meter, and every
  non-reserved Step Allowance to fit in the Work Pool.
- **One maximal Admission hold is the floor** (contract
  [§3](../contract/step-workflow-runtime-contract.md#3-static-validation)):
  the Attempt Ceiling, the Work Pool, every Reserved Step allowance and
  every non-reserved Step Allowance that declares `effective-tokens` or
  `cost-usd` must each hold Usable Threshold + output cap — `effective-tokens`
  at or above the token count, `cost-usd` at or above the same hold priced at
  the pin's prices (`input`, or max(`input`, `cache-write`) on a gateway pin,
  for the context part; `output` for the cap). A limit that does not declare
  the meter is not checked for it. The guarantee is one full-context request
  per fresh limit. Size a step for several holds: a re-entered step gets no
  fresh allowance, a parallel Sub-agent group shares what remains, and the
  hold does not cover finishing the step.

Exceeding any of them, or an Execution Ceiling refusal, yields
`limit-exceeded`, routed like any other result. Admission is worst-case:
before every model request the runtime checks that the request's upper
bound still fits, and refuses it otherwise. After the Attempt Ceiling is
spent no model request is admitted, but operation steps still run, so a
Hard Handoff chain of operations can always finish.

There is no separate model-turn limit to configure. The tool loop inside
one Step Execution is bounded by what is already counted: `tool-calls`
(plus effective tokens, cost and wall clock on every response), the bounded number
of malformed-report corrections, and the Context Threshold (contract [§11](../contract/step-workflow-runtime-contract.md#11-limits)).
Its only other bound is a code-owned iteration backstop, derived per Step
Execution from the step's remaining `tool-calls` room plus the
correction count plus a small slack — so it can fire only on a runtime
defect, ending the step `error` and naming the derived value, never
cutting off legitimate work early.

**Worked example** (`implement` in the example file):

| Meter | Attempt Ceiling | `ai-handoff` (reserved) | Work Pool |
| --- | --- | --- | --- |
| `cost-usd` | 20 | 1 | 19 |
| `effective-tokens` | 4 000 000 | 150 000 | 3 850 000 |
| `tool-calls` | 600 | 30 | 570 |
| `wall-clock-minutes` | 60 | 5 | 55 |

`process` (12 USD) and `self-review` (3 USD) each fit in the 19 USD Work
Pool; they share it, and `process` spends its 12 USD across all three of its
executions. Non-reserved model work stops at minute 55, leaving the reserved
5 minutes to `ai-handoff`. If `process` exhausts its allowance or the Work
Pool, it routes to `ai-handoff`, which still has its full 1 USD. In
`review`, the 0.5 USD reserve leaves a Work Pool of 5.5 USD / 1 350 000
effective-tokens / 280 tool calls / 25 minutes, and `review`'s 4 USD fits in it.

## Terminal Outcomes and end labels

Every Attempt ends with exactly one Terminal Outcome, recorded once
(contract
[§15](../contract/step-workflow-runtime-contract.md#15-terminal-outcomes-and-finalisation)):

| Terminal Outcome | Selected by | `error` / `limit-exceeded` may route to it |
| --- | --- | --- |
| `completed` | a Workflow End | no |
| `blocked` (needs a human's decision or information) | a Workflow End | no |
| `unfinished` (partial work handed to a human) | a Workflow End | yes |
| `failed` | a Workflow End | yes |
| `withdrawn`, `drift`, `interrupted`, `internal-failure` | the runtime only | — |

The runtime-only outcomes cannot be written as `{end: ...}`; they end the
Attempt outside the graph, and no Route is involved (contract
[§12](../contract/step-workflow-runtime-contract.md#12-sigterm-and-runtime-only-endings)).
There is no `canceled` outcome in the first release: removing the Selection
Label or closing the input is a withdrawal, and the operator contract has
no cancel command. A Terminal Outcome states which path ended the Attempt, not whether
anything was delivered: delivery is a separate fact taken from what
actually landed.

Every ending finishes the same way: in-flight writes are settled; the
runtime posts exactly one **terminal comment** from a fixed template (Attempt
id, outcome, the step and result that ended it, any limit fact, landed and
not-performed effects, uncommitted-file count); it removes the Selection
Label, applies the end labels, and removes its assignment when no other
Claim is in force. A Workflow's own `post-comment` (a handoff note, say) is
separate, so such an Attempt leaves two comments. Landed effects are never
reverted.

**End labels** are declared per Terminal Outcome, runtime-only outcomes
included:

```yaml
outcomes:
  unfinished: {labels: [ready-for-human]}
  internal-failure: {labels: [ready-for-human]}
```

The runtime hard-codes none, not even `ready-for-human`: an outcome with no
entry gets no label beyond the Selection Label's removal. `outcomes.canceled`
is rejected.

**An end label is never a Selection Label.** The Worker never applies a
Selection Label, so validation rejects an end label equal to the
`input.label` of any Workflow in the same file. It cannot see other files.
**Keeping Selection Labels disjoint from end labels across configuration
files is your responsibility:** under a shared identity, a Worker running
another file whose end label is this Workflow's Selection Label would
start this Workflow, and the actor check would accept it as the owner's
label. A dedicated agent account closes that gap.

## Walk through the example

[`examples/workflows.yaml`](../../examples/workflows.yaml) holds both
Workflows. Its prompt files live in
[`examples/prompts/`](../../examples/prompts/).

### `implement`: issue in, pull request out

- **Input:** issues labelled `agent:implement`, excluding `ready-for-human`
  and `wontfix`, skipping an issue whose own agent pull request is still
  open.
- **`defaults`:** every step runs once unless it says otherwise, and every
  `error` or `limit-exceeded` not routed by the step goes to `hard-handoff`.
- **`preflight`** (`check-readiness`): `ready` → `process`; `not-ready` →
  `{end: blocked}`.
- **`process`** (agent, `implement` skill plus the policy prompt, all five
  tool groups): reads why the last validation failed and the last
  self-review findings when they exist, which is why both inputs are
  optional. `done` → `validate`; `blocked` → `explain-blocked`, which posts
  the model's `reason` and ends `blocked`. It may run three times on a
  shared 12 USD allowance; its `limit-exceeded` (allowance, Work Pool, the
  Context Threshold, or a fourth entry refused) goes to `ai-handoff`, the
  only step that does so.
- **`validate`** (`run-validation`, up to three times): `passed` →
  `self-review`; `failed` → back to `process`.
- **`self-review`** (agent, `code-review` skill, read-only tools): reads
  `process.done.summary`; `approved` → `push`; `changes-needed` → back to
  `process`. Its third entry is refused and reaches `hard-handoff` through
  `defaults`.
- **`push`** (`commit-and-push`): `pushed` → `open-pr`; `no-changes` →
  `{end: blocked}`.
- **`open-pr`** (`open-pull-request`): body from `process.done.summary`,
  ready for review, and only if validation passed on the pushed revision.
  `opened` and `already-open` → `{end: completed}`; `requirement-unmet` →
  `hard-handoff`.
- **AI-assisted Handoff:** `ai-handoff` is a Reserved Step reading
  `process.facts` (required: every path into it has run `process`) and
  writing a note with the handoff prompt; `handoff-push` pushes whatever
  work exists and publishes the bounded, redacted receiver note with the
  verified work as a Round Handoff — `pushed` from the just-pushed
  revision, `no-changes` from the clean-tree revision the remote already
  serves — before ending `unfinished`. If `ai-handoff` errs or runs out
  of its reserve, `defaults` send it to `hard-handoff`.
- **Hard Handoff:** `hard-handoff` is a single `commit-and-push` with no
  model. It ends `unfinished` after pushing or finding nothing to push, and
  `failed` on its own `error` or `limit-exceeded`. Its own `limit-exceeded`
  must be overridden, because the default would route it to itself.
- **End labels:** `blocked`, `unfinished`, `failed` and `internal-failure`
  get `ready-for-human`.

### `review`: existing pull request in, review out

- **Input:** non-draft same-repository pull requests labelled
  `agent:review`, excluding `ready-for-human`. Nothing in the runtime knows
  this is a review; the policy is the Workflow's.
- **`defaults`:** `error` → `{end: failed}`; `limit-exceeded` →
  `{end: unfinished}`, so the Hard Handoff here is the terminal comment
  alone.
- **`review`** (agent, `code-review` skill plus the review policy, read-only
  tools): `reviewed` → `publish`; `limit-exceeded` → `ai-handoff`.
- **`publish`** (`publish-review`, body from `review.reviewed.body`):
  `published` → `{end: completed}`; `stale-head` (the head moved between
  the last check and the write) → `{end: failed}`; `error` → `publish`
  again, a retry bounded by `max-executions: 2`, after which the refusal
  ends `failed`.
- **`ai-handoff`** (Reserved Step) reads `review.facts` and writes a
  partial-review note; `handoff-comment` posts it and ends `unfinished`.
- **End labels:** `unfinished` and `failed` get `ready-for-human`.

## Common validation errors

Validation is strict and fail-fast: every violation refuses Worker startup,
and there are no warnings (contract
[§3](../contract/step-workflow-runtime-contract.md#3-static-validation)).
The errors operators meet most, grouped:

| Area | Rejected |
| --- | --- |
| YAML | Duplicate keys; anchors, aliases and merge keys (`<<`); custom tags; unknown or misspelt fields at any level. |
| Structure | A missing or unknown `start`; a step with both or neither executor; a Route to an unknown step; a `max-parallel-subagents` that is not an integer of at least 1. |
| Route coverage | A Route key the step can never produce; an unrouted business result; a business result in `defaults.routes`; `requirement-unmet` without `with.requires`; `stale-head` on an operation or input type that cannot produce it. |
| Unsupported constructs | A list of Route targets (fan-out); a per-step `model`; conditions or expressions (`when`, `if`, ...); a wildcard Route; `retry` or limit-handler keys (use Routes). |
| Graph | A step unreachable from `start`; a cycle of `limit-exceeded` Routes; a required input not proven present on every path (declare it `{optional: ...}`). |
| Ends | `error` or `limit-exceeded` routed directly to `completed` or `blocked`; an unknown end category; a runtime-only outcome used as `{end: ...}`; end labels for an unknown Terminal Outcome, `canceled` included; an end label equal to any Workflow's Selection Label or any Workflow's Pickup Label in the file. |
| Limits | `limits` on an operation step; a Reserved Step missing a meter; reserves not below the Attempt Ceiling; a Step Allowance larger than the Work Pool; the Attempt Ceiling, the Work Pool, or a Step Allowance below one maximal Admission hold in a meter it declares. |
| Input type | `publish-review` in an `issue` Workflow; `open-pull-request` in a `pull-request` Workflow. |
| Tools and data | A tool group outside the six (GitHub tools especially); declaring an always-present tool; declaring a reserved result name; a publication `body` that is not a `document` field or is a `<step>.facts`; an expression where a reference is expected; a facts-mode comment; a pull-request title in `with`; `input.comments` in a pull-request Workflow or as an operation `with.body` reference; a step id of `input`; the `advisor` group without an `agent.advisor` mapping (or the mapping without the group); an `agent.advisor` with an unknown `model` id, a missing or untrusted prompt file, an unknown key or an instructions list; a per-step `model`. |

Each error is a targeted message naming the offending path (for example
`workflows.implement.steps.process.routes`).

## Secrets and the environment

Secrets appear only as environment-variable **names** (`token-env`,
`api-key-env`), never as values (contract
[§2](../contract/step-workflow-runtime-contract.md#2-workflow-configuration),
[§18](../contract/step-workflow-runtime-contract.md#18-identity-and-authorization)).
Validation prints names, never values. The model key is redacted from the
transcript and stripped from every Validation Contract command, and the
`shell` group is credential-free. Where the configuration names separate
tracker and code-host credentials (the decided `version: 5` shape), both
are required at startup and both are registered in the single redaction
registry — one choke point, however many secrets.

`agent config validate --config <file> --workflow <id>` checks the whole file
statically, without opening an Attempt or writing to GitHub: `--workflow`
only selects which Workflow runs and never narrows validation or bundle
membership. CI runs the same validator over `examples/workflows.yaml`.

## Editor setup (JSON Schema)

`agent config schema` prints a JSON Schema (draft 2020-12) for the
`version: 4` file to stdout; the committed copy is
[`schemas/workflow-v4.schema.json`](../../schemas/workflow-v4.schema.json).
It is generated from the validator's own vocabulary, so the two cannot drift
apart. Point an editor running
[yaml-language-server](https://github.com/redhat-developer/yaml-language-server)
at it for completion, hover docs and early errors while editing:

```yaml
# yaml-language-server: $schema=../schemas/workflow-v4.schema.json
```

or, in VS Code settings (`yaml.schemas` from the YAML extension):

```json
{
  "yaml.schemas": {
    "../schemas/workflow-v4.schema.json": "workflows.yaml"
  }
}
```

The schema checks structure only: allowed and required keys, enums, scalar
types and bounds, the id shape and per-operation `with` keys. Everything
else stays with `agent config validate`, which remains required before
starting a Worker: cross-references, route exhaustiveness, the graph, limit
arithmetic, end-label checks, credential names (never values), prompt files
and YAML-level refusals. Contract
[§3](../contract/step-workflow-runtime-contract.md#3-static-validation) is
the authority.

## Prompt files and trust

The file and its prompt files are trusted operator input read from the
Worker's filesystem only, never from the Target Repository. Relative
`prompt:` paths resolve against the YAML file's directory and must stay
inside it; containment is rechecked on every run, and a prompt outside the
configuration directory (including via a symlink escape) refuses startup
naming the YAML location. A prompt that stays inside through a contained
symlink is accepted. Every referenced prompt in every Workflow is read as
raw bytes and must be strict UTF-8: a missing, unreadable or non-UTF-8
prompt refuses normally, without a traceback and without echoing file
contents. The bytes are preserved exactly — CRLF line endings and non-ASCII
content reach the model byte for byte — and used verbatim: no templating, no
interpolation. Prompt bytes are part of the pinned Effective Configuration
(see [Effective Configuration and digest](#effective-configuration-and-digest));
editing one after preparation refuses verification until re-prepared.

Nothing in the input reaches a step as instructions: the graph, the
instructions, the Skill Bundle and all authority come only from the
Effective Configuration pinned at Claim. The issue or pull-request title and
body are framed as data; comments, reviews and labels do not reach steps;
files in the working tree (such as `AGENTS.md`) are data the model may read
(contract
[§18](../contract/step-workflow-runtime-contract.md#18-identity-and-authorization)).

## Stopping the Worker

SIGTERM (`docker compose stop worker`) and the other runtime-only endings
are defined in contract
[§12](../contract/step-workflow-runtime-contract.md#12-sigterm-and-runtime-only-endings);
they are not Routes, and the YAML cannot change how they end an Attempt.
There is no way to cancel a single Attempt in the first release: `stop`
only drains intake, and removing the Selection Label withdraws the input
(`withdrawn`). An Attempt ended by SIGTERM records `interrupted` with
reason `shutdown` and needs attention: the Selection Label is removed, so
the work continues only when a human applies it again. Within
`worker.shutdown-grace-seconds` the live process finalises the Attempt
itself, and an operator sees:

- an in-flight model request is discarded;
- an external write whose Effect Intent was already recorded is carried out
  and settled, not diverted;
- the dirty tree is never committed or pushed; the terminal comment gives
  the count of uncommitted files, and the workspace stays local;
- the usual finalisation runs: terminal comment, Selection Label removed,
  end labels applied.

Whatever the grace does not cover is finished at the next startup. Give
the container a stop timeout longer than the grace (for example
`stop_grace_period` in Compose), or the stop becomes a process loss.

A process loss (SIGKILL, a crash) is recovered at the next
startup without re-entering the graph: the in-flight write is looked up and
settled, and the Attempt ends `interrupted`, or `withdrawn` if the input
was withdrawn meanwhile (contract
[§16](../contract/step-workflow-runtime-contract.md#16-recovery)).

## Upgrading: drain, backup and rollback

This release runs only `version: 4` files and refuses to recover records it
did not write, so upgrading is a coordinated operator sequence, not a
restart. A `version: 3` file is refused with exit 1 and a migration
diagnostic before installation, live calls, Claim or GitHub writes; the
refused file and any old installed artifacts are left untouched
(`version: 2` keeps its own refusal). There is
no converter, no dual engine and no automatic policy approval. The
versioned notice for the first v3 release is
[the v3 breaking-change notice](./v3-breaking-change-notice.md); the
release record with exact versions, source commits and evidence is
[the v3 release evidence](../plan/v3-release-evidence.md).

1. **Retain the last compatible release.** Keep its executable or image and
   its deployment inputs until the upgrade is proven: you need it to drain,
   and you need it again if you roll back.
2. **Drain with the compatible release.** Stop intake and finish or recover
   every open Attempt with the release that opened it — a terminal outcome
   alone does not prove finalisation is complete. Confirm the state is
   quiescent: no unfinished or unconfirmed Claim and no unsettled lifecycle
   work. Closed history stays listable, viewable and exportable throughout.
3. **Back up while stopped.** Stop the Worker, then take a coordinated
   backup of the persistent state (the state volume: Run Ledger, mirror,
   workspaces, evidence) and the operator inputs (the Workflow
   Configuration, its prompt files, `skill-policy.json` and the prepared
   bundle records) together. A state backup without its inputs — or inputs
   without their state — is not a restore point.
4. **Upgrade and re-prepare.** Change the YAML version to `4`: declare
   `context-window-tokens`, `context-threshold-tokens` and
   `output-cap-tokens` on every pin (direct pins newly — the old file
   has none) and rename `tokens` to `effective-tokens` in every
   `limits` block. The refusal names all three changes. Then declare
   the desired Git sources, exact names and optional refs explicitly in
   `skills`. For a deliberate no-skill deployment, omit `skills` or use
   `skills: []` and remove static skill references; do not assume a fixed
   application default. Supply an operator-approved policy matching the
   exact installed `SKILL.md` and the complete recursive file inventory.
   Historical per-file decisions may be carried forward only for matching
   reviewed content — the same source identity, `SKILL.md` digest and
   complete inventory. They never auto-approve host HEAD, a changed
   `SKILL.md`, added metadata or assets, or changed executable bits, and
   administrative files such as `agents/openai.yaml` need explicit
   dispositions too. Then run secret-free `agent config validate`,
   explicitly prepare with `--mode installed` or `--mode install`, and
   verify against the final roots; offline adoption of a symbolic ref needs
   saved symbolic-ref resolution evidence (`--resolution-manifest`), and an
   empty membership still needs the explicit valid policy and the three
   generated records. A legacy install/list report alone is insufficient.
   The mechanical part of the policy update — hashing, listing the
   inventory, collecting the review-aid candidates and working out which
   existing reviews still match — can be drafted offline with
   `agent draft-skill-policy --config <file> --home <path> --policy
   skill-policy.json --out skill-policy.draft.json`, which reads the
   declared members from the already-installed store. Its output is a
   draft for operator review, never an approval: every field that needs a
   human decision (each file's `disposition` and `reason`, each reference
   ruling's `classification` and `reason`, and each review's `reason`) is
   JSON `null`, so the draft always fails policy validation until the
   operator resolves every placeholder. Suggestions (earlier dispositions
   and classifications, `requires` candidates) appear only in the printed
   review report, visibly marked as unconfirmed, never in the draft file.
5. **Restart with runtime credentials only** — the target token and the
   selected Workflow's model key — keeping the mandatory live checks. Never
   use dummy secrets to make build validation pass. Update scripts,
   Compose, examples and downstream command renderers to the new flags —
   `--home` for `prepare-skill-bundle` and `verify-skill-bundle`,
   `--skill-home` for `run` — and the complete input set (`--config`,
   `--policy`, `--bundle-dir`); removed legacy options fail with a usage
   error pointing at their replacements. Supply the actual
   environment's Supported Toolchain Matrix and separate writable persistent
   state.
6. **Roll back only compatibly.** A new Worker refuses recovery of an
   incompatible or incomplete open record: it never invents missing
   identity or instruction archives from current deployment files, converts
   nothing and fabricates no archives. Once the new release has written
   new-version records or state, rolling back means running a compatible
   release or restoring the coordinated pre-upgrade backup with
   reconciliation of the external effects landed meanwhile. Never attach
   an older Worker blindly, never restore stale state while ignoring later
   GitHub effects, and never delete history or reset state to bypass a
   refusal.

### Keeping the old four-skill selection

To keep the previous behaviour on purpose, declare the old four-skill
selection explicitly as the reference deployment does:
[`docker/deployment/workflow.yml`](../../docker/deployment/workflow.yml)
pins `mattpocock/skills` at commit
`3cca18b368ae95cdbdebbff572ccafa662551015` with
`include: [implement, tdd, code-review, codebase-design]` and a reviewed
[`skill-policy.json`](../../docker/deployment/skill-policy.json) whose
per-file dispositions carry forward only for matching content, as above.
When moving the pin, `agent draft-skill-policy` drafts the update: reviews
that still match are carried forward unchanged, changed or new members get
placeholder-only reviews, and reviews that no longer match any declared
member are kept as stale entries (which is what preserves a rollback to
this selection). The draft still needs operator review — it cannot pass
validation until every placeholder is resolved.

## External builders: the Agent Forge sequence

An external builder — Agent Forge, or an operator assembling a derived
image by hand — supplies the same complete instruction package the
reference deployment bakes, and never interprets skill membership itself:
it passes explicit paths and lets the Worker's own phases decide. The
sequence, as performed by the `deployment` build target (contract
[§4.1](../contract/step-workflow-runtime-contract.md#41-startup) and the
Dockerfile's reference section), is:

1. **Assembly.** Build `FROM` the reusable base image without changing it:
   add the Target Project's toolchains, generate the Supported Toolchain
   Matrix from an explicit runtime selection, and bake one complete `version:
   4` instruction package — the Workflow Configuration, its prompt files
   and the reviewed `skill-policy.json`.
2. **Preparation.** As the runtime user, with no Worker GitHub or model
   secrets set: `agent config validate` (secret-free), then
   `agent prepare-skill-bundle --mode install` to resolve and install the
   declared subset and publish `requirements.json`, `manifest.json` and
   `validation.json` as one completed generation.
3. **Final-root verification.** After the last relevant copy, re-run
   `agent verify-skill-bundle` against the final roots, read-only and
   offline — a builder-stage log is never the acceptance proof. A relocated
   identical tree verifies; a byte-edited copy, or a declared member the
   install cannot provide, refuses or fails the build.
4. **The rendered `run` invocation.** Start the Worker with the explicit
   paths and runtime credentials only, for example (as the Compose `worker`
   service renders them):
   `agent run --config /etc/coding-agent/workflow.yml --workflow implement
   --policy /etc/coding-agent/skill-policy.json --bundle-dir
   /opt/coding-agent/skill-bundle --skill-home /home/agent --state-dir
   /var/lib/coding-agent --toolchain-matrix
   /opt/coding-agent/toolchain-matrix.json`.
   An operator-prepared external package runs the same way: mount its roots
   read-only, override `--config`/`--policy`/`--bundle-dir` to point at
   them, and keep `--skill-home` on the runtime home the package was
   prepared against.

Forge support is not advertised as verified until the Forge adapter is
exercised against this sequence; the reference deployment image above is
the proven path today.

## Effective Configuration and digest

At Claim the Attempt pins its Effective Configuration (contract
[§2.10](../contract/step-workflow-runtime-contract.md#210-effective-configuration-and-pinning)):
the selected Workflow, normalised and compiled (steps, Routes, result
schemas, limits, `requires`, `input` selector, `worker` settings, end
labels); the bytes of every prompt file; every available Skill Bundle
member's provenance, inventory and selected review — including on-demand-only
members — with the exact activation delivery bytes (the complete `SKILL.md`,
the ordered `inject` companions and the framed notices) under a versioned
framing; and the Pinned Model with its prices, Model Context Window,
context threshold and output cap. The Attempt records this identity in full
as record `version: 4` (its own numbering, separate from the YAML
`version`): the identity core has the sections
`workflow`, `file`, `prompts`, `skill_bundle`, `activations` and
`pinned_model`, and one SHA-256 digest covers the canonical core.

Excluded from the identity: relocation, image retags, producer versions,
verification mode, unused reviews, unrelated store artifacts, absolute
paths and timestamps. A commit change alters the identity even when the
bytes are identical, so provenance is never erased; a change to an
on-demand-only member, its review, its ordering, its notices, its prompt
bytes, or its non-delivered inventory changes it too. The serializer keeps
the established identity JSON representation — UTF-8 encoding of Python
JSON with sorted keys, compact separators, `ensure_ascii=True` and
`allow_nan=False`, no BOM and no trailing newline — and model numbers stay
numbers, so golden vectors freeze the numeric, Unicode and framing bytes.
Environment-variable values are never part of it; the names are.
Every needed prompt and activation blob is written, flushed and
digest-addressed in the Attempt Archive before the ledger transaction the
Claim depends on, and recovery validates the record and every required blob,
refusing — without repairing from current files — on any missing, corrupt,
escaping, symlinked or incompatible core or blob.
