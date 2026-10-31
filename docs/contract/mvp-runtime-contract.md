# MVP runtime contract

**Status: current.** This document is the only authority on how the Worker behaves in the MVP. It is the one living runtime contract: a deferred feature that returns does so as an amendment to this document, never by reviving the v1 contract.

It supersedes the **rule text** of these resolutions, which remain the record of *why* each rule exists:

- [What is the MVP parity contract, and which v1 guarantees does it supersede?](https://github.com/lbacik/coding-agent/issues/71#issuecomment-6542177173) (cited below as **#71**);
- [What does workflow YAML configure, and what does each ordered step guarantee?](https://github.com/lbacik/coding-agent/issues/72#issuecomment-6542567154) (**#72**);
- [Can a LangGraph skill step reproduce the reference /implement behavior?](https://github.com/lbacik/coding-agent/issues/73#issuecomment-6542890368) (**#73**);
- [How do Attempts recover and publish without duplicating GitHub effects?](https://github.com/lbacik/coding-agent/issues/74#issuecomment-6543048248) (**#74**).

The decisions of the planning map [Design: make skill execution semantic rather than prompt-only](https://github.com/lbacik/coding-agent/issues/57), [#60](https://github.com/lbacik/coding-agent/issues/60), [#61](https://github.com/lbacik/coding-agent/issues/61), [#62](https://github.com/lbacik/coding-agent/issues/62) and [#63](https://github.com/lbacik/coding-agent/issues/63), remain separate records. This document incorporates and links them and does not supersede them, except for the review amendments #71 and #73 already made to #62 (§10).

[The v1 runtime contract](./v1-runtime-contract.md) is a **frozen design record**. It is still worth reading, and it is where every deferred feature keeps its design (§19), but it requires nothing.

Vocabulary is fixed in [`CONTEXT.md`](../../CONTEXT.md) and is used here without redefinition. "The reference" is `simple-coding-agent` at `fb50952`.

**Acceptance rows.** Each section ends with the acceptance rows that prove it. The rows keep the ids issued in #71–#74: P-* (parity), W-* (Workflow Configuration and steps), G-* (graph and review) and REC-* (recovery). A v1 row carried over unchanged keeps its v1 id. The full matrix, with layers and owning slices, is `docs/plan/mvp-acceptance-matrix.md`.

## 1. Layers and ownership

| Layer | Owns | May write to GitHub |
| --- | --- | --- |
| **Supervisor** (`worker/`, plain Python, no graph) | Startup checks; the exclusive volume lock; startup recovery; polling; selection; intake; receiving SIGTERM; invoking the Attempt graph | **No: read-only** |
| **Attempt graph** (one fixed graph, §6) | Everything from Claim to a Terminal Outcome, including every external write, recovery's included | Yes, only through the write sequence of §13 |
| **Step subgraph** (one per Workflow Step, §8) | One Workflow Step's conversation with the model, its tools and its exit | No GitHub API write. Its only external write is the review-snapshot push inside `activate_code_review` (§10) |
| **Review subgraph** (invoked from inside `activate_code_review`) | Two isolated reviewer conversations | No |
| **Model** (inside the step and review subgraphs) | Reasoning, workspace file edits, repository-declared commands, local commits | **No, never** |

- **The model's toolset contains no GitHub mutation of any kind:** no label, comment, assignment or pull-request tool. "The agent never applies the Selection Label" is therefore structural, not a matter of prompt compliance.
- **There is no merge node.** The never-merge rule is enforced by that absence.
- **No framework checkpoint.** LangGraph graph state is ephemeral. The Run Ledger is the only durable state, and a restarted Worker recovers from it alone (§17, [ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md)).
- **Runtime context stays out of graph state.** Attempt-wide meters, Criterion Evidence, review records and the tracker port are passed to nodes as runtime context, not carried in graph state (#73).
- **One Worker per Target Repository and per state volume.** Multi-process and multi-host operation is outside the MVP (#72, #74).

## 2. Workflow Configuration

The operator supplies the Workflow Configuration, a YAML file, at startup. A process runs exactly one Workflow, selected explicitly. A Workflow fills only the implementation stage of an Attempt; the rest of the Attempt is the fixed skeleton of §6, which YAML cannot add to, remove from, reorder or re-judge ([ADR 0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md)).

### 2.1 Schema

```yaml
version: 1

target:
  kind: github
  repository: lbacik/coding-agent-sandbox
  base_branch: main
  token_env: GITHUB_TOKEN
  labels:
    ready_for_human: ready-for-human     # default; may be omitted

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api_key_env: ANTHROPIC_API_KEY
    prices_usd_per_mtok: {input: 2.00, output: 10.00, cache_read: 0.20, cache_write: 2.50}

worker:
  poll_interval_seconds: 60              # default; may be omitted
  shutdown_grace_seconds: 30             # default; may be omitted

workflows:
  implement:                             # the parity workflow
    model: sonnet
    selector:
      label: ready-for-agent
    steps:
      - id: implement
        skill: implement
    # limits omitted -> 60 min, $20, 4M tokens, 600 tool calls

  survey-then-implement:                 # illustrates the multi-step contract
    model: sonnet
    selector:
      label: ready-for-agent-surveyed
      exclude: [needs-design]
    limits:
      wall_clock_minutes: 90
      tool_calls: 800
    steps:
      - id: survey
        prompt_file: prompts/survey.md   # ends with report_step
      - id: implement
        skill: implement                 # ends with report_completion
```

| Field | Required | Meaning |
| --- | --- | --- |
| `version` | yes | Must be `1`. |
| `target.kind` | yes | The tracker adapter. `github` is the only value; any other is an unsupported integration. |
| `target.repository` | yes | The Target Repository, `owner/name`. |
| `target.base_branch` | yes | The base branch. The Base Revision is resolved from it at Claim, and the Project Profile is read there. Nothing assumes `main`. |
| `target.token_env` | yes | The **name** of the environment variable holding the Agent Identity's token. |
| `target.labels.ready_for_human` | no | The name of the `ready-for-human` label; default `ready-for-human`. It is the only configurable lifecycle label, and it stays a fixed selection exclusion. |
| `models.<id>` | at least one | A Pinned Model: `provider`, `model`, `endpoint`, `effort`, `api_key_env` (a name) and `prices_usd_per_mtok` with `input`, `output`, `cache_read` and `cache_write`, all required. |
| `worker.poll_interval_seconds` | no | Idle sleep between selection sweeps; default 60. |
| `worker.shutdown_grace_seconds` | no | Time the live Attempt has to reach its stop path after SIGTERM; default 30. |
| `workflows.<id>` | at least one | A Workflow. |
| `workflows.<id>.model` | yes | The id of exactly one `models` entry. A step never overrides it. |
| `workflows.<id>.selector.label` | yes | The Workflow's one Selection Label. |
| `workflows.<id>.selector.require_all` / `require_any` / `exclude` | no | Label filters (§4). |
| `workflows.<id>.limits` | no | `wall_clock_minutes` (default 60), `cost_usd` (default 20), `tokens` (default 4 000 000), `tool_calls` (default 600). |
| `workflows.<id>.steps` | yes, non-empty | Ordered Workflow Steps, each `id` plus exactly one of `skill` or `prompt_file`. |

- **Secrets appear only as environment-variable names** (`token_env`, `api_key_env`). An inline secret value is a validation error.
- **The Pinned Model's context configuration stays in code.** The context window, compaction threshold and effective token ceiling live in a code-owned table keyed by (provider, model). A pair outside that table is refused.
- **The Project Profile path is not configurable.** It is `docs/agents/project-profile.yml` in the Target Repository (§7).
- **The state volume is not part of the Workflow Configuration.** It is a property of the container and is passed on the command line.

### 2.2 Strict parsing

- `version: 1` is required.
- These are rejected: unknown fields at any level; duplicate keys; anchors and aliases; merge keys (`<<`); custom tags.
- Every id (model, workflow, step) matches `[a-z0-9-]+` and is unique within its map or list.
- A step has exactly one of `skill` or `prompt_file`.
- `skill` names a Skill Bundle member on the top-level allowlist. In the MVP the allowlist is `implement` alone. `tdd`, `codebase-design` and `code-review` stay nested and are activated by `implement`'s own text ([#60](https://github.com/lbacik/coding-agent/issues/60)).
- The Selection Label is not empty, is not the `ready_for_human` name or `wontfix`, does not match `wayfinder:*`, and does not appear in the same Workflow's `require_all`, `require_any` or `exclude`.

### 2.3 Trust and paths

The Workflow Configuration and its prompt files are trusted operator input.

- **They are read from the Worker's filesystem only, never from the Target Repository** ([ADR 0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md)).
- **Relative paths resolve against the YAML file's directory** and must stay inside it after symlink resolution.
- **Prompt content is used verbatim.** There is no templating and no interpolation.
- **The Target Project has no instruction channel to the model.** There is no equivalent of the reference's `model_instructions`. Project-specific guidance reaches the model only through a `prompt_file` the operator owns.
- **A custom skills directory is refused.** Operators customise only through `prompt_file`.

### 2.4 Commands and validation

- **`agent config validate --config <file> [--workflow <id>]`** is static and offline. It checks the schema, references, prompt-file existence and containment, skill membership and the allowlist, the (provider, model) table, and that every named environment variable is set. It prints variable names, never values. It makes no network call.
- **`agent run --config <file> --workflow <id>`** runs the same validation first, then the startup sequence of §3. `--workflow` is always required.
- **Every validation failure exits non-zero, with no Attempt and no GitHub write,** and the message names the YAML path of the fault. An unknown or missing `--workflow` lists the available ids.

### 2.5 Effective Configuration and pinning

- **Read once.** The process reads the YAML and every prompt file once, at startup.
- **Recorded at Claim.** The transaction that opens an Attempt (§5) records its **Effective Configuration**: the normalised selected Workflow, the file-level settings it runs under (`target`, the referenced model, `worker`), the bytes of every prompt file its steps activate, and a SHA-256 digest of all of it. Environment-variable values are never part of it; the names are.
- **Disclosed.** The pull-request body and the terminal comment show the Workflow id and the digest (§15).
- **Recovery uses pinned values** (§17): the repository, label names, base branch and rendered bodies come from the ledger. A restart with a changed file, another `--workflow` or another repository does not alter an unfinished Attempt.
- **A Continuation is a new Attempt** and takes the configuration current at its own Claim (§16).

**Rows:** W-CFG-1 … W-CFG-13, W-PIN-1, W-PIN-2, W-STP-7, P-CMD-2.

## 3. The Worker's lifecycle

### Startup

`agent run` performs these steps in order. A failure at any of them exits non-zero with no Attempt opened and no GitHub write.

1. **Validate the Workflow Configuration** (§2.4).
2. **Take the exclusive lock** (`flock`) on the state volume. A second process using the same state volume is refused.
3. **Verify the Skill Bundle** against its digest manifest.
4. **Run the non-mutating GitHub checks** of §18: token kind, the Agent Identity's numeric id, push permission on the Target Repository and rate-limit headroom.
5. **Run the Provider Capability Assertion** for the Workflow's Pinned Model ([ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md)).
6. **Open the Run Ledger.** An unfinished Attempt whose Effective Configuration is missing or unreadable is a refusal. The Worker never guesses it from the current configuration.
7. **Run startup recovery** (§17) for every unfinished Attempt, before any selection.

### The poll loop

- **The Worker is a continuously running process.** It sleeps for the poll interval only when a sweep claimed nothing, and selects again immediately after an Attempt ends.
- **Intake.** While the ledger holds an Attempt that is not `finished` or `released`, the Worker selects no new work. A recovery write that fails is retried on the next poll cycle.
- **One Attempt at a time.** The Worker is sequential.

### Halting

- **A Credential Failure halts the Worker immediately** with a non-zero exit. It cannot write, so it writes no comment. The failed Attempt's outcome is recorded (§14), and recovery finishes its writes after a restart with a working credential.
- **Three consecutive infrastructure-class failures halt the Worker** with a non-zero exit, only after the third Attempt is fully finalized. The infrastructure classes are setup, git, GitHub API and provider. Recovery write failures count the same way. The counter resets after any Attempt that ends without such a failure. Because the halt follows finalization, a restart does not loop.
- **SIGTERM stops intake** and stops the live Attempt as §11 describes. The process then exits.

**Rows:** P-WRK-1 … P-WRK-4, W-CFG-13, W-CFG-14, REC-CFG-2, REC-WRK-1.

## 4. Selection

**Candidates** are the open issues of the Target Repository that carry the Workflow's Selection Label.

**A candidate is excluded when** any of these holds. These guards are fixed; YAML cannot remove them.

- it is not open;
- it has any assignee, the Agent Identity included when no Attempt of the Worker is open for it (§5);
- it carries the `ready-for-human` label (by its configured name), even together with the Selection Label;
- it carries `wontfix`;
- it carries any `wayfinder:*` label;
- it has an open native blocker (GitHub issue dependencies);
- its body is empty;
- an open pull request carries an Attempt Marker for it (§15).

**Filters narrow, never widen.** A Workflow may add `require_all` (every listed label present), `require_any` (at least one present) and `exclude` (none present). The Worker never writes a filter label. Filters are evaluated at selection and at the Claim read-back, never at later Gates.

**Order:** `(created_at, number)` ascending.

- **No other label has a meaning.** `needs-info`, `round-finished` and `merge-conflict` are not queue signals in the MVP.
- **An excluded candidate is left untouched.** Selection writes nothing, including for an issue carrying both the Selection Label and `ready-for-human`.

**Rows:** P-SEL-1 … P-SEL-5, REC-CLM-3.

## 5. Claim

The Claim is an assignment of the Agent Identity, confirmed by reading the issue back ([ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md)). It adds no label and writes no comment.

1. **One ledger transaction opens Attempt `#<issue>/<n>`** in state `open`. It records the Fingerprint, the Base Revision resolved from the configured base branch, the Effective Configuration with its digest, and the Agent Identity's numeric id. `n` is one more than the highest `n` the ledger holds for the issue. Numbers are never reused, not even after a `released` Claim.
2. **Then the Worker assigns the Agent Identity** and records `assigned_by_us`.
3. **Then it reads the issue back** and re-evaluates every guard of §4 and the Workflow's filters. The settle delay before the read is a tuning constant, not part of this contract.
   - If another assignee is present or any guard fails, the Worker removes only its own assignment and marks the Attempt **`released`**. It writes no comment and changes no label, and it moves to the next candidate. A `released` Attempt is not a Terminal Outcome.
   - Otherwise it records **`claim-confirmed`**, and the Attempt proceeds.

**Whose assignment it is.** The Run Ledger decides whether an assignment of the Agent Identity is the Worker's. An Agent Identity assignment with no open Attempt is a human's: it keeps the issue ineligible, and the Worker never removes it.

**No orphaned Claim.** Every assignment the Worker makes has an open Attempt in the ledger, so recovery can always release or finish it (§17).

**Rows:** P-CLM-1, P-CLM-2, P-CLM-3, REC-CLM-1, REC-CLM-2, REC-CLM-3.

## 6. The Attempt graph

The application builds **one fixed graph**, whatever the Workflow Configuration says. Its nodes, in order:

```
claim → readiness → baseline → run_step ⇄ gate → delivery_snapshot → final_checks
      → publication_gate → delivery_intent → publish → terminal_writes
```

| Node | Does |
| --- | --- |
| `claim` | §5. Also the entry recovery uses to release an unconfirmed Claim. |
| `readiness` | Prepares the workspace (§7, including a Continuation's rebase, §16), reads the Project Profile at the Base Revision and resolves the Readiness Facts. A refusal routes straight to `terminal_writes`, with no model call. |
| `baseline` | Bootstraps and evaluates the baseline (§7). A red baseline routes straight to `terminal_writes`, with no model call. |
| `run_step` | Runs the Workflow's next step through a fresh step subgraph (§8). The Workflow Steps are data `run_step` consumes; they never change the topology. |
| `gate` | The Gate between steps: re-reads the issue, the limits and the stop signals (§11). It loops back to `run_step` while steps remain and none has stopped the list. |
| `delivery_snapshot` | Commits the candidate tree and pushes it (§12). |
| `final_checks` | One authoritative run of the Validation Contract (§12). |
| `publication_gate` | The last Gate, and the verdict (§12). |
| `delivery_intent` | Records the outcome `delivered` as a Delivery Intent; the point of no return. |
| `publish` | Opens the pull request (§13). |
| `terminal_writes` | Records the Terminal Outcome when it is not yet recorded, then performs its comment, label and assignment writes (§14). |

### Gates

A **Gate** re-reads GitHub state, re-evaluates the Attempt's limits and may stop the Attempt cooperatively. Gates sit:

- between graph nodes;
- at every step boundary (`gate`);
- inside `activate_code_review`, before the review-snapshot push (§10);
- before every external write (§13).

In addition, ceilings and SIGTERM are observed **after every model response and every tool result** (§11). A step's only in-tool Gate may be an hour away, so these in-node stop points are what make "the next Gate" prompt for limits and SIGTERM. Cancellation by a human is seen at Gates that re-read the issue.

**Rows:** G-STP-1.

## 7. Readiness, the Project Profile and the baseline

### Workspace

Every Attempt, a Continuation included, builds its workspace deterministically from the local mirror: `reset --hard` to its start commit, then `clean -fdx`. The start commit is the Base Revision, or for a Continuation the rebased head of §16. The Attempt works on its own branch, `agent/<issue>/<n>-<slug>`.

### The Project Profile

The Project Profile is `docs/agents/project-profile.yml` in the Target Repository, read **at the Base Revision of the configured base branch**. It is the only Target-Project-owned input (#72), and its schema is the v1 `schema: 2`, unchanged:

```yaml
schema: 2
language: python              # python | php | typescript
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "uv sync --frozen"
  test_all: "uv run pytest -q --junit-xml={evidence_dir}/test_all.xml"
  test_targeted: "uv run pytest -q --junit-xml={evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: types
    command: "uv run mypy src tests"
services: []
```

- `{path}` is substituted with a repository-relative test path, and `{evidence_dir}` with a per-Attempt directory that the runtime creates and alone writes.
- **`evidence` is required for the test commands.** `junit-xml` is the only format.
- **`evidence` is optional for `checks`.** Without it, a check is judged by its exit code.
- **`checks` is a list of named commands or the literal scalar `none`.** An absent key is a missing Readiness Fact; an empty list is rejected as ambiguous.
- **`services`** declares `{name, url_env}`. The deployment provides them; the Worker only verifies that they are reachable.
- **One working area per profile.** Final checks and the Publication Gate cover the declared area only, and the pull request says so when the repository holds more than one.
- **There is no prose fallback.** `AGENTS.md`, `CLAUDE.md` and `docs/agents/*` are not read for Readiness Facts. An absent profile is a missing Readiness Fact.

The reference's `setup` command corresponds to `bootstrap`, and its `check` to `test_all` plus `checks`.

**The Validation Contract is the profile as it stands at the Base Revision, pinned at Claim.** An Attempt that edits the profile changes the contract for the next Attempt and never for its own. Where the Target Issue *is* the profile change, the pull request states that it was validated with the base commands.

### Readiness Facts

`readiness` resolves the Readiness Facts with no model call: the runtime and package manager, the bootstrap command, the test command, how the test command reports individual results, the checks (or `none`), the **Acceptance Criteria** and the **Seam Set**.

- **Acceptance Criteria** are enumerated from the Target Issue body by the convention of [What text convention makes an Acceptance Criterion decidable without a model call?](https://github.com/lbacik/coding-agent/issues/66#issuecomment-5734119247). The parser recognises exactly one level-2 heading whose normalised text is `Acceptance criteria`, case-insensitively, and reads the non-empty direct checklist items beneath it, up to the next heading of the same or higher level. Each direct item, with its continued lines and nested content, is one criterion; nested checklist items never create criteria of their own. A checked box still declares a criterion and is never evidence. The criteria are numbered `AC-1`, `AC-2`, … in source order. No section, more than one, or a section without direct checklist items yields no criteria.
- **The Seam Set** is derived from the Target Issue alone ([ADR 0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)). It authorises rather than verifies.
- **The enumeration is the runtime's, never the model's.** A list the model composed would let an Attempt narrow its own task.

### Readiness refusals

There is no clarification in the MVP (§19). A Readiness refusal is terminal, happens before any model call, and ends `failed` with one of these reasons:

| Condition | Reason |
| --- | --- |
| A Readiness Fact is missing: no profile, an incomplete profile, no Acceptance Criteria, no Seam Set | `missing-readiness-fact` |
| The profile does not parse or violates the schema | `invalid-profile` |
| The profile is valid, but the image cannot satisfy it: a toolchain outside the Supported Toolchain Matrix, an unknown `schema` version, an unreachable declared service | `unsupported-environment` |

The terminal comment names exactly what is missing or wrong. The Selection Label and the Worker's assignment are removed and `ready-for-human` is added (§14).

### Baseline

After Readiness, `baseline` runs `bootstrap`, then `test_all` and every check, on a tree at the Base Revision, before any model call.

- **A bootstrap failure** is an infrastructure-class failure of class `setup`: `failed` / `infrastructure-failure`.
- **A red baseline** ends `failed` / `red-baseline`. Red means a non-zero exit or any reported test failure. The comment names the failing commands and, where evidence provides them, the individual failure identifiers. A suite that executes zero tests at the baseline is not red; the empty suite is the Attempt's to fill.
- **Baseline Failure exclusion is deferred** (§19). A red baseline is refused, not worked around.

**Rows:** P-RDY-1 … P-RDY-4, P-CTX-1, P-CMD-2.

## 8. Workflow Steps

`run_step` runs the Workflow's steps in order. Each step is a **fresh step subgraph** with no conversation history: `open → model → tools | silence → … → exit`.

### Opening a step

A step's first request contains, in order:

1. **The Pinned Prefix:** the Attempt Header and the Target Issue (its title and body; no comments).
2. **A Continuation Snapshot**, when one applies: from the previous step (below), from a Context-Window Handoff within this step (§11), or, for the first step of a Continuation, from the previous Attempt (§16).
3. **A forced activation call**, selected through `tool_choice`: `activate_implement` for a `skill: implement` step, `activate_step` for a `prompt_file` step.

- **The activation result is the skill body, or the prompt file's bytes, verbatim** ([ADR 0007](../adr/0007-upstream-skill-text-is-answered-never-rewritten.md)). It is exempt from result capping. It is an ordinary Exchange Unit, never a member of the Pinned Prefix ([ADR 0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md)).
- **No skill is injected eagerly.** A skill body reaches the model only through its own activation call.
- **A Companion File reaches the model whole, with the activation of the skill that owns it, or not at all.** No tool fetches one ([ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md)).
- **Nothing from a human comment, and no file of the Target Repository, reaches the model as an instruction.** The model may read repository files with its tools; they are data.

### The Attempt Header

The Worker authors the Attempt Header. It carries:

- the Attempt id, the Base Revision, the branch and, for a Continuation, the Attempt it continues;
- the Acceptance Criteria with their ids, and the Seam Set;
- the statement that tests run through the model's shell count for nothing, and that only the runtime-owned targeted-test tool produces Criterion Evidence;
- **an answer to every slash-command reference** made by any skill the Attempt can activate: `/tdd` → `activate_tdd`, `/code-review` → `activate_code_review`, `/codebase-design` → `activate_codebase_design`. This answers the reference and does not rewrite the skill ([ADR 0007](../adr/0007-upstream-skill-text-is-answered-never-rewritten.md), #73).

### Tools

Every step gets the same tool set. YAML cannot narrow or extend it.

- the work tools: file reading, search and editing in the workspace, and command execution;
- the runtime-owned targeted-test tool (§9);
- `activate_*` for each Skill Bundle member (`implement`, `tdd`, `codebase-design`, `code-review`), and `activate_step` in a `prompt_file` step;
- **by position:** `report_step` in every non-final step, `report_completion` in the final step. Each is offered only at its position.

### Exit

- **A non-final step ends with `report_step`:** `done` or `cannot-continue`, plus a justification bounded in size.
- **The final step ends with `report_completion`** (§9), which yields the Implementation Outcome.
- **Silence is answered, not obeyed.** A response with no tool calls and no accepted exit draws one deterministic continuation message, composed by the runtime from the unmet obligations: the criteria lacking evidence, whether a candidate diff exists, which targeted tests ran. The budget is **two continuations per work node**, monotonic and never reset by progress. When it is exhausted the node ends. No continuation is issued in the verification reserve phase or after a hard ceiling (§11).
- **Any step kind may stand at any position.**

### Carried context

- **The next step opens without the previous step's history.** This holds by construction: each step is a new subgraph invocation with its own message list.
- **The step boundary composes a Continuation Snapshot** with the handoff mechanism of [ADR 0013](../adr/0013-context-window-handoff.md). It carries the previous step's `report_step` justification as a bounded field.
- **The working tree carries over.**
- **Steps declare no typed inputs or outputs.** The result type is fixed: a `report_step` result or an Implementation Outcome.
- **Continuation and claim-rejection budgets belong to the work node.** A fresh node, after a step boundary or a Context-Window Handoff, opens with both restored. The Attempt-wide meters run on unbroken (§11).

### Failure and stop

- **A non-final step that ends `cannot-continue`** stops the step list. The Implementation Outcome is `completion not established` / `step-cannot-continue`.
- **A non-final step that ends in silence** after its continuations stops the list. The Implementation Outcome is `completion not established` / `undeclared`.
- **An infrastructure error in any step** (a provider failure after its retries, a git failure) stops the list, and the Attempt ends `failed` / `infrastructure-failure` with its class.
- **A ceiling in any step** yields `saved partial work`, or `completion not established` without a diff, and then `failed-limit` (§11).
- **Cancellation at a step-boundary Gate** yields `abandoned` (§11).
- **There is no step retry, no `continue_on_error` and no skipping.** Later steps do not start once the list has stopped.

In every case the Attempt continues to the fixed tail, so any diff is committed and pushed (§12).

**Rows:** W-STP-1, W-STP-2, W-STP-4, W-STP-5, W-STP-7, G-STP-1, G-HDR-1, P-CTX-1; v1 `L3-IMP-24` … `L3-IMP-31` (silence, claims and outcomes).

## 9. Declaring completion

[ADR 0014](../adr/0014-completion-is-declared-and-substantiated-never-inferred.md) holds unchanged.

**`report_completion` is the only non-exceptional exit** from the implementation stage, the counterpart of the forced activation that opens a step. Its schema is generated per Attempt from that Attempt's Acceptance Criteria: for each criterion id, the test identifiers covering it, or an explicit `uncovered`.

**The runtime owns the evidence.** `test_targeted` runs only through the runtime-owned targeted-test tool. That tool executes the Validation Contract's command, is the sole writer of `{evidence_dir}`, parses the JUnit result and returns it. It records **Criterion Evidence**: the identifiers of every test that executed, with its result, as a history across the Attempt. Tests the model runs through its shell count for nothing.

**Claim checks.** When `report_completion` is called:

1. **Evidence.** A named test identifier that is absent from Criterion Evidence, did not execute, or did not pass in its latest recorded run makes the claim fail. The call is returned with a tool result naming the failing entries. A return is not a progress event. After **two** returns, the third call is force-accepted.
2. **Review.** A claim made with a candidate diff and no completed review in the Attempt (§10) is returned **once**, naming the missing review. If it is made again without a review, it is accepted and the Publication Gate decides (§12).

**Implementation Outcomes.** The implementation stage ends in exactly one. It is decided when the stage ends, from the claim and Criterion Evidence alone. Final checks are an input to the Publication Gate, not to the Implementation Outcome.

| Implementation Outcome | Entry condition |
| --- | --- |
| `verified completion` | Claim accepted without force; candidate diff present; every criterion names at least one test whose latest Criterion Evidence result is a pass |
| `implemented but unverified` | Claim force-accepted after two returns; candidate diff present |
| `already satisfied` | Claim accepted; no candidate diff; **every** criterion covered by passing Criterion Evidence at the Base Revision |
| `saved partial work` | The stage was cut short by an external cause (ceiling, verification reserve, soft stall) with a candidate diff and no accepted claim |
| `completion not established` | Reason `undeclared` (continuations exhausted), `step-cannot-continue` (a non-final step reported it), `claims-unverified` (force-accepted with no candidate diff) or `criteria-uncovered` (a criterion explicitly `uncovered`) |

- **An explicitly uncovered criterion is accepted, not refused.** Refusing it would leave incomplete work one path to an accepted claim: asserting coverage it does not have.
- **The category follows the moment the claim was accepted, not the moment of measurement.** A ceiling crossed after an accepted claim still reports the outcome the claim earned.
- **An empty diff without a complete per-criterion justification** is `completion not established`, never `already satisfied`.
- **Only `verified completion` may use PASS wording.**
- **Red → green is disclosed, not enforced.** The pull request states, for each claimed test, whether Criterion Evidence ever recorded it failing (#73, consistent with [ADR 0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)).

**Rows:** P-DEL-3, P-DEL-9, G-TDD-1; v1 `L3-IMP-10`, `L3-IMP-20`, `L3-IMP-24` … `L3-IMP-31`.

## 10. Review

This section amends [#62](https://github.com/lbacik/coding-agent/issues/62) as #71 and #73 decided: a pull request requires a completed review, a completed review is declared by both seats, and an Attempt may invoke review at most three times.

**Review is model-invoked.** `activate_code_review` is a Skill Bundle activation tool, offered in every step. The runtime adds no unconditional review node.

**An invocation runs in this order:**

1. **The fourth invocation in an Attempt is refused** with a tool result. The limit of three is Attempt-wide, across steps.
2. **The in-tool Gate** re-reads the issue, the limits and the stop signals. A stop found here ends the Attempt at this Gate (§11), keeping whatever is already pushed.
3. **The review snapshot** commits the current tree (with the commit trailer of §18) and pushes the branch. The push precedes either reviewer ([ADR 0002](../adr/0002-commit-and-push-before-review.md)).
4. **The review subgraph** fans out two reviewer conversations, Standards and Spec. Each has its own message list and read-only tools, so isolation holds by construction. The two seats are hard-coded in one bounded component; the generic mechanism is [#65](https://github.com/lbacik/coding-agent/issues/65).
5. **The tool result** returned to the implementer carries both Review Reports, or the seat that made none.

**The Reviewer Brief** is composed by concatenation only: a Seat Assignment, the Role Block cut verbatim from the installed `code-review` skill, and the concrete inputs the Role Block refers to. The Seat Assignment names the conversation's single axis, states that **the conversation is itself the reviewer the Role Block describes**, and states that its review ends in a Review Report.

**A seat completes only through a Review Report:** a `report_review` call carrying its findings or an explicit "no findings". A reviewer conversation that merely ends has not reviewed. **An invocation is a completed review when both seats made a Review Report.**

**Every invocation is a full re-run** of both seats against the tree at call time, metered against the Attempt-wide meters. There is no reserved review budget.

**Review status**, recorded per Attempt and disclosed in the pull request with the invocation count:

| Status | Meaning |
| --- | --- |
| `not-invoked` | `activate_code_review` was never called |
| `incomplete` | Invoked, but no invocation completed |
| `fresh` | The latest completed review is of the delivered commit |
| `stale` | The latest completed review is of an earlier commit. A review from an earlier step becomes `stale` after later changes |

**The review gate** is met when the Attempt has at least one completed review, that is, a status of `fresh` or `stale`. **Findings never block**, whatever their severity; they are disclosed in full.

**Rows:** P-DEL-3, P-DEL-4, P-DEL-5, G-REV-1, G-REV-2, G-REV-3, W-STP-6.

## 11. Limits, cancellation and SIGTERM

### Meters

Limits are properties of the **Attempt**, shared by every step, review conversation and handoff, and configured per Workflow:

| Meter | Default |
| --- | --- |
| Wall clock, measured from the Attempt's opening in the Run Ledger, never from process start | 60 minutes |
| Cost, from the Pinned Model's prices per token bucket | $20 |
| Tokens | 4 000 000 |
| Tool calls | 600 |

- **600 tool calls is a proxy for the reference's 60 turns, not an equivalent.** The MVP has no turn limit.
- **Per-step budgets are deferred** (§19).
- **Usage is counted after every model response and flushed to the Run Ledger at that moment.** A work node stops its own loop as soon as a ceiling is crossed, rather than at the next Gate.
- **Cost is computed per token bucket from the Effective Configuration's prices.** `input_tokens` includes cache hits, and a cache write may report in a provider-specific field, so input and output alone are wrong in both directions.
- **The token ceiling and context-window configuration are one set per Pinned Model,** held in the code-owned table of §2.1.

### Progress Gates and the verification reserve

These hold as in v1. The Run Ledger records a `progress_event` only when a model response resolves an Acceptance Criterion or Seam Set uncertainty, changes the candidate diff, or produces new Criterion Evidence after such a change. Five consecutive responses without one record a `soft_stall`, after which the next model action is limited to one narrow progress-producing action, the claim or verification.

At 70% of any meter the ledger records a `Gate` event with a concrete next action and a remaining-budget snapshot. At 80% it records `reserve_entry`: broad exploration ends, and the remaining capacity is reserved for the claim, review, the Delivery Snapshot and the final checks. The phase is monotonic.

### Hard ceilings

When a ceiling is crossed before the Delivery Intent:

- the model loop makes no further model call and no model-initiated tool call;
- the Implementation Outcome follows §9: `saved partial work` with a diff, `completion not established` / `undeclared` without one, or the outcome an already accepted claim earned;
- a deterministic finalization commits the existing edits and pushes the branch; no final checks run;
- the Attempt ends **`failed-limit`**, naming the limit, with no pull request and `ready-for-human`.

After the Delivery Intent, publication completes; a meter cannot stop it.

### Context-Window Handoff

A context-window crossing inside a step is a durable handoff into a fresh node ([ADR 0013](../adr/0013-context-window-handoff.md)).

- Before a request would exceed the Pinned Model's usable threshold, the node stops. One bounded handoff node, with one model invocation, no tools and its own output cap, produces a versioned, redacted **Continuation Snapshot**. Its fields are the Attempt id and Base Revision, the branch and commit, changed files, commands and results, available evidence, unresolved questions and the next intended action.
- The snapshot is persisted as an Attempt artifact, addressed by its SHA-256 digest, recorded in the Run Ledger and charged to the Attempt-wide meters.
- **The snapshot has a hard size cap of 32 KiB after redaction,** enforced when it is composed, so a terminal comment that carries it always fits.
- The fresh node reopens exactly as the step did: the Pinned Prefix, the snapshot in place of history, and the same forced activation with identical bytes.
- **A malformed, over-limit, unpersistable or unverifiable snapshot** ends the Attempt `failed` / `handoff-failure`. A Pinned Prefix that cannot fit with its overhead is refused before any model work.
- Within a node, every assistant turn is carried back verbatim ([ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md)), and Exchange Units stay whole.

**An oversized tool result is capped** to its head, its tail and a pointer naming the stored artifact and its full size, so the model can ask for a range of it. Activation results are exempt.

### Cancellation

**A human cancels by closing the issue or removing the Selection Label.** A Gate before the Delivery Intent that sees either:

- stops the model loop;
- commits the existing edits and pushes the branch;
- ends the Attempt **`abandoned`**, with no pull request, the terminal comment, `ready-for-human`, the Selection Label removed (when still present) and the Worker's assignment removed.

After the Delivery Intent, cancellation does not stop publication. A pull request closed by a human before its receipt is a withdrawal (§13).

### SIGTERM

- **Intake stops** at once.
- **The live Attempt observes the signal after every model response and every tool result.** The in-flight model request is cancelled and the running command's whole process group is killed.
- **Before the Delivery Intent,** the Attempt then takes the claim-confirmed path of the recovery table (§17) while the process is still alive, within `shutdown_grace_seconds`: local commits are pushed fast-forward, the dirty tree is not committed, and the outcome is `failed` / `interrupted`, or `abandoned` if the issue was cancelled meanwhile.
- **After the Delivery Intent,** publication completes.
- **A second signal, or SIGKILL,** is an ordinary process loss (§17).

**Command timeouts** kill the command's whole process group, never only the shell.

**Rows:** P-LIM-1, P-CAN-1, P-CAN-2, P-CAN-3, W-STP-3, G-REV-3, P-CMD-1, REC-SIG-1, REC-SIG-2.

## 12. Delivery Snapshot, final checks and the Publication Gate

### Delivery Snapshot

`delivery_snapshot` runs for every Implementation Outcome. When the working tree differs from the Attempt's last commit, it commits the whole tree as the **Delivery Snapshot**, with the commit trailer of §18. It then pushes the branch whenever the branch has commits the remote does not. Commit and push precede the final checks ([ADR 0002](../adr/0002-commit-and-push-before-review.md)), so one process loss destroys at most one phase of an Attempt.

### Final checks

`final_checks` runs only for `verified completion`, the one Implementation Outcome that can reach a pull request. It is **one authoritative run** of `test_all` and every check against the Delivery Snapshot, outside the model's tool loop. There is no remediation round and no retry.

Its result is **Validation Evidence** in the Run Ledger: the commands, their exit codes, how many tests actually executed and the identifier of every individual failure.

- **Green** means every command exits zero, `test_all` executes at least one test and reports no failure, and every check passes. `checks: none` satisfies the checks.
- **Exit zero is necessary, not sufficient.** A run that executes no tests is missing evidence, and missing evidence is red.
- **There is no Baseline Failure exclusion.** Any failure is red.
- **After a green run the tree must be clean.** Any modified or untracked file left by the checks is a dirty tree.
- **"Verified" means Validation Evidence produced by the runtime.** The model's account of a test run is never evidence.

### The Publication Gate

`publication_gate` is the last Gate. It first handles stop signals like any Gate (§11). It then re-reads the Target Issue and decides the verdict.

**A pull request requires all of these:**

1. the Implementation Outcome is `verified completion`;
2. no **Drift**: the Fingerprint (title and body) is unchanged since Claim;
3. the **review gate** is met (§10);
4. the final checks are **green**;
5. the tree is **clean** after them.

When all hold, the verdict is a ready pull request and `delivery_intent` follows. Otherwise there is no pull request, and the first unmet condition, in the order above, names the reason. The terminal comment lists every unmet condition.

**Base-branch movement does not block.** The gate compares the base head with the Base Revision. When they differ, the pull request says so and names the commit the work was implemented and verified against. The Worker neither rebases nor merges.

### The Delivery Intent

Before the first delivery write, `delivery_intent` records the Terminal Outcome `delivered` as a **Delivery Intent** in the Run Ledger: the Attempt id, branch, head sha, the verdict, the rendered pull-request and comment bodies, and the writes the outcome calls for ([ADR 0003](../adr/0003-delivery-intent.md)).

**Writing the Intent is the point of no return.** A stop signal before it diverts the Attempt; after it, the publication completes as itself and is never repeated as a new Attempt.

**Rows:** P-DEL-1, P-DEL-2, P-DEL-5 … P-DEL-10, P-CAN-2.

## 13. External writes

### The write sequence

Every external write follows one sequence:

1. pass the Gate in front of it (before the Delivery Intent);
2. **record the intent** in the Run Ledger, keyed by **`(attempt_id, write_kind)`**;
3. **look the effect up remotely**;
4. **write only if it is absent**;
5. **record the receipt**: the remote sha, pull-request number or comment id.

- **The key is `(attempt_id, write_kind)`, not the payload.** The payload's digest is kept for audit only. `write_kind` is unique within an Attempt; a kind that can repeat, the review-snapshot push, carries its ordinal.
- **Bodies are rendered once,** when the outcome is recorded (§14), so a retried write sends the same bytes.
- **The absence of our effect and a human's reversal of it are different states,** and only the absence authorises a write.

### Effect identity

| Effect | Identity | Remote lookup | Rule |
| --- | --- | --- | --- |
| Branch | `agent/<issue>/<n>-<slug>`; the `agent/` prefix is the marker | `ls-remote` | **Fast-forward only, never force.** The remote ref is ours when it equals the last receipt's sha or an open push intent's sha, and a missing receipt is then recorded. Any other value is a foreign change: before a Delivery Intent the outcome is `interrupted`, with no push and a note in the comment; after one it is `failed` / `branch-moved`. |
| Commit | trailer `Attempt: #<issue>/<n>` | — | — |
| Pull request | head branch plus the Attempt Marker in the body | pull requests with `head=<owner>:<branch>`, `state=all` | Open with the marker: the receipt. Closed: a withdrawal, which ends `abandoned` and is never reopened. Absent: create it. |
| Terminal comment | the Attempt Marker with `kind=terminal` | the **full, paginated** comment list, filtered to the Agent Identity's numeric id and the marker | Present: the receipt. Absent: create it. Never a second one. |
| Labels | the pinned names | read back | Naturally idempotent. |
| Assignment removal | — | read back | Only when `assigned_by_us`; never another assignee. |

A comment or pull request by another account that carries our marker is not our receipt.

### Rate limits and refusals

Classification is by **status, header value and documented error signal**, never by the mere presence of `x-ratelimit-*` headers.

| Observation | Classification | Action |
| --- | --- | --- |
| 401, confirmed by the identity probe | Credential Failure | `failed` / `credential-failure`; **the Worker halts** (§3) |
| 403 with `x-ratelimit-remaining > 0` | Permission refusal | `failed` / `permission-refusal`; the Worker continues |
| 403 or 429 with `x-ratelimit-remaining: 0` | Primary rate limit | Wait until `x-ratelimit-reset`, then retry in place |
| 403 or 429 carrying the documented secondary-limit signal | Secondary rate limit | Honour `Retry-After`; back off exponentially when it is absent |

In-place retry is bounded at **3 attempts and 5 minutes**, and the waiting counts against the Attempt's wall clock. Beyond the bound the failure is infrastructure-class (`github`). Before an outcome is recorded it ends the Attempt `failed` / `infrastructure-failure`; after, the outcome stays recorded and recovery finishes its writes on the next poll cycle.

**Write scope stops short of Workflows.** The Worker refuses, itself, to push a commit touching `.github/workflows/`, and the Attempt ends `failed` / `workflows-path`. The platform's rejection is the second line.

**Rows:** P-REC-2, REC-INT-4, REC-DEL-1 … REC-DEL-6; v1 `L3-DEL-21`.

## 14. Terminal outcomes

There are four Terminal Outcomes: `delivered`, `abandoned`, `failed` and `failed-limit`. Every other name is a reason.

### Recording before publishing

**Every Terminal Outcome is recorded in the Run Ledger before its first write:** the outcome, its reason, the rendered comment and pull-request bodies, and the list of writes it calls for ([ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md)). Recovery finishes a recorded outcome and never re-decides it.

### Implementation Outcome → Terminal Outcome

| Implementation Outcome | Pull request | Terminal Outcome |
| --- | --- | --- |
| `verified completion`, every Publication Gate condition met | ready, `Closes #<issue>` | `delivered` |
| `verified completion`, a condition unmet | none; branch pushed | `failed` / `drift` \| `review-missing` \| `failing-validation` \| `dirty-tree` |
| `implemented but unverified` | none; branch pushed | `failed` / `unverified` |
| `already satisfied` | none | `failed` / `already-satisfied` |
| `saved partial work` | none; branch pushed | `failed-limit` for a ceiling, otherwise `failed` with the cause: `soft-stall` or `verification-reserve` |
| `completion not established` | none; branch pushed when a diff exists | `failed` / `no-completion-evidence`, or `failed-limit` when a ceiling ended the stage |

### Outcomes reached before or around implementation

| Route | Terminal Outcome |
| --- | --- |
| Readiness refusal (§7) | `failed` / `missing-readiness-fact` \| `invalid-profile` \| `unsupported-environment` |
| Red baseline (§7) | `failed` / `red-baseline` |
| Continuation rebase conflict (§16) | `failed` / `continuation-conflict` |
| Cancellation before the Delivery Intent (§11) | `abandoned` |
| A pull request closed by a human before its receipt (§13) | `abandoned` |
| SIGTERM, or a process loss, before an outcome was recorded (§11, §17) | `failed` / `interrupted`, or `abandoned` |
| The remote branch is not at the Delivery Intent's sha (§13) | `failed` / `branch-moved` |
| Context-Window Handoff failure (§11) | `failed` / `handoff-failure` |
| An infrastructure-class failure (setup, git, GitHub API, provider) | `failed` / `infrastructure-failure`, with the class |
| Credential Failure (§13) | `failed` / `credential-failure`; the Worker halts |
| Permission refusal (§13) | `failed` / `permission-refusal` |
| A push touching `.github/workflows/` (§13) | `failed` / `workflows-path` |

### The terminal writes

After a pull request, when there is one, `terminal_writes` performs, in this order:

1. **the terminal comment**, carrying the Attempt Marker (§15);
2. **removal of the Selection Label**, by its pinned name;
3. **`ready-for-human`**, by its pinned name, on **every outcome without a pull request**, `abandoned` included;
4. **removal of the Worker's own assignment**, only when `assigned_by_us`.

**`delivered`** adds no `ready-for-human`. The Worker never merges, never converts or creates a draft pull request, never deletes a remote branch and never force-pushes.

**Rows:** P-DEL-1, P-DEL-2, P-DEL-6, P-DEL-8, P-DEL-9, P-DEL-10, P-RDY-1 … P-RDY-4, P-LIM-1, P-CAN-1, REC-OUT-1, REC-OUT-2.

## 15. Pull request and comment contents

### The Attempt Marker

Every comment and pull-request body the Worker writes carries one machine-readable line:

```
<!-- coding-agent attempt=#<issue>/<n> kind=<terminal|delivery> -->
```

Commits carry the trailer `Attempt: #<issue>/<n>`, and branches the `agent/` prefix (§18). The rule reads in one direction only: no marker means not ours.

### The pull request

- **Title:** the Target Issue's title.
- **Base:** the configured base branch. **Head:** the Attempt's branch. Never a draft.
- **Body**, in this order:
  1. `Closes #<issue>` and the Attempt Marker (`kind=delivery`);
  2. the Attempt id, the Workflow id and the Effective Configuration digest;
  3. the Base Revision sha, and a base-movement note when the base branch has moved;
  4. for each Acceptance Criterion: its id and text, the claimed tests, their Criterion Evidence result, and whether each was ever observed failing;
  5. Validation Evidence: the commands, exit codes and executed test counts;
  6. the review status and invocation count, and both Review Reports of the latest completed review in full, inside `<details>` blocks;
  7. the Seam Set used;
  8. the declared working area when the repository holds more than one, and a note when the issue changed the Project Profile and was validated with the base commands.

A part over the size limit is truncated with an explicit marker and a pointer to the log, never silently. The review reports go in the body, not in separate comments, so the pull request never exists without its evidence.

### The terminal comment

One per Attempt, with the Attempt Marker `kind=terminal`. It states:

- the Terminal Outcome and its reason;
- a link to the pull request or, when there is none, to the pushed branch;
- the Attempt id, the Workflow id and the Effective Configuration digest;
- for a failure, one sentence on what a human must do next, and any reason-specific detail: the missing Readiness Fact, the failing baseline commands, the unmet Publication Gate conditions, the limit crossed, the count of uncommitted files recovery could not preserve, or a foreign push to the branch;
- the **Continuation Snapshot**, when there is one.

### The Continuation Snapshot in the comment

- **The snapshot is a versioned block carrying its SHA-256 digest,** within the 32 KiB cap of §11.
- **Live outcomes without a pull request.** When an Attempt ends without a pull request while the process is alive, the runtime composes a terminal snapshot deterministically, with no model call. It holds the fields of §11 that the runtime knows: the Attempt id, the Base Revision, the branch and pushed head, changed files, commands and results, and evidence. Unresolved questions and the next intended action are carried from the latest model-produced snapshot when one exists.
- **Recovered outcomes.** At recovery the comment carries the last persisted snapshot, but only if its commit is an ancestor of the pushed head; otherwise it carries none.

**Rows:** P-DEL-1, P-LIM-1, W-PIN-2, G-TDD-1, REC-INT-5, REC-WRK-3.

## 16. Continuation

A **Continuation** is a new Attempt that starts from the pushed work of the Target Issue's previous Attempt.

- **Only a human starts one,** by re-applying the Selection Label after a Terminal Outcome. There are no automatic new Attempts.
- **The previous work** is the head of the branch pushed by the most recent previous Attempt that pushed one. If that branch no longer exists on the remote, there is no previous work.
- **The new Attempt runs on its own new branch,** `agent/<issue>/<n>-<slug>`. It is created at the previous head and rebased onto the current Base Revision during `readiness`. The previous branch is never rewritten.
- **A rebase conflict** ends the Attempt `failed` / `continuation-conflict` with `ready-for-human` and no model call.
- **A deleted previous branch is a request for a fresh start.** The new Attempt starts from the Base Revision. The Worker itself never deletes a remote branch.
- **The Acceptance Criteria are re-enumerated** from the current body, and the Effective Configuration is the current one.
- **The baseline** is evaluated at the new Base Revision (§7).
- **The previous Continuation Snapshot** is read back from the previous Attempt's terminal comment through the paginated, author- and marker-filtered lookup, and its digest is verified. The first step opens with the Pinned Prefix and that snapshot. A snapshot that is absent or fails verification counts as none: the Attempt continues from the branch and its diff against the base, and the Attempt Header says so.
- **The snapshot is never committed to the Target Project's tree.** There is no `handoff` skill in the Skill Bundle.

**Rows:** P-HND-1 … P-HND-4, W-PIN-4, REC-WRK-3.

## 17. Recovery

Recovery runs at startup, before any selection, and **never calls the model** ([ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md)). The Supervisor reads the ledger and invokes the Attempt graph at the node the ledger state calls for; that node performs every write.

### Durable state

| State | Owner | Used by recovery |
| --- | --- | --- |
| Attempt lifecycle, identity, Fingerprint, Base Revision, Agent Identity id, `assigned_by_us` | Run Ledger | yes |
| Effective Configuration and its digest | Run Ledger, written at Claim | yes: repository, label names, base branch |
| Recorded Terminal Outcome, reason, rendered bodies, list of writes | Run Ledger, written before the first terminal write | yes: finished as recorded |
| Delivery Intent | Run Ledger, as a recorded outcome that calls for a pull request | yes |
| Write intents and receipts | Run Ledger | yes, then verified remotely |
| Continuation Snapshot | Run Ledger, and published in the terminal comment | yes: published if its commit is an ancestor of the pushed head |
| Usage, budgets, progress events | Run Ledger (event table) | the wall clock only |
| LangGraph graph state | the process | **no**: there is no checkpointer |
| Working tree | the state volume | local commits only |

### Storage

The Run Ledger is **one SQLite file** on the state volume, in WAL mode with `synchronous=FULL`, accessed under the volume lock. Tables are created with `CREATE TABLE IF NOT EXISTS`; there is no schema version in the MVP.

- **`attempts`:** `#<issue>/<n>`; `state`, one of `open` → `claim-confirmed` → `outcome-recorded` → `finished`, or `released`; the Effective Configuration and its digest; the Fingerprint, Base Revision and Agent Identity id; `assigned_by_us`.
- **`effects`:** unique on `(attempt_id, write_kind)`, with a status of `intent` or `receipt`, the remote id, and the rendered payload with its digest.
- **`outcomes`:** the Terminal Outcome, its reason and its writes. A Delivery Intent is an outcome row carrying the pull-request fields.
- **`run_ledger_events`:** progress, budgets and snapshot references.

**Liveness is the volume lock.** Once a Worker holds it, every open Attempt in the ledger was orphaned by a dead process. There is no Lease.

### The recovery table

| Crash point | Ledger state | Recovery |
| --- | --- | --- |
| Before the Attempt transaction | none | Nothing. |
| After the transaction, before the read-back | `open` | **`released`:** remove our assignment if present. No comment and no label change; the issue stays eligible, and its next Attempt is `#<issue>/<n+1>`. |
| Claim confirmed, no outcome recorded: during readiness, the baseline, any step, a review-snapshot push, the Delivery Snapshot, the final checks or the Publication Gate | `claim-confirmed` | **Preserve:** push local commits that descend from the remote head, fast-forward only. Do not commit the dirty tree; the comment states how many uncommitted files were not preserved. A foreign remote head means no push and a note. **Decide:** re-read the issue once. Closed, or the Selection Label gone, is **`abandoned`**; otherwise **`failed` / `interrupted`**. **Record** the outcome, then perform its terminal writes (§14). |
| An outcome without a pull request recorded, writes partly done | `outcome-recorded` | **Finish the remaining writes** of that outcome. Never re-decide it, and never write a second comment. |
| A Delivery Intent recorded, writes partly done | `outcome-recorded`, with an Intent | **Check the branch:** the remote branch must be at the Intent's sha, otherwise `failed` / `branch-moved`. **Find the pull request** by head branch and marker: open is the receipt, closed is a withdrawal (`abandoned`, never reopened), absent means create it. **Then** the comment, labels and assignment, each looked up before it is written. |
| Every write done, during workspace cleanup | `finished`, or `outcome-recorded` with every receipt | Mark `finished`. |

- **Pinned values only.** Recovery uses the repository, label names, base branch, Agent Identity and rendered bodies from the ledger.
- **Failures in recovery are bounded.** A GitHub write failure is infrastructure-class: retried on the next poll cycle, and three in a row halt the Worker (§3). A Credential Failure halts at once.
- **Cleanup is not part of an Attempt.** A workspace is discarded only when no unfinished Attempt exists, so recovery's fast-forward push always runs first. A crash during cleanup is harmless, because every new Attempt rebuilds its workspace from the mirror (§7).

### Ledger loss

Ledger loss is **outside the MVP's guarantees** and is handled by a runbook.

- **What still holds:** the remote lookups of §13 still prevent a duplicate pull request or terminal comment.
- **What does not:** an Agent Identity assignment with no ledger record is indistinguishable from the owner's own ([ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md)), so a human releases it.
- **Numbering:** to avoid branch collisions, the next `n` is one more than the highest of the ledger's `n`, the `agent/<issue>/<n>-*` branches and the terminal-comment markers.

**Rows:** P-REC-1 … P-REC-4, P-CLM-3, W-PIN-3, every REC-* row.

## 18. Identity and authorization

**The Agent Identity is the Target Repository owner's own GitHub account** ([ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md)). It authenticates with a fine-grained personal access token owned by that account, scoped to the single Target Repository, with the Workflows permission withheld. It is recognised by its **numeric account id**, resolved once at startup by `GET /user`.

- **The Attempt Marker, not the account, tells the Worker's writing apart from a human's.** Every artifact carries one in its own idiom: the marker line in a comment or pull-request body (§15), the `Attempt: #<issue>/<n>` trailer on a commit, the `agent/` prefix on a branch. No marker means not ours. A marker does not prove authorship, because whoever holds the credential can write one; lookups therefore also filter by the Agent Identity's id (§13).
- **Two structural abstentions.** The Worker **never edits the Target Issue's body** and **never applies the Selection Label**. A body edit is therefore always a human's, and the Selection Label is always a human's authorisation.
- **The Claim is an assignment** ([ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md), §5). The Worker removes only an assignment it recorded itself. An owner who assigns themselves to an issue has excluded it.
- **The Worker shares the owner's API rate limit,** so startup checks for headroom rather than assuming the budget is its own.
- **One identity for API calls and for git.** Author and committer use the id-based noreply address `<id>+<login>@users.noreply.github.com`. Every commit carries the trailer `Attempt: #<issue>/<n>`.
- **Commits are unsigned.** A base branch requiring signatures rejects the push as a non-transient failure.
- **One branch per Attempt,** `agent/<issue>/<n>-<slug>`. Force-push is never needed and the capability is never held. The Worker never deletes a branch.
- **Credentials pass through one choke point:** loaded into a redaction registry at startup, filtered out of both log streams and out of tool arguments and results, never present in the environment of child processes, and used for git through a credential helper rather than a remote URL.

### Preflight and startup checks

- **`agent preflight`** is an operator command, run against a sandbox repository and never at startup. It performs every write kind the contract needs (an issue comment, a label added and removed, an assignment made and removed, a branch pushed, a pull request opened and closed) and asserts that a push touching `.github/workflows/` is **rejected**.
- **Worker startup is non-mutating.** It calls `GET /user` for the numeric id and login and `GET /repos/{owner}/{repo}` for `permissions.push == true`, and checks rate-limit headroom.
- **Startup refuses the wrong kind of token.** The credential must carry the fine-grained prefix `github_pat_`. The `GET /user` response must carry no `X-OAuth-Scopes` header. A token below the configured remaining-lifetime threshold, where the response reports an expiry, is refused.
- **The Provider Capability Assertion** establishes in one small live call that the Pinned Model can call a tool, honours the pinned effort and reports non-zero usage, and asserts that the Effective Configuration prices it ([ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md)). A failed assertion exits non-zero with no Attempt opened.

**Rows:** P-WRK-3, REC-DEL-6; v1 `L3-DEL-21`.

## 19. Deferred

Deferred, not dropped. Each item keeps its full design in the frozen v1 contract. When one returns, its text moves into this document as an amendment.

| Item | Deferred by | v1 design |
| --- | --- | --- |
| Clarification: Clarification Round, Question Set, Qualifying Answer, Stale Answer, Carried Decision and the `needs-info` resume path | #71 | [§9](./v1-runtime-contract.md#9-clarification), [§2](./v1-runtime-contract.md#2-selection-and-label-algebra), [§3](./v1-runtime-contract.md#3-transition-table) T3–T6 |
| Delivery Draft (`delivered-as-draft`) | #71 | [§3](./v1-runtime-contract.md#3-transition-table) T9, [§11](./v1-runtime-contract.md#11-terminal-outcomes) |
| Baseline Failure exclusion | #71 | [§8](./v1-runtime-contract.md#8-validation-evidence-and-baseline-failures) |
| The remediation round ([#26](https://github.com/lbacik/coding-agent/issues/26)) | #71 | [§4](./v1-runtime-contract.md#4-node-inventory) |
| Model resumption after process loss (T13, T14, T14a) | #71, #74 | [§3](./v1-runtime-contract.md#3-transition-table), [§6](./v1-runtime-contract.md#the-delivery-intent) |
| Lease | #74 | [§1](./v1-runtime-contract.md#1-layers-and-ownership), [§3](./v1-runtime-contract.md#3-transition-table) T1 |
| Automatic Retry Budget (T10) | #71 | [§3](./v1-runtime-contract.md#3-transition-table) |
| Rebuilding Attempt counts from markers after a ledger loss (v1 `L3-DEL-16`, R5) | #74 | [§6](./v1-runtime-contract.md#6-external-writes) |

Deferred by #72, with no v1 design: per-step budgets; typed step inputs and outputs; additional top-level skills; tool-set customisation; renaming lifecycle labels other than `ready-for-human`; tracker adapters other than GitHub.

## 20. Differences from the reference

The reference is `simple-coding-agent` at `fb50952`. Its code and tests were the authority over its README.

### Reference defects not copied

| Reference behaviour | MVP | Decided in |
| --- | --- | --- |
| Recovery maps a PUSHING or PUBLISHING checkpoint without a handoff note to *complete*, so it can open a pull request for partial work | Recovery finishes a recorded outcome and never turns partial work into a pull request | #71, #74, [ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md) |
| Recovery re-decides the outcome, while its marker deduplication keeps the original comment | The outcome is recorded before publication; the comment cannot disagree with it | #74 |
| Recovery forces the local branch to the remote (`branch -f`) and discards unpushed commits | Unpushed local commits are pushed fast-forward | #71, #74 |
| A crash between claim and checkpoint leaves the issue assigned forever | The Attempt is opened before the assignment; recovery releases it | #71, #74 |
| A sealed but unpushed commit after an interrupted final check | Commit and push precede the final checks; recovery pushes local commits | #71, [ADR 0002](../adr/0002-commit-and-push-before-review.md) |
| The profile is bootstrapped from a hard-coded `main` | The profile is read at the configured base branch's Base Revision | #71 |
| Comment lookups use `first:100` / `last:100` with no author filter; open PRs are matched by head name only | Complete, paginated lookups filtered by author and marker | #71, #74 |
| A generic crash message and a generic PR summary | Reason-specific comment and evidence-bearing PR body (§15) | #71 |
| Dead `REVIEW_BLOCKING_SEVERITIES` | Findings are non-blocking by rule | #71 |
| A command timeout kills `sh` but not its process group | The whole process group is killed | #71 |
| Note-only "handoff" commits turn an empty attempt into a handoff | The Continuation Snapshot is never committed | #71 |
| The error guard exits before finalization, causing a restart loop | The halt follows full finalization | #71 |
| A dirty tree at recovery holds until an operator acts | `interrupted`, and the comment says what was not preserved | #74 |
| SIGTERM re-signals and dies immediately | SIGTERM is cooperative within a grace period | #74 |
| Its own earlier push can be misread as foreign | Our push is recognised from receipts | #74 |
| Recovery applies the current profile and environment | Configuration is pinned per Attempt | #72, #74 |
| README says `ready-for-agent` + `ready-for-human` is never eligible; the code claims it | Excluded | #71 |
| Reviewer briefs are model-written | Reviewer Briefs are composed by concatenation | #73 |

### Signals and rules added beyond the reference

| Reference | MVP | Decided in |
| --- | --- | --- |
| Selection: `ready-for-agent`, no assignee, no `claimed-by:*`, non-empty body, no blocker | Also excludes `ready-for-human`, `wontfix`, `wayfinder:*` and an open marked delivery PR; order ties broken by number | #71 |
| Claim: assign, wait, read back | The same, with no claim comment, and only the Worker's own assignment is ever removed | #71, [ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md) |
| Trusted context: body plus comments by the author and agent logins | Title and body; no comments | #71 |
| No readiness facts | A missing Acceptance Criterion, Seam Set or profile fact is a terminal refusal before any model call | #71 |
| A red check on a first attempt ends `incomplete` | A red baseline is a terminal refusal naming the failures | #71 |
| The SDK run ends; a Stop hook forces one review | `report_completion`, runtime-owned evidence and five Implementation Outcomes ([ADR 0014](../adr/0014-completion-is-declared-and-substantiated-never-inferred.md)) | #71 |
| At least one review call; findings never collected | At least one **completed** review, declared by both seats' Review Reports; at most three; status disclosed | #71, #73 |
| Review via Claude Code's native slash commands | The Attempt Header answers slash references with activation tools | #73 |
| Red → green observed in the reference run | Disclosed per test, not enforced | #73 |
| No Drift detection | Drift ends `failed` / `drift` with no PR | #71 |
| No failure label | `ready-for-human` on every outcome without a PR | #71 |
| Terminal comment: outcome, links, evidence, dedup marker | Also the reason, what a human must do, the Workflow id and digest, and the Continuation Snapshot | #71, #72 |
| PR body: `Closes #N`, generic summary, evidence | Also per-criterion evidence and red history, review status and reports, Base Revision, Workflow id and digest | #71, #72, #73 |
| No cancellation from the issue | Closing the issue or removing the Selection Label ends `abandoned` | #71 |
| 60 turns, $20, 4M tokens, 3600 s model timeout | 60 min wall clock, $20, 4M tokens, 600 tool calls; no turn limit | #71, #72 |
| Re-labelling continues the one branch per issue; the `handoff` skill writes `.agent/handoff/N.md` | A Continuation runs on a new branch per Attempt, with the runtime's Continuation Snapshot | #71 |
| Configuration from environment variables; labels hard-coded | A strict, versioned Workflow Configuration with configurable labels and pinning | #72 |
| `model_instructions` from the Target Repository's profile | No instruction channel from the Target Project | #72, [ADR 0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md) |
| An open tool surface (Claude Code offered `ScheduleWakeup`) | A closed tool set | #73 |
| No startup refusals | Configuration validation, Skill Bundle digest, token-kind checks and the Provider Capability Assertion | #71, #72 |
| Halt after three `infrastructure_error`s | Halt after three infrastructure-class failures, after finalization; a Credential Failure halts at once | #71 |

## 21. Differences from v1

### Retained

| v1 guarantee | Where now | Source |
| --- | --- | --- |
| Run Ledger authority over lifecycle history | §17 | #71 |
| The Attempt Marker, read in one direction | §15, §18 | #71 |
| Completion is declared and substantiated; runtime-owned Criterion Evidence | §9 | [ADR 0014](../adr/0014-completion-is-declared-and-substantiated-never-inferred.md) |
| Delivery Intent as the point of no return | §12 | [ADR 0003](../adr/0003-delivery-intent.md) |
| Commit and push before validation and review | §10, §12 | [ADR 0002](../adr/0002-commit-and-push-before-review.md) |
| Project Profile `schema: 2`; the Validation Contract pinned at the Base Revision | §7 | #71 |
| Seam Set as a Readiness Fact | §7 | [ADR 0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md) |
| Forced `activate_implement` and per-skill activation tools | §8 | [#60](https://github.com/lbacik/coding-agent/issues/60) |
| Context-Window Handoff and the Pinned Prefix as a region | §8, §11 | [ADR 0013](../adr/0013-context-window-handoff.md), [ADR 0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md) |
| Verbatim assistant turns; Companion Files injected or refused | §8, §11 | [ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md), [ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md) |
| Attempt-wide meters, usage counted per response, Price Table per bucket, progress Gates and the verification reserve | §11 | #71 |
| Rate-limit classification and bounded in-place retry | §13 | carried from v1 |
| Agent Identity as the owner's account; token-kind refusals; preflight split; Provider Capability Assertion | §3, §18 | [ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md), [ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md) |
| Branch `agent/<issue>/<n>-<slug>` and commit trailer `Attempt: #<issue>/<n>` | §13, §18 | carried from v1 |
| Credential Failure halts the Worker | §3 | #71 |
| Four of the five Terminal Outcomes and their reasons | §14 | #71 |

### Superseded

| v1 rule | MVP rule | Decided in |
| --- | --- | --- |
| §1: Attempt Graph with a `thread_id` checkpoint | One fixed graph, no checkpointer; recovery from the ledger only | #73, #74, [ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md) |
| §1, §4: a fixed `implement` node | Operator-configured Workflow Steps fill the implementation stage | #72, [ADR 0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md) |
| §2: ascending issue number; exclusions without native blockers or empty bodies | `(created_at, number)`; native blockers and empty bodies excluded; YAML filters | #71, #72 |
| §2, §3 T16: one explanatory comment for Selection Label + `ready-for-human` | Excluded with no write | #71 |
| §3, §10: `agent-running` label; no assignment ever | Claim by assignment of the Agent Identity, read back | #71, [ADR 0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md) |
| §3 T13, T14, T14a | The recovery table of §17 | #74 |
| §4 T8: unconditional, blocking `review_fanout` after validation | Model-invoked review; the review gate requires at least one completed review, at most three, findings non-blocking | #71, #73 |
| [#62](https://github.com/lbacik/coding-agent/issues/62): a review counts once invoked | A review is completed only when both seats made a Review Report | #73 |
| §5: Implementation Outcome decided after validation; force-accepted claims are `completion not established` / `claims-unverified` | Decided from the claim and Criterion Evidence at the end of the stage; a force-accepted claim with a diff is `implemented but unverified` | #71, #73 |
| §5: concrete ceilings are tuning constants | Defaults 60 min, $20, 4M tokens, 600 tool calls, configurable per Workflow | #71, #72 |
| §5: SIGTERM handling unspecified | SIGTERM observed with the ceilings, `shutdown_grace` | #74 |
| §6: idempotency key `(attempt_id, write_kind, payload_digest)` | `(attempt_id, write_kind)`; digest for audit only; outcome recorded before publication; paginated, author-filtered lookups; branch-ownership rule and `branch-moved` | #74 |
| §6: beyond the retry bound, the Automatic Retry Budget | An infrastructure-class failure | #71 |
| §7: profile and the prose fallback chain; clarification for missing facts | The profile alone; a missing fact is a terminal refusal | #71, #72 |
| §7, §8: red baseline excused per failure; validation with remediation | A red baseline is refused; one authoritative final run; any red is red | #71 |
| §11: five Terminal Outcomes | Four: `delivered-as-draft` deferred | #71 |
| §11: Terminal writes remove `agent-running`; no assignment change | Comment, Selection Label, `ready-for-human` on every outcome without a PR, then the Worker's own assignment | #71 |
| §12: PR body with Fingerprint, Carried Decisions, outstanding findings, `Closes` only for clean delivery | §15: per-criterion evidence and red history, review status, Workflow id and digest; every PR is a clean delivery | #71, #72, #73 |
| §12: a short issue comment | The terminal comment of §15, with the reason and the Continuation Snapshot | #71 |
| ADR 0013: the Continuation Snapshot works within an Attempt | It also carries work across Attempts, through the terminal comment | #71 |
| Model, prices and thresholds hard-coded; repository and token from CLI flags | The Workflow Configuration; context configuration stays in code | #72 |

### Deferred

Everything in §19.
