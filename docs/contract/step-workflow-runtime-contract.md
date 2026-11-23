# Step-workflow runtime contract

**Status: current.** This document is the only authority on how the Worker behaves once the step-workflow runtime is in place. It replaces [the MVP runtime contract](./mvp-runtime-contract.md), which is now a frozen design record, as [the v1 runtime contract](./v1-runtime-contract.md) was before it. The cutover slice (S10 of [the implementation plan](../plan/step-workflow-implementation-plan.md)) has landed on `main`: the Worker there runs only this runtime. Releases up to and including `v0.2.0` run the MVP runtime, which the MVP contract describes; no release ever contains both engines.

The rules below transcribe the accepted decisions of the planning map [Design: declarative step workflows compiled from YAML to LangGraph](https://github.com/lbacik/coding-agent/issues/276). Those resolutions remain the record of *why* each rule exists and are not restated here:

- [What may a Workflow configure, and which guarantees remain mandatory in the runtime?](https://github.com/lbacik/coding-agent/issues/279#issuecomment-6797755715) (cited as **#279**);
- [What is the execution and data contract of one Workflow Step?](https://github.com/lbacik/coding-agent/issues/280#issuecomment-6798402008) (**#280**);
- [How do Step Results select successors, bound loops and terminate a Workflow?](https://github.com/lbacik/coding-agent/issues/281#issuecomment-6798622518) (**#281**);
- [How do step limits, Attempt budgets and AI-assisted Handoff compose?](https://github.com/lbacik/coding-agent/issues/282#issuecomment-6798892866) (**#282**);
- [How are step transitions, terminal outcomes and external effects recorded and recovered?](https://github.com/lbacik/coding-agent/issues/283#issuecomment-6799526314) (**#283**);
- [Can one YAML contract express implementation and standalone review with explicit failure paths?](https://github.com/lbacik/coding-agent/issues/284#issuecomment-6800063171) (**#284**);
- [How are issue and pull-request inputs selected, claimed and pinned for a Workflow?](https://github.com/lbacik/coding-agent/issues/286#issuecomment-6799233346) (**#286**);
- [What specification, acceptance scenarios and implementation order make step workflows ready to build?](https://github.com/lbacik/coding-agent/issues/285#issuecomment-6800378643) (**#285**);
- [Which details did the step-workflow contract transcription leave open?](https://github.com/lbacik/coding-agent/issues/288#issuecomment-6801265307) (**#288**), which settles the points the first transcription left open and amends #283 point 11 and #286 point 3.

Where a mechanism of the MVP runtime is retained unchanged, its rule text is carried into this document, and the MVP decisions behind it ([#71](https://github.com/lbacik/coding-agent/issues/71#issuecomment-6542177173)–[#74](https://github.com/lbacik/coding-agent/issues/74#issuecomment-6543048248) and the ADRs) remain its record of *why*. The architectural decisions specific to this design are [ADR 0027](../adr/0027-workflow-policy-is-separate-from-runtime-mechanics.md) and [ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md).

Vocabulary is fixed in [`CONTEXT.md`](../../CONTEXT.md) and is used here without redefinition. The owner-reviewed prototype of the configuration syntax is on `research/workflow-yaml-prototype` at [`dc9e93f`](https://github.com/lbacik/coding-agent/tree/dc9e93f3cde841af93a92a3543d1bc65cea48bda/prototypes/workflow-yaml); it is evidence, not part of this contract. [`examples/workflows.yaml`](../../examples/workflows.yaml) is the shipped example, and [the operator guide](../ops/workflow-configuration.md) explains the file without adding rules.

**Acceptance rows.** Each section ends with the acceptance rows that prove it. Row ids are grouped by area: `CFG-`, `STEP-`, `ROUTE-`, `LIM-`, `INP-`, `OP-`, `REC-` and `OPC-`. The matrix, with layers and owning slices, is [`docs/plan/step-workflow-acceptance-matrix.md`](../plan/step-workflow-acceptance-matrix.md).

## 1. Layers and ownership

A **Workflow** is process policy: a configured graph of Workflow Steps, its Routes, its limits and its instructions. The **runtime** owns the mechanics that hold whatever the graph says: authorisation, the exclusive Claim, withdrawal and interruption, limits, factual execution records, durable external effects and deterministic finalisation ([ADR 0027](../adr/0027-workflow-policy-is-separate-from-runtime-mechanics.md), #279). Implementation and review are scenarios written in YAML and skills; the runtime contains no branch that knows which scenario it runs.

| Layer | Owns | May write to GitHub |
| --- | --- | --- |
| **Supervisor** (plain Python, no graph) | Startup checks; the exclusive volume lock; restart settlement and finalisation (§16); polling; selection; the Claim (§6); receiving SIGTERM; invoking the compiled Workflow graph; finalisation (§15) | Only Claim, finalisation and recovery writes, each through an Effect Intent (§14) |
| **Workflow graph** (compiled from the validated Workflow, §8) | One Attempt's Step Executions and Routes, from `start` to a Workflow End | Only through Runtime Operations (§10) |
| **`agent` executor** (inside one graph node, §9) | One Step Execution's conversation with the Pinned Model, its tools, its Sub-agents and its Step Result report | **No** |
| **`operation` executor** (inside one graph node, §10) | One Runtime Operation: deterministic code with no model call | Yes, the writes its catalog entry names, each through an Effect Intent |
| **Model** (inside an `agent` executor, and its Sub-agents) | Reasoning, workspace file edits, commands through the `shell` and `tests` groups | **No, never** |

- **No tool given to the model mutates GitHub.** Labels, comments, assignments, reviews and pull requests are written only by the runtime. There are no GitHub read tools for the model in the first release (#280).
- **There is no merge operation and no approval.** The never-merge rule is enforced by their absence (§10).
- **No framework checkpoint.** The graph is compiled with `checkpointer=None`; the Run Ledger is the only durable authority (§13).
- **One Worker runs exactly one Workflow** against one Target Repository and one state volume. Two scenarios are two Workflows and two Workers (#286).
- **The Worker owns the tool loop.** The model layer is always a Pinned Model called through a provider adapter inside a loop the runtime runs. A third-party coding agent that runs its own loop is out of scope ([ADR 0023](../adr/0023-the-worker-drives-only-models-whose-tool-loop-it-owns.md)).

**Rows:** STEP-3, STEP-13, CFG-40.

## 2. Workflow Configuration

The operator supplies the Workflow Configuration, one YAML file, at startup. It may hold several Workflows; a Worker runs exactly one, selected with `--workflow`. Only `version: 4` is accepted: a `version: 3` file is refused with a migration diagnostic before installation, live calls, Claim or GitHub writes, and there is no backward compatibility with, converter for, or second engine for older files ([#276 Notes](https://github.com/lbacik/coding-agent/issues/276); the old implementations are preserved at their tags). `version: 2` keeps its own refusal. [`examples/workflows.yaml`](../../examples/workflows.yaml) shows both example Workflows in full; the reference deployment's four-skill selection at its verified commit is `docker/deployment/workflow.yml` with its reviewed `skill-policy.json`.

### 2.1 The file

All keys are kebab-case.

| Key | Required | Meaning |
| --- | --- | --- |
| `version` | yes | Must be `4`. A `version: 3` file is refused with a migration diagnostic before installation, live calls, Claim or GitHub writes (§2.9); there is no converter and no dual engine. `version: 2` keeps its own refusal. |
| `target.kind` | yes | `github`, the only tracker adapter. |
| `target.repository` | yes | The Target Repository, `owner/name`. |
| `target.base-branch` | yes | The base branch. The Base Revision of an issue input is its tip at Claim (§7.1), and the Project Profile is read there. |
| `target.token-env` | yes | The **name** of the environment variable holding the Agent Identity's token. |
| `target.profile` | no | The instance-supplied Project Profile: a path relative to the configuration file's directory, resolved like a `prompt:` locator ([ADR 0030](../adr/0030-the-workflow-configuration-may-supply-the-project-profile.md)). Where present it is the Validation Contract's source; where absent the profile comes from the Target Repository (§10.2). |
| `models.<id>` | at least one | A Pinned Model: either a direct pin (`provider`, `model`, `endpoint`, `effort`, `api-key-env` (a name), `prices-usd-per-mtok` with `input`, `output`, `cache-read` and `cache-write`, `context-window-tokens`, `context-threshold-tokens` and `output-cap-tokens`, all required) or a gateway pin (`gateway` with exactly one of `url` or `url-env`, `model`, `endpoint`, `effort`, `api-key-env` (a name), the same three token keys and `prices-usd-per-mtok` with the same four buckets, all required). Exactly one of `provider` or `gateway` is present. `effort` is `low`, `medium` or `high`. A direct pin's `endpoint` must be one the code-owned table lists for its `provider`; a gateway pin's `endpoint` must be one the code-owned table lists for its gateway kind. The three token keys are positive integers; `context-threshold-tokens + output-cap-tokens` must not exceed `context-window-tokens`, and the threshold must exceed the safety margin plus request overhead. |
| `skills` | no | The configuration-wide Skill Bundle membership: a list of `{source, include, ref?}` entries. `source` is an HTTPS Git locator in the `https://` or `git+https://` spelling (no userinfo, query or fragment; SSH, local directories and other transports are refused); `include` names the exact artifact member skills, case- and whitespace-sensitive with no alias or frontmatter substitution; `ref`, when present, is a nonempty string naming a commit, tag or branch (`ref: null` and empty refs are refused). Duplicate sources (including the two accepted spellings of one URL) and duplicate member names across sources are refused. A missing field and `skills: []` both mean an empty bundle. |
| `workflows.<id>` | at least one | A Workflow (§2.2). |
| `worker.poll-interval-seconds` | no | Idle sleep between selection sweeps (§4.2); default 60. |
| `worker.shutdown-grace-seconds` | no | Time the live Attempt has to finalise after SIGTERM (§12); default 30. |

- **Secrets appear only as environment-variable names.** An inline secret value is a validation error.
- **A gateway pin names its gateway** (`gateway.kind`, today only `litellm`, from a closed code-owned list) **and its gateway address** — exactly one of `gateway.url` (a literal `http` or `https` URL) or `gateway.url-env` (the name of an environment variable holding the URL, validated like `api-key-env`: a name, never a value). Either spelling takes a value with a host, an optional port and an optional path; userinfo, a query or a fragment is refused. A `url-env` value is resolved once per Worker process at `run` startup, before the Provider Capability Assertion (§4.1), and an unset, empty or malformed value refuses startup with no Attempt opened. The URL is canonicalised (lowercase scheme and host, no trailing `/`). The pin's `model` is the gateway's own name, free-form and never checked against a table. The pin key is the label `litellm:<model>@responses`.
- **Every pin declares its own Model Context Window.** `context-window-tokens` is the window a request and its response share ([ADR 0032](../adr/0032-token-limits-are-declared-per-pin-in-the-workflow-configuration.md)). No code-owned window, output-cap or price table remains: the pair table in [`provider/config.py`](../../src/coding_agent/provider/config.py) admits the (`provider`, `model`) pair and endpoint only and carries no token budget. The per-request output cap (`max_tokens`) comes from the pin and is sent with every request (§11.4). Only the safety margin (1,000) and request overhead (1,000) stay code-owned defaults — they describe the runtime's own framing, not any model's budget — and the threshold must exceed the safety margin plus request overhead on every pin. The window, threshold and output cap are part of the pinned identity (§2.10).
- **The Project Profile has two sources** ([ADR 0030](../adr/0030-the-workflow-configuration-may-supply-the-project-profile.md)). Where `target.profile` is absent the profile is `docs/agents/project-profile.yml` in the Target Repository; where it is present the named file beside the Workflow Configuration is the Validation Contract's source and the repository's own profile is never read for readiness, validation or final checks (§10.2). The locator resolves like a `prompt:` locator and stays inside the configuration directory.
- **The state volume is not part of the configuration.** It is passed on the command line.
- **The `worker` block is optional** (#288 point 3). Its two settings are file-level, as in the MVP, and are recorded in the Effective Configuration (§2.10). They stay configurable because the poll interval consumes the owner's shared rate limit ([ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md)) and the grace must fit the deployment's stop timeout, which code cannot know.

### 2.2 Workflow keys

| Key | Required | Meaning |
| --- | --- | --- |
| `model` | yes | The id of one `models` entry: the Attempt's one Pinned Model (§9.1). |
| `input.type` | yes | `issue` or `pull-request` (§5.1). |
| `input.label` | yes | The Workflow's Selection Label. |
| `input.require-all` / `require-any` / `exclude` | no | Label filters (§5.3). |
| `input.drafts` | no | `include` or `exclude` (default); `pull-request` Workflows only. |
| `input.skip-open-agent-pr` | no | `true` skips an issue whose own agent pull request is open; `issue` Workflows only (§5.3). |
| `limits` | yes | The Attempt Ceiling: `cost-usd`, `effective-tokens`, `tool-calls` and `wall-clock-minutes`, all four required (§11). |
| `max-parallel-subagents` | no | At most this many Sub-agents of one group run at once (§9.8): an integer `>= 1`, default 2 (#288 point 3). |
| `outcomes.<Terminal Outcome>.labels` | no | End labels the runtime applies when the Attempt ends with that Terminal Outcome, runtime-only outcomes included (§15.3). No end label may equal a Selection Label (§3). |
| `start` | yes | The id of the entry step (§8.1). |
| `defaults.max-executions` | no | The Execution Ceiling of every step that does not declare one; default 1. |
| `defaults.routes` | no | Routes for the runtime-reserved results `error` and `limit-exceeded` only. |
| `steps.<id>` | yes, non-empty | A map from step id to Workflow Step (§2.3). Map order is meaningless. |

### 2.3 Steps

Every step has **exactly one executor** and the same control keys.

| Key | Applies to | Meaning |
| --- | --- | --- |
| `agent` | executor | A mapping with `instructions`, `tools`, `inputs` and `results` (§9). `results` is required. |
| `operation` | executor | The name of a catalog Runtime Operation (§10). |
| `with` | `operation` steps | The operation's parameters (§2.6). |
| `max-executions` | every step | The step's Execution Ceiling: a positive integer (§8.3). |
| `routes` | every step, required | A map from Step Result name to a target: a step id or `{end: completed\|blocked\|unfinished\|failed}` (§8). |
| `limits` | `agent` steps | The step's Step Allowance, any subset of the four meters (§11.2). |
| `reserved` | `agent` steps | `true` marks a Reserved Step, which must then declare all four meters in `limits` (§11.3). |

`agent` holds:

- **`instructions`:** an ordered list of `skill: <name>` (a Skill Bundle member) and `prompt: <path>` (an operator prompt file) items (§9.3);
- **`tools`:** a list of tool groups from `workspace-read`, `workspace-write`, `shell`, `tests` and `subagents` (§9.4);
- **`inputs`:** a list of references (§2.5);
- **`results`:** a map from result name to its payload, a map from field name to field type (§2.4).

### 2.4 Step Results and payload types

A payload field has one of these types: `document`, `string`, `integer`, `boolean`, `evidence-ref`, `{list: T}` or `{enum: [...]}`. There is no literal JSON Schema and no executable expression (#284).

- **`document`** fields are the only source of Step Artifacts and of publication bodies (§9.6, §10).
- **`evidence-ref`** names a test identifier that the runtime checks when the report is validated (§9.5).
- **`error` and `limit-exceeded` are runtime-reserved** and cannot be declared. Nor can the catalog's conditional results `requirement-unmet` and `stale-head`.
- **Result names and field names** match `[a-z0-9]+(-[a-z0-9]+)*`. An empty payload is `{}`.

### 2.5 Inputs and references

An input is a dotted reference, checked statically:

- `<step>.<result>.<field>`: a payload field of the latest Step Execution of `<step>`, which must have ended with `<result>`;
- `<step>.facts`: the Execution Summary of the latest Step Execution of `<step>` (§9.7);
- `{optional: <reference>}`: the same reference, allowed to be absent.

An input reads the **latest** Step Execution of the named step, even one from an earlier round of a loop. A required input must be proven available on every path (§3). Operation bodies (`with.body`) are required inputs under the same proof.

### 2.6 Operation parameters

| Parameter | Operations | Meaning |
| --- | --- | --- |
| `with.body` | `open-pull-request`, `publish-review`, `post-comment` (required) | A reference to a `document` field. |
| `with.draft` | `open-pull-request` | `true` opens a draft pull request. |
| `with.requires` | `commit-and-push`, `open-pull-request`, `publish-review`, `post-comment` | A list of evidence that must hold for the revision being published. The only requirable entry in the first release is `run-validation: passed` (§10.1). |

The pull-request title is not a parameter (§10.5).

### 2.7 Strict parsing

- `version: 4` is required. A `version: 3` file is refused with a migration diagnostic naming exactly what to change (`version: 4`; the three token keys on every pin; `tokens` → `effective-tokens`); `version: 2` keeps its own refusal. There is no converter and no dual engine.
- **Rejected:** unknown fields at any level; duplicate keys; anchors and aliases; merge keys (`<<`); custom tags; wrong types. The default PyYAML loader silently overwrites duplicate keys and is not used (#281).
- **The `skills` declaration is strict:** an entry's source or member that fails the §2.1 grammar is refused before any subprocess runs or anything secret-bearing is logged; a `skill:` an agent step names that no entry declares is refused naming the instruction path; the whole declaration always counts as the bundle membership whatever `--workflow` selects — `--workflow` selects only which Workflow runs and never narrows validation or membership (§2.9).
- **Every id** (model, Workflow, step) matches `[a-z0-9]+(-[a-z0-9]+)*` and is unique within its map. A step id never collides with the reserved end-node prefix (§8.5).
- **Unsupported constructs are rejected with a targeted message,** never ignored: a list of Route targets, fan-out or join keys, nested Workflows, a per-step `model`, conditions or expressions (`when`, `if`, `condition`), wildcard Routes, a `retry` construct and limit handlers other than Routes.

### 2.8 Trust and paths

The Workflow Configuration, its prompt files and the Skill Bundle are trusted operator input ([ADR 0027](../adr/0027-workflow-policy-is-separate-from-runtime-mechanics.md), #279).

- **They are read from the Worker's filesystem only, never from the Target Repository.**
- **Relative paths resolve against the YAML file's directory** and must stay inside it after symlink resolution. Prompt locators resolve against the configuration directory; containment is rechecked on every run. A prompt may be a contained symlink; an escaping symlink is refused.
- **Prompt content is the raw file bytes, decoded as strict UTF-8 and used verbatim,** with no templating, no interpolation and no newline normalisation: CRLF line endings and non-ASCII content are preserved byte for byte. A missing, unreadable or non-UTF-8 prompt is a normal refusal naming its YAML location, never a traceback and never a late failure.
- **A skill named by a step must be a declared member of the Skill Bundle.** There is no ambient discovery and no fallback read of the skills store or the network: an unknown, undeclared, path-shaped, absolute, traversal or differently-cased name is a typed refusal with no filesystem access. A custom skills directory is refused.
- **Input content has no instruction or authority channel** (§7.4).

### 2.9 Commands

Four phase commands share one typed phase API; each returns validated values plus structured diagnostics, never a bare boolean. Independent faults within a phase are collected together so one pass fixes them; dependent checks are skipped, never reported as passing. Diagnostics name the phase, YAML path, skill/source and file without echoing secrets or file contents. Exit `0` is success, `1` is a refusal or failure, `2` is a usage error.

- **`agent config validate --config <file> [--workflow <id>]`** is static and offline. It runs every check of §2.7, §2.8 and §3 — strict syntax, all graphs, all prompts, static membership, and inert graph compilation of every Workflow, including ones that invoke no operation, model, tool or credential. It requires no installed skills and no secrets: no environment-variable value is read, no value is printed, and no network call is made. Its success message claims only what it checked ("static only; no secrets checked"). `--workflow` only selects which Workflow runs; the whole file is always validated and the whole `skills` declaration always counts as the bundle membership, so a defect in an unselected Workflow or member is never hidden. A `version: 3` file is refused with exit 1 and a migration diagnostic; `version: 2` keeps its own refusal. Removed legacy options (for example `run --skills-home`, `--skill-bundle-dir`, the install/list report inputs and the ignore-file input) fail with exit 2 and point at their replacements.
- **`agent prepare-skill-bundle --mode installed|install --config <file> --policy <file> --bundle-dir <dir> --home <dir> [--resolution-manifest <file>]`** adopts (`installed`, offline: nothing downloaded or updated behind the operator's back) or installs (`install`, resolving each source group's ref once to an immutable commit as the intended runtime user) the declared subset, then publishes `requirements.json`, `manifest.json` and `validation.json` — each at `schemaVersion: 1` in canonical JSON (UTF-8, sorted keys, compact, literal non-ASCII, no floats, lowercase 64-hex digests) — together as one completed generation, and only after every check passes. A failed or partial installation publishes no new successful generation and discloses what it changed. Unrelated skills in a shared store are left untouched and never activatable; exposure symlinks are optional. `--mode` is always required; `--resolution-manifest` carries a saved symbolic-ref resolution when offline adoption must not re-resolve a branch. Network access happens only in `install`.
- **`agent verify-skill-bundle --config <file> --policy <file> --bundle-dir <dir> --home <dir>`** re-checks the final copied roots offline and read-only: the YAML, prompt and policy bytes must equal the prepared ones even when semantically equivalent, or verification refuses. It never refreshes or repairs records. A relocated identical prepared tree verifies against its new trusted roots; an incomplete multi-stage copy refuses. Its success message names its scope. A builder-stage log is never the acceptance proof.
- **`agent run --config <file> --workflow <id> --policy <file> --bundle-dir <dir> --skill-home <dir>`** repeats the full offline deployment verification before any secret or live check (§4.1), then checks only the selected Workflow's secrets, then runs the live checks. There is no skip-bundle, skip-credentials or build-only switch: the weaker build-only path cannot be selected at runtime.
- **Every validation failure exits non-zero** with no Attempt and no GitHub write, and the message names the YAML path of every fault. An unknown `--workflow` exits 1 and lists the available ids; `--workflow` is always required by `run`.

### 2.10 Effective Configuration and pinning

- **Read once.** The Worker reads the YAML, every prompt file, the policy and the prepared bundle records once, at startup, and builds one immutable activation registry from the same verified bytes (§9.3). Edits require a restart and never change an open Attempt (#279); disk edits made after startup are ignored until a restart passes full validation.
- **Pinned at Claim** (#283 point 6). The Attempt records its Effective Configuration in full, as record `version: 4` (its own numbering, separate from the YAML `version`). Its identity core has the sections `workflow`, `file`, `prompts`, `skill_bundle`, `activations` and `pinned_model`:
  - `workflow`: the canonical, compiled Workflow: steps, Routes, Step Result schemas, limits, `requires`, the `input` selector and the end labels;
  - `file`: the file-level settings it runs under (`target`, the referenced model and the `worker` settings with their defaults applied);
  - `prompts`: the digest of every operator prompt file the selected Workflow needs;
  - `skill_bundle`: every available bundle member's provenance (source identity, resolved commit), complete recursive inventory and selected review;
  - `activations`: the exact delivery bytes of every available activation — the complete `SKILL.md`, the ordered `inject` companions and the framed notices — with the activation framing version and the delivery digest and size, including on-demand-only members;
  - `pinned_model`: the Pinned Model with its prices, Model Context Window (`model_context_window_tokens`), output cap and context threshold; a gateway pin records its full identity there — the gateway kind, the canonical URL, the model, the endpoint, the effort, the output cap and the threshold. A pin whose address came from the environment also records the source (`gateway_url_source: env:<NAME>`); a literal pin's record carries no such key.

  One SHA-256 digest covers the canonical core. Excluded from the identity: relocation, image retags, producer versions, verification mode, unused catalog edits, unrelated store artifacts, absolute paths, image tags, timestamps and the digest of `validation.json`. A commit change alters the identity even when the bytes are identical, so provenance is never erased. The serializer keeps the current identity JSON representation: UTF-8 encoding of Python JSON with sorted keys, compact separators, `ensure_ascii=True` and `allow_nan=False`, no BOM and no trailing newline; model numbers stay numbers. Environment-variable values are never part of it — with the single exception of the resolved gateway URL, which is not a secret and carries no userinfo by rule; their names are.
- **Archived before Claim.** Every needed prompt and every available activation's components and delivery bytes are written to the Attempt Archive, flushed and digest-addressed before the ledger transaction the Claim depends on, so no confirmed Attempt references content that was never stored (§6.2, §13.3). `not-delivered` bodies are never archived; their inventory digests are pinned.
- **Recovery uses pinned values only.** A missing or unreadable pinned record refuses startup ([ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md), retained); recovery validates the record version, the core digest, the reference index and every required blob, and refuses — naming the Attempt and the fault — on any missing, corrupt, escaping, symlinked or incompatible core or blob, without repairing from current files (§16).

**Rows:** CFG-1, CFG-2, CFG-32, CFG-36, CFG-38, CFG-39, CFG-41, CFG-42, CFG-45, STEP-17.

## 3. Static validation

Every violation refuses Worker startup; there are no warnings (#281 point 9). Every command validates the whole file: `--workflow` never hides a fault in another Workflow and never narrows bundle membership. Inert graph compilation of every Workflow that invokes no operation, model, tool or credential is an additional check, never a replacement for the rules below. The validator checks, in addition to §2.7 and §2.8:

- **Structure:** `start` names a step; step ids are unique; `max-executions` is a positive integer; `max-parallel-subagents`, when present, is an integer `>= 1`, so `0`, a negative number or a non-integer (for example `1.5` or `"2"`) is refused, as in the MVP.
- **Routes:**
  - every target is an existing step or a YAML-selectable Workflow End (`completed`, `blocked`, `unfinished`, `failed`); a runtime-only Terminal Outcome is not a target;
  - every Route key belongs to the step's result set: its declared results, or its catalog results, plus `error` and `limit-exceeded`;
  - **coverage is exhaustive after applying `defaults.routes`;** a business result (model-declared or catalog) is always routed in the step itself;
  - `defaults.routes` names only `error` and `limit-exceeded`;
  - conditional catalog results require their condition: `requirement-unmet` only on an operation with `with.requires`; `stale-head` only on `publish-review` and on `commit-and-push` in a `pull-request` Workflow.
- **Graph:**
  - every step is reachable from `start`;
  - the graph of `limit-exceeded` Routes after applying defaults is **acyclic**, self-Routes included; the message names the cycle. `error` Routes are exempt, because every `error` comes from a Step Execution that consumes its ceiling (§8.3);
  - a runtime-reserved result routes directly only to an end that allows it: `unfinished` and `failed` allow it, `completed` and `blocked` do not (§15.1);
  - **required-input availability is proven** (below).
- **Limits:** `limits` only on `agent` steps; no `defaults.limits`; a Reserved Step declares all four meters; the sum of reserved allowances stays below the Attempt Ceiling in every meter; a non-reserved Step Allowance never exceeds the Work Pool in any meter (§11.3); and every limit that declares `effective-tokens` or `cost-usd` holds at least one maximal Admission hold for its step — `effective-tokens` ≥ step Usable Threshold + `output-cap-tokens`, `cost-usd` ≥ step Usable Threshold × the `input` price (max(`input`, `cache-write`) on a gateway pin) + `output-cap-tokens` × the `output` price — checked on the Attempt Ceiling against the largest per-step hold over all agent steps, on the Work Pool against the largest over non-reserved agent steps, and on every Reserved Step allowance and every non-reserved Step Allowance that declares the meter against its own step's hold. One step's hold is its largest admissible input — its soft Usable Threshold where the step configures soft, else its hard or pin-default Usable Threshold, raised to the pin-default Usable Threshold where the step can start Sub-agents — plus the pin's full output cap. The pin-default Usable Threshold is the pin's `context-threshold-tokens` minus the code-owned safety margin (1,000) and request overhead (1,000). A limit that does not declare the meter is not checked for it. The guarantee is one full-context request per fresh limit: it does not cover a re-entered step whose accumulated allowance has run low, a parallel Sub-agent group at peak context, or finishing the step.
- **Context Thresholds:** `context-thresholds` only on `agent` steps; operations hold no conversation, so `context-thresholds` on an operation is refused. The policy is a mapping with required positive-integer `hard-tokens` and optional positive-integer `soft-tokens`; nulls, unknown fields and non-integer values are refused. Pin-dependent bounds are proven against the Workflow's Pinned Model before any network call: `hard-tokens` must exceed the code-owned safety margin plus request overhead, and `hard-tokens` plus the pin's `output-cap-tokens` must not exceed the Model Context Window (`context-window-tokens`); where configured, `soft-tokens` must sit strictly between the safety margin plus request overhead and `hard-tokens`. On an opted-in step the spelling `hard-context-threshold` is reserved to the runtime and cannot be declared as a business result; steps without the policy keep it as an ordinary business result. Where the policy configures `soft-tokens` the spelling `soft-context-threshold` is likewise reserved to the runtime and cannot be declared; steps without a soft policy keep it as an ordinary business result. An opted-in step needs a step-local Route for `hard-context-threshold`; the hard Route is proven model-free — every reachable Runtime Operation outcome, including `error`, `limit-exceeded` and conditional results, routes through operation steps only to an `unfinished` or `failed` Workflow End — so an agent successor and any reachable cycle are refused, while a direct permitted Workflow End is valid. A soft-configured step additionally needs a step-local inheriting Route (`{step: <id>, inherit-conversation: true}`) for `soft-context-threshold` into a Reserved agent receiver step carrying an explicit hard-only policy and all four Step Allowance meters; the receiver's `hard-context-threshold`, `error` and `limit-exceeded` Routes are proven model-free through the same operation-only rule to an `unfinished` or `failed` Workflow End, so the receiver never re-enters an agent step and its fallbacks never cycle. A soft Route without inheritance, to a non-reserved step, to an operation step, to a receiver without a hard-only policy or without all four meters, or whose fallbacks reach an agent step, is refused naming the route.
- **Pins:** exactly one of `provider` or `gateway` is present; `gateway.kind` is from the closed code-owned list (only `litellm`); the gateway address is exactly one of `gateway.url` (a literal `http` or `https` URL) or `gateway.url-env` (an environment-variable name, validated like `api-key-env` — a value-shaped name is refused); the URL, however spelled, has a host, an optional port and an optional path, and userinfo, a query or a fragment is refused; `endpoint` is admissible for the pin kind (`responses` for `litellm`, the code-owned table's endpoint for a direct pin's provider); `model` is a non-empty string; `api-key-env` is a required environment-variable name; `effort` and the prices are valid as for a direct pin; **every pin, direct or gateway, declares `context-window-tokens`, `context-threshold-tokens` and `output-cap-tokens` as positive integers**; `context-threshold-tokens + output-cap-tokens` does not exceed `context-window-tokens`; the threshold exceeds the code-owned safety margin plus request overhead. Worker startup runs the Provider Capability Assertion against the gateway for a gateway pin, like a direct pin (§4.1); gateway pins assert the answering model from the gateway's own identity headers (§9.1).
- **Input type:** `publish-review` only in `pull-request` Workflows; `open-pull-request` only in `issue` Workflows; `drafts` only for `pull-request` and `skip-open-agent-pr` only for `issue` (#286).
- **Tools and data:** tool groups from the five of §9.4 only; no always-present tool declared; no declared result named like a reserved or conditional result; every `with.body` references a `document` field; every reference is a reference, never an expression; no facts-mode `post-comment` and no `with.title` (#284).
- **Static membership:** every static `skill:` names a member of the configuration-wide `skills` declaration (§2.1, §2.7); an undeclared skill refuses startup naming the instruction path. Declaration and static references are checked with no installed bundle and no secrets.
- **End labels:**
  - `outcomes` keys name Terminal Outcomes of §15.1, so `outcomes.canceled` is refused (#288 point 2);
  - no `outcomes.<outcome>.labels` entry equals the `input.label` of **any** Workflow in the same file, because the Worker never applies a Selection Label (§18, #288 point 1).

**Required-input proof** (#281 point 10, refined by #284). A required input `X.R.field` is valid only if a forward must-analysis over the Route graph, including refusal continuations, shows that on every path to the consumer the latest Step Execution of X ended with R. A required `X.facts` only needs X to have executed on every path. **An Execution Ceiling refusal edge implies that the refused step has already run**, so the analysis never treats that step's latest result as absent on that edge; without this, a Reserved Step reading the required `process.facts` would be rejected. A configuration the analysis cannot prove is rejected, and the input must be declared optional.

A successful LangGraph compilation (§8.5) is an additional check, never a replacement.

The 35 invalid configurations of the prototype, each with its `# expect:` line, live in `tests/fixtures/` and are the table-driven acceptance of this section, with `examples/workflows.yaml` as the valid case; dedicated refusal tests cover `version: 2` and `version: 3` files and legacy CLI options.

**Rows:** CFG-3 … CFG-31, CFG-33 … CFG-37, CFG-43, CFG-44, CFG-46.

## 4. The Worker's lifecycle

The Worker's lifecycle is the MVP's, retained, with the changes this section names.

### 4.1 Startup

`agent run` performs these steps in order. A failure at any of them exits non-zero with no Attempt opened and no GitHub write. There is no skip-bundle, skip-credentials or build-mode switch.

1. **Validate the Workflow Configuration** (§2.9, §3): local configuration, prompt, policy, records and store checks — static, offline, with no secrets and no network.
2. **Take the exclusive lock** (`flock`) on the state volume. A second process using the same state volume is refused.
3. **Open the operator store and start the control listener.** The instance state is `starting` from here until readiness.
4. **Repeat the full offline deployment verification** before any secret or live check: the Skill Bundle deployment is re-verified against the final roots — configuration, prompt and policy bytes equal to the prepared ones, complete inventory, provenance, review matching and closure for every declared member — and the immutable activation registry is built from the verified bytes (§9.3). The wording inside a skill body is never a refusal.
5. **Check the selected secrets' presence:** only the target token (the `target.token-env` name) and the selected Workflow's model key (its `api-key-env` name) are required. A key used only by another Workflow never blocks startup; no secret value is ever printed. Credential names and model/key pairings are still validated statically for every Workflow at step 1.
6. **Run the non-mutating GitHub checks** of §18: token kind, the Agent Identity's numeric id, push permission on the Target Repository, rate-limit headroom and the identity mode.
7. **Run the Provider Capability Assertion** for the Workflow's Pinned Model ([ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md)), with its output cap. A gateway pin with `gateway.url-env` resolves its address here first, once per Worker process: the value must pass the same rules as a literal `gateway.url` and is canonicalised the same way, and an unset, empty or malformed value refuses startup — reported at this phase, naming the variable but never the refused value — with no Attempt opened. A resolved address takes effect only at restart. The gating probe binds the probe tool with no forced `tool_choice` (`auto`), as every work node binds its tools, and sends a prompt asking for a call: only a call to the probe tool whose arguments match its schema passes — a text answer, a call to another tool, or arguments that do not match refuse startup with no Attempt opened, with no retry on a text answer. The diagnostic forced probe keeps the old shape — a forced `tool_choice` naming the probe tool with a prompt asking for no tool call — and its outcome is recorded but never refuses startup by itself. Gateway identity gates on every probe, including the forced one: retry or fallback evidence above 0, or a model group other than the pin, on a response or on an error alike, refuses startup. Where the gating probe response reports the effort it ran at, the reported effort must equal the pinned effort exactly and the invalid-effort probe is skipped; where it reports none, the same request sent with a deliberately invalid effort must be refused as an invalid request naming the effort. Every pin that speaks the Responses API, direct or through a gateway, sends `store: False` and `include: ["reasoning.encrypted_content"]` on every request, so each assistant turn carries its reasoning inside the turn instead of referring to provider-side or gateway-side state ([ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md)). For a gateway pin the adapter speaks the gateway's `/v1` Responses endpoint with the gateway key, the pinned effort and the pin's output cap on every request, exposes response headers on each message, and carries the per-request suppression (`disable_fallbacks: true`, `num_retries: 0`) on every request (§9.1). The probe response goes through the same identity and effort checks as every Attempt response (§9.1): a probe naming another model, or reporting another effort, refuses startup with no Attempt opened — the operator pins the name and the effort the response reports. Every check's result is recorded as a **Provider Capability Report** — `price`, `tool-call-auto`, `tool-call-forced` (diagnostic), `usage`, `identity`, `effort`, in run order — persisted in the operator store on success and on refusal, and served as `status.provider_capability` by `agentctl status` (live and `--offline`), `null` where no assertion has run yet.
8. **Open the Run Ledger.** A ledger holding an unfinished legacy Attempt refuses startup (§16.4). An unfinished Attempt whose Effective Configuration is missing, unreadable or of an incompatible record version is a refusal: the new Worker never invents missing identity or instruction archives from current files.
9. **Settle and finalise** every unfinished Attempt (§16), before any selection. While unfinished Attempts are being settled the state is `recovering`; afterwards the Worker is `idle` or `paused` and emits `ready`.

Each check of steps 4–7 is reported as the `startup_check` naming the current check while it is held. An invalid configuration exits at step 1 with nothing recorded.

### 4.2 The poll loop

- **The Worker is a continuously running process.** It sleeps for the poll interval (`worker.poll-interval-seconds`, §2.1) only when a sweep claimed nothing, and selects again immediately after an Attempt ends.
- **Intake.** While the ledger holds an Attempt that is not `finished` or `released`, the Worker selects no new work. A recovery write that fails is retried on the next poll cycle.
- **One Attempt at a time.** The Worker is sequential.

### 4.3 Durable Intake and Operator Commands

Retained from the MVP unchanged ([ADR 0026](../adr/0026-durable-intake-and-operator-commands-serialise-at-two-points.md)): an operator drains the Worker with `stop`, undoes the drain with `resume` and looks up any command with `command <request-id>`.

- **Store.** Intake (one row: `open`/`closed` plus the pending `stop`; absent means open) and Operator Commands (request ID unique, `sequence`, kind, canonical arguments, payload digest, repository, acknowledgement, detail, timestamps; rows never deleted) live in the Run Ledger's SQLite file. A fresh volume starts with Intake open; a restart keeps the recorded state.
- **Claim boundary.** The Intake check and the commit of the Claim intent (§6.2) happen in one immediate write transaction. A `stop` committed before the boundary prevents the Claim (nothing assigned, nothing written); one committed after stays `accepted` until its Attempt ends.
- **Finalisation.** The transition of an Attempt to `finished` or `released` completes a pending `stop` in the same write transaction, on every path that reaches it. Recording an outcome is not enough.
- **Commands gate only the decision to open a Claim and never write lifecycle state.** A `stop` never interrupts model work and never acts as SIGTERM. A `resume` only reopens Intake and supersedes a pending `stop`. Every `accepted` command an earlier process left is resolved against the ledger before the first Claim boundary.
- **SIGTERM is distinct from durable Intake** (§12): it never writes the durable row.

### 4.4 Halting

- **A Credential Failure halts the Worker** with a non-zero exit (§16.3).
- **An asserted pin violation halts the Worker** with a non-zero exit (§16.3).
- **Three consecutive infrastructure-class failures halt the Worker,** only after the third Attempt is fully finalised. The classes are setup, git, GitHub API and provider. The counter resets after any Attempt that ends without such a failure.
- **A routed provider failure counts toward the halt** as an infrastructure-class failure of class `provider` (#570): an Attempt with any Step Execution that ended `error` on a provider failure (§9.6) counts whatever End the Workflow routed to. The Terminal Outcome stays the routed one; only the counter sees the provider class, read from the durable `provider_failure` marker in the recorded Step Execution facts, so it holds on the live path and after a restart. A Context Overflow never counts (#563 decision 1: a Context Overflow never halts the Worker).
- **Three consecutive recovery write failures halt** with `recovery-write-failure` (§16.3).
- **SIGTERM** stops intake and ends the live Attempt as §12 describes; the process then exits.

**Durable Halts.** Every non-zero exit the Worker decides on after taking the volume lock records a durable Halt before exiting: the reason, the detail and the time, plus a `halted` Contract Event. A restart reports it as `instance.last_halt` until the next halt replaces it. Restarting a halted Worker is a deployment action, never an Operator Command.

### 4.5 Contract Events and Attempt Archives

One producer emits every Contract Event, redacted and capped at 300 characters; the newest 50 of the current process are `status.recent_events`. An **Attempt Archive** is the Attempt's directory on the state volume, created at the Claim boundary, holding its `events.jsonl`, its Transcript, its evidence files and, in this design, its **Step Artifacts and Execution Summaries** (#285 point 10, S9) — plus the pinned instruction content: every needed prompt's bytes and every available activation's component and delivery bytes, each written, flushed and digest-addressed before the ledger transaction the Claim depends on, so no confirmed Attempt references content that was never stored (§6.2, §13.3). `not-delivered` bodies are never archived; their inventory digests are pinned.

- **Attempt-scoped events** are `claim_opened`, `phase_changed` (the native phase is the step id plus the Step Execution number, §19), `outcome_recorded`, `attempt_finished`, `attempt_released`, and `native.` step events: a Step Execution opened and ended, a Route taken, a refused entry and a Workflow End. The MVP's review and Publication-Gate events are not emitted.
- **`attempts list`** and **`attempts show`** keep their MVP shape and paging. The manifest kinds are `transcript`, `command_output` (Validation Evidence and test evidence), `record` (Step Artifacts, Execution Summaries, the rendered terminal comment and publication bodies) and `other`. The manifest never includes the Run Ledger database, workspaces, mirrors, caches, credentials or the Workflow Configuration.

### 4.6 Offline Read

Retained from the MVP unchanged: `agentctl --contract 1 --offline <operation>` reads a stopped Worker's volumes read-only, never takes the lock, reads the ledger through a private copy that includes any `-wal` file, serves `identity`, `status`, `command` and `attempts *`, answers mutations `unsupported`, and leaves the state directory unchanged.

**Rows:** CFG-38, CFG-42, REC-11, REC-27, OPC-7; and the retained MVP lifecycle rows (see the matrix).

## 5. Workflow Inputs and selection

### 5.1 Input type and Selection Label

- **One input type per Workflow:** `issue` or `pull-request` (#286 point 1). Type-specific operations used with the wrong type are rejected statically (§3).
- **Every Workflow names its own Selection Label,** for issues and pull requests alike. It is the scenario's entry point and a human's authorisation.
- **Authorisation is checked, not assumed.** At the Claim read-back the Worker reads the latest `labeled` event for the Selection Label and tests its actor according to the **identity mode** the Worker detected at startup (§18, #288 point 1):
  - **shared identity** (the Agent Identity is the Target Repository owner, [ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md)): only a `Bot`-type actor is refused. An actor equal to the Agent Identity is the owner and is accepted;
  - **dedicated identity:** an actor equal to the Agent Identity or a `Bot`-type account is refused (#286 point 3).

  The actor, the time and the identity mode (`shared-identity` under a shared identity) are recorded with the Attempt. No YAML key selects the mode.
- **The label stays on the input for the whole Attempt;** its presence is continuing authorisation. Its removal by a human is a withdrawal (§7.3). When the Attempt ends the Worker always removes it (§15.3), so further work needs a human to apply it again.

### 5.2 Mandatory guards

A candidate is an open input of the Workflow's type carrying its Selection Label. It is **excluded** when any of these holds; YAML cannot remove them:

- it is not open;
- the Selection Label is absent, or was applied by an actor the identity-mode test of §5.1 refuses (checked at the read-back);
- it has **any** assignee;
- it has an open native blocker (GitHub issue dependencies);
- **pull requests only:** its head branch is not in the Target Repository. Fork pull requests are never eligible in the first release.

Another Workflow's Claim in force on the input also excludes it (§6.1). Selection writes nothing: an excluded candidate is left untouched.

### 5.3 Filters and selector options

- **`require-all`, `require-any` and `exclude` narrow, never authorise.** The Worker never writes a filter label. Filters are evaluated at selection and at the Claim read-back, never at later Gates.
- **The MVP's fixed exclusions are YAML now:** `ready-for-human`, `wontfix` and `wayfinder:*` are excluded only when a Workflow lists them in `exclude`. A non-empty body is scenario readiness policy.
- **`drafts`** (`pull-request` only): `exclude` by default; `include` makes draft pull requests eligible. A draft/ready change during an Attempt is ignored.
- **`skip-open-agent-pr`** (`issue` only): skip an issue that has an open pull request **authored by the Agent Identity and bearing that issue's Attempt Marker**. A pull-request Workflow inherits no such exclusion, so a pull request an implementation Attempt opened stays eligible for review.

### 5.4 Ordering and priority

- **Order:** `(created_at, number)` ascending, for both types.
- **The priority command** (`next issue N`) passes the same guards and applies to the Worker's input type. It is advertised only by `issue` Workers (§19).

**Rows:** INP-4, INP-6, INP-8 … INP-12, INP-19, INP-20.

## 6. Claim and Attempt identity

### 6.1 Exclusivity and the Claim Comment

- **Exclusivity is global per input:** at most one open Attempt per issue or pull request in the Target Repository, whatever its Workflow. Scenarios on the same pull request run one after another.
- **The visible ownership record** is the Agent Identity's assignment **plus a Claim Comment** bearing an Attempt Marker with `kind=claim`, the Workflow id and the Attempt id. Workers may share one Agent Identity, so assignment alone cannot arbitrate. **Among Claim Comments in force, the lowest comment id wins.**
- A Claim Comment stops being in force when its Attempt's terminal comment is published (§15.3), or when recovery deletes it (§16.2).

### 6.2 The Claim sequence

1. **Record the Claim intent** in the Run Ledger, in the transaction of the Intake check (§4.3).
2. **Assign the Agent Identity** (an Effect Intent, §14).
3. **Post the Claim Comment** (an Effect Intent).
4. **Read the input back:** open; Selection Label present and authorised (§5.1); no other assignee; own Claim Comment winning; every guard and filter of §5.
5. **Pin** from the read-back (§7): write every needed prompt and every available activation's components and delivery bytes to the Attempt Archive, flushed and digest-addressed, and record the Effective Configuration core, its digest and the content-reference index — before the dependent ledger transaction. A crash before the archive is durable leaves no confirmed Claim depending on missing content; orphaned unreferenced archive files are permitted.
6. Record **`claim-confirmed`**. Only now does an Attempt exist.

A Worker whose read-back fails or loses deletes its own Claim Comment, removes the assignment **only if no other Claim Comment is in force**, and records the Claim `released`. It writes no terminal comment and changes no label. A `released` Claim is not a Terminal Outcome.

**Whose assignment it is.** The Run Ledger decides whether an assignment of the Agent Identity is the Worker's. Every assignment the Worker makes has a Claim intent in the ledger, so recovery can always release or finish it.

### 6.3 Attempt identity

- **An Attempt is identified by `(workflow-id, input number, n)`,** rendered `<workflow>#<N>/<n>`, for example `review#42/1`. Issues and pull requests share one GitHub number space, so the type is not part of the id.
- **`n` is counted per (Workflow, input)** from the Run Ledger: one more than the highest `n` recorded. Numbers are never reused, not even after a `released` Claim. After a ledger loss `n` is rebuilt from GitHub (§16.5).
- **The Workflow id appears** in every Attempt Marker, in the commit trailer and, for an issue input, in the Attempt branch `agent/<workflow>/<issue>/<n>-<slug>`.

### 6.4 Release

At the end of an Attempt the Worker removes the Selection Label and removes its assignment when no other Claim is in force (§15.3). A Claim orphaned by a permanently lost Worker volume is released by a human, who removes the assignment and the Claim Comment ([the ledger-loss runbook](../runbooks/ledger-loss.md)).

**Rows:** INP-1, INP-2, INP-5, INP-6, INP-13, REC-10.

## 7. Pinned input state, Gates, Drift and withdrawal

### 7.1 Issue input

- **Fingerprint:** the digest of title and body **taken from the Claim read-back**, the same content the model sees. No comments and no referenced specification.
- **Base Revision:** the base-branch tip at Claim. The workspace is checked out exactly at it (§9.10).

### 7.2 Pull-request input

The read-back pins the title/body digest, the head SHA, the base branch name and the base-branch tip.

- **Base Revision:** the merge base of the pinned head and that tip, so the change-versus-base reader shows exactly the pull request's own changes.
- **Input Head:** the pinned head, which the Attempt reviews or changes. The workspace is checked out at the Input Head; `publish-review` publishes against the current Input Head.
- **The Attempt's own compare-and-swap push** advances the Input Head and is not Drift (§10.4).

### 7.3 Gates, Drift and withdrawal

A **Gate** re-reads the input and the Attempt's limits. Gates sit **before every Step Execution and before every external write**. In addition, limits and SIGTERM are observed after every model response and every tool result inside an `agent` step (§11, §12).

**Drift** is checked at every Gate:

- an edit to the title or body;
- for a pull request, a head that differs from the Input Head (someone else pushed);
- for a pull request, a change of base branch.

New commits on the base branch are not Drift; they are only disclosed. **Drift ends the Attempt with the runtime-only Terminal Outcome `drift`** and runs no further step. A `stale-head` result covers only the race between the Gate and the write (§10.4, §10.6).

**Withdrawal** is checked at every Gate, including the Gate before every external write: the Selection Label removed by a human, the input closed, or a pull request merged. **A withdrawal ends the Attempt `withdrawn`.** A draft/ready change is ignored. A withdrawal after a publication landed does not undo it (§14.3).

Both endings run the finalisation of §15.

### 7.4 Attempt facts and the authority boundary

**The facts available to Workflow Steps** are: the Attempt id, the Workflow id, the input type and number; the title and body, framed as data in the Pinned Prefix; the Base Revision and the current revision; and for pull requests the base branch name, head branch name and Input Head. Comments, reviews, labels, author and CI status are not supplied. The changed-file list is read with `workspace-read`.

**Input content has no instruction or authority channel.** The graph, the instructions, the Skill Bundle and every authority come only from the Effective Configuration pinned at Claim. Files in the working tree, such as `AGENTS.md` or repository-local skills, are data the model may read. Issue and pull-request comments do not reach steps in the first release.

**Rows:** INP-1, INP-3, INP-6, INP-7, INP-14 … INP-18.

## 8. The Workflow graph

### 8.1 Entry and Routes

- **Exactly one entry:** the Workflow-level `start`. The start step may be re-entered like any other. A Workflow is left only through a Workflow End.
- **Every step has a `routes` map** from result name to target. After applying `defaults.routes`, coverage is exhaustive over every result the step can produce (§3); **a step's own Route wins over the default.**
- **A Route is selected only by the Step Result name.** There are no expressions, guards, payload predicates, Attempt-fact conditions or execution-count conditions. A scenario that needs a branch declares distinct result names. Payloads are data, never control.
- **The initial Workflow entry is fresh,** and every Route enters fresh unless it opts into Conversation Inheritance.
- **Conversation Inheritance** (`{step: <id>, inherit-conversation: true}`) hands the receiver the exact immediate source Step Execution's conversation, in the same live Attempt: the Attempt Header and Workflow Input first, then the source framing and original complete Exchange Units through the last whole unit in order, then a Worker-authored receiver entry frame. Only `true` opts in, and only between agent steps; scalar, omitted and explicit `false` forms enter fresh and canonicalise identically. The source history stays in memory (the redacted Transcript remains the audit record); the original provider-returned assistant objects and matching tool results travel without rewriting, and a pending incomplete unit is dropped as a whole. Where no eligible complete unit exists — including after an entry refusal, which creates no Step Execution — the receiver records `error` with a redacted reason before any model request and follows its effective Route.
- **Round Handoff annotations are step-local Route metadata.** An `unfinished` Workflow End Route for `pushed` or `no-changes` from an issue Workflow's `commit-and-push` step may carry `round-handoff: {kind: ai-assisted, note: <step>.<result>.<field>}` or `round-handoff: {kind: hard}`. `ai-assisted` requires the note to name a declared `document` field proven available on every path to the publishing step; `hard` carries no note. The annotation never appears on other results, step targets, other Workflow Ends or `defaults.routes`, and never in pull-request Workflows. While Round Handoff enablement is omitted the annotation validates but the Route keeps ordinary `unfinished` semantics. The special input `{optional: round-handoff.note}` is accepted only on possible first agent steps (found by following effective Routes from `start` through Runtime Operations); it is absent on a fresh Attempt.

### 8.2 Workflow Ends

A Route target `{end: <category>}` ends the Workflow. The YAML-selectable categories are `completed`, `blocked`, `unfinished` and `failed`; each is the Terminal Outcome of the Attempt it ends (§15.1). The runtime records the fact "the Workflow ended at declared end X after result R of step S", not a claim that the work succeeded. Zero successors means a Route to a Workflow End, not a separate construct.

### 8.3 Execution Ceilings and retries

- **Each step has an Execution Ceiling,** `max-executions`, default 1 (or `defaults.max-executions`).
- **A Route into a step that has reached its ceiling is refused:** no Step Execution is created and the sequence number does not advance; the runtime records a transition fact naming the step and the reason `execution-ceiling`; routing continues along **that step's** `limit-exceeded` Route.
- **Retry is a Route** back to the same step, bounded by its Execution Ceiling. There is no retry construct. Two mechanisms stay distinct and create no Step Execution: bounded correction of a malformed report inside one Step Execution (§9.5), and transient provider retries inside one request (§11.4).
- **Termination is guaranteed:** real Step Executions are bounded by the sum of Execution Ceilings, and every refusal chain is finite because the `limit-exceeded` graph is acyclic (§3). The Execution Ceiling is independent of budgets; no Route renews a budget (§11).

### 8.4 Runtime checks and defects

Deferred to run time: Execution Ceilings and limits; report validation, including `evidence-ref`; operation preconditions, which yield their catalog results; and the input-availability guard (a missing required input ends the consumer `error` without a model call).

**A result absent from the step's path map can arise only from a runtime defect.** It is not routed through `error`; it ends the Attempt `internal-failure` (§15.1). A `GraphRecursionError` is treated the same way. Like `recursion_limit` (§8.5), the per-Step-Execution tool-loop backstop is derived from the configured bounds (§11.1) and is only a backstop: it can fire only where the meters and the code-owned counts did not already end the step.

### 8.5 LangGraph mapping

The compiler maps the validated Workflow onto a LangGraph `StateGraph` with no generated code (#281 point 12):

- **Nodes:** one node per Workflow Step, its callable taken from a trusted registry by executor kind (`agent` / `operation`). The node first checks the Execution Ceiling; a refused entry creates no Step Execution and yields the refusal, which routes through the step's `limit-exceeded` Route.
- **Edges:** the only plain edge is `add_edge(START, <start>)`. Every step has exactly one `add_conditional_edges(step, router, path_map)`, whose keys are the step's result names after defaults and whose values are nodes. The router returns exactly one key, never a list or `Send`. Steps are never joined by `add_edge`, even when only one Route exists. `Send`, `Command(goto)` and `interrupt` are not used.
- **Ends:** each end category used gets a runtime-owned end node under a reserved name prefix, which records the ending and leads to `END`.
- **Persistence:** `checkpointer=None`. **`recursion_limit`** is derived from the sum of Execution Ceilings and is only a backstop (§8.4).

**Rows:** ROUTE-1 … ROUTE-11, CFG-40.

## 9. Agent steps

An `agent` step runs one conversation in the runtime-owned tool loop (#280).

### 9.1 Conversation and model

- **One conversation per Step Execution.** No message history crosses steps except through an inheriting Route (§8.1, §9.2); otherwise the shared Attempt workspace is the only implicit continuity. A Step Execution never opens a second main conversation: reaching the Context Threshold ends it (§9.9).
- **Several instructions in one step are one bundle in one conversation,** with no ordering or execution guarantee between them; a guaranteed sequence needs separate steps.
- **One Pinned Model per Attempt** (provider, model, endpoint, effort), used by every Step Execution and every Sub-agent. There is no fallback. A per-step `model` is rejected (§2.7).
- **The answering model is asserted from each response's own evidence** ([ADR 0029](../adr/0029-the-answering-model-is-asserted-from-each-responses-evidence.md)): a direct pin's response model must equal the pinned model id exactly. Both current pins echo the pinned id with no snapshot suffix; when a vendor resolves the pinned name to a snapshot, the operator pins the name the vendor reports. A response that reports no model carries no evidence either way and is not a violation; any reported difference is a pin violation (§12).
- **The reported effort is asserted from each response's own evidence** ([ADR 0029](../adr/0029-the-answering-model-is-asserted-from-each-responses-evidence.md), as amended by #474): a response that reports the effort it ran at (`reasoning_effort` in the response metadata) must report the pinned effort exactly — string equality, no normalisation. A mismatch is a pin violation with the same reaction (§12): the response is discarded, its usage charged, the Attempt ends `interrupted`, and the Worker halts. A response that reports no effort is not a violation, even where the probe reported one.
- **A gateway pin's response is from the Pinned Model only when the `x-litellm-model-group` header equals the pinned name and `x-litellm-attempted-fallbacks` is `0`.** Any deployment the gateway spreads that name over counts as the Pinned Model. `x-litellm-model-id`, `x-litellm-model-name`, the call id and the observed `x-litellm-version` are recorded, never asserted; the body `model` is never read for identity. A successful response with either identity header missing fails closed, as does a model group that does not match, attempted fallbacks above `0`, or attempted retries above `0` (a missing retry header means `0`; a present-but-unparseable count fails closed). An error response without identity headers is not a violation — it is classified by its status as today — but retry or fallback evidence above `0` on an error is still a violation.
- **Every gateway request carries the per-request suppression: `disable_fallbacks: true` and `num_retries: 0`.** The Worker's own provider retry is then the only retry, and each of its tries passes Admission and counts against the Attempt Ceiling. The operator should additionally configure the gateway so a pinned name has no fallbacks or retries, and no key should set `metadata.disable_fallbacks: false` — a recommendation, not a guarantee: the assertion above holds whatever the gateway is configured to do. The Worker never reads the gateway's configuration.
- **Stated assumptions (issue #337).** Until real recordings replace them: the header names above are assumed; the identity check covers successful responses only; retry or fallback evidence on an error is still a violation; the suppression fields are assumed honoured — a gateway that ignores them is caught by the evidence, not by the request.
- **Step Execution identity:** (Attempt, step id, sequence number from 1). Its result, artifacts, execution facts and usage are immutable once it ends.
- **Within a conversation,** every assistant turn is carried back verbatim ([ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md)), and Exchange Units stay whole.

### 9.2 The opening frame

The Worker-authored **Pinned Prefix** ([ADR 0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md)) is:

1. **the generic Attempt Header:** the Attempt facts of §7.4, and an answer to every reference an Attempt cannot satisfy (for example a skill's reference to a user it cannot ask);
2. **the input,** its title and body framed as data;
3. **the step's declared inputs** (§9.7);
4. **the step's Step Result contract:** the declared result names and payload fields, and how to report one (§9.5).

The runtime no longer enumerates Acceptance Criteria or a Seam Set. Scenario facts reach a step only as declared inputs or as text the scenario's skill reads from the input (#285 point 6).

**An inheriting entry** (§8.1) opens with the Attempt Header (1) and the input (2) as above, then the source framing and the source's original complete Exchange Units through the last whole unit — the original provider-returned assistant objects and all matching tool results, verbatim, in order — then a Worker-authored receiver entry frame that identifies the preceding content as prior work and makes the receiver's declared inputs, Step Result contract and instructions current. Historical calls and results are model input under the receiver's own tools and contract: the receiver binds its own tools in declared order, validates reports against its own contract, accounts spend to its own Step Execution and starts a fresh Conversation Context anchor, and its guard and Admission evaluate the fully formed inherited opening request.

### 9.3 Instructions

- **`instructions` are activated by the runtime at conversation open,** in the declared order: each skill body or prompt file's bytes verbatim, as an ordinary Exchange Unit exempt from result capping ([ADR 0007](../adr/0007-upstream-skill-text-is-answered-never-rewritten.md)).
- **One immutable registry serves every activation.** After deployment verification succeeds, the Worker builds one immutable in-memory registry of exactly the declared, verified members — each with its name, provenance, inventory, selected review, `SKILL.md` bytes, ordered `inject` companion bytes, notices and the deterministic delivery under a versioned framing — from the same bytes that passed verification. One lookup and one delivery path serve static instructions (keeping their position among prompt instructions) and generic on-demand activation: the complete `SKILL.md`, then each `inject` companion in policy order, with unavailable notices framed outside the upstream bytes, which are never rewritten ([ADR 0007](../adr/0007-upstream-skill-text-is-answered-never-rewritten.md)). The registry outlives on-disk changes and is never reloaded; adopting edits requires a restart that passes full validation.
- **The whole declared bundle stays available on demand** in every `agent` step through the skill-activation tool, including members no static instruction names: indirect activation needs no static reference. A Companion File reaches the model whole with its skill's activation, or not at all: `not-delivered` companions are omitted from every delivery and every replay, with no skill-resource reader and no implicit network fetch ([ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md)). That guarantee is exactly what it claims to be: generic tools or project code may still read a `not-delivered` file, and the documentation states so honestly. There is process and filesystem isolation work the runtime does not do ([ADR 0018](../adr/0018-runtime-owned-validation-authenticates-the-invocation-not-the-code.md), [ADR 0025](../adr/0025-withholding-a-shell-only-disguises-execution.md)).
- **Unknown names are typed refusals, not file reads.** Unknown, undeclared, absolute, traversal, path-shaped or differently-cased names are refused as typed normal outcomes with no filesystem, store or network access, no fallback read and no authority change; repeated and indirect activations keep the existing duplicate-acknowledgement behaviour. Upstream content that happens to begin with `Error:` is content, so typed outcomes — never string prefixes — signal refusal.
- **Instructions grant no authority.** Activating a skill binds no tool, runs no command, activates no dependency and triggers no Runtime Operation; in particular, activating `code-review` attributes no Review, pushes nothing and opens no gate (#279). Sub-agents keep their restricted toolset and never hand off.

### 9.4 Tools

Tools are declared per step, **deny by default**, from five groups:

| Group | Tools |
| --- | --- |
| `workspace-read` | File reading, listing and search; the change-versus-base reader (commit list `base..HEAD` plus the diff against the Base Revision over the working tree including untracked, non-ignored files, bounded with an explicit total and the full text behind the capped-result reader) |
| `workspace-write` | File writing and editing |
| `shell` | A credential-free command runner in the Project Profile's working directory, with a process-group kill on a code-owned timeout, the Execution Footprint and the Ref Guard. **Never evidence** ([ADR 0025](../adr/0025-withholding-a-shell-only-disguises-execution.md)) |
| `tests` | The targeted-test tool, which records evidence (§13.4), and the diagnostic check tool, bound only when the Validation Contract declares checks; a check result is diagnostic, never evidence |
| `subagents` | Sub-agents with their fixed read-only tools (§9.8) |

- **Always present and not declarable:** the Step Result report tool, skill activation and the capped-result reader.
- **No tool mutates GitHub,** and there are no GitHub read tools in the first release.
- **Commits, the branch and HEAD are the runtime's.** A moved branch or HEAD is restored by the Ref Guard; only `commit-and-push` commits (§10.4).

### 9.5 Reporting a Step Result

- **One tool call reports the Step Result** and is the agent's only exit: a declared result name with its payload.
- **A malformed report** (an undeclared name, a payload violating its schema, an `evidence-ref` that does not hold) returns to the model as a tool error with a description, a bounded number of times; then the Step Execution ends `error`.
- **A response with no report,** including a response truncated at the output cap, takes the same bounded-correction path and then ends `error` (#280, #282 point 9). A response cut short by the context window is not an output-cap truncation and never takes bounded correction: it is a provider Context Overflow (§9.6).
- **`evidence-ref` validation:** each referenced test must have run through the `tests` group's evidence-recording tool and passed **on the current tree** (§13.4). The runtime does not judge whether tests establish a criterion; criteria and coverage are scenario policy.
- **A Step Result is never evidence** of what executed.

### 9.6 Step Artifacts and endings without a result

- **Step Artifacts** are created only from payload fields typed `document`: named, immutable, redacted, stored outside the Target Repository tree, in the Attempt Archive. There is no artifact-writing tool.
- **A Step Execution ending `error` or `limit-exceeded` has no Step Result and no Step Artifact.** Its execution facts (Transcript, evidence, Execution Footprint, usage) are kept, and the workspace is never rolled back.
- **A provider failure after its retries ends the Step Execution `error`** (#570): a `ModelError` that is non-retryable, or a retryable one whose three tries are exhausted, ends the Step Execution `error`, routed by the Workflow like any other `error`. The detail and the Execution Summary name the provider error class and the HTTP status where there is one, redacted with the Attempt's redaction registry; the Summary carries the durable `provider_failure` marker (error class, status, context-overflow flag) the poll loop reads for §4.4. The failed request's hold settles charged at its worst case (§11.4). Auth (401), permission (403) and not-found (404) errors take this same path, with no immediate halt and no new Terminal Outcome reason. A pin violation is classified before all of this and never becomes `error` (§12).
- **A provider Context Overflow is a classified, expected path, not a retryable failure** (#563): two signals, one classification — (a) a refused request (`ContextOverflowError` through LangChain on every route, OpenAI's separate input ceiling included); (b) a successful response cut short because the request and its response did not fit (Anthropic `model_context_window_exceeded`). (b) is not an output-cap truncation and skips bounded correction (§9.5). The Step Execution ends `error`, routed by the Workflow; there is no provider retry, and the end is never `limit-exceeded`. The detail and the Execution Summary name the Context Overflow, the signal (`refused` or `cut-short`), the Conversation Context sent (value and basis) and the declared Model Context Window. The Summary's `provider_failure` marker carries the error class, the status, `context_overflow: true`, the signal, the sent Conversation Context and the Model Context Window; it never counts toward the halt (§4.4). A refused request settles at its hold's worst case, recorded as `charged` with basis `upper-bound`; a cut-short response settles with its usage (§11.4). The end is never `internal-failure`, never a Worker halt, and carries no `attention_required`.

### 9.7 Inputs and the Execution Summary

- **Declared inputs only:** the Attempt facts of §7.4, and payload fields, Step Artifacts or Execution Summaries of the latest Step Execution of a named step (§2.5). An absent optional input is stated as absent.
- **A required input missing at run time** — which static proof makes a runtime defect — ends the consumer `error` without a model call.
- **The Execution Summary** (`<step>.facts`) of a Step Execution is runtime-composed, redacted and size-capped (a code-owned cap). It holds the changed files, the commands and their results, revision-bound evidence, the limit crossed and the spend, the provider failure where the Step Execution ended `error` on one (§9.6). It never includes the Transcript or model message content. It exists for every Step Execution, including one ending `limit-exceeded` or `error`.

### 9.8 Sub-agents

The MVP's Sub-agent mechanics are retained ([ADR 0020](../adr/0020-one-sub-agent-per-call-consecutive-calls-run-as-one-concurrent-group.md), [ADR 0024](../adr/0024-a-stopped-sub-agent-discards-its-in-flight-request.md)), without review attribution.

- **One call starts one Sub-agent:** a separate, read-only conversation answered by the Pinned Model, with its own message list and no caller history. Each unbroken run of consecutive spawn calls in one assistant turn is one group; every other call is a barrier.
- **The opening** is the Worker-authored framing followed by the caller's `instructions` verbatim: no Pinned Prefix and no caller history.
- **The bound set is exactly** the file reading, listing, search and line tools, the change-versus-base reader, the capped-result reader and the Sub-agent report tool. A Sub-agent binds no write tool, no shell, no test tool, no activation tool, no step report and no spawn of its own.
- **Input validation, the 8-round bound, the 32 KiB report rule and the result to the caller** are as in the MVP: `description` required, non-blank, at most 120 characters; `instructions` required, non-blank, at most 64 KiB; `reported`, `no-report` / `silent` and `no-report` / `round-limit` endings. A refused model request, a settled spend that crossed a limit, or a tool-call batch that crossed a tool-call limit ends the Sub-agent `no-report` / `ceiling`.
- **A Sub-agent model failure ends the Sub-agent, never the caller** (#570): a non-retryable or exhausted `ModelError` in a Sub-agent's request ends that Sub-agent `no-report` / `provider-failure`; a provider Context Overflow — a refused request or a response cut short by the window — ends it `no-report` / `context-window` (no new ending), the same ending as the context guard, with the cause recorded (guard vs Context Overflow, with the `refused` / `cut-short` signal). The caller gets the ending as its `spawn_subagent` tool result and its Step Execution continues; other Sub-agents in the group, running or queued, are unaffected. A Sub-agent provider failure never counts toward the halt-after-three by itself (§4.4) — if the provider is really down, the caller's own next request fails and ends the Step Execution `error`, and that is what counts.
- **Concurrency:** within a group at most `max-parallel-subagents` run at once; the rest queue in call order and are never refused. Results join in call order. Ledger and Transcript writes from concurrent Sub-agents never interleave inside a record.
- **Spend:** each Sub-agent request is admitted individually (§11.4) and its spend — every model response and every billable tool call it requests, except its `report_subagent` exit — counts against the calling Step Execution, its step's allowance and the Attempt (§11.2, LIM-13). Each billable tool-call batch is charged before any call in the batch runs; a batch crossing a tool-call limit is not executed, ends that Sub-agent `no-report` / `ceiling`, and, once the group has settled, ends the calling Step Execution `limit-exceeded` (moment `tool-call`).
- **A Sub-agent never hands off;** one whose next request would exceed the usable context ends `no-report` / `context-window`. The guard compares the Sub-agent's own Conversation Context ([ADR 0031](../adr/0031-the-conversation-context-is-anchored-on-provider-reported-input.md)): the `input_tokens` of its most recent request that reported usage plus `bytes/4` of the messages appended since (basis `anchored`), or, with no measured response yet, `bytes/4` of the whole request (basis `estimated`). A response without usage never moves the anchor. Every one of the Sub-agent's requests compares the same measure for its Admission hold (§11.4).
- **The Run Ledger** records `subagent_queued`, `subagent_started` and `subagent_ended`, each with the Attempt id, the Step Execution and the call id; `subagent_ended` also carries the ending (`reported`, `silent`, `round-limit`, `ceiling`, `context-window`, `stopped` or `provider-failure`, plus `refused` where the spawn group's Admission refused the member before it ran and `pin-violation` where the member's request violated the pin), the member's wall `duration_seconds` (`0.0` where it never ran), the member's own usage cell in the per-origin cell shape (§11.1) — always present, possibly all zero — and, where the member compared at least one request, the peak Conversation Context (`conversation_context_peak`) over its requests; the redacted provider error class and status for a `provider-failure` ending; and — for a `context-window` ending — the compared Conversation Context, the Usable Threshold in force, and the cause (`guard` vs Context Overflow with its `refused` / `cut-short` signal) (§13.2).

### 9.9 Context Threshold

The main conversation of a Step Execution never outgrows the pin's Context Threshold ([ADR 0033](../adr/0033-crossing-the-context-threshold-ends-the-step-execution.md)):

- Before a request would exceed the Usable Threshold — the Context Threshold (`context-threshold-tokens`) minus the safety margin minus the request overhead — the request is not sent. The decision compares the main conversation's Conversation Context ([ADR 0031](../adr/0031-the-conversation-context-is-anchored-on-provider-reported-input.md)) against the Usable Threshold, for both bases.
- **The Step Execution ends `limit-exceeded`** with the limit fact `{"scope": "step", "reason": "context-threshold"}` — which also carries the crossing `conversation_context` and the `usable_threshold` in force — routed by the Workflow's `limit-exceeded` Route like any other limit — for example to an AI-assisted Handoff step (§11.3). The Execution Summary carries the same limit record. There is no in-step continuation: the runtime never opens a fresh conversation inside a Step Execution and never asks the model to summarise its own work.
- **An agent step may carry its own `context-thresholds` policy** (required `hard-tokens`). On such a step the Hard Usable Threshold — `hard-tokens` minus the code-owned safety margin and request overhead — replaces the Pinned Model's guard. The fully formed opening request is already the first compared request, and every later request is compared the same way: one Conversation Context value is measured once for the guard and reused for the Admission hold, the retry admissions and the recorded `model_response` event. Equality passes — only a request strictly over the Hard Usable Threshold selects the Hard Handoff. The crossing request is never sent — no Admission hold is taken and no provider call runs — and the Step Execution reports `hard-context-threshold` with the crossing `conversation_context`, its basis and the Hard Usable Threshold in the `hard_context_threshold` Run Ledger event and the Execution Summary limit record, routed by the step-local hard Route to the model-free Hard Handoff. Steps that omit the policy keep the Pinned Model's guard and existing outcomes. An accepted Step Result ends the step without another request. Admission refusals, Sub-agents (which keep the pin guard), provider Context Overflow and Attempt-level failures keep their separate behavior.
- **A policy may add the Soft Context Threshold** (optional `soft-tokens`, strictly between the safety margin plus request overhead and `hard-tokens`). The Soft Usable Threshold — `soft-tokens` minus the same margin and overhead — is evaluated after the hard guard on the opening and on every post-tool main-conversation request, for both Conversation Context bases. Equality passes at each bound: a request at or below the soft bound sends normally; a request strictly over the Soft Usable Threshold but at or below the hard one is never sent — no Admission hold is taken and no provider call runs — and the Step Execution reports `soft-context-threshold` onto the step-local inheriting Route. A request crossing both bounds selects the Hard Handoff: the hard guard keeps precedence. An accepted Step Result ends the step before another request is compared, so a report that fits never trips either bound. The soft outcome carries the source's complete Exchange Units and opening tail into the Reserved receiver, which runs its own conversation from that inherited history: its full inherited request is admitted against its own remaining Step Allowance and the Attempt Ceiling after the Work Pool is exhausted, keeping the Attempt deadline, the pin's full output cap, cumulative per-origin charging and bounded retries with report correction. The receiver's own hard guard, Admission refusals, provider Context Overflow, invalid reports and reserve or Attempt Ceiling exhaustion end it through its hard, `error` and `limit-exceeded` Routes — every branch model-free through Runtime Operations to a Workflow End — and Hard Handoff makes zero further model requests, including after the deadline. The successful receiver produces its declared note under its own Step Execution.
- A Sub-agent that reaches the Usable Threshold ends without a report (`context-window`, §9.8).
- A Pinned Prefix that cannot fit with its overhead is refused before any model work, ending the Step Execution `error`.

An oversized tool result is capped to its head, its tail and a pointer to the stored artifact and its full size; activation results are exempt.

**AI-assisted Handoff and Hard Handoff are not runtime concepts.** They are scenario names for configured steps (§11.3).

### 9.10 Workspace

Every Attempt builds its workspace deterministically from the local mirror: `reset --hard` to its start commit, then `clean -fdx`. The start commit is the Base Revision for an issue input and the Input Head for a pull-request input (§7). An issue Attempt works on its own branch `agent/<workflow>/<issue>/<n>-<slug>`; a pull-request Attempt works on the pull request's head branch. The workspace is kept while any Attempt is unfinished.

**Rows:** STEP-1 … STEP-16, STEP-18, STEP-19.

## 10. Runtime Operations

A Runtime Operation is deterministic code with no model call. Its results are runtime-authored and fixed by the catalog; YAML only routes them. Every operation may also end `error` (an operational failure, a refused permission, a timeout) and receives `limit-exceeded` only as an Execution Ceiling refusal. Operations are bounded by code-owned timeouts, never by `limits` (§11.6).

### 10.1 Catalog and preconditions

The first-release catalog is closed (#285 point 7):

| Operation | Input type | Purpose | Results |
| --- | --- | --- | --- |
| `check-readiness` | both | Project Profile, toolchain, services, then `bootstrap` | `ready` / `not-ready` |
| `run-validation` | both | The pinned Validation Contract on the current tree | `passed` / `failed` |
| `commit-and-push` | both | Persist the workspace as a commit and push it | `pushed` / `no-changes`, and `stale-head` in a `pull-request` Workflow |
| `open-pull-request` | `issue` | Open a pull request from the pushed revision | `opened` / `already-open` |
| `publish-review` | `pull-request` | Publish a review against the Input Head | `published` / `stale-head` |
| `post-comment` | both | Post a comment on the input | `posted` |

An operation with `with.requires` can also yield `requirement-unmet`.

**Preconditions are factual; policy is the Workflow's.** Wherever an operation is placed, it enforces authorisation (an active Claim; no withdrawal or Drift, checked at the Gate before its write) and that it publishes exactly what it names: the pushed revision must equal a clean committed tree, and a reviewed or pushed-to head must still be the Input Head.

**`requires`** is a declarative list of evidence that must hold **for the revision being published**. The runtime checks it before the write and yields `requirement-unmet` when it does not hold, so graph ordering cannot bypass scenario policy. The only requirable entry is `run-validation: passed`: a `passed` result of `run-validation` bound to the revision being published (§13.4). **Evidence from the base tree never satisfies `requires`,** because it is bound to a different tree: the base tree's identity is never the pre-commit identity of a commit that contains changes, so base-tree evidence is never inherited by `commit-and-push` (§10.4).

Every write an operation performs follows §14.

### 10.2 `check-readiness`

It reads the Project Profile at the start commit, checks the toolchain against the Supported Toolchain Matrix and the declared services for reachability, and then runs `bootstrap` (#285 point 6).

- **`not-ready`:** the profile is missing, does not parse, violates the schema, or the environment cannot satisfy it (a toolchain outside the matrix, an unknown `schema` version, an unreachable service). Its facts name exactly what is missing or wrong.
- **`error`:** `bootstrap` fails.
- **`ready`:** otherwise.

There is no baseline operation. A Workflow that wants a green baseline places `run-validation` before its first change (for example a `baseline` step routing `failed` to `{end: blocked}`).

**The Project Profile** has two sources ([ADR 0030](../adr/0030-the-workflow-configuration-may-supply-the-project-profile.md)): `docs/agents/project-profile.yml` in the Target Repository where `target.profile` is absent, or the instance-supplied file the Workflow Configuration names where it is present — in which case the repository's own profile is never read for readiness, validation or final checks, even when it exists. Either way the profile keeps the MVP's `schema: 2` unchanged: `language`, `working_directory`, `toolchain`, `commands` (`bootstrap`, `test_all`, `test_targeted`), `evidence` (`junit-xml` only; required for the test commands, optional for checks), `checks` (a list of named commands, or the scalar `none`; an absent key is `not-ready`; an empty list is rejected) and `services` (`{name, url_env}`, verified reachable only). There is no prose fallback. `{path}` is substituted with a repository-relative test path and `{evidence_dir}` with a per-Attempt directory the runtime alone creates and writes.

**The Validation Contract is the profile as it stands at the start commit, pinned when `check-readiness` (or the first `run-validation`) reads it.** An Attempt that edits the profile changes the contract for later Attempts, never for its own. Where the deployment supplies the contract it is pinned at Claim with the rest of the Effective Configuration instead: a candidate edit of the repository's `docs/agents/project-profile.yml` then changes no contract and is never reported as a contract change.

### 10.3 `run-validation`

It runs the pinned Validation Contract (`test_all` and every check) on the current tree and yields `passed` or `failed`, recording **Validation Evidence** bound to the tree's revision identity (§13.4): the commands, exit codes, executed test counts and failure identifiers ([ADR 0004](../adr/0004-failure-identifiers-as-evidence.md)). Failed means a non-zero exit or any reported test failure.

**Validation trust boundary** ([ADR 0018](../adr/0018-runtime-owned-validation-authenticates-the-invocation-not-the-code.md), retained). The runtime guarantees that the commands come from the pinned Validation Contract, never from the model; that it invokes them itself with credentials stripped from their environment, kills their whole process group on timeout, and parses exit codes and JUnit results itself. It does not guarantee that executed candidate code was honest, that evidence files and the Run Ledger are intact against such code, or that the code had no network effects. "Passed" is an authentic runtime report of executing the candidate, not tamper-resistant proof; human review remains the last trust boundary.

### 10.4 `commit-and-push`

- **Commit:** the runtime stages the workspace as `git add -A` would, commits with the Agent Identity as author and committer and the trailer naming the Attempt id, and records the revision. A clean workspace with nothing to push yields `no-changes`. A commit created from a dirty tree inherits the validation result recorded for that tree's pre-commit (dirty) identity, so `requires` checked against the new revision sees the validation of the content the commit was made from; the binding is recorded once the commit exists, whether or not the push then lands. A later `run-validation` on the commit itself overrides the inherited binding.
- **Issue input:** pushes the Attempt branch `agent/<workflow>/<issue>/<n>-<slug>`, **fast-forward only, never forced**.
- **Pull-request input:** pushes to the pull request's head branch as a **compare-and-swap against the current Input Head**. A landed push advances the Input Head. A remote head that moved yields **`stale-head`**; nothing is ever force-pushed.
- **`pushed`** carries the pushed revision.
- **Write scope stops short of Workflows:** the runtime refuses to push a commit touching `.github/workflows/`, and the operation ends `error`. The platform's rejection is the second line.
- **A remote branch moved by someone else** on an issue Attempt branch is never overwritten; the push is not performed and the operation ends `error`.

### 10.5 `open-pull-request`

`issue` Workflows only. It opens a pull request from the pushed revision of the Attempt branch to the configured base branch.

- **Title:** a fixed runtime template over the Target Issue title. Neither YAML nor the model sets it.
- **Body:** the named `document` field (`with.body`), rendered as a publication body (§14.4) whose provenance footer carries the Base Revision.
- **Draft:** per `with.draft`, default ready.
- **Closing keyword** (#288 point 4): a ready pull request (`with.draft: false`, the default) carries `Closes #<issue>`; a draft carries the non-closing `Part of #<issue>`. There is no parameter for it.
- **`already-open`:** the Attempt's pull request already exists and is open.

### 10.6 `publish-review`

`pull-request` Workflows only. It publishes a review through the GitHub review API with the **COMMENT** event, against the current Input Head, with the named `document` field as its body, rendered as a publication body (§14.4) whose provenance footer carries the reviewed Input Head. A head that moved between the Gate and the write yields **`stale-head`** and the review is recorded `not-performed`. Approve and request-changes are not in the catalog.

### 10.7 `post-comment`

It posts one comment on the input issue or pull request, with the named `document` field as its body, rendered as a publication body (§14.4) whose provenance footer carries no revision. There is no facts-mode comment: the terminal comment reports the Attempt facts (§15.3).

**Rows:** OP-1 … OP-20, STEP-15.

## 11. Limits

### 11.1 Meters

Four dimensions are metered (#282): **cost** (USD, per token bucket from the pinned prices), **effective tokens** (cache reads excluded; the limit key is `effective-tokens`), **tool calls** and **wall clock**. There is no model-request counter; repetition is bounded by Execution Ceilings (§8.3). The context window is not a budget; reaching the Context Threshold ends the Step Execution `limit-exceeded` (§9.9). Usage is recorded in the Run Ledger as each response arrives.

**Spend is carried per Usage Origin** (`main`, `subagent`) in per-bucket cells. One cell holds the four bucket token counts (input, cache read, cache write, output), the four bucket costs, the number of responses, the number of responses without usage, and Effective Tokens (stored, because the `max(0, input − cache read)` clamp is per response); raw tokens, total cost and cache-hit rate are derived. The meter keys cells by step × origin, accumulating over all of a step's Step Executions; the Attempt is the sum of all cells, and a Step Execution's own spend is the step meter minus its opening baseline, per origin. There are no per-Sub-agent cells. A `charge_hold` worst-case charge goes to the origin of the request, booked in the input bucket and counted in that origin's responses without usage; a request's origin is always known, and provider retries count under the origin of the request they retry. **The response count is a measurement, not a meter:** §11.1's "no model-request counter" still holds for limits — the count is reported, never enforced. **Tool calls split per origin:** a Sub-agent's own tool calls are `subagent`, and the caller's `spawn_subagent` call is `main`. Enforcement is unchanged: the four meters count all origins against the step and the Attempt (§11.2).

**The tool loop inside one Step Execution has no model-turn meter either.** Its only bound besides the four meters is a code-owned iteration backstop, derived once per Step Execution — never configured, never routed as `limit-exceeded`: the step's remaining `tool-calls` room (the minimum over its Step Allowance, the Work Pool or reserve, and the Attempt Ceiling) plus the bounded-correction count (§9.5) plus the final report plus a small slack. Every iteration charges at least one billable tool call, uses a correction, or returns, so the meters and the code-owned counts always end the step first; the backstop can fire only on a runtime defect. When it fires the step ends `error`, and the detail and the Execution Summary name the backstop and the value it derived to.

### 11.2 Attempt Ceiling and Step Allowances

- **The Attempt Ceiling** (Workflow `limits`) is mandatory and declares all four meters.
- **A Step Allowance** (step `limits`) is optional, on `agent` steps only, any subset of the meters. It **accumulates over all Step Executions of that step** within the Attempt; nothing renews it on re-entry.
- **Sub-agent spend and provider retries** count against the calling step and the Attempt.
- **Step wall clock** is the sum of the durations of the step's Step Executions, including tools and Sub-agents.
- **Every crossing routes as `limit-exceeded`:** a Step Allowance, the Work Pool, a reserve, the Attempt Ceiling or an Execution Ceiling refusal. The reason changes the recorded fact, never the routing. There is no separate handler construct.

### 11.3 Reserved Steps and the Work Pool

- **A Reserved Step** (`reserved: true`) declares a complete Step Allowance in all four meters, **carved out of** the Attempt Ceiling.
- **Work Pool** = Attempt Ceiling − Σ Reserved Step allowances, per meter. All non-reserved steps, with their Sub-agents, share only the Work Pool.
- **A Reserved Step draws only on its own allowance,** even while the Work Pool has headroom. A re-entry gets only what remains of it.
- **Wall clock:** non-reserved model work stops at the Attempt deadline minus the reserved minutes.
- **With no Reserved Step,** an exhausted Work Pool refuses every `agent` step (`limit-exceeded`), leaving only Runtime Operations. **A Reserved Step that exhausts its allowance** ends `limit-exceeded`; acyclicity forces that Route towards a deterministic end.
- **AI-assisted Handoff** (typically a Reserved Step) and **Hard Handoff** (Runtime Operations and/or a Workflow End) are scenario names, not runtime concepts. The MVP's 70% progress gate and 80% verification reserve are not part of this runtime; a scenario expresses that intent with a smaller Step Allowance and a `limit-exceeded` Route to a Reserved Step.

### 11.4 Admission

**Before every model request** — provider retries and Sub-agent requests included:

1. the runtime computes an upper bound: the whole Conversation Context ([ADR 0031](../adr/0031-the-conversation-context-is-anchored-on-provider-reported-input.md)) priced as uncached input, plus the request's output cap. The measure is the same one every context decision compares — anchored where a measured response anchors the conversation, else the whole-request estimate — and no hold prices `bytes/4` of the whole request directly. Pricing stays the strict worst case: a gateway pin prices the measure at max(`input`, `cache-write`), with no cache discount; direct pins price it at the plain `input` rate;
2. if the bound exceeds what remains in the applicable Step Allowance, Work Pool or reserve, or the Attempt Ceiling, the request is not sent and the Step Execution ends `limit-exceeded` (moment `before-request`);
3. the bound is **held** while the request is in flight, so concurrent Sub-agents cannot both pass on the same headroom, and is settled with the actual usage on response;
4. a response without usage settles at its hold's worst case, whether it succeeded or failed; a refused Context Overflow settles at its hold's worst case, recorded as `charged` with basis `upper-bound`, while a response cut short by the window settles with its usage.

**Without usage** means the raw response usage carries no `input_tokens` or no `output_tokens`, or either field is not a non-negative integer — judged from the raw response usage, never from a normalised view that may turn an absent field into `0`. A response that settles at its worst case is recorded as `charged`, and the Step Execution continues: missing usage is not a pin violation. On a direct pin, absent cache-read or cache-write detail fields count as `0`; reasoning tokens are part of the output count and price at the `output` rate.

**A gateway pin is metered truthfully.** The Worker's own meters are the only cost authority: reported usage × the pin's configured prices. The `x-litellm-response-cost` response header is never read, not even as evidence. When a gateway response carries no `cache_write_tokens` field, the uncached remainder (`input_tokens` − cache reads) prices at max(`input`, `cache-write`): token buckets stay as reported, so effective tokens stay true, and only the remainder's dollar amount uses the max rate. A present field, even with value `0`, is used as reported — an operator whose upstream bills no cache-write premium sets `cache-write` = `input` to make the price exact. The Admission upper bound for a gateway pin prices its context estimate at the same max rate; direct pins price the estimate at the plain `input` rate, unchanged. Each gateway response with an unreported cache write increments the Attempt's durable counter — the ledger marker the Status Snapshot reads.

**Accepted limits.** The Worker cannot tell measured counts from LiteLLM estimates, nor a proxy cache hit from a fresh answer: both report through the same usage fields. A cache hit that reports the original usage over-charges.

**The output cap** (`max_tokens`) comes from the pin itself — every pin, direct or gateway, declares `output-cap-tokens` — is sent with every request and is part of the Effective Configuration digest. A response truncated at the cap is an ordinary response (§9.5). Holds and their settlements are recorded durably (§13.2). `agent provider-check --config <file> <pin>` reads the pin's prices and output cap from the file and sends its probe with `max_tokens` set to that cap, so an output cap above the model's maximum is refused there.

**Per-step maxima for the token and cost floors** (§3): one step's largest admissible request is its soft Usable Threshold where the step configures soft, else its hard or pin-default Usable Threshold — raised to the pin-default Usable Threshold where the step can start Sub-agents, because the member guard still compares the pin-default — plus the pin's full output cap. The Attempt Ceiling is checked against the largest per-step maximum, the Work Pool against the largest over non-reserved steps, and each Step Allowance against its own step's maximum; the four-meter allocation checks are retained.

**The Status Snapshot** reports a figure that includes a worst-case charge — or a max-priced unreported cache write — as **`upper-bound`**: the figure keeps its value — a ceiling on the true spend — and carries the basis with a reason. Figures the Attempt has not produced yet still read as `unavailable`, never zero; `unavailable` is never produced for a worst-case charge.

### 11.5 Tool calls and wall clock

- **Tool calls are counted when the model requests them — Sub-agent batches included, each batch charged before it runs.** A call beyond the limit is not executed, and the Step Execution ends `limit-exceeded` (moment `tool-call`).
- **Every command and model-request timeout** is min(code-owned timeout, remaining step / pool / reserve time). On expiry the command's whole process group is killed, or the request abandoned and charged per §11.4 (moment `deadline`).

### 11.6 After the Attempt Ceiling

Once the full Attempt Ceiling is spent, no further model request or tool call is admitted, but **the graph keeps routing**: `agent` steps are refused (`limit-exceeded`, moment `entry-refusal`), and `operation` steps still execute under their own code-owned timeouts, so a YAML Hard Handoff runs. Attempt wall clock is measured from the Attempt's opening, includes operation time, and once exceeded bars model work, not cleanup. Only process interruption (SIGTERM included) and internal failure stop the runtime outside the graph (§12).

### 11.7 The limit fact and precedence

A `limit-exceeded` fact records the **scope** (step, Work Pool, reserve, Attempt Ceiling, Execution Ceiling), every **dimension** crossed, the **limit**, the **spend** and the **moment** (`before-request`, `tool-call`, `deadline`, `entry-refusal`). Reaching the Context Threshold is not a meter crossing: it records the step-scope fact `{"scope": "step", "reason": "context-threshold"}` with no dimension (§9.9). A step-policy hard or soft crossing is likewise not a meter crossing: the Step Execution reports the routed Step Result with the crossing Conversation Context and the Usable Threshold in its Execution Summary limit record. The **primary reason** follows: process interruption > Attempt Ceiling > Work Pool / reserve > Step Allowance > Execution Ceiling. The Terminal Outcome records the primary fact when one occurred (§15.1).

**Rows:** LIM-1 … LIM-17.

## 12. SIGTERM and runtime-only endings

Some endings happen outside the graph; YAML cannot select or route them (#281, #283):

| Ending | Cause | Terminal Outcome |
| --- | --- | --- |
| Drift | §7.3 | `drift` |
| Withdrawal | Selection Label removed, input closed, pull request merged (§7.3) | `withdrawn` |
| Process interruption | SIGTERM during a live Attempt (reason `shutdown`, below); a process loss, settled at restart (reason `process-loss`, §16.1); a Credential Failure (reason `credential-failure`, §16.3); an asserted pin violation (reason `pin-violation`, below) | `interrupted` |
| Internal failure | A runtime defect (§8.4), or an infrastructure-class failure (setup, git, GitHub API) that escaped the Attempt outside the graph | `internal-failure` |

Each of them, wherever it is observed, ends the Attempt with the same finalisation (§15.2):

- **an in-flight model request is discarded,** and running Sub-agents end `no-report` / `stopped` ([ADR 0024](../adr/0024-a-stopped-sub-agent-discards-its-in-flight-request.md));
- **an in-flight write is settled** (§14.2); a stop signal arriving after an Effect Intent does not divert that one write;
- **the dirty tree is never committed or pushed;** the terminal comment reports the count of uncommitted files, and the workspace stays local;
- **no further Workflow Step runs.**

**SIGTERM** stops intake at once and is observed after every model response and every tool result; the running command's process group is killed. **A SIGTERM-ended live Attempt records `interrupted` with reason `shutdown`** (#288 point 2), distinct from `process-loss` and `credential-failure`. Within `worker.shutdown-grace-seconds` (§2.1) the live process runs the finalisation above itself: it discards the in-flight request, settles the in-flight write, never commits or pushes the dirty tree, records the outcome and performs the terminal writes (§15.2). Whatever it does not finish is finished by recovery at the next start (§16.1). The Selection Label is removed, so continuing the work needs a human to apply it again (`attention_required` is `true`, §19). A second signal, or SIGKILL, is an ordinary process loss (§16.1).

**An asserted pin violation ends the live Attempt as `interrupted` with reason `pin-violation`** (§9.1): the Workflow never routes it — an operator configuration fault would repeat in every Attempt. The violating response is discarded: never added to the conversation, never carried back to the model, never retried by the provider retry. Its usage is charged, and its observed identity and the violation are recorded in the Run Ledger. The Worker halts at once with a non-zero exit, as on a Credential Failure (§16.3); after the halt, a restart's recovery finishes the Attempt's terminal writes (REC-13). The set of Terminal Outcomes stays closed: only this reason is new.

**There is no cancellation in the first release** (#288 point 2, amending #283 point 11). Closing the input or removing the Selection Label is a withdrawal (§7.3), and operator contract major 1 has no cancel command: `stop` only drains Intake (§4.3). Nothing produces a `canceled` Terminal Outcome; an operator cancel command and its outcome are future work (§20).

**Rows:** REC-16, REC-24, REC-25, INP-16, INP-17.

## 13. Recording

### 13.1 The Run Ledger

The Run Ledger stays the **single authority** (#283, [ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)): one SQLite file on the state volume, WAL mode, `synchronous=FULL`, accessed under the volume lock. There is no second store and no checkpointer. **Liveness is the volume lock**; there is no Lease.

- **The ledger carries an explicit schema version,** and its evolution is additive. Until the cutover the MVP runtime keeps using the same ledger (#285 point 10).
- **Attempt states:** a Claim intent, then `claim-confirmed`, `outcome-recorded` and `finished`; or `released` for a Claim that never held.

### 13.2 Step Executions and transitions

- **Step Execution opening,** written **before** the step starts: step, sequence number and entry Route (previous step, Step Result).
- **Step Execution end,** committed **before** the router selects a successor: the Step Result with its payload, execution facts, usage and any `limit-exceeded` fact.
- **Step Execution `usage`** carries the execution's own spend plus `by_origin` (`main` / `subagent`): one per-bucket cell per origin, the same cells §11.1 names (four bucket token counts, four bucket costs, responses, responses without usage, stored Effective Tokens). Totals and counters stay as today — `model_calls` stays, equal to `main`'s response count — and wall clock is not split per origin. The `prefix_too_large` and `missing_inputs` paths record the full shape with zeros, never missing fields — except after an archived restore without `by_origin`, where the key is omitted so per-origin figures stay `unavailable` instead of restarting from zero against a restored total. Archived records without `by_origin` read per-origin figures as `unavailable` ("not recorded"), never 0; summing Step Executions gives `unavailable` per origin where any contributing record lacks it, and old spend is never assigned to `main` — a missing key is never read as 0.
- **The Attempt usage flush** carries `usage` (the Attempt total, the restore source) plus `by_origin` (`main` / `subagent`); the origins sum to `usage`, and the flush carries no per-step data. Restart reads `usage` and `by_origin` together: where the last flush has no `by_origin`, later flushes carry none — `by_origin` is never restarted from zero against a restored total — and per-origin figures stay `unavailable`. Native `status` shows the per-origin cells of a Step Execution and of the Attempt where recorded, `unavailable` otherwise.
- **Transition facts:** the Route taken; a refused entry (`execution-ceiling`, no Step Execution); the Workflow End reached.
- **Admission holds and their settlements** (§11.4). A hold still open after a crash is charged its worst case.
- **A Step Execution left open by a process loss** is closed by recovery as *interrupted by process loss*: a runtime fact, never a Step Result, never routed.
- **Per-response observed identity.** Every model response in a Step Execution — and every model response in every Sub-agent — records one `model_response` ledger event: the Attempt id, the Step Execution (step, sequence number; a Sub-agent response also carries its call id), the response index within that execution, and the observed `response_model`. For a direct pin the observed identity is the response model the provider reports in that response's own metadata (`model`, falling back to `model_name`). The value is taken from the response's own evidence, never copied from the pin. A response whose metadata carries no model records `response_model: null`: the identity was not reported, never the pin key. For a gateway pin the observed `response_model` is the `x-litellm-model-group` header (`null` where unreported), and the event additionally records the gateway fragment: model group, model id, model name, attempted fallbacks, attempted retries, call id (the gateway's own call id where present, else the response id) and the observed gateway version. The event also carries the reported effort beside `response_model`: the effort the response reports it ran at (`reasoning_effort` in the response metadata), or `null` where the response reports none. Violating responses record them too; a violation observed on a failed request records the fragment from the error's headers. Where the response was cut short, the event records how: `cut_short` is `output-cap` where the response was truncated at the output cap (§9.5) or `context-window` where the provider cut the response short because the request and its response did not fit (§9.6).
- **A refused Context Overflow records a `context_overflow` event** (§9.6). A refused request has no response, so it has no `model_response` event: the overflow fact travels on its own event carrying the `refused` signal, the provider error class and status, the compared `conversation_context`, the Usable Threshold in force, the declared Model Context Window and the charged hold id.
- **The Sub-agent's answering model is observed.** `subagent_ended` carries `answering_model` — the most recent reported response model across that Sub-agent's responses (the most recent reported model group for a gateway pin), `null` where none reported one — and the ordered `response_models` list. No ledger field copies the pin key as the record of the answering model; the copied-pin-key record is retired.
- **A `subagent_ended` carries its member's own spend, peak, ending and duration.** Every ending path (`reported`, `silent`, `round-limit`, `ceiling`, `context-window`, `stopped`, `provider-failure`, plus `refused` and `pin-violation`) records `ending` (the one string), `duration_seconds` (the member's own wall time, `0.0` where it never ran), `usage` (the member's own usage cell in the §11.1 cell shape: explicit, possibly all zero, never missing) and, where the member compared at least one request, `conversation_context_peak` (the peak compared value with its basis). A running member's live Conversation Context is not recorded separately: it is the latest per-request `model_response` record with its call id. Within one Step Execution the `subagent_ended` cells sum to that execution's `subagent` origin cell, worst-case charges included. Archived `subagent_ended` records without these fields read `unavailable` — a missing peak reads `unavailable`, never 0, and a missing cell reads `unavailable`, never a zero cell.
- **Every model request records the Conversation Context it was compared with** ([ADR 0031](../adr/0031-the-conversation-context-is-anchored-on-provider-reported-input.md)). Each `model_response` event — the main conversation's and every Sub-agent's — carries `conversation_context` (the compared value, its basis, and for an anchored value the anchor's `input_tokens` with its request reference and the appended estimate) and the Usable Threshold in force. A Step Execution that ends `limit-exceeded` on the Context Threshold records one `context_threshold` event carrying the compared `conversation_context` and the Usable Threshold, and carries both in its limit fact; a step that reports `hard-context-threshold` records one `hard_context_threshold` event carrying the crossing `conversation_context` and the Hard Usable Threshold, and carries both in its Execution Summary limit record; a step that reports `soft-context-threshold` records one `soft_context_threshold` event carrying the crossing `conversation_context` and the Soft Usable Threshold, and carries both in its Execution Summary limit record beside the receiver failure evidence the receiver's own Step Execution records — the source crossing and the receiver failure stay distinct in the Run Ledger and the Execution Summary; the operator's `context_peak` scans `context_threshold`, `hard_context_threshold` and `soft_context_threshold` events beside `model_response` and `context_overflow`. A `context-window` `subagent_ended` carries the compared `conversation_context`, the Usable Threshold, and the cause (`guard` vs Context Overflow with its `refused` / `cut-short` signal). Records written before these fields travelled read `unavailable`, never zero.

### 13.3 Bulk content

Step Artifacts, Execution Summaries, the Transcript and evidence files live in the Attempt Archive on the state volume (§4.5). The ledger references each by digest; the files are an index target, not a competing authority. The Transcript keeps the MVP's shape: every request, response and tool call, redacted, each record with a `conversation` label (`step/<step-id>/<n>`, `subagent:<call id>`) and one monotonic `seq` per file under one per-file lock. Nothing reads the Transcript back.

The Archive also holds the pinned instruction content: the Attempt's Effective Configuration record (`version: 4`, §2.10) and, digest-addressed under a content-reference index the Run Ledger records, every needed prompt's bytes and every available activation's component and delivery bytes — written and flushed before the ledger transaction the Claim depends on, so archive durability orders before dependent Claim state (§6.2). `not-delivered` bodies are not archived; only their inventory digests are pinned. Recovery replays no graph and no model: it finishes a recorded outcome from the pinned identity and the archived blobs, even after the deployment has changed and the former store or image is gone (§16).

### 13.4 Evidence and revision binding

**Every runtime-produced evidence record carries the identity of the tree it was produced on:** the commit when the tree is clean, otherwise a digest over tracked plus non-ignored untracked files (the Execution Footprint set). Evidence is produced only by the runtime: the `tests` group's evidence-recording tool and `run-validation`. A Step Result is never evidence; `shell` output and diagnostic checks are never evidence. A commit `commit-and-push` creates from a dirty tree inherits that tree's validation result: the new commit is bound as the dirty tree was (§10.4). Tree identity itself does not change.

**Rows:** REC-1, REC-2, REC-3, LIM-8, STEP-15.

## 14. External effects

### 14.1 Effect Intent and Effect Receipt

**Every external write** — pushes, pull requests, reviews, comments, labels, assignments and the Claim Comment — has an **Effect Intent** and an **Effect Receipt** ([ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)). This replaces the MVP's Attempt-level Delivery Intent.

- **Key:** `(Attempt id, step, Step Execution number, write ordinal)`, carried in the write's Attempt Marker. Claim and finalisation writes, which belong to no step, use reserved key positions.
- **Contents:** the exact rendered payload and the expected target state (the revision to push, the head a compare-and-swap expects, the addressed input).
- **Timing:** written after the Gate, immediately before the write.
- **Receipt:** the remote sha, pull-request number, review id or comment id, recorded when the write lands.

### 14.2 Settlement on restart

For an intent without a receipt:

1. look the write up remotely by its key;
2. if it landed, record the receipt;
3. if it is absent, check the operation's factual preconditions again (active Claim, no withdrawal, revision or head compare-and-swap);
4. if they hold, perform the write once; otherwise record it **`not-performed`** with its reason.

- **Reversal is not absence.** A human-closed own pull request is never re-created ([ADR 0003](../adr/0003-delivery-intent.md)'s rule, retained).
- **A compare-and-swap push to a pull-request head:** a remote head equal to the intended commit means landed; equal to the expected head means absent; anything else is foreign and yields `stale-head` or Drift, never a forced push.
- **Lookups filter by the Agent Identity's numeric id** and the marker; a write by another account carrying our marker is not our receipt. Comment lookups read the full, paginated list.

### 14.3 No point of no return; nothing reverted

- **Each write is its own.** There is no Attempt-level point of no return. A withdrawal or Drift after a publication landed does not undo it, and later writes pass their own Gates.
- **Landed effects are never reverted** when the Attempt later ends badly. The runtime does not close a pull request, convert it to draft, delete a branch, or remove reviews or comments; it reports them in the terminal comment. Clean-up is Workflow policy or a human's job.

### 14.4 The Attempt Marker, rate limits and refusals

**The Attempt Marker** is one machine-readable line in every comment, review and pull-request body the Worker writes. It carries the Attempt id (`<workflow>#<N>/<n>`), the write's kind (`claim`, `terminal`, or the operation's write) and its Effect Intent key. Commits carry the Attempt id as a trailer; issue Attempt branches carry the `agent/<workflow>/` prefix. The rule reads in one direction only: no marker means not ours. A marker does not prove authorship, so lookups also filter by the Agent Identity's id.

**Publication bodies** (#288 point 4) are the bodies of the pull request, the review and the `post-comment` comment that Runtime Operations publish (§10.5–§10.7). The runtime renders each from a fixed template around the Workflow's `document`:

- **The `document` is published verbatim.** The runtime does not rewrite model-authored text, so a closing keyword the model writes takes effect on merge; human review owns that.
- **A provenance footer**, appended after the `document`, carries the Attempt id, the Workflow id, the publishing step id, the Effective Configuration digest and the revision: the Base Revision for a pull request, the reviewed Input Head for a review, none for a comment. The Attempt Marker follows it. Validation evidence is not added by the runtime: it belongs to the Workflow's `document`.
- **The closing keyword** of a pull request (§10.5) is part of the runtime template, not of the `document`.
- **Size:** when a body would exceed GitHub's limit, only the `document` is truncated, with an explicit truncation marker and a pointer to the Step Artifact in the Attempt Archive. The footer, the closing keyword and the Attempt Marker are never truncated.
- **The rendered body is recorded with its Effect Intent,** and recovery publishes it unchanged (§14.2).

**Rate limits and refusals** keep the MVP's classification by status, header value and documented error signal:

| Observation | Classification | Effect |
| --- | --- | --- |
| 401, confirmed by the identity probe | Credential Failure | §16.3 |
| 401, unconfirmed | Infrastructure-class (`github`) | the operation ends `error`; counted toward the infrastructure halt |
| 403 with `x-ratelimit-remaining > 0` | Permission refusal | the operation ends `error`, routed by the Workflow |
| 403/429 with `x-ratelimit-remaining: 0` | Primary rate limit | wait until `x-ratelimit-reset`, retry in place |
| 403/429 with the secondary-limit signal | Secondary rate limit | honour `Retry-After`; otherwise exponential back-off |

In-place retry is bounded at 3 attempts and 5 minutes, and the waiting counts against the Attempt's wall clock. Beyond the bound the failure is infrastructure-class: the operation ends `error`; a Claim or finalisation write is retried on the next poll cycle. The identity probe is one unretried `GET /user`, decided by its status alone.

**Rows:** REC-4 … REC-9, REC-15, OP-13, OP-14, OP-18, OP-19, OP-20.

## 15. Terminal Outcomes and finalisation

### 15.1 The Terminal Outcome set

The set is closed and flat (#283 point 11, as amended by #288 point 2: there is no `canceled`):

| Terminal Outcome | Selected by | Reserved results may route to it directly |
| --- | --- | --- |
| `completed` | Workflow End | no |
| `blocked`: needs a human's decision or information | Workflow End | no |
| `unfinished`: partial work handed to a human | Workflow End | yes |
| `failed` | Workflow End | yes |
| `withdrawn`, `drift`, `interrupted`, `internal-failure` | the runtime only (§12) | n/a |

- The Terminal Outcome records the step, Step Result and Workflow End that ended the Attempt, plus the primary `limit-exceeded` fact when one occurred (§11.7).
- **There are no `delivered` or `failed-limit` outcomes.** Delivery is a separate fact derived from Effect Receipts; a limit is a reason, not an outcome.
- **`unfinished` is not an Operator Handoff.** The operator contract's `handoff now` stays `unsupported` and is never mapped onto a Workflow path.

### 15.2 The finalisation sequence

Every ending, inside or outside the graph:

1. **settle** in-flight effects (§14.2);
2. **record** the Terminal Outcome, the rendered terminal comment and the list of terminal writes, **once and immutably**: the first record wins;
3. **perform** the terminal writes, each with its own Effect Intent and Receipt.

Recovery finishes a recorded outcome and never re-decides it ([ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md), as amended by ADR 0028).

### 15.3 The terminal writes

- **The terminal comment:** always exactly one, rendered by the runtime from a fixed template with no model call, carrying the Attempt Marker with `kind=terminal`. It states the Attempt id and the Terminal Outcome; the ending step, Step Result and Workflow End; the limit fact; the landed effects with links; the `not-performed` effects; and the uncommitted-file count. It is posted even on a closed or merged input, and it ends the Claim Comment's force (§6.1). A Workflow's own `post-comment` is separate, so such an Attempt leaves two comments.
- **The Selection Label** is removed.
- **End labels** declared by the Workflow for that Terminal Outcome (`outcomes.<outcome>.labels`, runtime-only outcomes included) are applied. The runtime hard-codes none, not even `ready-for-human`. Static validation keeps every end label distinct from every Selection Label in the file (§3), and the Worker never writes its own Workflow's Selection Label whatever the configuration says (§18).
- **The assignment** is removed when no other Claim is in force.

Then the Attempt is marked `finished`, which completes a pending `stop` (§4.3), and its Attempt Archive is sealed.

**Rows:** REC-13, REC-14, REC-26, ROUTE-4, OPC-1.

## 16. Recovery

Recovery runs at startup before any selection and **never calls the model** ([ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)). It recovers from the pinned identity and the archived blobs: the Attempt's Effective Configuration record (`version: 4`, §2.10) and the digest-addressed prompt and activation bytes in the Attempt Archive. Recovery works after the deployment has changed and without the former image or store; it never repairs history from current files, never replays the graph or the model, and never restores secrets.

### 16.1 Restart

**A restart never enters the Workflow graph and never runs a Workflow Step,** neither `agent` nor `operation`. For each unfinished Attempt it first validates the pinned identity and the archive, then does exactly two things:

1. **validates** the Effective Configuration record version, its schema, the core digest, the reference-index consistency and every required blob's digest and size; a missing, corrupt, escaping, symlinked or incompatible core or blob refuses recovery, naming the Attempt and the fault, with no partial success and no current-file repair;
2. **settles** the effect that was in flight (§14.2), and closes any open Step Execution as interrupted by process loss (§13.2);
3. **finishes the lifecycle** (§15.2): a recorded outcome is finished as recorded — settled from the archived bytes, never from the current deployment's files; otherwise the Attempt ends **`interrupted`** with reason `process-loss`, or **`withdrawn`** if the input was withdrawn meanwhile.

The dirty tree is never committed or pushed. Recovery never resumes or re-runs a Sub-agent; it discloses how many had no report at the interruption. Model resumption after process loss is rejected, not deferred (§20).

### 16.2 Unconfirmed Claim

A Claim intent or assignment whose read-back never happened, or happened and lost, is not an Attempt. Recovery deletes its own Claim Comment by key, removes the assignment if no other Claim is in force, and marks the record **`released`**. There is no Terminal Outcome and no terminal comment.

### 16.3 Credential and recovery-write failures

- **A Credential Failure** ends the Attempt `interrupted` with reason `credential-failure`, and the Worker halts. Recovery finishes the terminal writes after the credential is fixed. A single refused permission is an ordinary operation `error` (§14.4).
- **An asserted pin violation** ends the Attempt `interrupted` with reason `pin-violation`, and the Worker halts (§9.1, §12). Recovery finishes the terminal writes after the operator re-pins the name the vendor reports. The set of Terminal Outcomes stays closed: only this reason is new.
- **Recovery write failures** are retried every sweep; three consecutive failures halt the Worker with `recovery-write-failure`.

### 16.4 Incompatible records, drain, upgrade and rollback

A new Worker **refuses to start (Halt)** on a state volume holding any open record it cannot recover:

- an unfinished Attempt written by an older runtime (the MVP runtime, or any Attempt whose Effective Configuration record is missing, unreadable or of an incompatible version) — the message names the Attempts and tells the operator to drain them with the last compatible release before upgrading;
- an unfinished Attempt whose pinned identity or archive blobs fail validation (§16.1) — the message names the Attempt and the fault; the Worker never invents missing identity or instruction archives from current deployment files, never converts the record and never fabricates archives.

Closed history stays listable, viewable and exportable with existing numbering (`attempts list`, `attempts show`, export); nothing is deleted and no legacy finisher is carried into the new runtime.

Upgrading is a coordinated, operator-driven sequence (see [the operator guide](../ops/workflow-configuration.md#upgrading-drain-backup-and-rollback)):

1. **Drain with the compatible release:** stop Intake, finish or recover every open Attempt with the release that opened it, and verify that no unfinished or unconfirmed Claim and no unsettled lifecycle work remain before touching the state volume. A terminal outcome alone does not prove finalisation is complete.
2. **Back up while stopped:** take a coordinated backup of the persistent state volume and of the operator inputs (Workflow Configuration, prompts, policy, prepared bundle records) while the Worker is stopped.
3. **Upgrade and re-prepare:** install the new release, declare `version: 4` — adding `context-window-tokens`, `context-threshold-tokens` and `output-cap-tokens` to every pin (direct pins newly) and renaming `tokens` to `effective-tokens` in every `limits` block — with an explicit `skills` membership, supply the reviewed policy, re-prepare and re-verify against the final roots, then restart with runtime credentials only.
4. **Roll back only compatibly:** once the new release has written new-version records or state, rolling back means running a compatible release or restoring the coordinated pre-upgrade backup with reconciliation of external effects — never attaching an older Worker blindly, never restoring stale state while ignoring later GitHub effects, and never deleting history or resetting state to bypass a refusal.

### 16.5 Ledger loss

Ledger loss is outside the runtime's guarantees and is handled by [the ledger-loss runbook](../runbooks/ledger-loss.md).

- **What still holds:** remote lookups by Effect Intent key and marker prevent duplicate pull requests, reviews and comments.
- **What does not:** an Agent Identity assignment or Claim Comment with no ledger record is released by a human.
- **Numbering** is in the first release: the next `n` for a (Workflow, input) is one more than the highest of the ledger's `n`, the Attempt branches `agent/<workflow>/<issue>/<n>-*` and the Attempt Markers on GitHub.

**Rows:** REC-5 … REC-12, REC-17 … REC-22, CFG-42.

## 17. Continuation

**There is no runtime Continuation in the first release** (#285 point 11).

- **Every issue Attempt starts at its Base Revision,** on its own branch `agent/<workflow>/<issue>/<n>-<slug>`.
- **Continuing earlier work is scenario policy.** A Handoff path may `open-pull-request` with `with.draft: true`, and a `pull-request` Workflow (for example a `fixup` Workflow) continues on that pull request through its compare-and-swap push.
- The MVP's Continuation and its `continuation-conflict` reason are MVP-only.

## 18. Identity and authorization

Retained from the MVP, with the Attempt id and branch of §6.3.

- **The Agent Identity is the Target Repository owner's own GitHub account** ([ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md)), authenticated with a fine-grained personal access token scoped to the single Target Repository, with the Workflows permission withheld, and recognised by its **numeric account id**, resolved once at startup by `GET /user`.
- **The identity mode is detected, not configured** (#288 point 1). At startup the Worker compares the Agent Identity's numeric id (`GET /user`) with the Target Repository owner's id (`GET /repos/{owner}/{repo}`). Equal ids are a **shared identity** (ADR 0005); different ids are a **dedicated identity** ([#11](https://github.com/lbacik/coding-agent/issues/11)). The mode decides the Selection Label actor test (§5.1). No YAML key controls it.
- **Two structural abstentions.** The Worker never edits an input's title or body, and never applies a Selection Label. The second is **enforced, not assumed:** static validation refuses an end label equal to any Workflow's Selection Label in the same file (§3), and a runtime guard keeps the Worker from ever writing its own Workflow's Selection Label.
- **Residual gap, documented rather than closed.** Under a shared identity, a second Worker running a *different* configuration file could apply an end label that is this Workflow's Selection Label, and the actor test would accept it. Keeping Selection Labels disjoint from end labels across files is the operator's responsibility, stated in [the operator guide](../ops/workflow-configuration.md). A dedicated identity closes the gap.
- **The Claim** is the assignment plus the Claim Comment (§6, [ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md) as amended). The Worker removes only an assignment it recorded itself.
- **One identity for API calls and for git.** Author and committer use the id-based noreply address. Commits are unsigned. The Worker never force-pushes and never deletes a branch.
- **Credentials pass through one choke point:** loaded into a redaction registry at startup, filtered out of logs, tool arguments and results, never present in the environment of child processes (Validation Contract commands, `shell`, `tests`), and used for git through a credential helper.
- **Startup is non-mutating:** `GET /user` for the numeric id and login, `GET /repos/{owner}/{repo}` for `permissions.push == true`, rate-limit headroom, and a token of the fine-grained kind (`github_pat_` prefix, no `X-OAuth-Scopes` header).
- **`agent preflight`** remains an operator command against a sandbox repository, never run at startup. It performs every write kind this contract needs, including a Claim Comment and a COMMENT review, and asserts that a push touching `.github/workflows/` is rejected.

## 19. Operator-contract projection

The native vocabulary is producer-owned, so it changes in one producer slice, S9 (#283 point 18, #285 point 5). Coordination with [Spec: coding-agent serves operator contract major 1 as a producer](https://github.com/lbacik/coding-agent/issues/245) is recorded there.

- **`attempt_id`:** `<workflow>#<N>/<n>`.
- **`outcome.native`:** the Terminal Outcome (§15.1).
- **`outcome.reason`:** the limit or error fact when there is one; otherwise the Workflow End with the step and Step Result that reached it.
- **`delivery.pull_request`:** only a pull request **created** by the Attempt, from the `open-pull-request` receipt. Published reviews and comments have no field in major 1.
- **`attention_required`:** `true` for `failed`, `unfinished`, `blocked`, `interrupted` (a SIGTERM-ended `shutdown` included) and `internal-failure`, and whenever an effect is unsettled or `not-performed`; `false` for `completed`, `withdrawn` and `drift`.
- **Native phase:** the step id plus the Step Execution number.
- **Budget:** the four meters with effective tokens, limits from the Attempt Ceiling.
- **Capabilities:** `next-issue` is advertised only by `issue` Workers; a `pull-request` Worker answers `unsupported`. `handoff now` stays `unsupported`.
- **Events and archives:** §4.5.

The upstream note in `agent-operator-contract` §16 listing the old four outcomes is a separate pull request that does not block the cutover. An optional minor field for published reviews is future work (§20).

**Rows:** OPC-1 … OPC-8.

## 20. Future and disposition of the MVP's deferred items

This section replaces the MVP contract's §19 (#285 point 12).

**Absorbed by this design:**

| MVP deferred item | Now |
| --- | --- |
| Delivery Draft | `open-pull-request` `with.draft` |
| The remediation round; the Automatic Retry Budget | Routes and Execution Ceilings (§8.3) |
| Per-step budgets | Step Allowances (§11.2) |
| Typed step inputs and outputs | Step Results, payload types and inputs (§2.4, §2.5) |
| Tool-set customisation | Tool groups (§9.4) |
| Additional top-level skills | `instructions` (§9.3) |
| Renaming lifecycle labels | `outcomes.<outcome>.labels` (§15.3) |

**In the first release:** rebuilding Attempt numbering from markers after a ledger loss (§16.5, slice S6).

**Rejected rather than deferred:** model resumption after process loss ([ADR 0028](../adr/0028-every-external-write-is-an-intent-settled-once.md)); the Lease.

**Future** (listed without design):

- Clarification. Until then: `blocked`, a human answer, and the Selection Label applied again.
- Baseline Failure exclusion.
- Isolation of validation commands from runtime state ([ADR 0018](../adr/0018-runtime-owned-validation-authenticates-the-invocation-not-the-code.md)).
- Allow/deny command patterns for `shell` ([ADR 0025](../adr/0025-withholding-a-shell-only-disguises-execution.md)).
- Tracker adapters other than GitHub.
- Approve / request-changes; merge / auto-merge.
- GitHub read tools for the model.
- Comments or reviews as inputs; fork pull requests as inputs. Each needs its own trust decision.
- `requires` beyond `run-validation: passed`.
- A per-step `model`.
- Workflow-level fan-out/join; nested Workflows.
- An AttemptSummary field for published reviews.
- An operator cancel command and its Terminal Outcome, with a later minor of the operator contract (#288 point 2). Until then the first release has no `canceled` outcome (§12).

## 21. Differences from the MVP

**Retained:** the Supervisor lifecycle and its `worker` settings with their defaults (§2.1), Durable Intake and Operator Commands, Halting, Contract Events, Attempt Archives and Offline Read (§4); the Project Profile and Validation Contract (§10.2, §10.3); the validation trust boundary; the owned tool loop, verbatim assistant turns, the Pinned Prefix region, verbatim skill activation and Companion Files; Sub-agent mechanics (§9.8); the Run Ledger as single authority with no checkpointer and no Lease; recording an outcome before its writes; pinned values only in recovery; rate-limit classification; identity and authorisation (§18), except the Selection Label actor test, which now depends on the detected identity mode (§5.1).

**Replaced:**

| MVP | Step-workflow runtime |
| --- | --- |
| One fixed Attempt graph; a Workflow fills only the implementation stage ([ADR 0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md)) | A compiled, configured graph of Workflow Steps ([ADR 0027](../adr/0027-workflow-policy-is-separate-from-runtime-mechanics.md)) |
| `version: 2` configuration | `version: 4` (§2), no compatibility: refused with a migration diagnostic, never converted |
| Issues only; fixed exclusions | `issue` or `pull-request` input; exclusions in YAML (§5) |
| Claim = assignment ([ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md)) | Assignment + Claim Comment, label actor checked by identity mode, global exclusivity (§5.1, §6) |
| Attempt `#<issue>/<n>`, branch `agent/<issue>/<n>-<slug>` | `<workflow>#<N>/<n>`, `agent/<workflow>/<issue>/<n>-<slug>` (§6.3) |
| Readiness Facts with Acceptance Criteria and Seam Set; a mandatory baseline | `check-readiness`; criteria are scenario policy; a baseline is an optional `run-validation` (§10.2) |
| `report_step` / `report_completion`; Implementation Outcome | Named Step Results with payloads (§9.5) |
| Review gate attributed to `code-review` ([ADR 0021](../adr/0021-a-review-is-the-first-sub-agent-group-after-the-code-review-activation.md)); review in the pull-request body | No attribution; a review is a `publish-review` operation (§10.6) |
| Delivery Snapshot, final checks, Publication Gate, Delivery Intent | Operations with `requires`; an Effect Intent per write (§10, §14) |
| A runtime-composed pull-request body: `Closes #N`, summary, evidence, review reports, Base Revision, Workflow id and digest | The Workflow's `document` verbatim, a closing keyword (`Closes` when ready, `Part of` when draft) and a provenance footer; only the `document` is truncated (§10.5, §14.4) |
| Progress Gates, 70%/80% reserve, `failed-limit` | Step Allowances, Reserved Steps, admission, `limit-exceeded` Routes (§11) |
| Terminal Outcomes `delivered`, `failed`, `failed-limit`, `abandoned` | The closed set of §15.1; Delivery from receipts |
| Closing the issue or removing the Selection Label cancels (`abandoned`); SIGTERM ends `failed` / `interrupted` | Withdrawal ends `withdrawn`; SIGTERM ends `interrupted` / `shutdown`; no cancellation (§12) |
| Intents and receipts only for pushes and the pull request | Every external write (§14) |
| Recovery re-enters the fixed graph at a named node ([ADR 0003](../adr/0003-delivery-intent.md), [ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md)) | A restart never enters the graph (§16, ADR 0028) |
| Continuation across Attempts | None at runtime (§17) |
| Hard-coded `ready-for-human` | Workflow-declared end labels (§15.3) |
