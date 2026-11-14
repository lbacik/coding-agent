# Coding Agent

A containerised agent that runs declarative step Workflows against one configured GitHub
repository: for example, it takes an issue, implements it with the upstream `/implement` skill and
hands a pull request back to a human, or it reviews an existing pull request. Which inputs it takes,
which steps run in which order, what each step may do and spend, and how it ends are all declared in
a YAML [Workflow Configuration](#configuring-a-workflow).

The vocabulary this README uses — **Worker**, **Workflow**, **Target Repository**,
**Project Profile**, **Skill Bundle**, **Validation Contract** — is fixed in
[`CONTEXT.md`](./CONTEXT.md); the behaviour is fixed in
[the step-workflow runtime contract](./docs/contract/step-workflow-runtime-contract.md).

## What exists today

`main` runs the step-workflow runtime: all slices S1–S10 of
[the step-workflow implementation plan](./docs/plan/step-workflow-implementation-plan.md) are in
place, and the cutover (S10) removed the fixed MVP Attempt graph and its `version: 1`
configuration. Releases up to and including `v0.2.0` still run the MVP runtime described by
[the MVP runtime contract](./docs/contract/mvp-runtime-contract.md), now a frozen design record.

| Command | Slice | What it does |
| --- | --- | --- |
| `agent preflight` | v1 S0b | Performs every write probe the contract's identity section requires, against a **sandbox** repository. |
| `agent startup-check` | v1 S0b | The non-mutating startup checks: identity, token kind, push permission, rate-limit headroom. |
| `agent verify-skill-bundle` | v1 S1b | The three verification layers over a build-time `agent-installer` run. Runs during the image build; re-runnable against a built image. |
| `agent validate` | v1 S2c | Runs a Project Profile's Validation Contract for real against a checkout: bootstrap, `test_all` and every check, or one `test_targeted` file. |
| `agent provider-check` | v1 S3.3 | The Provider Capability Assertion, standalone: one live call proving a Pinned Model can be used at all. No GitHub access. |
| `agent config validate` | S1 | Validates a `version: 2` Workflow Configuration file statically, offline, with no Attempt and no GitHub write. |
| `agent run` | S10 | The Worker: runs one Workflow of the configuration — selects its inputs (issues or pull requests) by Selection Label, claims each by assignment, runs the Workflow's steps and finalises with a terminal comment, one Attempt at a time. |

## Requirements

- **CLI use**: Python 3.13 and [uv](https://docs.astral.sh/uv/). A GitHub fine-grained token for
  the two commands that talk to GitHub.
- **Container use**: Docker. The base image carries coding-agent, agent-installer and their own
  dependencies; a derived deployment image adds the Supported Toolchain Matrix — Python 3.13/uv,
  PHP 8.4/Composer, Node 22/pnpm in the reference deployment — so a Target Project's own
  toolchain does not need to be installed on the host.

## Using the agent from the CLI

```sh
uv sync                 # install into .venv, from the committed lockfile
uv run agent --help
```

Run it as `uv run agent <command>`, or activate the environment (`source .venv/bin/activate`) and
call `agent` directly. Every command exits `0` on success and non-zero with the failure named on
stderr.

### The credential

The two GitHub commands read the token from an environment variable, `GITHUB_TOKEN` by default;
`--token-env` names a different one. An unversioned `.env` at the repository root is loaded at
startup for local operator use (existing environment variables are never overridden) — it is
`.gitignore`d and must stay that way. The containerised Worker never uses it; it gets its
credential from the deployment environment.

Provisioning the token and the sandbox repository is
[the S0a runbook](./docs/runbooks/s0a-agent-identity-credential.md).

### Additional local environment files

The CLI always loads an optional `.env` in its working directory. For local IDE settings that
should supplement it (for example a database URL used by `run`), pass an application-level
file before the command:

```sh
uv run agent --app-env-file ./tmp/jh-api-agent.env run ...
```

Variables already in the process or `.env` keep precedence. This is intentionally separate from
uv's own `--env-file` option, which some IDE launchers do not forward.

### `agent startup-check` — read-only

```sh
GITHUB_TOKEN=github_pat_... uv run agent startup-check --repo <owner>/<sandbox-repo>
```

Prints the Agent Identity's account id and login, whether `permissions.push`
is true, and the rate-limit headroom. Performs no write.

### `agent preflight` — writes, so sandbox only

```sh
GITHUB_TOKEN=github_pat_... uv run agent preflight --repo <owner>/<sandbox-repo>
```

Opens a probe issue of its own, comments on it, adds and removes a label, pushes a branch, opens
and closes a pull request, confirms that a push touching `.github/workflows/` is **rejected**, then
closes the probe issue with the results summarised on it. An empty sandbox gets its first commit
made for it. **Never run this against the real Target Repository** — it writes for real, and the
rejected-workflow probe only means anything where the Workflows permission was deliberately
withheld.

### `agent validate` — no GitHub access

Runs one checkout's own Validation Contract, exactly as the harness node will:

```sh
uv run agent validate \
  --project-dir /path/to/target-project \
  --evidence-dir /path/to/evidence
```

The Project Profile is read from `<project-dir>/docs/agents/project-profile.yml` unless `--profile`
points elsewhere. `--skip-bootstrap` skips the bootstrap command when it has already run;
`--targeted <path>` runs `test_targeted` against that one test file instead of `test_all` and the
checks. Each command is reported with its exit code, its executed test count and its individual
failure identifiers, read out of the JUnit XML the profile declares:

```
[PASS] bootstrap: command='uv sync --frozen' exit=0 executed=None failures=[] (passed)
[FAIL] test_all: command='uv run pytest …' exit=1 executed=2 failures=['tests.test_extra::test_shout'] (failed)
```

Running it against this repository's own fixtures needs the fixture's toolchain on the host, which
is what the reference deployment image exists for — see the container section below.

### `agent provider-check` — no GitHub access

```sh
uv run agent provider-check --provider anthropic
uv run agent provider-check --provider openai
```

Makes one live call to the named Pinned Model and asserts the Provider Capability: tool calling
works, the pinned effort level is honoured, usage is reported non-zero, and a Price Table entry
exists for it (ADR 0009). A refusal exits non-zero with nothing opened; `agent run` makes the
same assertion at startup, for the Pinned Model its Workflow Configuration names.

### `agent run` — the Worker; writes, so sandbox only until you mean it

```sh
uv run agent config validate --config ./tmp/workflow/workflow.yml --workflow implement
uv run agent run --config ./tmp/workflow/workflow.yml --workflow implement
```

The repository, base branch, token variable, Pinned Model, prices, limits and steps all come
from the [Workflow Configuration](#configuring-a-workflow); `run` takes no repository or provider
flag of its own. `--workflow` selects which of the file's Workflows this process runs: one Worker
runs one Workflow, so two scenarios (say `implement` and `review`) mean two Workers. Startup runs
in the contract's order — validate the configuration, verify the installed Skill Bundle, run the
non-mutating GitHub checks, run the Provider Capability Assertion, open the Run Ledger — and any
refusal exits non-zero with no Attempt opened and no GitHub write. A `version: 1` file is refused,
and so is a Run Ledger holding an unfinished MVP Attempt.

The Worker then polls. It selects the open inputs — issues or pull requests, as the Workflow's
`input` declares — carrying the Workflow's Selection Label, takes the oldest eligible one, claims
it by assigning the Agent Identity, posting a Claim Comment and reading it back, and runs the
Workflow's steps from `start`, following each step's Routes until a Workflow End. Each Attempt ends
in exactly one Terminal Outcome, recorded in the Run Ledger before the terminal comment is
written; the Selection Label is then removed and the Workflow's end labels are applied. It sleeps
for `worker.poll-interval-seconds` only when a sweep claimed nothing. A Credential Failure halts
it at once, and three consecutive infrastructure-class failures halt it after the third Attempt is
finalised.

SIGTERM ends the live Attempt `interrupted` within `worker.shutdown-grace-seconds`, and an Attempt
a process loss interrupted is settled at the next startup; see
[Stopping the Worker](./docs/ops/workflow-configuration.md#stopping-the-worker).

**Point it at the sandbox until you mean it**: it assigns, comments, labels, pushes and opens pull
requests for real.

Other flags: `--state-dir` (default `/var/lib/coding-agent`) for the Run Ledger, mirror, workspace
and evidence root; `--skills-home` (default the current user's home) for where the Skill Bundle is
installed; `--skill-bundle-dir` and `--skill-bundle-ignore-file` (defaulting to the image's
`/opt/coding-agent/skill-bundle` and `/opt/coding-agent/skill-bundle-ignore.txt`) for the
build-time reports the bundle is verified against at startup. Outside the image, point those at an
`agent-installer install --json` / `list --json` pair of your own.

Outside the image, also generate the Supported Toolchain Matrix from the toolchains installed on
the host first:

```sh
uv run python scripts/generate-toolchain-matrix.py
```

The CLI discovers `./tmp/toolchain-matrix.json` by default locally. Set
`CODING_AGENT_TOOLCHAIN_MATRIX` or pass `--toolchain-matrix PATH` to use another file. A derived
deployment image generates the same file at build time from an explicit runtime selection
(`--runtime python --runtime php --runtime node`) and keeps using its immutable build output at
`/opt/coding-agent/toolchain-matrix.json`; a missing or malformed file is reported before any
Attempt state or model call is created. The bare base image carries no Matrix at all, so
`agent run` against it refuses at startup, naming the missing file.

### Developing on the agent itself

```sh
uv run pytest        # no network, no Docker
uv run mypy src tests
```

## Configuring a Workflow

The Worker's whole policy lives in one operator-owned YAML file, the **Workflow Configuration**
(`version: 2`). It declares the Target Repository, the Pinned Model(s) and one or more Workflows.
A Workflow says which inputs it takes, which steps run and in which order, what each step may do
and spend, and how the Attempt ends; the runtime itself knows no scenario. The file and every
prompt file it names are read once at startup, and an edit affects the next Attempt, never one
already open.

- **Start from the example.** [`examples/workflows.yaml`](./examples/workflows.yaml) holds two
  complete Workflows: `implement` (issue in, pull request out, with a self-review loop and an
  AI-assisted Handoff) and `review` (existing pull request in, review out). Its prompt files are in
  [`examples/prompts/`](./examples/prompts).
- **Read the guide.** [The operator guide](./docs/ops/workflow-configuration.md) explains every
  key: input selection, agent and operation steps, Routes and loops, limits and Reserved Steps,
  Terminal Outcomes and end labels, the common validation errors and how the Worker stops.
- **The contract is the authority.** Where the guide and
  [the step-workflow runtime contract](./docs/contract/step-workflow-runtime-contract.md)
  disagree, the contract wins.

A minimal Workflow, trimmed from the example — readiness, one agent step, validation with one
fix-up round, then push and a pull request:

```yaml
version: 2

target:
  kind: github
  repository: OWNER/REPO
  base-branch: main
  token-env: GITHUB_TOKEN               # the variable's name, never its value

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}

workflows:
  implement:
    model: sonnet
    input:
      type: issue                       # or pull-request
      label: agent:implement            # the Selection Label a human applies
      exclude: [ready-for-human]
    limits:                             # Attempt Ceiling: all four meters are mandatory
      cost-usd: 10
      tokens: 2000000
      tool-calls: 300
      wall-clock-minutes: 45
    outcomes:                           # end labels per Terminal Outcome
      unfinished: {labels: [ready-for-human]}
      failed: {labels: [ready-for-human]}

    start: preflight
    defaults:
      routes:                           # reserved results only
        error: {end: failed}
        limit-exceeded: {end: unfinished}

    steps:
      preflight:
        operation: check-readiness
        routes:
          ready: process
          not-ready: {end: blocked}

      process:
        agent:
          instructions:
            - skill: implement
          tools: [workspace-read, workspace-write, shell, tests]
          inputs:
            - optional: validate.facts  # why the previous validation failed, if it ran
          results:
            done:
              summary: document
        max-executions: 2               # one fix-up round after a failed validation
        routes:
          done: validate

      validate:
        operation: run-validation
        max-executions: 2
        routes:
          passed: push
          failed: process

      push:
        operation: commit-and-push
        routes:
          pushed: open-pr
          no-changes: {end: blocked}

      open-pr:
        operation: open-pull-request
        with:
          body: process.done.summary
          requires: [run-validation: passed]
        routes:
          opened: {end: completed}
          already-open: {end: completed}
          requirement-unmet: {end: failed}
```

Validation is strict and fail-fast — unknown keys, unrouted results, unreachable steps, limits that
do not add up all refuse startup — so check a file before running it:

```sh
uv run agent config validate --config ./tmp/workflow/workflow.yml                       # every Workflow
uv run agent config validate --config ./tmp/workflow/workflow.yml --workflow implement  # one
```

A `version: 1` file from `v0.2.0` or earlier is refused, with no converter: rewrite it by hand
against the guide.

## Using the agent in Docker

The container story has two layers. The **base image** — the default output of `docker build .` —
packages coding-agent, agent-installer and only the dependencies those applications need
(Python/uv for the Worker, Node for agent-installer). A **deployment image**, built `FROM` the
base, adds the Target Project's toolchains, the Supported Toolchain Matrix generated from an
explicit runtime selection, the installed Skill Bundle, and verification reports. Only a
deployment image (plus an operator-supplied Workflow Configuration) can run a Workflow:
`agent run` against the bare base refuses at startup, naming the missing Matrix.

> **Agent Forge migration (future work).** Agent Forge currently builds this repository's
> Dockerfile directly. Its adapter must be updated to build a derived deployment image before
> the new base can serve as a complete Worker deployment; that support is not implemented here.

### Build the images

```sh
docker build -t coding-agent:dev .                       # the reusable base image
docker build --target deployment -t coding-agent:deployment .   # the reference deployment image
```

The reference deployment build pins every toolchain to one version (Python 3.13/uv, PHP 8.4/Composer,
Node 22/pnpm), generates the Supported Toolchain Matrix it actually produced at
`/opt/coding-agent/toolchain-matrix.json` from an explicit `--runtime` selection, installs the
Skill Bundle from a pinned upstream commit, and then runs `agent verify-skill-bundle` — so a
bundle that silently shrank, drifted off its pin, or lost a companion file **fails the build**,
naming what was wrong.

The base build fetches no Skill Bundle and installs no PHP, Composer or pnpm; its default command
prints CLI help (`agent --help`) and needs no Workflow Configuration.

### The short way: compose

[`compose.yaml`](./compose.yaml) encodes the run flags below — the read-only root filesystem, the
writable `/tmp`, the state volume, the credential names — so a command is just its own arguments:

```sh
docker compose run --rm agent startup-check --repo <owner>/<sandbox-repo>
docker compose run --rm agent --help
```

The `worker` service is the Worker itself, `agent run`, reading `workflow.yml` and the prompt
files it names from `WORKFLOW_CONFIG_DIR` (mounted read-only; default `./tmp/workflow`). `WORKFLOW`
selects the Workflow id (default `implement`):

```sh
WORKFLOW_CONFIG_DIR=./tmp/workflow WORKFLOW=implement docker compose up worker
```

`stop_grace_period` (60 s) stays above the default `worker.shutdown-grace-seconds` (30 s); raise
both together.

It is never restarted automatically: a halt is meant to stay a halt until an operator looks.

The `worker` carries the operator-contract discovery labels, so a Forge-deployed
container is discoverable as a Managed Instance. `AGENT_INSTANCE_NAME` names it,
defaulting to `coding-agent`, so it resolves with nothing set.

The `agent` service runs the base image; `worker` and `validate` build the `deployment` target,
the reference/example deployment image (test-only, not a production image).

Compose builds the images on first use and reuses them afterwards; `docker compose build` rebuilds
them when the Dockerfile or the sources they copy have moved.

The `validate` service additionally mounts a checkout at
`/var/lib/coding-agent/workspaces/target`. `TARGET_PROJECT` names it, and defaults to this
repository's Python L2 fixture, so the service runs as a real demonstration with nothing set:

```sh
docker compose run --rm validate                       # the fixture: one test fails, by design
TARGET_PROJECT=../my-project docker compose run --rm validate
TARGET_PROJECT=../my-project docker compose run --rm validate \
  --skip-bootstrap --targeted tests/test_greeting.py
```

The credential is passed by name only. `GITHUB_TOKEN` is what the CLI reads by default;
`SANDBOX_TOKEN` is passed through as well, so a sandbox credential already sitting in `.env` can be
selected with `--token-env SANDBOX_TOKEN` instead of being copied under a second name. Compose
interpolates `.env` when it loads the file, so `docker compose config` prints those values in
plaintext — read it, don't paste it.

`docker compose down -v` removes the state volume and everything cached in it.

Compose does not replace the verification scripts further down: those build the image themselves,
run one deliberately failing build, and drive containers with flags of their own.

### Run a command by hand

The image defines no entrypoint: pass the command you want. It already runs as the non-root
`agent` user (uid 1000), and expects a read-only root filesystem with `/tmp` and one volume at
`/var/lib/coding-agent` writable.

```sh
docker volume create coding-agent-state

docker run --rm --read-only --tmpfs /tmp \
  -v coding-agent-state:/var/lib/coding-agent \
  coding-agent:dev agent --help
```

Everything a package manager writes — uv (and, in a deployment image, Composer and pnpm) caches,
mirrors, workspaces, evidence, logs — is redirected onto that volume, so the read-only root
filesystem is not a special mode: it is how the image is meant to run.

### Talk to GitHub from the container

Pass the credential in from the deployment environment, never bake it into the image:

```sh
docker run --rm --read-only --tmpfs /tmp \
  -v coding-agent-state:/var/lib/coding-agent \
  -e GITHUB_TOKEN \
  coding-agent:dev agent startup-check --repo <owner>/<sandbox-repo>
```

### Validate a Target Project inside the image

Validating needs a deployment image: the bare base carries no Target Project toolchain and no
Toolchain Matrix. Mount the checkout under the volume's workspace area — writable, because
`bootstrap` installs into it — and give the evidence its own directory:

```sh
docker run --rm --read-only --tmpfs /tmp \
  -v coding-agent-state:/var/lib/coding-agent \
  -v "$PWD/../my-project:/var/lib/coding-agent/workspaces/my-project" \
  coding-agent:deployment \
  agent validate \
    --project-dir /var/lib/coding-agent/workspaces/my-project \
    --evidence-dir /var/lib/coding-agent/artifacts/my-project
```

If the project should stay untouched on the host, mount it `:ro` and copy it onto the volume first,
the way [`scripts/l2-verify/python.sh`](./scripts/l2-verify/python.sh) does.

### Verify a build

Two scripts exercise the images the way a real bootstrap would, rather than reading the Dockerfile.
Both build the images themselves and clean up the volumes they create:

```sh
./scripts/verify-image.sh          # base CLIs and refusals, deployment matrix, read-only rootfs, Skill Bundle layers
./scripts/verify-l2-fixtures.sh    # bootstraps and validates the Python, PHP and TypeScript fixtures
```

`verify-image.sh` checks the base image (uid/HOME, `agent`/`agentctl`/`agent-installer` under a
read-only rootfs, `uv` refusing interpreter downloads, no PHP/Composer/pnpm/Skill Bundle/Matrix,
CLI help as the default command, and `agent run` refusing at startup naming the missing Matrix)
and the reference deployment image (Matrix contents, real pnpm/Composer/uv bootstraps, Skill
Bundle verification and the exact exposed skill set). It also induces a failure on purpose — it
drops a skill from the derived build and asserts that the build fails naming the missing member.
`verify-l2-fixtures.sh` runs the fixtures against the reference deployment image. Both honour
`IMAGE_TAG` for the deployment image (default `coding-agent:verify`); `verify-image.sh` honours
`BASE_IMAGE_TAG` for the base image (default `coding-agent:base`).

## What a Target Project must declare

`agent validate` reads the Target Project's **Project Profile** at
`docs/agents/project-profile.yml`. It declares how the project bootstraps, tests and checks itself,
how those commands report individual results, and what runtime versions and services they need:

```yaml
schema: 2
language: python
working_directory: "."
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: uv sync --frozen
  test_all: uv run pytest -q --junit-xml={evidence_dir}/test_all.xml
  test_targeted: uv run pytest -q --junit-xml={evidence_dir}/test_targeted.xml {path}
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
```

`checks: none` is a declaration that the project has no checks, and satisfies the requirement —
it is not the same as leaving `checks` out. A missing `evidence` block is a missing Readiness Fact,
and an unknown `schema` or a toolchain outside the Supported Toolchain Matrix makes the environment
unsupported: in each case `agent validate` refuses rather than guessing.

Working profiles for all three languages live under [`fixtures/l2/`](./fixtures/l2) — Python with
uv and pytest, PHP with Composer, PHPUnit, a non-root `working_directory` and a declared service,
TypeScript with pnpm, vitest and a `tsc` check.

## Reading further

- [`CONTEXT.md`](./CONTEXT.md) — the glossary the whole effort speaks.
- [`docs/ops/workflow-configuration.md`](./docs/ops/workflow-configuration.md) — the operator guide to writing a Workflow Configuration.
- [`docs/contract/step-workflow-runtime-contract.md`](./docs/contract/step-workflow-runtime-contract.md) — what the Worker must do.
- [`docs/plan/step-workflow-implementation-plan.md`](./docs/plan/step-workflow-implementation-plan.md) and [the step-workflow acceptance matrix](./docs/plan/step-workflow-acceptance-matrix.md) — the build order and what proves each slice.
- [`docs/runbooks/`](./docs/runbooks) — the agent identity credential and recovering from a lost Run Ledger.
- [`docs/adr/`](./docs/adr) — the decisions, and why they went the way they did.
- Frozen design records: [the MVP runtime contract](./docs/contract/mvp-runtime-contract.md), [plan](./docs/plan/mvp-implementation-plan.md) and [acceptance matrix](./docs/plan/mvp-acceptance-matrix.md), which describe `v0.2.0` and earlier; [the v1 runtime contract](./docs/contract/v1-runtime-contract.md), [plan](./docs/plan/v1-implementation-plan.md) and [acceptance matrix](./docs/plan/v1-acceptance-matrix.md).
