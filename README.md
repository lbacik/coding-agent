# Coding Agent

A containerised agent that takes an issue from one configured GitHub repository, implements it
with the upstream `/implement` skill, and hands a pull request back to a human.

The vocabulary this README uses — **Worker**, **Target Repository**, **Project Profile**,
**Skill Bundle**, **Validation Contract** — is fixed in [`CONTEXT.md`](./CONTEXT.md); the
behaviour it will have is fixed in [the v1 runtime contract](./docs/contract/v1-runtime-contract.md).

## What exists today

The project is being built in the slices [the implementation plan](./docs/plan/v1-implementation-plan.md)
lays out. Slices S0–S3 are in place, which is what the `agent` command below can do:

| Command | Slice | What it does |
| --- | --- | --- |
| `agent preflight` | S0b | Performs every write probe the contract's identity section requires, against a **sandbox** repository. |
| `agent startup-check` | S0b | The non-mutating startup checks: identity, token expiration, push permission, rate-limit headroom. |
| `agent verify-skill-bundle` | S1b | The three verification layers over a build-time `agent-installer` run. Runs during the image build; re-runnable against a built image. |
| `agent validate` | S2c | Runs a Project Profile's Validation Contract for real against a checkout: bootstrap, `test_all` and every check, or one `test_targeted` file. |
| `agent provider-check` | S3.3 | The Provider Capability Assertion, standalone: one live call proving a Pinned Model can be used at all. No GitHub access. |
| `agent implement` | S3 | Takes one Target Issue through the model's bounded tool loop and ends with a Delivery Snapshot: mirror, workspace, Base Revision, Seam Set, Pinned Prefix, tool loop, commit, push, then S2's Validation Contract run against it. |

`agent implement` does not yet open a pull request, run a review, or apply labels — that is a
later slice (S4 onward).

## Requirements

