# Step-workflow acceptance matrix

**Status: current.** This document defines what must be true before the step-workflow runtime replaces the MVP runtime, and how each thing is shown to be true. Behaviour is defined by [the step-workflow runtime contract](../contract/step-workflow-runtime-contract.md). The slices are in [the step-workflow implementation plan](./step-workflow-implementation-plan.md). [The MVP acceptance matrix](./mvp-acceptance-matrix.md) is a frozen design record.

The catalogue of scenarios was agreed in [What specification, acceptance scenarios and implementation order make step workflows ready to build?](https://github.com/lbacik/coding-agent/issues/285#issuecomment-6800378643) (**#285**). It incorporates the scenarios of its sibling decisions, cited by ticket number:

| Cited as | Decision |
| --- | --- |
| #279 | [What may a Workflow configure, and which guarantees remain mandatory in the runtime?](https://github.com/lbacik/coding-agent/issues/279#issuecomment-6797755715) |
| #280 | [What is the execution and data contract of one Workflow Step?](https://github.com/lbacik/coding-agent/issues/280#issuecomment-6798402008) |
| #281 | [How do Step Results select successors, bound loops and terminate a Workflow?](https://github.com/lbacik/coding-agent/issues/281#issuecomment-6798622518) |
| #282 | [How do step limits, Attempt budgets and AI-assisted Handoff compose?](https://github.com/lbacik/coding-agent/issues/282#issuecomment-6798892866) |
| #283 | [How are step transitions, terminal outcomes and external effects recorded and recovered?](https://github.com/lbacik/coding-agent/issues/283#issuecomment-6799526314) |
| #284 | [Can one YAML contract express implementation and standalone review with explicit failure paths?](https://github.com/lbacik/coding-agent/issues/284#issuecomment-6800063171) |
| #286 | [How are issue and pull-request inputs selected, claimed and pinned for a Workflow?](https://github.com/lbacik/coding-agent/issues/286#issuecomment-6799233346) |
| #288 | [Which details did the step-workflow contract transcription leave open?](https://github.com/lbacik/coding-agent/issues/288#issuecomment-6801265307) |

## How to read a row

Every row states four things besides its scenario:

- **Layer.** The section a row sits in is its layer, L1–L4 (below).
- **Owed by.** The slice that must make the row green: S1–S10 (S7 is split into S7a and S7b). A slice is done when its rows are green.
- **Contract.** The section of [the step-workflow runtime contract](../contract/step-workflow-runtime-contract.md) the row proves.
- **Source.** The decision the row comes from.

### Row identifiers

Ids are stable and are never renumbered. A row whose meaning changes gets a new id. Rows are grouped by area; the prefix names the area, not the layer.

| Prefix | Area |
| --- | --- |
| `CFG-` | Workflow Configuration, static validation, pinning. `CFG-1` … `CFG-35` are the 35 invalid configurations, numbered as their fixture files. |
| `STEP-` | One Workflow Step: `agent` executor, Step Results, inputs, Step Artifacts, evidence. |
| `ROUTE-` | Routes, Execution Ceilings, Workflow Ends, the compiled graph. |
| `LIM-` | Meters, Attempt Ceiling, Step Allowances, Reserved Steps, admission. |
| `INP-` | Workflow Inputs, selection, Claim, pinning, Drift and withdrawal. |
| `OP-` | Runtime Operations and their writes. |
| `REC-` | Recording, external effects, Terminal Outcomes, finalisation and recovery. |
| `OPC-` | The operator-contract projection. |

These ids belong to this matrix. They are unrelated to the MVP matrix's `REC-CLM-*`, `REC-DEL-*` and similar ids.

**Traces.** The 13 execution traces of the [owner-reviewed prototype](https://github.com/lbacik/coding-agent/blob/dc9e93f3cde841af93a92a3543d1bc65cea48bda/prototypes/workflow-yaml/README.md#execution-traces) are rows; each names its trace as "Trace: *&lt;Workflow&gt; / &lt;name&gt;*" and expects the trace's Step Executions, writes and Terminal Outcome exactly. They run against `examples/workflows.yaml`. The trace *review / Operator cancels* (REC-23) is retired, because the first release has no cancellation (#288); REC-25 re-expresses it as a SIGTERM.

## Four layers

The L1–L4 scheme of the MVP is retained (#285 point 8).

| Layer | What it exercises | Fixtures | Cost | When it runs |
| --- | --- | --- | --- | --- |
| **L1: Provider contract** | The provider's own contract: tool calling, usage reporting, the output cap | Real provider API, smallest prompts | small, real | Explicitly, marked; never in the default run |
| **L2: Language and profile** | `check-readiness` and `run-validation` per language; evidence parsing | Three real repositories, inside the image | none | Default run, in the image |
| **L3: Behaviour scenarios** | Validation, the compiled graph, steps, limits, inputs, effects, recovery, injected crashes | Fake GitHub and scripted model in `tests/support/` | none | Default run |
| **L4: Real smoke** | An implementation, then a review of the pull request it opened, through `agent run` | Sandbox repository, real provider | real | Manual, before the release tag |

- **Every slice runs end to end against a scripted model before any live run.**
- **The fake GitHub is held to the real one.** It gains a contract suite for the new writes (Claim Comment, review, comment, lookup by Effect Intent key), and the real side of that suite runs against the sandbox repository (OP-14).
- **Real process death** runs through `conformance/producer-suite.sh kill` (REC-20), in addition to the crashes L3 injects.
- **Retained regressions.** MVP rows that prove a mechanism this contract retains unchanged keep running under their MVP ids: the L1 provider rows, the L2 language rows, the Worker lifecycle (startup, poll loop, Durable Intake and Operator Commands, Halting, Contract Events, Offline Read), identity and authorisation, rate-limit classification and the Sub-agent mechanics this contract keeps. A row whose mechanism is replaced is withdrawn with its suite in S10.

---

## L1: Provider contract

Runs for every provider the code-owned model table supports.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| LIM-17 | A request sent with the Pinned Model's code-owned output cap, asked for more output than the cap allows | The provider accepts the cap and stops at it; the response is an ordinary model response carrying usage | S5 | §11 | #282 |
| STEP-17 | The Provider Capability Assertion at startup includes the Pinned Model's output cap | The assertion succeeds with the cap set; a model outside the code-owned table refuses startup | S2 | §2.10, §4 | #282, #283 |

## L2: Language and profile

Runs once per supported language repository.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| OP-1 | `check-readiness` on a repository with a valid Project Profile and its toolchain | `ready`; the bootstrap ran; the facts name toolchain and services | S7a | §10.2 | #285 |
| OP-2 | `check-readiness` with no profile, an invalid profile, and an unsupported environment (toolchain outside the matrix, unknown `schema`, unreachable service) | `not-ready` each time, with a fact naming what is missing or wrong; no model call | S7a | §10.2 | #285 |
| OP-3 | `check-readiness` whose `bootstrap` fails | `error` | S7a | §10.2 | #285 |
| OP-4 | `run-validation` on a green and on a red tree | `passed` / `failed`, with Validation Evidence bound to the tree's revision identity | S7a | §10.3, §13.4 | #280, #285 |

## L3: Behaviour scenarios

### Configuration and validation (`CFG-`)

Table-driven: one test runs every fixture in `tests/fixtures/workflows/invalid/` (moved from the prototype in S1) and asserts its `# expect:` message, and one asserts that `examples/workflows.yaml` is valid.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| CFG-1 | A duplicate key | Refused: duplicate key | S1 | §2.7 | #281, #284 |
| CFG-2 | An unknown field (`max-execution`) | Refused: unsupported field | S1 | §2.7 | #281, #284 |
| CFG-3 | `start` names no step | Refused: not a step id | S1 | §3 | #281 |
| CFG-4 | A Route to an unknown target | Refused: unknown target | S1 | §3 | #281 |
| CFG-5 | A Route key outside the step's result set | Refused: not a possible result | S1 | §3 | #281 |
| CFG-6 | A declared business result left unrouted | Refused: result is not routed | S1 | §3 | #281 |
| CFG-7 | A business result in `defaults.routes` | Refused: defaults may route only runtime-reserved results | S1 | §3 | #281 |
| CFG-8 | `requirement-unmet` routed on an operation without `with.requires` | Refused: not a possible result | S1 | §3 | #281 |
| CFG-9 | `stale-head` routed on an operation that cannot produce it | Refused: not a possible result | S1 | §3 | #281, #286 |
| CFG-10 | A list of Route targets (fan-out) | Refused: exactly one target | S1 | §3 | #281 |
| CFG-11 | A per-step `model` | Refused with the targeted message | S1 | §3 | #280 |
| CFG-12 | A condition or expression on a step | Refused: conditions/expressions are not supported | S1 | §3 | #281 |
| CFG-13 | A wildcard Route | Refused: no wildcard | S1 | §3 | #281 |
| CFG-14 | A step unreachable from `start` | Refused: unreachable | S1 | §3 | #281 |
| CFG-15 | `limit-exceeded` Routes forming a cycle | Refused; the message names the cycle | S1 | §3 | #281 |
| CFG-16 | A reserved result routed directly to `{end: completed}` | Refused | S1 | §3 | #281, #283 |
| CFG-17 | A reserved result routed directly to `{end: blocked}` | Refused | S1 | §3 | #281, #283 |
| CFG-18 | An unknown end category | Refused | S1 | §3 | #283 |
| CFG-19 | A runtime-only Terminal Outcome as a Workflow End | Refused | S1 | §3 | #281, #283 |
| CFG-20 | A required input not proven on every path | Refused: not proven | S1 | §3 | #281 |
| CFG-21 | `limits` on an `operation` step | Refused | S1 | §3 | #282 |
| CFG-22 | A Reserved Step whose allowance lacks a meter | Refused: declares all four meters | S1 | §3 | #282 |
| CFG-23 | Reserved allowances reaching the Attempt Ceiling | Refused: must stay below the Attempt Ceiling | S1 | §3 | #282 |
| CFG-24 | A Step Allowance larger than the Work Pool | Refused: exceeds the Work Pool | S1 | §3 | #282 |
| CFG-25 | `publish-review` in an `issue` Workflow | Refused: available only in `pull-request` Workflows | S1 | §3 | #286 |
| CFG-26 | `open-pull-request` in a `pull-request` Workflow | Refused: available only in `issue` Workflows | S1 | §3 | #286 |
| CFG-27 | A GitHub tool requested for the model | Refused: unknown tool group | S1 | §3 | #280 |
| CFG-28 | An always-present tool declared | Refused: always present | S1 | §3 | #280 |
| CFG-29 | A publication body from a non-`document` field | Refused | S1 | §3 | #280, #284 |
| CFG-30 | An expression in a reference position | Refused: not a reference | S1 | §3 | #284 |
| CFG-31 | A reserved result declared as a model result | Refused | S1 | §3 | #280 |
| CFG-32 | An anchor or alias | Refused | S1 | §2.7 | #284 |
| CFG-33 | End labels for an unknown Terminal Outcome | Refused | S1 | §3 | #283, #284 |
| CFG-34 | A facts-mode `post-comment` | Refused: there is no facts-mode comment | S1 | §3 | #284 |
| CFG-35 | A pull-request title from the payload | Refused: fixed runtime template | S1 | §3 | #284 |
| CFG-36 | `examples/workflows.yaml`, both Workflows | Valid; CI validates it on every change | S1 | §2, §3 | #285 |
| CFG-37 | A Reserved Step reading the required `process.facts`, reached only through `process`'s `limit-exceeded` Route, including its Execution Ceiling refusal | Valid: a refusal edge implies the refused step has run | S1 | §3 | #284 |
| CFG-38 | `agent config validate` on a valid and on an invalid file | Offline, no network call; prints environment-variable names, never values; an invalid file exits non-zero naming the YAML path of every fault; an unknown or missing `--workflow` lists the ids | S1 | §2.9 | #285 |
| CFG-39 | An inline secret; a prompt path escaping the configuration directory; a skill outside the Skill Bundle | Each refused at startup | S1 | §2.8 | #279 |
| CFG-40 | The compiled graph of each example Workflow | One node per step; `add_edge(START, start)` is the only plain edge; exactly one conditional edge per step whose keys are the step's results after defaults; one end node per used category; no `Send`, `Command(goto)` or `interrupt`; `checkpointer=None`; `recursion_limit` derived from the sum of Execution Ceilings | S3 | §8.5 | #281 |
| CFG-41 | Open an Attempt, then restart with an edited configuration file | The Attempt's Effective Configuration (compiled Workflow, prompt bytes, statically named skill texts, Skill Bundle identity, Pinned Model with prices, context window and output cap, `worker` settings) and its one digest are unchanged; recovery uses only pinned values | S2 | §2.10 | #283 |
| CFG-42 | Startup with an unfinished Attempt whose pinned record is missing or unreadable | Startup refused | S2 | §2.10, §16 | #283 |
| CFG-43 | An end label equal to its own Workflow's Selection Label; an end label equal to another Workflow's Selection Label in the same file | Refused each time: the Worker never applies a Selection Label | S1 | §3 | #288 |
| CFG-44 | `outcomes.canceled` | Refused: `canceled` is not a Terminal Outcome of the first release | S1 | §3, §15.1 | #288 |
| CFG-45 | A file with no `worker` block and a Workflow with no `max-parallel-subagents`; the same file with explicit values | Valid; the defaults are a 60 s poll interval, a 30 s shutdown grace and 2 parallel Sub-agents; explicit values are used as given | S1 | §2.1, §2.2 | #288 |
| CFG-46 | `max-parallel-subagents` of `0`, `-1`, `1.5` and `"2"` | Each refused | S1 | §3 | #288 |

### Workflow Steps (`STEP-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| STEP-1 | Two `agent` steps in sequence | The second opens a new conversation with no message history from the first; the workspace carries over | S4 | §9.1 | #280 |
| STEP-2 | An `agent` step with ordered `skill:` and `prompt:` instructions | Activated by the runtime at conversation open in the declared order, verbatim; the whole pinned Skill Bundle stays activatable on demand; instructions grant no tool | S4 | §9.3 | #280 |
| STEP-3 | An `agent` step declaring only `workspace-read` | Only that group's tools plus the always-present report, skill-activation and capped-result-reader tools are bound; no GitHub tool exists in any group | S4 | §9.4 | #280 |
| STEP-4 | The opening frame of an `agent` step | The Pinned Prefix is the generic Attempt Header, the input framed as data, the declared inputs and the Step Result contract; no Acceptance Criteria or Seam Set is enumerated | S4 | §9.2 | #280, #285 |
| STEP-5 | A report naming an undeclared result, then one whose payload violates its schema | Each returns to the model as a tool error; after the bounded number of corrections the Step Execution ends `error` | S4 | §9.5 | #280 |
| STEP-6 | A response with no report, repeated | Bounded correction, then `error` | S4 | §9.5 | #280, #282 |
| STEP-7 | A `done` report whose `evidence-ref` names a test that did not run through `tests`, or failed, or ran on an earlier tree | The report is malformed; corrected or ends `error` | S4 | §9.5 | #280 |
| STEP-8 | A report with a `document` field | One named, immutable, redacted Step Artifact is stored outside the Target Repository tree | S4 | §9.6 | #280 |
| STEP-9 | A Step Execution ending `error` or `limit-exceeded` | No Step Result and no Step Artifact; Transcript, evidence, footprint and usage kept; the workspace is not rolled back | S4 | §9.6 | #280 |
| STEP-10 | A step entered three times | Step Executions numbered 1, 2, 3; each one's result, artifacts, facts and usage immutable once ended | S3 | §9.1, §13.2 | #280 |
| STEP-11 | Inputs reading a step that ran twice; an optional input whose step never ran; a required input missing at run time | The latest Step Execution is read; the optional input is absent; the missing required input ends the consumer `error` without a model call | S4 | §9.7 | #280, #281 |
| STEP-12 | A `<step>.facts` input after that step ended `limit-exceeded` | The Execution Summary exists: changed files, commands and results, revision-bound evidence, the limit crossed, the spend, the latest Continuation Snapshot when there is one; redacted; within the code-owned cap; no Transcript or message content | S4 | §9.7 | #282 |
| STEP-13 | An `agent` step with `subagents` spawning a group after activating `code-review` | Sub-agents run with their fixed read-only tools; their spend counts to the step and the Attempt; no Review is attributed and no commit, push or gate follows from the activation | S4 | §9.8 | #279, #280 |
| STEP-14 | A conversation approaching the context threshold; then an invalid snapshot; then too many handoffs | A Context-Window Handoff continues the same Step Execution and its spend counts to it; an invalid snapshot ends the Step Execution `error`; exceeding the code-owned handoff count yields `limit-exceeded` | S4 | §9.9 | #282 |
| STEP-15 | Evidence recorded on a clean tree and on a dirty one | Each record carries the tree identity: the commit when clean, otherwise the digest over tracked plus non-ignored untracked files | S7a | §13.4 | #280 |
| STEP-16 | Every request of an Attempt, including Sub-agents | All use the one Pinned Model; there is no fallback | S4 | §9.1 | #280 |

### Routing and the graph (`ROUTE-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| ROUTE-1 | A Workflow whose YAML lists `start` last | Execution begins at `start`; step order in YAML is meaningless | S3 | §8.1 | #281 |
| ROUTE-2 | A step with its own `error` Route and a default `error` Route | The step's Route is taken | S3 | §8.1 | #281 |
| ROUTE-3 | A Route into a step at its Execution Ceiling | No Step Execution is created and the sequence does not advance; an `execution-ceiling` transition fact is recorded; routing follows that step's `limit-exceeded` Route | S3 | §8.3 | #281 |
| ROUTE-4 | A Workflow End reached | The fact "ended at end X after result R of step S" is recorded, and the Terminal Outcome is X | S3 | §8.2 | #281, #283 |
| ROUTE-5 | A runtime defect returns a result outside the step's path map; separately, a `GraphRecursionError` | Neither is routed through `error`; the Attempt ends `internal-failure` | S3 | §8.4 | #281 |
| ROUTE-6 | Trace: *implement / Happy path* | `preflight#1=ready, process#1=done, validate#1=passed, self-review#1=approved, push#1=pushed, open-pr#1=opened`; writes: push, pull request; `completed` | S3 | §8 | #284 |
| ROUTE-7 | Trace: *implement / Validation keeps failing* | `process` and `validate` alternate three times; the fourth `process` entry is refused; `ai-handoff#1=handoff, handoff-push#1=pushed, handoff-comment#1=posted`; `unfinished` | S3 | §8.3 | #284 |
| ROUTE-8 | Trace: *implement / Self-review never approves* | The third `self-review` entry is refused; its default `limit-exceeded` Route runs `hard-handoff#1=pushed`; `unfinished` | S3 | §8.3 | #284 |
| ROUTE-9 | Trace: *implement / Model says blocked* | `process#1=blocked, explain-blocked#1=posted`; one comment written; `blocked` | S3 | §8 | #284 |
| ROUTE-10 | Trace: *review / Happy path* | `review#1=reviewed, publish#1=published`; one review written; `completed` | S3 | §8 | #284 |
| ROUTE-11 | Trace: *review / Publish retried* | `publish#1=error, publish#2=error`; the retry is a Route bounded by `max-executions: 2`; `failed` | S3 | §8.3 | #281, #284 |

### Limits (`LIM-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| LIM-1 | Spend in each of the four meters | Cost, effective tokens (cache reads excluded), tool calls and wall clock are metered per step and per Attempt; there is no request counter | S5 | §11.1 | #282 |
| LIM-2 | `process ⇄ validate` looping three times under a 12 USD Step Allowance | All `process` Step Executions draw on one allowance; exhausting it routes `limit-exceeded` to `ai-handoff` | S5 | §11.2 | #282 |
| LIM-3 | The Work Pool exhausted during `process` | `process` ends `limit-exceeded`; `ai-handoff` runs on its intact reserve | S5 | §11.3 | #282 |
| LIM-4 | The reserve exhausted during `ai-handoff` | `ai-handoff` ends `limit-exceeded`; the Hard Handoff runs with no model | S5 | §11.3 | #282 |
| LIM-5 | A Reserved Step running while the Work Pool still has headroom | It draws only on its own allowance; a re-entry gets only what remains | S5 | §11.3 | #282 |
| LIM-6 | A request whose worst-case bound exceeds the remaining allowance, pool, reserve or ceiling | The request is not sent; the Step Execution ends `limit-exceeded` with moment `before-request` | S5 | §11.4 | #282 |
| LIM-7 | Two concurrent Sub-agents whose requests each fit the headroom alone but not together | The second is not admitted: the first's hold is in force while it is in flight | S5 | §11.4 | #282 |
| LIM-8 | A failed request with no usage metadata; a crash with an admission hold open | Each is charged its admitted worst case | S5 | §11.4, §13.2 | #282, #283 |
| LIM-9 | A tool call beyond the tool-call limit | Counted when requested; not executed; the Step Execution ends `limit-exceeded` with moment `tool-call` | S5 | §11.5 | #282 |
| LIM-10 | A command or model request running into the remaining time | Its timeout is min(code-owned timeout, remaining step/pool/reserve time); on expiry the process group is killed or the request abandoned and charged | S5 | §11.5 | #282 |
| LIM-11 | The Attempt Ceiling exhausted mid-Workflow | No further model request or tool call; `agent` steps are refused `limit-exceeded`; `operation` steps still run under their own timeouts, so the Hard Handoff runs | S5 | §11.6 | #282 |
| LIM-12 | Several limits crossed at once | The fact records scope, every dimension crossed, limit, spend and moment; the primary reason follows the precedence process interruption > Attempt Ceiling > Work Pool / reserve > Step Allowance > Execution Ceiling | S5 | §11.7 | #282 |
| LIM-13 | Sub-agent spend, Context-Window Handoff spend and provider retries | Counted against the calling step and the Attempt | S5 | §11.2 | #282 |
| LIM-14 | Trace: *implement / Process over budget* | `process#1=limit-exceeded, ai-handoff#1=limit-exceeded, hard-handoff#1=pushed`; `unfinished` | S5 | §11.3 | #284 |
| LIM-15 | Trace: *review / Review over budget* | `review#1=limit-exceeded, ai-handoff#1=partial, handoff-comment#1=posted`; `unfinished` | S5 | §11.3 | #284 |
| LIM-16 | Wall clock with a Reserved Step | Non-reserved model work stops at the Attempt deadline minus the reserved minutes; Attempt wall clock runs from opening and includes operation time, and bars model work, not cleanup | S5 | §11.3, §11.6 | #282 |

### Inputs and Claim (`INP-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| INP-1 | Existing-PR review: a `review` Workflow (`pull-request`, `agent:review`) claims an open same-repository pull request | The read-back pins the Input Head and the merge-base Base Revision; the review step runs; `publish-review` publishes against the Input Head; at the end the label is removed and the assignment released | S6 | §5, §6, §7 | #286 scenario 1 |
| INP-2 | Second scenario on the same PR: `fixup` (`agent:fixup`) labelled while `review` holds the pull request | `fixup` skips it while the `review` Claim is in force, claims it after release; its compare-and-swap push advances the Input Head without Drift | S6 | §6, §7 | #286 scenario 2 |
| INP-3 | Foreign push during an Attempt; a foreign push between the Gate and the write | The first ends the Attempt `drift` at the next Gate; the second yields a routable `stale-head` from `publish-review` or `commit-and-push` | S6 | §7 | #286 scenario 3 |
| INP-4 | Issue-to-PR implementation | An `implement` Workflow opens a pull request, which stays eligible for `review`; with `skip-open-agent-pr` the implementation Workflow skips the issue while its agent pull request is open | S6 | §5 | #286 scenario 4 |
| INP-5 | Two Workers sharing one Agent Identity claim the same input | The lowest Claim Comment id wins; the loser deletes its comment and does not remove the assignment | S6 | §6 | #286 scenario 5 |
| INP-6 | A label applied by a bot; label removal, closure or merge during an Attempt | Refused at Claim; each later event is a withdrawal, including at the Gate before a write. A label applied by the Agent Identity depends on the identity mode (INP-19, INP-20) | S6 | §6, §7 | #286 scenario 6, #288 |
| INP-7 | An issue body edited during an Attempt; a crash between assignment and the ledger write | The edit ends the Attempt `drift`; the crash is recovered from the recorded Claim intent and the assignment released | S6 | §7, §16 | #286 scenario 7 |
| INP-8 | A fork pull request labelled for review | Never eligible | S6 | §5 | #286 scenario 8 |
| INP-9 | Candidates failing each mandatory guard: closed, label absent, any assignee, an open native blocker | Each excluded; selection writes nothing | S6 | §5.2 | #286 |
| INP-10 | `require-all`, `require-any`, `exclude` and `drafts` filters | Filters narrow, never authorise; drafts excluded by default | S6 | §5.3 | #286 |
| INP-11 | An open pull request merely containing an Attempt-Marker substring, by another author | Does not exclude the issue under `skip-open-agent-pr`: only the Agent Identity's open pull request with that issue's marker does | S6 | §5.3 | #286 |
| INP-12 | Several eligible inputs; `next issue N` | Ordered `(created_at, number)` ascending; the priority command passes the same guards | S6 | §5.4 | #286 |
| INP-13 | Two Attempts of `implement` and one of `review` on input 42 | Ids `implement#42/1`, `implement#42/2`, `review#42/1`; the Workflow id appears in the marker, the commit trailer and the issue Attempt branch `agent/implement/42/<n>-<slug>` | S6 | §6.3 | #286 |
| INP-14 | Claim of an issue whose body changes between listing and read-back | The Fingerprint is the read-back's title and body; the workspace is checked out exactly at the Base Revision pinned at Claim | S6 | §7.1 | #286 |
| INP-15 | The Attempt facts given to a step | Attempt id, Workflow id, input type and number, title and body as data, Base Revision, current revision, and for pull requests base branch, head branch and Input Head; no comments, reviews, labels, author or CI status | S6 | §7.4 | #286 |
| INP-16 | Trace: *implement / Issue edited mid-run* | `preflight#1=ready, process#1=done`, then `drift`; no writes besides finalisation | S6 | §7.3 | #284 |
| INP-17 | Trace: *review / Foreign push* | `review#1=reviewed`, then `drift` | S6 | §7.3 | #284 |
| INP-18 | New commits on the base branch during an Attempt | Not Drift; disclosed only | S6 | §7.3 | #286 |
| INP-19 | Shared identity (the Agent Identity's id equals the Target Repository owner's): the Selection Label last applied by the Agent Identity, and by a `Bot`-type account | The first is accepted as the owner's authorisation; the second is refused at Claim; the Attempt records the actor, the time and the mode `shared-identity`; no YAML key selects the mode | S6 | §5.1, §18 | #288 |
| INP-20 | Dedicated identity (the ids differ): the Selection Label last applied by the Agent Identity, by a `Bot`-type account, and by a human | The first two are refused at Claim; the human's is accepted; the actor and time are recorded | S6 | §5.1, §18 | #286, #288 |

### Runtime Operations (`OP-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| OP-5 | `commit-and-push` with changes and with none (issue Workflow) | `pushed` with the revision / `no-changes`; fast-forward only, never forced | S7a | §10.4 | #280 |
| OP-6 | `commit-and-push` to a pull-request head whose remote head equals the Input Head, and one where it moved | Compare-and-swap push lands and advances the Input Head / `stale-head`, nothing forced | S7a | §10.4 | #286 |
| OP-7 | `open-pull-request` | Opened from the pushed revision, title from the fixed runtime template over the Target Issue title, body from the named `document` field, draft per `with.draft`; an existing one is `already-open` | S7b | §10.5 | #280, #284 |
| OP-8 | `publish-review` | Published through the GitHub review API as a COMMENT event against the current Input Head, body from the named `document` field; a moved head yields `stale-head` | S7b | §10.6 | #280, #285 |
| OP-9 | `post-comment` on an issue and on a pull request | One comment on the input, body from the named `document` field | S7b | §10.7 | #284 |
| OP-10 | A publication with `requires: [run-validation: passed]` when validation passed only on an earlier tree, or only on the base tree | `requirement-unmet` before any write | S7b | §10.1 | #280, #285 |
| OP-11 | An operation placed anywhere in the graph after a withdrawal or without an active Claim | Not performed; the precondition holds regardless of graph placement | S7b | §10.1 | #280 |
| OP-12 | Trace: *review / Head moved at write time* | `publish#1=stale-head`; the review is `not-performed (stale-head)`; `failed` | S7b | §10.6 | #284 |
| OP-13 | A single refused permission on a write; a push touching `.github/workflows/` | The operation ends `error`, routed by the Workflow | S7a | §10.4, §14.4 | #283 |
| OP-14 | The fake-GitHub contract suite for the Claim Comment, review, comment and lookup by Effect Intent key | Passes against the fake and against the sandbox repository | S6, S7b | §14 | #285 |
| OP-17 | `open-pull-request` with `with.draft: false` (the default) and with `with.draft: true` | The ready pull request carries `Closes #<issue>`; the draft carries `Part of #<issue>` and no closing keyword | S7b | §10.5 | #288 |
| OP-18 | A pull request, a review and a `post-comment` comment | Each body is the `document` verbatim, then the provenance footer (Attempt id, Workflow id, publishing step id, Effective Configuration digest, and the Base Revision / the reviewed Input Head / no revision) and the Attempt Marker; the runtime adds no validation evidence | S7b | §14.4 | #288 |
| OP-19 | A `document` that would make a body exceed GitHub's limit | Only the `document` is truncated, with an explicit truncation marker and a pointer to the Step Artifact; the closing keyword, footer and marker are intact; the rendered body is recorded with its Effect Intent and recovery publishes it unchanged | S7b, S8 | §14.4 | #288 |
| OP-20 | A model-authored `document` containing `Closes #<n>` | Published verbatim; the runtime does not rewrite it | S7b | §14.4 | #288 |

### Recording, effects and recovery (`REC-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| REC-1 | Any Step Execution | Its opening (step, sequence number, entry Route) is written before it starts; its end (result with payload, facts, usage, any `limit-exceeded` fact) is committed before the router selects a successor | S2, S3 | §13.2 | #283 |
| REC-2 | A Route taken, a refused entry, a Workflow End | Each recorded as a transition fact | S3 | §13.2 | #283 |
| REC-3 | A fresh ledger and one written by the MVP runtime | The ledger carries an explicit schema version; evolution is additive; the old runtime keeps working until S10 | S2 | §13.1 | #283, #285 |
| REC-4 | Every external write kind: push, pull request, review, comment, label, assignment, Claim Comment | Each has an Effect Intent keyed `(Attempt id, step, Step Execution number, write ordinal)` with the rendered payload and expected target state, written after the Gate immediately before the write, and an Effect Receipt; the key travels in the write's Attempt Marker | S2, S7b | §14.1 | #283 |
| REC-5 | Crash between an Effect Intent and its write; the write landed | Restart finds it remotely by key and records the receipt; nothing is written twice | S8 | §14.2 | #283 |
| REC-6 | Crash between an Effect Intent and its write; the write is absent | Restart re-checks the factual preconditions; if they hold the write is performed once, otherwise recorded `not-performed` with its reason | S8 | §14.2 | #283 |
| REC-7 | Crashes injected before an intent, and after a write before its receipt | Before: no write is attempted at restart. After: the receipt is recovered by lookup | S8 | §14.2 | #285 |
| REC-8 | A human-closed own pull request found at restart | Never re-created; reported | S8 | §14.2 | #283 |
| REC-9 | Drift after a pull request opened | The pull request is kept and listed in the terminal comment | S8 | §14.3 | #283 |
| REC-10 | An unconfirmed Claim found at restart | Own Claim Comment deleted by key; assignment removed if no other Claim is in force; record `released`; no Terminal Outcome, no terminal comment | S8 | §16.2 | #283 |
| REC-11 | Startup on a state volume holding a legacy Attempt that is neither `finished` nor `released` | Halt, telling the operator to drain it with the last pre-change release; nothing deleted. Active only from S10 | S10 | §16.4 | #283, #285 |
| REC-12 | Restart with a Step Execution left open by a process loss | It is closed as interrupted by process loss (a runtime fact, never routed); no Workflow Step runs; the Attempt ends `interrupted`, or `withdrawn` if the input was withdrawn meanwhile | S8 | §16.1 | #283 |
| REC-13 | Any ending | The Terminal Outcome, rendered terminal comment and terminal-write list are recorded once and immutably before the terminal writes; a second record is refused | S8 | §15.2 | #283 |
| REC-14 | Terminal writes | Exactly one terminal comment from the fixed template (Attempt id, Terminal Outcome, ending step, Step Result and Workflow End, limit fact, landed and `not-performed` effects, uncommitted-file count), posted even on a closed or merged input; Selection Label removed; the Workflow's end labels for that outcome applied; assignment removed when no other Claim is in force | S8 | §15.3 | #283 |
| REC-15 | An Attempt that opened a pull request and then ends `failed` | Nothing landed is reverted: no close, no draft conversion, no branch deletion, no removal of reviews or comments | S8 | §14.3 | #283 |
| REC-16 | A runtime-only ending (SIGTERM, withdrawal, Drift) with a model request in flight, and with a write in flight | The request is discarded; the write is settled | S8 | §12 | #283, #285, #288 |
| REC-17 | A Credential Failure | The Attempt ends `interrupted` / `credential-failure` and the Worker halts; recovery finishes the terminal writes after the credential is fixed | S8 | §16.3 | #283 |
| REC-18 | Three consecutive recovery write failures | The Worker halts with `recovery-write-failure` | S8 | §16.3 | #283 |
| REC-19 | Trace: *implement / Worker crash* | `process#1` closed as interrupted by process loss; `interrupted` | S8 | §16.1 | #284 |
| REC-20 | Real process death via `producer-suite.sh kill` | Restart settles and finalises the Attempt as REC-12 says | S8 | §16.1 | #285 |
| REC-21 | Restart with a dirty tree | The dirty tree is never committed or pushed; the terminal comment reports the uncommitted-file count | S8 | §16.1 | #283 |
| REC-22 | Ledger loss, then a new Attempt on an input with earlier Attempts | `n` is rebuilt from the Attempt branches and markers on GitHub, so no branch or marker collides | S6 | §16.5 | #285, #286 |
| ~~REC-23~~ | ~~Trace: *review / Operator cancels*~~ | **Retired** by #288: the first release has no cancellation and no `canceled` outcome. Replaced by REC-25 | — | — | #284, #288 |
| REC-24 | SIGTERM during a Step Execution | The live Attempt takes the restart finalisation while the process is alive, within `worker.shutdown-grace-seconds`, and ends `interrupted` with reason `shutdown` (or `withdrawn`); whatever the grace does not finish is finished by recovery at the next start | S8 | §12 | #283, #288 |
| REC-25 | SIGTERM during `review` of the example `review` Workflow | `review#1` ends without a Step Result; `interrupted` / `shutdown`; the Selection Label is removed; `attention_required` is `true` | S8 | §12 | #284, #288 |
| REC-26 | Static validation bypassed, with a terminal-write list naming the Workflow's own Selection Label as an end label | The Worker never writes its own Workflow's Selection Label | S8 | §15.3, §18 | #288 |

### Operator projection (`OPC-`)

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| OPC-1 | `attempts show` for each Terminal Outcome | `outcome.native` is the Terminal Outcome; `outcome.reason` is the limit or error fact, otherwise the Workflow End with the step and Step Result that reached it | S9 | §19 | #283, #285 |
| OPC-2 | An Attempt that opened a pull request; one that published only a review | `delivery.pull_request` from the `open-pull-request` receipt / `delivery` null | S9 | §19 | #283 |
| OPC-3 | `attention_required` | `true` for `failed`, `unfinished`, `blocked`, `interrupted`, `internal-failure` and whenever an effect is unsettled or `not-performed`; `false` for `completed`, `withdrawn` and `drift`; a SIGTERM-ended `interrupted` / `shutdown` is `true` | S9 | §19 | #283, #288 |
| OPC-4 | `attempt_id` and native phase | `<workflow>#<N>/<n>`; the phase is the step id plus Step Execution number | S9 | §19 | #285 |
| OPC-5 | The budget projection | The four meters with effective tokens, from the Attempt Ceiling and its spend | S9 | §19 | #285 |
| OPC-6 | Capabilities of an `issue` Worker and of a `pull-request` Worker | `next-issue` advertised only by the `issue` Worker; the other answers `unsupported`; `handoff now` stays `unsupported` | S9 | §19 | #283, #285 |
| OPC-7 | An Attempt Archive after an Attempt with Step Artifacts and a `limit-exceeded` step | `native.` step events in `events.jsonl`; Step Artifacts and Execution Summaries in the manifest | S9 | §4.5, §19 | #285 |
| OPC-8 | `conformance/producer-suite.sh` | Green against the new runtime | S9, S10 | §19 | #285 |

## L4: Real smoke

Manual, on the sandbox repository, with a real provider, before the release tag that contains the cutover.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| OP-15 | `agent run --workflow implement` on a labelled sandbox issue | A pull request is opened from a pushed revision on which `run-validation` passed; `completed`; the label is removed | S10 | §8–§15 | #285 |
| OP-16 | `agent run --workflow review` on that pull request | A COMMENT review is published against its head; `completed` | S10 | §8–§15 | #285 |
