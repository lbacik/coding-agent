# coding-agent as an Agent Forge backend: capabilities and semantic gaps

Research for [Which Agent Forge operations can coding-agent support without changing its execution guarantees?](https://github.com/lbacik/coding-agent/issues/189), under [Design: a shared Agent Forge operator contract for interchangeable coding agents](https://github.com/lbacik/coding-agent/issues/187). This note establishes source facts and architectural options. Recommendations below are proposals for owner decisions, not accepted changes to the runtime contract.

## Evidence baseline and scope

Inspected on 2026-11-05, with immutable source links throughout:

| Repository | Revision | Role |
| --- | --- | --- |
| coding-agent | `f68517d884f77186b4c51a30d38ec5ad382392df` | Current implementation and accepted guarantees |
| simple-coding-agent | `3fe74702d802a7fbbc4f8a96265a678be27be995` | Current reference operational interface; package version 0.8.1 |
| agent-forge | `04d6effde16bd8fff1978bbf362100968f0e705a` | Current consumer and scaffolding |

This was static source, contract, and test inspection. No live containers, credentials, provider calls, runtime tests, or production data were used. The analysis distinguishes implemented behavior from contractual intent and from proposed new behavior. The current MVP contract is authoritative; frozen v1 material is not an implementation promise. [Current contract][C-contract]

## Finding

coding-agent already has substantial internal evidence suitable for observation: durable Attempts, pinned configuration, publication receipts, cumulative usage and costs, validation evidence, redacted model transcripts, and Continuation Snapshot artifacts. It does **not** currently expose the reference `agentctl` control/telemetry surface or its archive layout. Compatibility requires a backend integration, new operational control state, and Forge changes; replacing a source URL or renaming files is insufficient. [CLI][C-cli] [Ledger][C-ledger] [Worker wiring][C-wiring] [Forge image template][F-image]

The useful common boundary is operational: discover an instance, identify its backend and capabilities, observe it, submit idempotent operator requests, and retrieve attempt evidence. The fixed execution skeleton and recovery verdicts should remain owned by each backend. In particular, a shared interface must not imply that effective tokens equal cache-inclusive tokens, that intake drain equals SIGTERM, that an internal context handoff equals operator handoff, or that a recorded outcome means publication and archive completion. These distinctions follow directly from the sources compared below. [Meters][C-meters] [Shutdown implementation][C-shutdown] [Reference control][S-control] [Outcome recording][C-outcome]

## 1. Launch, configuration, profiles, and deployment

### Existing capabilities

`agent run --config FILE --workflow ID [--state-dir PATH]` is the long-running Worker entry point. Other operator commands include offline `config validate`, non-mutating GitHub `startup-check`, provider capability checks, Skill Bundle verification, and standalone Project Profile validation. The installed executable is `agent`; no `agentctl` project script is declared. Default state root is `/var/lib/coding-agent`. [CLI][C-cli] [Package scripts][C-package]

The image runs as user `agent`, uses its own virtualenv on PATH, carries a build-time Supported Toolchain Matrix and verified Skill Bundle, and has a default Worker CMD rather than an ENTRYPOINT. Compose supplies `entrypoint: [agent]`, read-only root filesystem and tmpfs, a `worker` service, a read-only operator configuration mount, and a named state volume. The Worker uses `restart: "no"`, `init: true`, and a 60-second Docker grace period above the default 30-second runtime grace. Those choices are operational behavior, especially the deliberate absence of automatic restart following credential or infrastructure halts. [Image][C-image] [Compose][C-compose] [Startup and halting][C-startup]

Startup installs the SIGTERM handler, validates and resolves configuration, takes the exclusive volume lock, loads the toolchain matrix, verifies the Skill Bundle, checks GitHub identity and permissions, makes the provider capability assertion, then opens the Run Ledger. Recovery precedes selection in the poll loop. A failure exits nonzero without opening an Attempt or making a GitHub write; the provider assertion is nevertheless a real provider call. The first progress line follows configuration parsing, so Docker's running state alone does not establish readiness. [Supervisor][C-supervisor] [Capability assertion][C-capability]

### Incompatibilities to account for

Forge's Dockerfile template copies `simple_coding_agent/`, upstream private install scripts, skills, and Claude managed settings, and bakes an external profile at `/opt/agent-profile`. Those are backend-specific build inputs. Its runtime-status helper enumerates `agent`/`agent-N` Compose services and parses `agentctl status`; the current coding-agent Compose `agent` service is a one-shot command seat, while its long-running service is `worker`. Backend metadata or dedicated backend templates must identify the managed service explicitly. [Forge image template][F-image] [Forge pool status][F-pool] [Compose][C-compose]

A coding-agent Workflow Configuration is trusted operator-owned YAML, read at startup and pinned per Attempt; it selects repository, base branch, secret environment-variable names, model, labels, ordered Workflow Steps, and ceilings. YAML can configure only the implementation stage, not weaken the fixed selection, readiness, review, or publication skeleton. Repository-owned `model_instructions` cannot be silently imported as operator instructions. That would reopen ADR 0016. [Workflow schema][C-config] [ADR 0016][C-adr16]

The Project Profile is a separate repository-owned `schema: 2` document at `docs/agents/project-profile.yml`, read at the Base Revision. It requires runtime/package-manager declarations, bootstrap, full and targeted test commands, JUnit evidence, explicit checks, and service facts. The reference profile has `setup`, `check`, optional timeouts/env, and optional model instructions; Forge normally supplies it outside the Target Repository. A conversion cannot invent targeted-test semantics, individual test evidence, or Acceptance Criteria and the Seam Set. A runtime `agent validate --profile` convenience option does not mean `agent run` accepts that external profile: the Attempt baseline explicitly reads the repository-relative profile at the Base Revision. [Project Profile contract][C-profile] [Baseline reader][C-baseline] [Reference profile][S-profile] [Forge image template][F-image]

**Proposal:** generate backend-specific launch/configuration files, reuse the official coding-agent image/build approach, validate the actual Supported Toolchain Matrix, and require a suitable repository Project Profile. Decide explicitly whether external profile injection is desired; it changes the accepted source and pinning contract. Keep any operator prompt files outside the Target Repository. Do not copy simple-agent provider variables, Claude pricing settings, or restart policies by convention.

## 2. State ownership, identity, selection, and pools

The Run Ledger is one SQLite database (`run-ledger.sqlite3`) on the state volume, using WAL and synchronous FULL. Its tables hold Attempts, outcomes, effects, and events. The Worker holds `.worker.lock` for its entire lifetime; liveness for restart recovery is ownership of that volume lock, with no Lease or framework checkpointer. One Worker per **Target Repository and state volume** is the accepted MVP scope. Separate volumes solve filesystem contention, not multi-Worker claim coordination for one repository. [Storage contract][C-storage] [Volume locking][C-volume] [ADR 0017][C-adr17]

Attempts use native IDs `#<issue>/<n>`, with monotonically allocated local numbers, branches `agent/<issue>/<n>-<slug>`, pinned Fingerprint/Base Revision/configuration/identity, and lifecycle states `open`, `claim-confirmed`, `outcome-recorded`, `finished`, or `released`. Claim opens the ledger row before assigning the Agent Identity and reading the issue back. An existing assignment excludes selection; same-login shared-pool idempotent assignments have no per-Worker marker in this implementation. [Claim implementation][C-claim] [Claim contract][C-claim-contract]

The reference's optional `WORKER_ID`/`claimed-by:<id>` pool coordination and Forge's pool diagnostics are therefore not generic interfaces coding-agent already supports. Creating `agent-2` with another state volume and the same repository/login would exceed the current contract, and the per-repository Attempt numbering also needs a defined authority. A common observer should use a compound identity such as backend + managed instance + repository + native attempt ID rather than treating `#23/1` as globally unique. [MVP ownership][C-ownership] [Forge pool diagnostics][F-pool] [Reference glossary][S-context]

**Proposal:** initially advertise no same-repository pool capability for coding-agent. Preserve its native identifiers; a common opaque ID can wrap them. Keep recovery state authoritative in the Run Ledger; shared DTOs or projections must not create a second lifecycle authority. Shared command persistence is possible only with an explicit ownership/transaction design aligned with the Worker, not independent file edits by Forge.

## 3. Every reference control operation compared

None of these CLI operations is currently exposed by coding-agent. “Natural seam” means an implementation route exists; it does not mean the operation is available today. Reference command identity, acknowledgment, and retry requirements apply if compatibility is promised. [Current CLI][C-cli] [Reference reply contract][S-replies]

| Reference operation | coding-agent baseline and safe interpretation | Change or decision needed |
| --- | --- | --- |
| `status` | Ledger/runtime can identify unfinished Attempt, configuration, and last recorded outcome, but no live status endpoint exists. | Publish a consistent runtime view with explicit starting/ready/recovering/active/idle/draining/paused/halting states; human formatter can preserve Forge's parser fields. |
| `telemetry --json` | Usage/evidence exist internally; no socket operation or snapshot schema is exposed. | Add bounded read-only projection, capability/implementation identity, freshness, and unknown-value semantics. |
| `command <id>` | No durable operator-command record or lookup. | Add request identity/payload matching, durable acknowledgment lifecycle and query. Do not substitute ledger events for a defined command API. |
| `stop` | SIGTERM currently stops intake **and interrupts** the active Attempt before exiting. Reference stop drains the active Attempt and keeps process alive. | Add independent durable intake state checked atomically against Claim. Never alias to `request_shutdown()`. |
| `stop --after N` | No persisted stop countdown. Existing `max_attempts` is a test bound, not an operator command. | Define exactly-once counting of fully finalized Attempts and handling of released Claims/recovery; active Attempt counts as one under reference semantics. |
| `resume` | Shutdown flag is process-wide, terminal in production; reset function is a test seam. Halted Worker has exited. | Resume only durable paused intake. Decide how blocked recovery and process halts are represented; restarting a process is not the same command. |
| `next issue N` | Selection applies fixed guards/label filters and normal ordering. No one-shot priority. | Select among otherwise eligible issues; validate before acceptance and again at Claim. Must not apply Selection Label or bypass existing assignees/blockers/open delivery guards. |
| `handoff now` | Context-Window Handoff produces a Continuation Snapshot and continues the same Attempt in a fresh node. It is not reference operator handoff. | Optional unsupported capability is honest; adding a partial-work stop-and-publish action requires defined outcome, dirty-work preservation, deadlines, publication, intake, and later authorization semantics. |
| `recovery retry <attempt>` | Startup/poll recovery already finishes pinned outcomes without model calls; repeated failure can halt process. | Could request a safe retry through existing recovery logic, but needs endpoint lifetime, command acknowledgments and concurrency rules. Never recompute a recorded verdict. |
| `recovery release <attempt> --saved-at ...` | No counterpart to reference retained-work operator release. | Explicit new abandonment/effect-completion policy; cannot delete ledger state or bypass recorded terminal writes under a generic “release” name. |
| `errors reset` / alias | Consecutive infrastructure-failure counter is a local variable in Worker loop; third failure halts process. | Define whether counter becomes durable/visible and whether resetting a running guard is supported. It cannot reset a process that already exited. |

Evidence for the table: reference controls, countdown and handoff semantics in [ControlStore operations][S-control-ops], [countdown finalization][S-countdown], recovery in [reference lifecycle][S-recovery], and [error reset][S-reset]; coding-agent selection in [Claim][C-claim], test-only bounds and in-process counters in [Worker loop][C-loop], shutdown in [shutdown.py][C-shutdown], recovery authority in [ADR 0017][C-adr17], and context handoff in [current contract][C-handoff].

### Idempotence and wrong-instance protection

The reference contract promises request-ID replay with identical payload, rejection on payload mismatch, durable ordered sequence, and acknowledgment states `accepted`, `completed`, `superseded`, `not fulfilled`. Success exit 0 means committed, not necessarily completed. A timeout/connection loss can mean unknown outcome, so retry must retain the same ID and lookup must remain possible. Mutating replies are an extensible single-line `key: value` block, with no mutation `--json` mode. `--expect-repo` guards the five instance-targeting commands before idempotency lookup. A shared controller that only shells out and retries with a fresh UUID can duplicate effects. [Reference reply contract][S-replies] [Reference command store][S-control]

**Proposal:** share request/reply types, CLI rendering, validation, fixtures, and conformance cases first. Backend lifecycle adapters should decide effects. The reference socket wire format is explicitly outside the published CLI contract, so interoperability does not require freezing its current Python internals. Capability negotiation should distinguish unsupported operations from rejected valid operations and from uncertain transport outcomes. [Reference contract boundary][S-replies]

## 4. Publication, outcomes, and recovery

The current Publication Gate requires verified completion, unchanged Fingerprint, completed review, green final checks, and clean tree. Delivery Intent durably records the delivered verdict before external publication. After that point limits, cancellation, and SIGTERM cannot revoke publication. All terminal outcomes and rendered writes are recorded before their first terminal effect; recovery finishes those writes without model execution or reinterpretation. A command server must feed these boundaries, not open pull requests or modify labels independently. [Publication Gate][C-publication] [Outcome recording][C-outcome] [ADR 0017][C-adr17]

Native outcomes are `delivered`, `failed`, `failed-limit`, and `abandoned`, with detailed reasons. Implementation Outcomes are a separate judgment about evidence and working tree. `released` is a failed/unconfirmed Claim lifecycle state, not a Terminal Outcome. The reference's `complete`, `incomplete`, `infrastructure_error`, `no_changes`, and `handoff` cannot be reproduced by mechanically renaming native outcomes. [Native outcome table][C-outcomes] [Reference glossary][S-context]

**Proposal for observation only:** carry native outcome/reason and lifecycle state always; optionally add a common display category such as delivered, needs-human, cancelled, or error. Preserve distinctions including readiness failure, budget limit, interrupted, withdrawal, and branch-moved. A green model claim is not delivered; an `outcome-recorded` delivered Attempt can still have pending effects. Do not use a display classification to choose publication or recovery behavior.

Before Delivery Intent, a human cancels by closing the issue or removing its Selection Label; at a Gate this preserves edits through the cancellation path and ends abandoned. SIGTERM takes the interrupted recovery path, preserving committed work fast-forward and reporting uncommitted files it could not preserve. These are materially different preservation policies. Current implementation also documents a limitation: an HTTP provider call already blocked runs to provider timeout; runtime commands can be cancelled mid-flight as process groups. Although the contract says in-flight model cancellation, instantaneous cancellation is not established by the implementation. [Cancellation and SIGTERM contract][C-cancellation] [Shutdown implementation limitation][C-shutdown] [Stop-point tests][C-sigterm-tests]

## 5. Logs, transcripts, archives, and evidence access

coding-agent's operator output is plain `run:` text on stdout/stderr, including startup, final outcome, and transcript path. Stream wrappers redact registered secrets after initialization; a redacted stream is not a structured event schema. The reference emits JSONL with timestamp/level/event/issue_number/phase/detail and mirrors events into each attempt's `agent_output.json`. A log adapter must add real event producers if stable structured lifecycle events are required; regex over prose is not a reliable long-term API. [Worker output][C-wiring] [Stream redaction][C-redaction] [Reference event logger][S-observability]

Current coding-agent layout includes:

| Data | Current location or owner | Public integration status |
| --- | --- | --- |
| Lifecycle, outcomes, write receipts, usage, progress | `<state>/run-ledger.sqlite3` | Internal authoritative storage, no published Forge query command |
| Git mirror and working tree | `<state>/mirrors/<owner>/<repo>.git`, `<state>/workspaces/<owner>/<repo>` | Internal mutable execution state |
| Attempt evidence | `<state>/evidence/<owner>/<repo>/<issue>/<attempt-number>/` | Real artifacts; no common manifest/export API |
| Model transcript | `transcript.jsonl` within evidence | Redacted backend-native records, not Claude session JSONL |
| Continuation Snapshot | Content-addressed `continuation-snapshots/<digest>.json` through artifact store | Versioned internal snapshot, not a simple-agent handoff note |

The locations above come from [Supervisor wiring][C-wiring], [transcript writer][C-transcript], and [snapshot persistence][C-snapshot]. Transcripts contain request/response/error records with `seq` and `at`; new messages are recorded incrementally. A final tool result without a later model request is not in the transcript; its outcome is in the ledger. Therefore a transcript alone is not an exhaustive durable tool-result or publication history. [Transcript contract][C-transcript]

Forge currently reads `/data/state/*.json`, `/data/logs/<issue>/<run>/attempt.json`, and `attempt_kind`/`outcome`, and copies whole run directories. Its cache considers the mere presence of an `outcome` key final and immutable. coding-agent records outcomes before writes complete: exposing an early `outcome` in a compatibility archive would cause Forge to cache incomplete evidence forever. Native `outcome-recorded` must remain distinct from an archive-ready terminal marker. This is also an existing reference/consumer mismatch: simple-agent `_commit_finalization` writes the outcome, then backfills an archived PR and performs completion/control accounting. Forge's claim of immutability on outcome presence is stronger than that ordering guarantees. [Reference finalization ordering][S-finalize] [Forge locator][F-locate] [Forge archive cache][F-fetch] [Outcome recording][C-outcome]

**Proposal:** prefer a bounded attempt-list/detail/artifact-manifest/export interface over making Forge query private SQLite tables or emulate Claude transcripts. Artifacts can retain native schemas with type, media type, backend, provenance and redaction metadata. Define archive completeness explicitly and paginate or bound history. If a temporary `/data/logs` compatibility projection is selected, publish it atomically only when its declared evidence set is complete, and ensure Forge invalidates active caches. Projection failure must not change the execution outcome. Access only known artifact IDs within the attempt root; coding-agent already has path containment helpers for tool-result artifacts, but no operator export command. [Artifact store][C-artifacts]

## 6. Figures: available, derivable, unavailable, and new instrumentation

Availability below concerns trustworthy source data, not an existing telemetry endpoint. All exported numbers need basis and scope. The reference v1 figure contract explicitly distinguishes measured/estimated/configured/sdk_assumed/unavailable and requires unknowns to remain null, not zero. [Reference figure contract][S-telemetry]

| Figure or field | coding-agent evidence | Honest export |
| --- | --- | --- |
| Repository, workflow, model, configuration digest, limits | Effective Configuration, pinned at Claim | Direct; configured; distinguish current startup config from active Attempt pin |
| Native Attempt ID, lifecycle, recorded outcome/reason, Base Revision, effect receipts | Ledger rows | Direct; observed lifecycle data; outcome is not necessarily finalized |
| Raw cache-inclusive token total; input/output/cache-read/cache-write | Cumulative UsageTotals, flushed after model response/tool result | Provider-derived totals; publish only with metadata availability/completeness tracking |
| Effective token total | Input less cache reads plus output; includes cache writes | Derived from provider counts; explicit `accounting=effective`, not a renamed raw budget |
| Cost and bucket cost | Provider token counts × pinned configured price table | Computed/estimated monetary figure with pricing provenance, not provider invoice measurement |
| Tool calls | Runtime counter | Measured runtime counter; never call it turns |
| Attempt elapsed time | Ledger opening timestamp to observation, also stored with usage | Derived wall clock; define frozen final value separately from current time |
| Context threshold / usable threshold / handoff cap | ContextWindowConfig | Configured runtime control limits; not necessarily provider's advertised full context window |
| Current request context | Deterministic request estimate before model invocation | Estimated; add live publication hook and last/peak tracking |
| Handoff count, snapshot digests, context estimate at crossing, handoff usage | Handoff events and trace | Direct/derived historical observations; not SDK compactions |
| Model-response count | Can count transcript response records | Derived historical count with role/scope caveats; a live counter is new instrumentation |
| Main-only cache hit rate | Per-response transcript usage offers raw inputs | Derivable only after role/conversation attribution and denominator rules; current cumulative ledger mixes work/review/handoff |
| Last finished Attempt / PR / finished_at | Finished lifecycle plus outcome/effect records | Requires a summary query/projection; outcome created_at is not completion time; no dedicated durable finished_at column |
| Process start, heartbeat, startup readiness, phase observation time | No common published snapshot | New instrumentation; cannot infer progress from Docker running or static archive age |
| Turn max, automatic-continuation max/index | No equivalent configured ceilings; no turn limit | Unsupported/not applicable, not 0; tool-call ceiling is a different unit |
| SDK auto-compact threshold, SDK compaction records, Claude CLI version | Different runtime implementation | Not applicable; do not fake SDK metadata |
| Subagent running list and latest/peak context | Separate isolated review conversations exist, but no equivalent live telemetry list | New role-aware instrumentation; reviewer seats are not generic spawned SDK subagents |
| Provider quota/rate-limit remaining | No trustworthy telemetry source established in inspected implementation | Unavailable; explicit reason |

Sources: [Workflow schema][C-config], [usage model and accounting][C-usage], [usage flush][C-usage-flush], [context estimation][C-estimate], [handoff trace][C-trace], [transcript][C-transcript], [ledger schema][C-ledger], and [limits contract][C-meters].

### Budget semantics are a compatibility blocker

simple-coding-agent defines the token budget as every token category unweighted, including subagents. coding-agent keeps that raw throughput for audit but enforces **effective tokens**, excluding repeated prompt-cache reads. For example, with 100,000 input tokens, 90,000 cache-read tokens and 1,000 output tokens, raw throughput is 101,000 while effective work is 11,000. A `used/hard_ceiling` bar must use matching accounting on both sides. Export both totals and declare which one is enforced; do not change accounting to satisfy a legacy widget. [Reference token-budget definition][S-context] [Effective accounting][C-usage] [Accounting tests][C-budget-tests]

The runtime also has progress Gate/reserve phases at 70%/80% of **any meter**, and soft-stall handling. They are not the reference's percentage/reserve-turn soft threshold and bounded skill handoff. A common UI can display a native phase, but should not imply a reference handoff is imminent. [Progress policy][C-progress] [Reference handoff/control][S-control]

An availability edge case matters: `usage_breakdown(None)` and absent bucket fields become zero internally, while startup capability assertion only proves nonzero usage for its probe response. Aggregated zero is therefore not proof of measured zero for every later response or optional category. New telemetry instrumentation should retain “reported / absent / partially reported” information at the provider-response boundary. Do not stamp all current UsageTotals fields as measured unconditionally. [Usage parsing][C-usage] [Capability probe][C-capability]

## 7. Responsive control and freshness integration seams

The Worker calls the Attempt graph synchronously, and model invocation, tool execution and GitHub recovery can block. An endpoint pumped only between poll sweeps would be unresponsive during an Attempt. A listener thread can serve prepared immutable snapshots, but the existing RunLedger uses one `sqlite3.connect(self.path)` connection without explicit cross-thread configuration or a command-serialization design. It is not safe to call existing ledger methods directly from an added socket thread. [Worker loop][C-loop] [Ledger construction][C-ledger-construction] [Model/tool loop][C-step]

**Proposed seam:** a small backend-local operator coordinator owns intake intent, durable command acknowledgments and prepared observation snapshots. Serialize command acceptance with the same boundary that decides to open/claim an Attempt; a stop committed before the Claim boundary prevents it, while a later stop applies to that active Attempt. Keep telemetry snapshot construction free of GitHub/git/provider I/O and bounded in size/time. Have Worker-thread lifecycle/usage/node hooks publish redacted snapshots, including review and finalization phases. Durable command ownership may be an extension of the Run Ledger or a store with a formally defined relationship; that storage choice remains open under ADR 0017.

Publish distinct times and meanings:

- `generated_at`: endpoint answered now.
- `process_started_at` or process epoch: restart identity.
- `observed_at` per control/attempt section: age of underlying observation.
- Runtime activity/progress observation: last relevant model/tool/lifecycle event, with blocked/waiting reason.
- Explicit startup/recovery/intake state and supported capabilities.

This separation is necessary because the reference contract describes telemetry sequence as an update counter, but its current implementation increments it for each **served snapshot**, not each progress publication. Sequence movement therefore proves polling, not model progress. Its ADR also describes a sequential listener while current server code uses a thread per connection. Copying prose without checking source would preserve misleading freshness or scheduling assumptions. [Reference snapshot code][S-sequence] [Reference transport ADR][S-adr] [Reference listener threads][S-listener-threads]

The reference endpoint and CLI already demonstrate useful bounds: 256 KiB snapshot, 3-second telemetry timeout, bounded control-lock wait with cached fallback, unknown-schema handling, and structured failure envelopes. These are candidates for a common contract, not facts about coding-agent today. [Reference telemetry contract][S-telemetry] [Reference cached control view][S-cached]

## 8. Options and decision frontier

| Option | Benefits | Costs and constraints |
| --- | --- | --- |
| Full simple-agent compatibility facade inside coding-agent | Minimal immediate Forge parser changes | Must implement actual control semantics and archive finality, preserve text formats, and represent SDK-only fields honestly; large accidental contract surface |
| Forge backend adapters around native agent APIs | Preserves native deployment, artifacts, and outcomes | Some duplicated presentation/transport logic until a neutral core is established; coding-agent still needs real control APIs |
| Shared versioned operational contract with small shared library and backend lifecycle adapters | Common fixtures, CLI/client behavior, capability handling and telemetry figures; isolates backend policy | Requires coordinated release/versioning; identify mandatory core versus optional capabilities before library extraction |

**Research recommendation:** evaluate the third option with a thin reference compatibility layer in Forge. Share schemas, request/reply parsing/rendering, transport utilities and conformance fixtures where independent of lifecycle. Keep Claim, Publication Gate, recovery decisions, budget accounting, configuration trust, and backend-native evidence inside each agent. A shared library is not itself a reason to merge their execution models. Existing public reference contracts and its private ControlSurface protocol provide extraction starting points. [Reference published contract][S-replies] [Reference transport protocol][S-listener] [ADR 0016][C-adr16] [ADR 0017][C-adr17]

Decisions now sharp enough for owner tickets:

1. Which operations are mandatory for “replacement”: observation and drain/resume only, or countdown, priority, operator handoff and recovery actions too? How does Forge expose unsupported capabilities?
2. Which compatibility is required: existing CLI text/schema/archive layout, or operator-visible behavior with Forge backend adapters?
3. What exactly does an operator partial-work stop guarantee for coding-agent, and which native Terminal Outcome does it produce? Does it require a new preservation path or an accepted contract amendment?
4. What is the common telemetry core, including budget accounting units, provenance, role scope, freshness, completion, and explicit not-applicable versus unavailable?
5. Is artifact access a versioned export API or legacy path projection, and what makes a cached archive immutable?
6. Which package/repository owns the contract, reference fixtures and conformance suite, and how are compatible agent/Forge versions discovered?
7. Are same-repository pools explicitly excluded for the first integration? Supporting them needs new coordination beyond one-volume locking and identity assignment.
The owner has already scoped this map to **new instances and fresh volumes only**, with no migration of existing simple-agent state or history. This is settled input from the [map Notes](https://github.com/lbacik/coding-agent/issues/187), not an open decision. Neither state format should be treated as interchangeable.

## Validation and limitations

Verified the three checkout revisions, read the cited source ranges and relevant existing tests, and checked this documentation-only diff for whitespace and source-link targets. Existing tests cited above were inspected, **not rerun**. No runtime compatibility is claimed, and no behavior, schema, domain glossary, accepted ADR, issue state, or sibling repository was changed. Full Forge/reference consumption details are recorded in the [companion research note](https://github.com/lbacik/coding-agent/blob/88c70a281a73a35ae975af20faaa47387eddd8cf/docs/research/2026-11-05-agent-forge-reference-contract.md); this note focuses on coding-agent capability and semantic fit.

## Immutable sources

[C-contract]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L1-L18
[C-ownership]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L20-L34
[C-cli]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/cli.py#L95-L304
[C-package]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/pyproject.toml#L1-L19
[C-image]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/Dockerfile#L109-L220
[C-compose]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/compose.yaml#L10-L107
[C-supervisor]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/worker/supervisor.py#L254-L385
[C-startup]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L150-L174
[C-wiring]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/worker/supervisor.py#L391-L480
[C-capability]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/provider/capability.py#L101-L150
[C-config]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/workflow/schema.py#L26-L131
[C-adr16]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md#L24-L54
[C-adr17]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md#L18-L36
[C-profile]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L260-L295
[C-baseline]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/graph/attempt.py#L414-L436
[C-claim]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/worker/claim.py#L70-L149
[C-claim-contract]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L178-L214
[C-storage]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L732-L781
[C-volume]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/worker/volume.py#L1-L109
[C-loop]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/worker/run.py#L186-L443
[C-shutdown]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/shutdown.py#L1-L106
[C-cancellation]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L508-L528
[C-handoff]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L496-L507
[C-publication]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L543-L572
[C-outcomes]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L628-L666
[C-outcome]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/ledger/store.py#L602-L656
[C-ledger]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/ledger/store.py#L258-L320
[C-ledger-construction]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/ledger/store.py#L232-L260
[C-redaction]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/redaction.py#L278-L299
[C-transcript]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/transcript.py#L1-L93
[C-snapshot]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/context_handoff.py#L324-L368
[C-artifacts]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/result_capping.py#L45-L70
[C-usage]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/ceilings.py#L49-L210
[C-usage-flush]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/ledger/store.py#L524-L555
[C-meters]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/docs/contract/mvp-runtime-contract.md#L459-L494
[C-progress]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/step_progress.py#L1-L18
[C-estimate]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/context_handoff.py#L76-L110
[C-trace]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/context_handoff.py#L434-L492
[C-step]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/src/coding_agent/runtime/graph/step.py#L534-L658
[C-sigterm-tests]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/tests/test_mvp_sigterm.py#L285-L492
[C-budget-tests]: https://github.com/lbacik/coding-agent/blob/f68517d884f77186b4c51a30d38ec5ad382392df/tests/test_ceilings.py#L173-L302
[S-profile]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/examples/simple-coding-agent-profile.yml#L1-L20
[S-context]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/CONTEXT.md#L23-L117
[S-control]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L1-L154
[S-recovery]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L1617-L1733
[S-replies]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/agents/agentctl-command-replies.md#L1-L112
[S-observability]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/observability.py#L40-L145
[S-telemetry]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/agents/telemetry-snapshot.md#L18-L154
[S-sequence]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/telemetry.py#L835-L905
[S-cached]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L3657-L3689
[S-listener]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control_server.py#L23-L82
[S-adr]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/docs/adr/0006-telemetry-through-agentctl-not-http.md#L1-L14
[F-image]: https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/create-agent/assets/Dockerfile.template#L8-L111
[F-pool]: https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/pool-status.sh#L78-L154
[F-locate]: https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/locate-agent.sh#L104-L133
[F-fetch]: https://github.com/lbacik/agent-forge/blob/04d6effde16bd8fff1978bbf362100968f0e705a/.agents/skills/debug-agent/scripts/fetch-run.sh#L18-L70

[S-control-ops]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L237-L747
[S-countdown]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control.py#L1404-L1415
[S-reset]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L3523-L3559
[S-finalize]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/lifecycle.py#L5099-L5177
[S-listener-threads]: https://github.com/lbacik/simple-coding-agent/blob/3fe74702d802a7fbbc4f8a96265a678be27be995/simple_coding_agent/control_server.py#L213-L230