- **CLI use**: Python 3.13 and [uv](https://docs.astral.sh/uv/). A GitHub fine-grained token for
  the two commands that talk to GitHub.
- **Container use**: Docker. The image carries the Supported Toolchain Matrix — Python 3.13/uv,
  PHP 8.4/Composer, Node 22/pnpm — so a Target Project's own toolchain does not need to be
  installed on the host.

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
should supplement it (for example a database URL used by `implement`), pass an application-level
file before the command:

```sh
uv run agent --app-env-file ./tmp/jh-api-agent.env implement ...
```

Variables already in the process or `.env` keep precedence. This is intentionally separate from
uv's own `--env-file` option, which some IDE launchers do not forward.

### `agent startup-check` — read-only

```sh
GITHUB_TOKEN=github_pat_... uv run agent startup-check --repo <owner>/<sandbox-repo>
```

Prints the Agent Identity's account id and login, the token's expiration, whether `permissions.push`
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
is what the image exists for — see the container section below.

### `agent provider-check` — no GitHub access

```sh
uv run agent provider-check --provider anthropic
uv run agent provider-check --provider openai
```

Makes one live call to the named Pinned Model and asserts the Provider Capability: tool calling
works, the pinned effort level is honoured, usage is reported non-zero, and a Price Table entry
exists for it (ADR 0009). A refusal exits non-zero with nothing opened; the same assertion also
runs as the first step of `agent implement`.

### `agent implement` — writes, so sandbox only

```sh
GITHUB_TOKEN=github_pat_... uv run agent implement \
  --repo <owner>/<sandbox-repo> \
  --issue 42 \
  --target-language python \
  --provider anthropic
```

Asserts the Provider Capability, fetches the Target Issue, mirrors and checks out the Target
Repository at its Base Revision, computes the Fingerprint, confirms the Seam Set, composes the
Pinned Prefix (the `implement` and `tdd` skills, `tdd`'s two Companion Files, and the Attempt
Header), reads the Project Profile, then runs the model's bounded tool loop — the model edits,
tests, commits and pushes a Delivery Snapshot, which S2's Validation Contract harness then runs
against. **Never run this against the real Target Repository**: it commits and pushes for real,
and no pull request exists yet to gate it (a later slice).

Other flags: `--state-dir` (default `/var/lib/coding-agent`) for the mirror, workspace and
evidence root; `--skills-home` (default the current user's home) for where the Skill Bundle is
installed; `--attempt` (default `1`) for this Attempt's number; `--token-env` as in the two
commands above.

The Attempt ends in exactly one terminal category, printed on its own line and echoed to a non-zero
exit except the first: `verified completion` (0 — a pushed Delivery Snapshot has clean Validation
Evidence), `implemented but unverified` (a pushed Delivery Snapshot exists without successful validation),
`no change produced` (no workspace diff exists; audit evidence is retained), or `saved partial work`
(a candidate diff exists but the Attempt did not reach verified completion). A terminal category never
uses `PASS` wording unless it is `verified completion`; the lower-level stage and validation lines
remain diagnostic evidence.

When running the command outside the image, generate the Supported Toolchain Matrix from the
toolchains installed on the host first:

```sh
uv run python scripts/generate-toolchain-matrix.py
GITHUB_TOKEN=github_pat_... uv run agent implement \
  --repo <owner>/<sandbox-repo> \
  --issue 42 \
  --target-language python \
  --toolchain-matrix ./tmp/toolchain-matrix.json
```

The CLI also discovers `./tmp/toolchain-matrix.json` by default locally. Set
`CODING_AGENT_TOOLCHAIN_MATRIX` or pass `--toolchain-matrix PATH` to use another file. The Docker
image keeps using its immutable build output at `/opt/coding-agent/toolchain-matrix.json`; a missing
or malformed local file is reported before any Attempt state or model call is created.

### Developing on the agent itself

```sh
uv run pytest        # 121 tests, no network, no Docker
uv run mypy src tests
```

## Using the agent in Docker

### Build the image

```sh
docker build -t coding-agent:dev .
```

The build pins every toolchain to one version, installs the Skill Bundle from a pinned upstream
commit, and then runs `agent verify-skill-bundle` — so a bundle that silently shrank, drifted off
its pin, or lost a companion file **fails the build**, naming what was wrong. The build publishes
the Supported Toolchain Matrix it actually produced at
`/opt/coding-agent/toolchain-matrix.json`.

### The short way: compose

[`compose.yaml`](./compose.yaml) encodes the run flags below — the read-only root filesystem, the
writable `/tmp`, the state volume, the credential names — so a command is just its own arguments:

```sh
docker compose run --rm agent startup-check --repo <owner>/<sandbox-repo>
docker compose run --rm agent --help
```

Compose builds the image on first use and reuses it afterwards; `docker compose build` rebuilds it
when the Dockerfile or the sources it copies have moved.

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

Everything a package manager writes — uv, Composer and pnpm caches, mirrors, workspaces, evidence,
logs — is redirected onto that volume, so the read-only root filesystem is not a special mode: it
is how the image is meant to run.

### Talk to GitHub from the container

Pass the credential in from the deployment environment, never bake it into the image:

```sh
docker run --rm --read-only --tmpfs /tmp \
  -v coding-agent-state:/var/lib/coding-agent \
  -e GITHUB_TOKEN \
  coding-agent:dev agent startup-check --repo <owner>/<sandbox-repo>
```

### Validate a Target Project inside the image

Mount the checkout under the volume's workspace area — writable, because `bootstrap` installs into
it — and give the evidence its own directory:

```sh
docker run --rm --read-only --tmpfs /tmp \
  -v coding-agent-state:/var/lib/coding-agent \
  -v "$PWD/../my-project:/var/lib/coding-agent/workspaces/my-project" \
  coding-agent:dev \
  agent validate \
    --project-dir /var/lib/coding-agent/workspaces/my-project \
    --evidence-dir /var/lib/coding-agent/artifacts/my-project
```

If the project should stay untouched on the host, mount it `:ro` and copy it onto the volume first,
the way [`scripts/l2-verify/python.sh`](./scripts/l2-verify/python.sh) does.

### Verify a build

Two scripts exercise the image the way a real bootstrap would, rather than reading the Dockerfile.
Both build the image themselves and clean up the volumes they create:

```sh
./scripts/verify-image.sh          # uid, toolchain matrix, read-only rootfs, Skill Bundle layers
./scripts/verify-l2-fixtures.sh    # bootstraps and validates the Python, PHP and TypeScript fixtures
```

`verify-image.sh` also induces a failure on purpose — it drops a skill from the bundle and asserts
that the build fails naming the missing member. Both honour `IMAGE_TAG` (default
`coding-agent:verify`).

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
- [`docs/contract/v1-runtime-contract.md`](./docs/contract/v1-runtime-contract.md) — what the Worker must do.
- [`docs/plan/v1-implementation-plan.md`](./docs/plan/v1-implementation-plan.md) and [the acceptance matrix](./docs/plan/v1-acceptance-matrix.md) — the build order and what proves each slice.
- [`docs/adr/`](./docs/adr) — the decisions, and why they went the way they did.
