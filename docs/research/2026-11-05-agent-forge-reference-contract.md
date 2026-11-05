# Agent Forge's reference agent contract

Research for [What operator contract does Agent Forge consume from simple-coding-agent?](https://github.com/lbacik/coding-agent/issues/188), under [Design: a shared Agent Forge operator contract for interchangeable coding agents](https://github.com/lbacik/coding-agent/issues/187).

This note records facts and integration options. It does not choose the future architecture, promise feature parity, or authorize implementation or migration.

## Sources and scope

Inspected on 2026-11-05, with clean source working trees:

| Repository | Inspected revision | Role |
| --- | --- | --- |
| `lbacik/agent-forge` | `04d6effde16bd8fff1978bbf362100968f0e705a` | Current consumer: deployment templates, operational skills, shell helpers |
| `lbacik/simple-coding-agent` | `3fe74702d802a7fbbc4f8a96265a678be27be995` | Reference implementation and published CLI/telemetry contracts; package version 0.8.1 |

All source links below are immutable commit links. No live agent instances, secrets, target repositories, or persisted agent volumes were inspected. This is a source review, not a deployment or concurrency test. Coding-agent's implementation gap is investigated separately; this note establishes the reference side.

## Three different compatibility surfaces

1. **Automatic Forge dependencies:** shell parsing, file paths, container users, Compose service names and volume layout. These fail mechanically if changed.
2. **Operator instructions:** skills describe how a human or coding assistant reads evidence and submits control actions. These can change alongside the backend but remain real workflow requirements.
3. **Published dashboard contracts:** simple-coding-agent explicitly publishes telemetry schema v1, discovery, command replies and synthetic examples. Forge's checked-in scripts do not currently call telemetry, but dismissing that interface would omit an existing published consumer contract.

Forge is currently a scaffolding and operational-skills repository, not a long-running control plane with its own HTTP API. Its [root instructions](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/AGENTS.md#L1-L98) define per-instance Compose projects and changes in shared skills. The distinction above prevents a source-package import or an internal SQLite table from becoming a permanent backend requirement accidentally.

## Deployment and configuration

Forge's [Compose template](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/assets/compose.yaml.template#L24-L62) builds from a named Git context pointing at simple-coding-agent and `AGENT_SRC_REF`, sets image `simple-coding-agent:<short-name>`, loads `${AGENT_ENV_FILE:-.env}`, mounts `agent_data:/data`, uses `restart: unless-stopped`, enables Docker init, and disables core dumps. This is concrete deployment coupling, not merely selecting a different Python entrypoint.

The [Dockerfile template](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/assets/Dockerfile.template#L64-L111) copies `pyproject.toml`, `simple_coding_agent/`, managed Claude settings, `scripts/install-skills.sh`, and `skills/`; installs the package; and builds the skill bundle. Its [final section](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/assets/Dockerfile.template#L128-L160) bakes in the profile, creates `/data` and `/run/simple-coding-agent` owned by UID 1000 user `agent`, sets `HOME` and `DATA_DIR`, and starts `python -m simple_coding_agent`. A replacement requires backend-specific build instructions or a generalized image contract; copying these private source paths is not intrinsically necessary for management compatibility.

The [bundled-services shape](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/references/bundled-services.md#L8-L27) starts services as root, waits for readiness, then `exec gosu agent python -m simple_coding_agent`. The worker process must receive forwarded signals and run with the same user as diagnostics. The exact module name, Claude pricing file and SDK installation are backend-specific; init, permissions, service readiness and durable-volume ownership are operational requirements.

Forge generates an operator-owned `simple-coding-agent-profile.yml` from the Target Repository's CI. The [reference loader](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/config.py#L258-L302) accepts only `setup`, `check`, `base_branch`, `timeout`, `setup_timeout`, `env`, `model_instructions`; unknown keys fail. `PROFILE_PATH` overrides its default in-repository location. Forge [validates profiles by importing that Python loader](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/SKILL.md#L352-L368), another internal implementation dependency to replace or adapt explicitly.

Profile commands run sequentially through `sh -c` with a shared deadline; the [command runner](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/command_runner.py#L50-L134) builds only `PATH` plus declared profile environment and strips known credential keys. Forge separately aligns the model shell's container environment with service configuration. A future common deployment contract needs to describe equivalent bootstrap/validation behavior without assuming both agents accept the same YAML or validation-evidence model.

## Discovery, pools and claims

The published dashboard discovery label is `io.github.lbacik.simple-coding-agent.role=agent`, set on the image, including [Forge's custom-built images](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/assets/Dockerfile.template#L17-L22). It is [documented as dashboard discovery](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/agents/telemetry-snapshot.md#L156-L166). Forge's helpers instead use standard Compose project/service labels.

The [resolver](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/lib/resolve.sh#L116-L162) assumes first service `agent` with volume `<project>_agent_data`; later services `agent-N` with `<project>_data_N`. It supports local OrbStack host paths or remote Docker contexts. When the worker is stopped it can start the same image with `--entrypoint sh -u agent`, mounting `/data` read-only. Thus remote/offline inspection currently needs a compatible OS user, shell and utilities, not just the worker executable. Pool enumeration also [parses service names directly from compose.yaml](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/pool-status.sh#L78-L103).

The [pool helper](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/pool-status.sh#L139-L154) executes `echo "WORKER_ID=$WORKER_ID"; agentctl status` as `agent`, then parses text. With `--github`, it reads only `TARGET_REPO` from `.env` and compares active issues against `claimed-by:<worker-id>` labels and assignees. [Missing labels and orphaned claims](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/pool-status.sh#L186-L289) are interpreted using this ownership convention. Simple-coding-agent's [configuration](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/config.py#L450-L467) restricts `WORKER_ID` to `[a-z0-9-]`, maximum 32 characters.

**Implication:** a backend with only a local lock and a GitHub assignee is not automatically compatible with same-login pools. Forge could report legitimate claims as missing labels, and multiple workers need an actual shared-queue ownership policy. Adding a status field or cosmetic label does not establish mutual exclusion. Pool identity and claim coordination must be specified separately from telemetry identity; schema-v1 `identity` names the repository and process provenance, not a general fleet worker identifier.

## Control CLI and durable command semantics

### Existing text consumers

The [CLI formatter](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/agentctl.py#L342-L393) emits `intake:`, `active_attempt:`, and `pending_next_issue:`. Forge's pool helper recognizes a reply only if `^intake: ` appears, extracts the exact prefixes, and identifies an active issue only from `active_attempt: issue #N...`. It masks the command exit code with `|| true`; malformed/missing status is printed inline without aborting the pool. This text format is an automatic API today. `status --json` is not implemented; machine readers have the telemetry `control` section instead.

### Commands and effects

The [parser](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/agentctl.py#L483-L637) exposes:

| Operation | Reference behavior |
| --- | --- |
| `stop` | Stop **intake**, finish the active attempt. Completes immediately when idle/already stopped; otherwise accepted until durable finalization. Process and socket stay available. |
| `stop --after N` | Count fully finalized attempts, including the currently active one first. Every terminal outcome counts once; a held finalization does not. New plans replace old plans. |
| `resume` | Permit later intake and replace a pending stop plan; does not requeue an issue or resume interrupted model execution. A recovery hold can reject it. If a handoff already owns the model, resume is deferred until handoff finalization. |
| `next issue N` | One-shot priority for the next permitted claim; checks eligibility before acceptance, never grants authorization or resumes intake. New priority supersedes the previous priority, not a stop plan. |
| `handoff now` | Cooperative bounded handoff at a safe model boundary, followed by publication, issue release and cleanup. Completes only when those effects durably finish; otherwise `not fulfilled` and recovery retains work. Idle/held instances reject it. |
| `recovery retry <attempt>` | Recheck retained evidence and retry unconfirmed safe finalization, never rerun the model or renew a handoff deadline. |
| `recovery release <attempt> --saved-at <ref>` | Record where work was secured, confirm a failure comment, then release/clean up/finalize. Failure retains the hold; no automatic requeue. |
| `errors reset` / `consecutive-errors reset` | Reset the durable infrastructure-error guard through the live process. |
| `command <id>` | Read the stored acknowledgement; unknown ID is not proof that a still-running concurrent submission will never commit. |

Sources: [control-store transitions](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L237-L242), [stop-after](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L486-L503), [resume including deferred handoff](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L365-L415), [lifecycle eligibility and handoff](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L1486-L1613), [recovery](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L1617-L1685), [finalization accounting](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L1404-L1414), and [CLI help](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/agentctl.py#L619-L635).

By contrast, SIGTERM/SIGINT are **process interruption**. The [interruption handler](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L175-L237) best-effort emits `attempt_interrupted` and re-signals with normal signal semantics. Docker stop is not the CLI's drain operation and does not promise a successful handoff.

### Acknowledgements, retries, errors

The [published command-reply contract](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/agents/agentctl-command-replies.md) defines successful stdout as five single-line `key: value` entries: `repository`, `request_id`, `sequence`, `acknowledgement`, `effect`. Key order is not contractual; readers ignore additional keys. Acknowledgements are `accepted`, `completed`, `superseded`, `not fulfilled`; exit 0 means committed, **not necessarily completed or fulfilled**. Replies are text, with no version field and no JSON mode.

Exit 1 means rejection/no accepted change or unknown lookup ID. Exit 2 covers ambiguous transport loss **and** argparse usage errors (`usage:` prefix). Mutating transport errors preserve the generated request ID and advise retrying the identical command with that ID. [CLI implementation](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/agentctl.py#L264-L339) flattens embedded newlines and prints a repository line on answered rejections. The phrase `not submitted` must not be interpreted as proof of non-commit: the implementation explicitly treats connection loss as ambiguous.

Request IDs are durably stored; the same ID and canonical payload return the current stored record across restart, without a second effect. A different kind/payload under the same ID rejects ([comparison](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L1947-L1966), [restart/idempotency tests](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/tests/test_control.py#L71-L104)). Command `sequence` orders durable commands and is distinct from process-local telemetry sequence.

`--expect-repo` protects `stop`, `stop --after`, `resume`, `handoff now`, and `next issue`: trim/case-insensitive comparison occurs **before idempotency lookup**, is not part of canonical payload, and a mismatch stores nothing. Recovery and error-reset operations do not support that guard ([server](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control_server.py#L622-L700)). [Synthetic command replies and their conformance tests](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/tests/test_agentctl_command_replies_examples.py#L1-L39) are reusable primary compatibility fixtures.

### Transport and responsiveness

Production transport is private AF_UNIX `/run/simple-coding-agent/control.sock`, mode 0600 under a 0700 runtime directory; CLI `--socket`/`AGENTCTL_SOCKET` are development overrides. One JSON object plus newline travels each way, with a 1 MiB reader bound. Clients normally use a 10-second socket timeout ([server startup](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control_server.py#L69-L145), [client](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control_server.py#L703-L777)). Forge and the published dashboard execute the CLI, so socket bytes and SQLite tables need not be shared implementation internals if CLI behavior is preserved.

Actual server code starts a daemon thread **per connection**. Mutating operations serialize with lifecycle boundaries through the control lock; telemetry and command lookup can respond while another command waits. A lookup can temporarily return no row for a submission that commits later ([server](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control_server.py#L203-L248), [responsiveness test](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/tests/test_control_responsive.py#L47-L135)). A future facade needs equivalent externally observable responsiveness, not necessarily the same threading design.

## Logs and durable artifacts

The reference [event logger and archive writer](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/observability.py#L17-L144) redact configured credential strings at write time; emit stdout JSONL fields `timestamp`, `level`, `event`, `issue_number`, `phase`, `detail`; mirror issue-associated events into `agent_output.json`; and store artifacts in `/data/logs/<issue>/<started_at-with-colons-replaced>/`. Redaction is value replacement, not a guarantee of arbitrary secret detection.

The [operator log reference](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/references/log-layout.md#L36-L200) expects:

- `state/consecutive_errors.json`; an active-only `state/attempt.json`; private `state/control.sqlite3`; working clone `repo/`.
- Archived `attempt.json` with timing, outcome, model usage and execution evidence.
- `setup_*`, `baseline_check_*`, `check_*` stdout/stderr logs.
- Numbered tool input/result JSON and assistant text files. Their counter is shared; concurrent calls/results need not be adjacent. Tool names are in event `detail`, not the input JSON. Skill-call evidence is inline.
- Lifecycle outcomes `complete`, `incomplete`, `handoff`, `no_changes`, `infrastructure_error`, distinct from model execution status and conflict-attempt result categories.

Forge's [locate helper](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/locate-agent.sh#L104-L135) actually enumerates these paths, prints `state/*.json`, and extracts `outcome`/`attempt_kind`. Its [fetch helper](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/fetch-run.sh#L45-L72) sorts directory names for latest, copies via Docker/tar and reuses a cached directory forever when `attempt.json` contains the text `"outcome"`. **Do not emit `outcome: null` into an active archive while keeping this helper unchanged.**

However, Forge's documented immutable-archive assumption is stronger than upstream's completion ordering. Upstream [writes outcome before PR backfill, completion-ledger/control accounting and checkpoint deletion](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L5100-L5154); its [archive writer merges later metadata into attempt.json](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/observability.py#L107-L118). Thus presence of `outcome` is an existing cache heuristic, not a transactional sealed-archive guarantee. Inference: a fetch in that interval can cache an incomplete view. A common contract should explicitly define archive sealing/export generation or accept revalidation; it should not canonize the stronger assumption as already proven.

Startup/polling events without issue identity exist only in Docker stdout; [Forge instructions](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/SKILL.md#L183-L225) rely on them for startup failures. The disk archive is not a complete process audit trail. Current upstream additionally [copies redacted CLI transcripts](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/observability.py#L147-L175), so the Forge file inventory is not exhaustive.

## Telemetry: implemented public schema, not OTEL

The published [schema-v1 contract](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/agents/telemetry-snapshot.md) specifies `agentctl telemetry --json`, called without TTY as `/usr/local/bin/agentctl` via `docker exec --user agent`. It publishes no HTTP port, requires no extra token/configuration and is already wired into the [production entrypoint](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/__main__.py#L117-L185). There is no OTEL/Prometheus dependency in this integration. This conclusion concerns inspected source, not an inventory of external infrastructure.

Snapshot sections are `schema_version`, `generated_at`, process-local `sequence`, `identity` (repository, agent/SDK/CLI versions, process start), `control`, `current_attempt`, `last_finished`, `recent_events`, `truncated`. Control has intake/recovery state, current issue and phase, pending command/priority, stop plan, handoff/deadlines, error guard and recent commands. It shares the status shape but deliberately uses cached lifecycle observations instead of status's live network/git checks ([builders](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L3601-L3689)).

The attempt view reports token budget measured/estimated split, ceilings and soft thresholds, measured token categories, turns and limits, continuation index/limit, cache hit rate/count, main/subagent context and compactions, elapsed/timeout. Provider rate limits are unavailable in v1; USD cost is intentionally excluded. Figure objects carry `{value, basis}` with `measured`, `estimated`, `configured`, `sdk_assumed`, `unavailable`; unavailable is null, never zero. Missing means unknown; a null attempt means no active attempt, not unknown. A different backend must report unavailable for figures it cannot establish, rather than pretend SDK-specific estimates are measurements.

Freshness needs several clocks: `generated_at` means the endpoint answered; `control_observed_at` dates the control section; `current_attempt.observed_at` dates the last executor publication. After model execution figures freeze until finalization. A busy control lock reuses the last section after at most a one-second lock wait; if none exists it fails. `last_finished` is seeded from archived summaries on startup; recent events reset with the process ([publication/snapshot code](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/telemetry.py#L662-L708), [live views](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/telemetry.py#L833-L905), [control cache](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L3657-L3689)).

Recent events are up to 50, newest last; `text` is limited to 300 characters and noisy `limits_checked`/`tool_call` are excluded. Snapshots trim oldest events then subagents to target 256 KiB. If non-trimmable content still exceeds that limit, the CLI rejects it; trimming alone is not a universal cap proof ([buffer](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/telemetry.py#L118-L167), [trimming](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/telemetry.py#L1180-L1210), [CLI validation](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/agentctl.py#L181-L238)).

Telemetry uses a three-second socket timeout; the dashboard contract sets a five-second caller budget and at most one in-flight call per instance. On error stdout is JSON `{ok:false,error:{code,message}}`: `agent_unreachable` exits 2; `unsupported_operation` or `snapshot_failed` exits 1. An old CLI rejecting `telemetry` has usage stderr, exit 2, empty stdout; the dashboard must distinguish this from a JSON error. The [CLI handling](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/agentctl.py#L181-L238) distinguishes malformed/oversized replies from reachability. The socket's `settimeout` applies to operations; source review does not establish a strict end-to-end wall-clock deadline across process startup/connect/read.

Schema version is an integer major: unknown fields and additive enum values do not bump it; removing/renaming/reinterpreting fields does. Unknown figure basis renders as estimated. A newer unsupported major may use only stable fields `schema_version`, `generated_at`, `identity.repository`, `identity.agent_version`. The published synthetic snapshots and error examples provide compatibility fixtures, but are not proof that all source behavior still matches the prose.

## Documentation/code discrepancies to carry forward

| Claim or assumption | Observed implementation | Consequence |
| --- | --- | --- |
| Telemetry documentation's scope note says serving is still a separate future change. | Parser, entrypoint and server already implement it. | Treat current code as implemented, not a proposed future endpoint. |
| `sequence` documented as incrementing per published telemetry update. | `TelemetryReporter.snapshot()` increments for each successfully assembled reply; [test explicitly expects per-snapshot increment](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/tests/test_telemetry.py#L214-L223). | An increasing sequence does not prove model progress; use observation timestamps. |
| [ADR 0006](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/adr/0006-telemetry-through-agentctl-not-http.md) says connections are answered one at a time. | Per-connection daemon threads keep telemetry/lookup responsive during blocked submissions. | Preserve tested behavior, not the stale concurrency rationale. |
| Telemetry control is described as the same status builder. | Same payload family, different observation/I/O policy and potentially cached values. | Do not equate their freshness or force network checks into telemetry. |
| Forge's `outcome` means an immutable cached archive. | Metadata writes and finalization can follow outcome persistence. | Define explicit sealing or refresh behavior. |
| [Forge provenance docs](https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/references/log-layout.md#L337-L352) mention an environment SDK expectation. | Current config pins SDK expectation from installed package metadata and [ignores the old environment override](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/config.py#L45-L65), [warning](https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/config.py#L320-L325). | Avoid carrying outdated Claude deployment checks into a neutral contract. |

## Options and sharp decisions for the map

The source evidence supports three feasible directions, without selecting one:

- **Legacy facade:** make coding-agent emit compatible `agentctl`, telemetry and exported artifacts; keep Forge mostly unchanged. This inherits text parsing, branded discovery, current pool assumptions and some backend-specific concepts. Private state need not match if exported views do.
- **Backend adapters in Forge:** select image/build/profile/diagnostic adapters and normalize a small operational model. This makes capability differences explicit but needs tests to prevent Forge becoming an expert in both workers' internals.
- **Versioned common contract with optional implementation library:** extract schemas, fixture-based conformance and public CLI behavior; optionally share transport, redaction, artifact export and telemetry helpers. Share lifecycle implementation only if its semantics are actually the same. Command idempotency cannot be supplied by a CLI wrapper alone: it must participate in each worker's durable claim/finalization boundaries.

The next decisions can now be stated precisely:

1. Is compatibility required with today's manual diagnostics only, with automatic Forge helpers, and/or with the published fleet dashboard contracts?
2. Which control actions are mandatory versus advertised capabilities, especially cooperative handoff, recovery, stop-after counting and next-issue priority?
3. Which lifecycle and outcome semantics can be normalized without misrepresenting either backend's retained work, publication or authorization rules?
4. Should artifact compatibility be stable paths/files, a list/export API, or both; what establishes finality and cache validity?
5. How are backend, worker, process and attempt identities exposed; are same-login pools and mixed-backend claims supported?
6. Should image construction and profile validation be backend adapters or an independent common specification?
7. Does a shared package initially own only data schemas and conformance fixtures, or transport/CLI/persistence helpers too?

These questions are architecture decisions for the owner. The existing facts do not require HTTP, a shared SQLite schema, a shared execution framework, or identical source-package layout.

## Verification and limitations

- Recorded clean source revisions and traced behavioral claims to pinned source, tests or explicitly identified operator documentation.
- Cross-checked published CLI/telemetry prose against parser, server, reporter, lifecycle and fixture tests; discrepancies are recorded above.
- Checked every pinned source URL's local repository/path and line bounds, and ran `git diff --check` on this research-only change.
- No runtime tests, image builds, live CLI calls, concurrency benchmarks or dashboard inspection were performed. Tests cited above were read, not executed. No migration of existing state is specified here; a backend compatibility claim still needs conformance and deployment validation.
