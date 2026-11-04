# Workflow Configuration: operator guide

**Status: current.** This is an operator guide to writing the Workflow
Configuration YAML. It is never a second source of truth: behaviour is
defined by [the MVP runtime contract](../contract/mvp-runtime-contract.md)
§2 (Workflow Configuration) alone, and this document links there wherever a
rule is summarised. Where this guide and the contract disagree, the contract
wins.

Related: [the MVP implementation plan](../plan/mvp-implementation-plan.md)
slice M5, and [ADR 0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md)
(a Workflow fills only the implementation stage).

## What the file decides

One YAML file declares the Target Repository, the Pinned Model(s), the
Worker's timing and every Workflow the process may run. A process reads the
file (and every prompt file it names) once at startup, runs exactly one
Workflow selected with an explicit `--workflow`, and pins that Attempt's
[Effective Configuration](#effective-configuration-and-digest) at Claim
(contract §2.5). Editing the file affects the next Attempt, never one already
open.

A minimal file, with everything omittable omitted:

```yaml
version: 1

target:
  kind: github
  repository: OWNER/REPO
  base_branch: main
  token_env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api_key_env: ANTHROPIC_API_KEY
    prices_usd_per_mtok: {input: 2.00, output: 10.00, cache_read: 0.20, cache_write: 2.50}

workflows:
  implement:
    model: sonnet
    selector:
      label: ready-for-agent
    steps:
      - id: implement
        skill: implement
```

## Fields

See contract §2.1 for the authoritative table.

- `version` must be `1`.
- `target.kind` is `github`; anything else is an unsupported integration
  (contract §2.1).
- `target.repository` is `OWNER/REPO`; `target.base_branch` is the branch
  the Base Revision resolves from (it is not always `main`).
- `target.token_env` and each model's `api_key_env` name an environment
  variable (contract §2.1). They never hold the secret itself: a value that
  looks like a secret is a validation error, and validation prints names,
  never values.
- `target.labels.ready_for_human` renames the `ready-for-human` label
  (default `ready-for-human`). It is the only configurable lifecycle label.
- `models.<id>` pins provider, model, endpoint, effort, the key variable
  name and the per-bucket prices. The variable `api_key_env` names is the
  credential the model call uses, whatever its name; it is redacted from the
  transcript and stripped from every Validation Contract command. Cost is
  computed per bucket from `prices_usd_per_mtok`: `input` prices only the
  uncached remainder of the prompt, `cache_read` and `cache_write` their own
  tokens. The context configuration for the (provider, model) pair lives in
  code, not in this file: a pair the code does not own is refused (contract
  §2.1).
- `worker.poll_interval_seconds` (default 60) and
  `worker.shutdown_grace_seconds` (default 30) time the Supervisor loop.
- `workflows.<id>.model` names exactly one `models` entry; a step never
  overrides it.
- `workflows.<id>.selector.label` is the Workflow's one Selection Label, with
  optional narrowing filters `require_all`, `require_any` and `exclude`
  (contract §4).
- `workflows.<id>.limits` caps the Attempt: `wall_clock_minutes` (default
  60), `cost_usd` (default 20), `tokens` (default 4 000 000) and `tool_calls`
  (default 600). Every work node, reviewer conversation and handoff counts
  against them. Providers count the same prompt differently, so revisit
  `tokens` when a Workflow moves to another model (contract §11).
- `workflows.<id>.steps` is a non-empty ordered list. Each step has an `id`
  and exactly one of `skill` (a Skill Bundle member on the top-level
  allowlist — in the MVP, `implement` alone) or `prompt_file` (a file of
  operator-owned instructions).

Every id matches `[a-z0-9-]+`.

## Strictness

Validation is strict (contract §2.2): unknown fields at any level, duplicate
keys, anchors, aliases, merge keys (`<<`) and custom tags are all refused, as
is a step with both or neither of `skill`/`prompt_file`, an empty `steps`
list or a duplicated step id. The Selection Label must not be empty, equal
the `ready-for-human` name or `wontfix`, match `wayfinder:*`, or appear in
the same Workflow's `require_all`, `require_any` or `exclude`.

## Secrets and the environment

Secrets appear only as environment-variable **names** (contract §2.1). Every
variable the file names must be set when the process starts; an unset one
fails validation naming the variable, never a value. `agent config validate`
checks all of this statically and offline (contract §2.4): no Attempt, no
GitHub write, no network call.

## Prompt files and trust

The file and its prompt files are trusted operator input read from the
Worker's filesystem only, never from the Target Repository (contract §2.3).
Relative `prompt_file` paths resolve against the YAML file's directory and
must stay inside it after symlink resolution. Prompt content is used
verbatim: no templating, no interpolation. A custom skills directory is
refused; customise through `prompt_file`.

## Stopping the Worker

`docker compose stop worker` sends SIGTERM (contract §3, §11; rows P-CAN-3,
REC-SIG-1, REC-SIG-2). Intake stops at once — no new Claim is made — and the
live Attempt observes the signal after every model response and every tool
result. The in-flight model request is discarded at the next stop point and
the running command's whole process group is killed. Before the Delivery
Intent the Attempt takes the claim-confirmed recovery path: local commits are
pushed fast-forward, the dirty tree is never committed (the comment states
how many uncommitted files were not preserved), and the outcome is
`failed` / `interrupted`, or `abandoned` where the issue was cancelled
meanwhile. After the Delivery Intent publication completes as `delivered`. A
second signal, or SIGKILL, is an ordinary process loss recovered at the next
startup (M8).

Keep `stop_grace_period` in `compose.yaml` above
`worker.shutdown_grace_seconds` (default 30 s): Docker's default 10 s stop
timeout would SIGKILL the Worker in the middle of that grace period.

## Effective Configuration and digest

A valid file yields the Effective Configuration (contract §2.5): the
normalised selected Workflow (defaults filled in), the file-level settings
it runs under (`target`, the referenced model, `worker`), the bytes of every
prompt file its steps activate, and a SHA-256 digest of all of it.
Environment-variable values are never part of it; the names are.
`agent config validate --config <file> --workflow <id>` prints the digest;
the pull-request body and the terminal comment disclose it.
