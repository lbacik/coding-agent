# Workflow Configuration: operator guide

**Status: describes the running Worker (`version: 2`).** This is an operator
guide to writing the Workflow Configuration YAML for step Workflows. It is
never a second source of truth: behaviour is defined by [the step-workflow
runtime contract](../contract/step-workflow-runtime-contract.md) alone, and
this guide restates only what an operator needs to write a valid file,
linking the contract section wherever a rule is summarised. Where this guide
and the contract disagree, the contract wins.

**Upgrading from `version: 1`.** This release runs only `version: 2`
step-workflow files: a legacy `version: 1` configuration is refused at
startup (contract [§16.4](../contract/step-workflow-runtime-contract.md#164-legacy-attempts)),
and a ledger holding an unfinished MVP Attempt refuses startup too. Drain
MVP Attempts with the last pre-change release until none remain unfinished,
then upgrade. There is no backward compatibility and no converter: a
`version: 1` file is rewritten by hand (contract [§21](../contract/step-workflow-runtime-contract.md#21-differences-from-the-mvp)).

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

The file has four required top-level keys and an optional `worker` block.
All keys are kebab-case.

```yaml
version: 2
target: {...}      # the Target Repository
models: {...}      # Pinned Model definitions, referenced by id
workflows: {...}   # Workflow id -> Workflow
worker: {...}      # optional: poll interval and shutdown grace
```

## File-level fields

- `version` must be `2`.
- `target.kind` is `github`. `target.repository` is `OWNER/REPO`, for example
  `lbacik/coding-agent-sandbox`. `target.base-branch` is the branch the Base
  Revision of an issue Attempt resolves from. `target.token-env` names the
  environment variable holding the GitHub token.
- `models.<id>` pins `provider`, `model`, `endpoint`, `effort`,
  `api-key-env` (the name of the variable holding the key) and
  `prices-usd-per-mtok` (`input`, `output`, `cache-read`, `cache-write`).
  `input` prices only the uncached remainder of the prompt. `effort` is
  `low`, `medium` or `high`. The (`provider`, `model`) pair and its
  `endpoint` must come from the code-owned table in
  [`provider/config.py`](../../src/coding_agent/provider/config.py)
  (contract
  [§2.1](../contract/step-workflow-runtime-contract.md#21-the-file)).
  This release knows two:

  | `provider` | `model` | `endpoint` |
  | --- | --- | --- |
  | `anthropic` | `claude-sonnet-5` | `messages` |
  | `openai` | `gpt-5.6-luna` | `responses` |

  Any other pair, or another endpoint for the provider, refuses startup.
  The context window and the per-request output cap for the pair are
  code-owned, not configured; they are pinned and covered by the digest.
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
    output-cap-tokens: 32000
    context-threshold-tokens: 100000
    prices-usd-per-mtok: {input: 2.0, output: 10.0, cache-read: 0.20, cache-write: 2.50}
```

What each key means:

- `gateway.kind` is the gateway kind, from a closed code-owned list: today
  only `litellm`. `gateway.url` is the gateway's literal `http` or `https`
  URL (host, optional port and path; no userinfo, query or fragment). It is
  canonicalised (lowercase scheme and host, no trailing `/`).
- `model` is the gateway's own name, free-form and never checked against a
  table: pin the L1 alias the gateway reports in `x-litellm-model-group`.
  Any deployment the gateway spreads that name over counts as the Pinned
  Model, and the pin key is the label `litellm:<model>@responses`.
- `endpoint` is the endpoint the code-owned table admits for the gateway
  kind (`responses` for `litellm`). `effort` is `low`, `medium` or `high`.
- `api-key-env` names the environment variable holding the gateway key; the
  value never appears in the file. The key must reach the Worker process
  (for Compose, see `compose.gateway-example.yaml`).
- `output-cap-tokens` and `context-threshold-tokens` are the pin's own
  output cap and context-window threshold. The safety margin, request
  overhead and handoff cap stay code-owned defaults, and the threshold must
  exceed the safety margin plus request overhead. A direct pin naming
  either key is refused.
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

A model that refuses a forced tool, or that accepts an invalid effort,
cannot be pinned: the Provider Capability Assertion refuses the pin at
startup with no Attempt opened (contract
[§4](../contract/step-workflow-runtime-contract.md#4-the-workers-lifecycle)).
The probe binds its tool with a forced `tool_choice` naming it and sends a
prompt asking for no tool call — only a call to the probe tool passes — and
then proves the effort reaches the wire by sending the same request with a
deliberately invalid effort, which the gateway must refuse naming the
effort. When a vendor resolves the pinned name to a snapshot, pin the name
the vendor reports.

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

There is no lifecycle-label setting at file level: the labels applied at the
end of an Attempt are declared per Workflow in
[`outcomes`](#terminal-outcomes-and-end-labels).

## A Workflow

| Key | Required | Meaning |
| --- | --- | --- |
| `model` | yes | The id of one `models` entry: the Attempt's one Pinned Model, used by every step and Sub-agent. A step never overrides it. |
| `input` | yes | Which Workflow Inputs the Worker selects: [Input selection](#input-selection). |
| `limits` | yes | The Attempt Ceiling, all four meters: [Limits](#limits). |
| `max-parallel-subagents` | no | How many Sub-agents of one group may run at once: an integer of at least 1, default 2. |
| `outcomes` | no | End labels per Terminal Outcome: [Terminal Outcomes and end labels](#terminal-outcomes-and-end-labels). |
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
`implement#42/1`. An issue Attempt starts at its Base Revision on its own
branch `agent/<workflow>/<issue>/<n>-<slug>`; a pull-request Attempt works
on the pull request's head (the **Input Head**), with the merge base as its
Base Revision.

**Drift and withdrawal** are checked before every step and every external
write (contract
[§7](../contract/step-workflow-runtime-contract.md#7-pinned-input-state-gates-drift-and-withdrawal)).
Editing the title or body, someone else pushing to the pull request, or
retargeting its base ends the Attempt `drift`. Removing the Selection Label,
closing the input or merging the pull request ends it `withdrawn`. New
commits on the base branch are disclosed, not Drift.

**No Continuation across Attempts.** Each issue Attempt starts afresh at its
Base Revision. A Workflow that wants later work to continue earlier work
does so by policy, for example by opening a draft pull request on its
handoff path and letting a `pull-request` Workflow continue on it (contract
[§17](../contract/step-workflow-runtime-contract.md#17-continuation)).

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

- **`instructions`** is an ordered list of `skill:` (a skill of the pinned
  Skill Bundle) and `prompt:` (an operator prompt file) items, activated
  together when the conversation opens; the whole Skill Bundle is also
  available on demand. Instructions grant no authority. Several
  instructions in one step carry no ordering guarantee; a guaranteed
  sequence needs separate steps.
- **`tools`** are denied by default and granted from five groups:

  | Group | Grants |
  | --- | --- |
  | `workspace-read` | File reads, listing, search, the change-versus-base reader. |
  | `workspace-write` | File writes and edits. |
  | `shell` | A credential-free command runner. Its output is never evidence. |
  | `tests` | Targeted tests that record evidence; diagnostic checks. |
  | `subagents` | Sub-agents with fixed read-only tools; their spend counts to the step and the Attempt. |

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
  every agent step through its opening frame.
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

Four meters are counted: `cost-usd`, `tokens` (effective tokens, excluding
cache reads), `tool-calls` and `wall-clock-minutes` (contract
[§11](../contract/step-workflow-runtime-contract.md#11-limits)).

- **Attempt Ceiling** (Workflow `limits`, mandatory, all four meters): the
  whole Attempt, from its opening, including Sub-agents, Context-Window
  Handoffs and operation time.
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

Exceeding any of them, or an Execution Ceiling refusal, yields
`limit-exceeded`, routed like any other result. Admission is worst-case:
before every model request the runtime checks that the request's upper
bound still fits, and refuses it otherwise. After the Attempt Ceiling is
spent no model request is admitted, but operation steps still run, so a
Hard Handoff chain of operations can always finish.

**Worked example** (`implement` in the example file):

| Meter | Attempt Ceiling | `ai-handoff` (reserved) | Work Pool |
| --- | --- | --- | --- |
| `cost-usd` | 20 | 1 | 19 |
| `tokens` | 4 000 000 | 150 000 | 3 850 000 |
| `tool-calls` | 600 | 30 | 570 |
| `wall-clock-minutes` | 60 | 5 | 55 |

`process` (12 USD) and `self-review` (3 USD) each fit in the 19 USD Work
Pool; they share it, and `process` spends its 12 USD across all three of its
executions. Non-reserved model work stops at minute 55, leaving the reserved
5 minutes to `ai-handoff`. If `process` exhausts its allowance or the Work
Pool, it routes to `ai-handoff`, which still has its full 1 USD. In
`review`, the 0.5 USD reserve leaves a Work Pool of 5.5 USD / 1 400 000
tokens / 280 tool calls / 25 minutes, and `review`'s 4 USD fits in it.

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
  shared 12 USD allowance; its `limit-exceeded` (allowance, Work Pool, or a
  fourth entry refused) goes to `ai-handoff`, the only step that does so.
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
  work exists, and `handoff-comment` posts the note and ends `unfinished`.
  If `ai-handoff` errs or runs out of its reserve, `defaults` send it to
  `hard-handoff`.
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
| Ends | `error` or `limit-exceeded` routed directly to `completed` or `blocked`; an unknown end category; a runtime-only outcome used as `{end: ...}`; end labels for an unknown Terminal Outcome, `canceled` included; an end label equal to any Workflow's Selection Label in the file. |
| Limits | `limits` on an operation step; a Reserved Step missing a meter; reserves not below the Attempt Ceiling; a Step Allowance larger than the Work Pool. |
| Input type | `publish-review` in an `issue` Workflow; `open-pull-request` in a `pull-request` Workflow. |
| Tools and data | A tool group outside the five (GitHub tools especially); declaring an always-present tool; declaring a reserved result name; a publication `body` that is not a `document` field or is a `<step>.facts`; an expression where a reference is expected; a facts-mode comment; a pull-request title in `with`. |

Each error is a targeted message naming the offending path (for example
`workflows.implement.steps.process.routes`).

## Secrets and the environment

Secrets appear only as environment-variable **names** (`token-env`,
`api-key-env`), never as values (contract
[§2](../contract/step-workflow-runtime-contract.md#2-workflow-configuration),
[§18](../contract/step-workflow-runtime-contract.md#18-identity-and-authorization)).
Validation prints names, never values. The model key is redacted from the
transcript and stripped from every Validation Contract command, and the
`shell` group is credential-free.

`agent config validate --config <file> --workflow <id>` checks a file
statically with no runtime secrets: only credential *names* are checked,
never values. It opens no Attempt and writes nothing to GitHub. CI runs the
same validator over `examples/workflows.yaml` with no secrets in the
environment.

## Editor setup (JSON Schema)

`agent config schema` prints a JSON Schema (draft 2020-12) for the
`version: 2` file to stdout; the committed copy is
[`schemas/workflow-v2.schema.json`](../../schemas/workflow-v2.schema.json).
It is generated from the validator's own vocabulary, so the two cannot drift
apart. Point an editor running
[yaml-language-server](https://github.com/redhat-developer/yaml-language-server)
at it for completion, hover docs and early errors while editing:

```yaml
# yaml-language-server: $schema=../schemas/workflow-v2.schema.json
```

or, in VS Code settings (`yaml.schemas` from the YAML extension):

```json
{
  "yaml.schemas": {
    "../schemas/workflow-v2.schema.json": "workflows.yaml"
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
inside it. Prompt content is used verbatim: no templating, no interpolation.

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

## Effective Configuration and digest

At Claim the Attempt pins its Effective Configuration (contract
[§2](../contract/step-workflow-runtime-contract.md#2-workflow-configuration)):
the selected Workflow, normalised and compiled (steps, Routes, result
schemas, limits, `requires`, `input` selector, end labels); the bytes of
every prompt file; the text of every skill a step names, plus the identity
and digest of the whole Skill Bundle; and the Pinned Model with its prices,
context window and output cap. One digest covers all of it.
Environment-variable values are never part of it; the names are. Recovery
uses pinned values only.
