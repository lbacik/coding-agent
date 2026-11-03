# MVP implementation plan

**Status: current.** This plan gives the ordered route from today's `agent implement` to a Worker that runs a configured Workflow, selects an issue and hands back a reviewed pull request. Behaviour is defined by [the MVP runtime contract](../contract/mvp-runtime-contract.md). This document says only in what order it gets built and how each step proves itself. [The v1 implementation plan](./v1-implementation-plan.md) is a frozen design record. Its slice names (`S0b`, `S3.6`, …) stay valid as citations.

Companion: [the MVP acceptance matrix](./mvp-acceptance-matrix.md).

The slices follow the build order of [Can a LangGraph skill step reproduce the reference /implement behavior?](https://github.com/lbacik/coding-agent/issues/73#issuecomment-6542890368), as amended by [What is the migration cut and module layout for the LangGraph runtime?](https://github.com/lbacik/coding-agent/issues/80#issuecomment-6544199163): **M5 is Workflow Configuration and M6 is the GitHub adapter.** Module ownership and the migration cut come from that second resolution.

## How a slice is shaped

Each slice is **vertical and externally demonstrable**: it ends with something a human can run and watch, even where later layers are still missing.

These rules hold across every slice:

- **Scripted before live.** Each slice runs end to end against a scripted model before any live run.
- **A slice is done when its rows are green, not when its code is written.** Each row in the matrix names the slice that owes it. Every row that is already green, `met (pre-MVP)` rows included, stays green.
- **A slice never fakes a layer it will later replace with a different shape.**
  - The temporary `local` tracker adapter implements the same `TrackerPort` as the GitHub adapter that replaces it.
  - The Attempt graph has its eleven nodes from M1 on. A node whose behaviour belongs to a later slice is a pass-through until that slice fills it; the graph's shape never changes.
- **Test doubles stay out of `src/`.** The fake tracker and the scripted model live in `tests/support/`, so no configuration can select a fake.
- **A Terminal Outcome is recorded before it is published,** from M1 on ([ADR 0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md)). M8 only reads what earlier slices already write correctly.

### The citation rule

**A module a slice touches cites the MVP contract.** When a slice creates, rewrites or edits a module, that module's `contract §N` citations switch to [`docs/contract/mvp-runtime-contract.md`](../contract/mvp-runtime-contract.md), and its acceptance-row citations switch to MVP row ids. A carried v1 row keeps its own id, so a citation of it stays valid.

Citations in modules no slice touches stay as they are. The v1 contract is frozen, so they are historical rather than false. There is no standalone citation sweep.

## The migration cut: re-point, don't fork

There is never a second execution path.

1. **M1 renames `implement/` to `runtime/`** as its first commit, with no behaviour change: `git mv` plus imports. `contract §N` comments move with their files.
2. **M1 re-points `agent implement --issue N` to the Attempt graph** and deletes the old orchestration in the same slice.
   - It runs over the temporary `local` tracker adapter. That adapter reads the issue from GitHub and writes nothing to GitHub. The Terminal Outcome goes to the Run Ledger and to stdout.
   - Live smoke runs are therefore possible from M1.
3. **M5 adds `agent config validate`** and gives `agent implement` its `--config --workflow` arguments.
4. **M6 adds `agent run --config <file> --workflow <id>`**, when it can do its whole job, from Selection to pull request. The same slice deletes `agent implement` and `tracker/local.py`.
5. **`preflight`, `startup-check`, `provider-check`, `verify-skill-bundle` and `validate` stay unchanged.**

## Module map

```
src/coding_agent/
  runtime/            reused leaves (was implement/)
    graph/            attempt.py  fixed Attempt graph + runtime-owned lifecycle nodes
                      step.py     step subgraph (replaces run_tool_loop)
                      review.py   review subgraph, report_review
                      tools.py    activate_*, report_step, report_completion
                      context.py  runtime context passed via RunnableConfig
    toolset, exchange, result_capping, context_handoff, ceilings, prompt_cache,
    git, seam, fingerprint, readiness, pinned_prefix, delivery, paths
    loop.py           keeps only the helpers #73 reuses
  tracker/            port.py (TrackerPort Protocol), local.py (M1–M5), github.py (M6)
  ledger/             store.py: SQLite, WAL, synchronous=FULL; RunLedger moved out of progress.py;
                      run_ledger_events + attempts / effects / outcomes
  workflow/           Workflow Configuration: schema, strict parser, Effective Configuration + digest
  worker/             Supervisor: poll loop, intake, SIGTERM, volume lock, startup recovery
  skillbundle/        + activation bodies, Role Block extraction, Reviewer Brief composition
  github/, identity/, profile/, provider/, validate/, preflight/   unchanged except where a slice names them
tests/support/        fake tracker, scripted model
```

**Reused unchanged:**

- `build_file_tools`;
- `run_targeted_test` and `run_validation_contract`;
- `parse_profile_yaml` and `derive_seam_set`;
- `InMemoryUsageLedger` and `ceiling_crossed`;
- `apply_cache_breakpoints`;
- `build_chat_model` and the price table;
- the `run_tool_loop` helpers #73 lists;
- `github/issues.fetch_issue`, used by both tracker adapters;
- `identity/`, which provides the Agent Identity's id.

### Run Ledger tables by slice

| Slice | Adds |
| --- | --- |
| M1 | The `ledger/` package, with `run_ledger_events`, `attempts` and `outcomes`. Until M5, the Effective Configuration column holds the CLI-derived settings and their digest |
| M3 | `effects`, keyed `(attempt_id, write_kind)`, at the first external write: the review-snapshot push behind the in-tool Gate |
| M5 | The normalised Workflow Configuration replaces the CLI-derived settings in `attempts` |
| M6 | `assigned_by_us` and the Claim states `claim-confirmed` and `released` |
| M8 | The volume lock, startup recovery and the Supervisor reading the tables |

## The slices

| # | Slice | Prerequisites | Model calls |
| --- | --- | --- | --- |
| M1 | Walking skeleton | the existing S0–S2 work | scripted; live smoke possible |
| M2 | Step contract | M1 | scripted |
| M3 | Review | M2 | scripted; live review run |
| M4 | Tail | M3 | scripted |
| M5 | Workflow Configuration | M4 | scripted; the Provider Capability Assertion is live |
| M6 | GitHub adapter | M5 | scripted, plus the full paid run (L4-1) |
| M7 | Gates, limits, cancellation and SIGTERM | M6 | scripted |
| M8 | Continuation and recovery | M7 | scripted; recovery never calls the model |

The order is strictly linear. Each slice builds on the graph, ledger and tracker port of the one before it.

---

### M1: Walking skeleton

**Scope.**

- The `implement/` → `runtime/` rename, as the first commit.
- The fixed Attempt graph (`runtime/graph/attempt.py`), all eleven nodes, with the happy path through `report_completion`.
- `claim` over the `local` tracker adapter.
- `readiness` resolving the Readiness Facts from the profile alone, with no prose fallback.
- `baseline`, which refuses a red baseline.
- One step subgraph for the `implement` step.
- The forced `activate_implement`; all four `activate_*` tools; the Attempt Header with its slash-command answers.
- The runtime-owned targeted-test tool, producing Criterion Evidence with the ids of executed tests.
- The `ledger/` package with `attempts` and `outcomes`.
- `agent implement --issue N` re-pointed to the graph.

**Modules.**

- **New:** `runtime/graph/{attempt,step,tools,context}`, `tracker/{port,local}`, `ledger/`, `tests/support/`.
- **Rewritten:**
  - `pinned_prefix`: no eager injection; slash answers in the Attempt Header;
  - `toolset.build_test_targeted_tool`: Criterion Evidence;
  - `validate/junit`: ids of passing tests;
  - `skillbundle`: activation bodies;
  - `profile/readiness` and `runtime/readiness`: the prose fallback chain removed.
- **Changed:** `cli implement` re-pointed; `langgraph` added as a dependency.
- **Retired:** `implement/attempt.py` (`run_implement_attempt`, `AttemptReport`), `implement/skeleton.py`, `run_tool_loop`, `build_opening_messages`. Their tests (`test_attempt`, `test_implement`, `test_loop`, the `implement` part of `test_cli`) go with them. The rows those tests prove move to the new modules' tests.

**Done when.** `agent implement --issue N` runs a small real task on the sandbox repository through the Attempt graph. It records the Terminal Outcome in the Run Ledger and prints it, and it writes nothing to GitHub. A missing Readiness Fact and a red baseline each end before any model call. The M1 rows of the matrix are green.

**Not in this slice.** Silence handling, claim returns and multiple steps (M2); review (M3); final checks and the Publication Gate verdicts (M4); any GitHub write.

---

### M2: Step contract

**Scope.**

- Silence continuations, claim returns, and the Implementation Outcomes, which replace `AttemptPolicy` and `terminal_outcome`.
- Positional exit tools: `report_step` and `report_completion`.
- Multiple steps, each a fresh step subgraph, with the step-boundary Continuation Snapshot.
- The Context-Window Handoff inside a step.
- The progress events, soft stall and verification reserve, moved with the outcome logic.
- Until M5 there is no Workflow Configuration, so multi-step lists exist only in scripted tests. `agent implement` runs the one `implement` step.

**Modules.** `runtime/graph/step.py` (silence, claim returns, positional exit tools), `runtime/graph/tools.py`, `context_handoff` (the step-boundary snapshot). **Retired:** `AttemptPolicy` and `terminal_outcome` in `progress.py`.

**Done when.** The pilot regression (L3-IMP-24) ends `completion not established` / `undeclared`. A two-step scripted run shows no shared message list. Every Implementation Outcome of contract §9 is produced by a scripted run. The M2 rows are green.

**Not in this slice.** Review, and therefore the review return of a claim (M3).

---

### M3: Review

**Scope.**

- The review subgraph: two seats, Seat Assignment v2, and `report_review`.
- The review gate: at least one completed review, at most three invocations.
- The review status (`not-invoked`, `incomplete`, `fresh`, `stale`).
- The review snapshot: commit and push behind the in-tool Gate. This is the first external write, so it brings the `effects` table, the branch and commit identity rules, and the push refusals (`.github/workflows/`, signed-commit rejection).

**Modules.** **New:** `runtime/graph/review.py`. **Rewritten:** `skillbundle` (Role Block extraction and Reviewer Brief composition); `delivery.py` (snapshot commit and push behind the in-tool Gate, keyed as effects); `ledger/` (`effects`).

**Done when.** A scripted Attempt invokes review, both seats return a Review Report, and the snapshot is pushed before either reviewer starts. A live run on the sandbox repository shows both seats reviewing, not writing prompts. The M3 rows are green.

**Not in this slice.** The stop signals the in-tool Gate checks (M7). G-REV-3 is owed by M7 for that reason.

---

### M4: Tail

**Scope.**

- `delivery_snapshot`, `final_checks` and `publication_gate` with their verdicts, including Drift and base-branch movement.
- `delivery_intent`.
- The pull-request body and terminal comment, rendered once and recorded in `outcomes`.
- The red-history disclosure.
- Over the `local` tracker, `publish` and `terminal_writes` record their writes without performing them.

**Modules.** `runtime/graph/attempt.py` (tail nodes); `ledger/` (`outcomes` carry the rendered bodies and the Delivery Intent); the rendering of the pull-request body and comment.

**Done when.** Every Publication Gate verdict and every Implementation Outcome → Terminal Outcome mapping of contract §14 is produced by a scripted run, with the rendered body recorded before any write. The M4 rows are green.

**Not in this slice.** Opening the pull request on GitHub (M6).

---

### M5: Workflow Configuration

**Scope.**

- The `workflow/` package: the schema, the strict parser and the Effective Configuration with its digest.
- `agent config validate`.
- `agent implement --config <file> --workflow <id>`.
- The Pinned Model, its prices and its limits read from the Effective Configuration.
- The real Effective Configuration pinned in `attempts`.

**Modules.** **New:** `workflow/`. **Rewritten:** `provider/config.py` (reads the Pinned Model from the Effective Configuration); `provider/capability.py` (asserts that the Effective Configuration prices the model); `cli`.

**Handoff document.** `docs/ops/workflow-configuration.md`: an operator guide to writing the YAML. It is never a second source of truth; it links the contract's §2.

**Done when.** Every W-CFG refusal exits non-zero with the YAML path and no network call. A multi-step Workflow runs through `agent implement --config --workflow`. Editing the YAML mid-Attempt changes nothing. The M5 rows are green.

**Not in this slice.** Selection and the Supervisor (M6).

---

### M6: GitHub adapter

**Scope.**

- `tracker/github.py`: Selection, the Claim by assignment with read-back, the pull request, and the terminal writes in contract order.
- Rate-limit and refusal classification, with bounded in-place retry.
- The `worker/` Supervisor's poll loop and intake.
- `agent run`, with its startup sequence.
- `assigned_by_us` and the Claim states.
- The tracker contract suite, run against the fake and against the sandbox repository.

**Modules.** **New:** `tracker/github.py`, `worker/` (poll loop, intake). **Changed:** `cli` (`agent run`). **Retired:** `agent implement`, `tracker/local.py`.

**Done when.** A container comes up and, through `agent run --workflow implement`, takes `ready-for-agent` issues in `(created_at, number)` order, one at a time, and delivers. The first real pull request (L4-1) is opened, and its cost is written down (L4-3). A revoked credential halts the Worker. The M6 rows are green.

**Not in this slice.** Cancellation, SIGTERM and the limit stops (M7); recovery (M8). Until M8, a restart with an unfinished Attempt is handled by hand.

---

### M7: Gates, limits, cancellation and SIGTERM

**Scope.**

- The between-step Gate and the in-tool Gate, with their stop signals: issue closed, Selection Label removed, a ceiling, SIGTERM.
- The in-node stop points after every model response and tool result.
- Hard-ceiling finalization.
- SIGTERM and `shutdown_grace_seconds` in `worker/`.
- Process-group kill and per-command timeouts.

**Modules.** `runtime/graph/` (Gates, stop points); `worker/` (SIGTERM, grace); `validate/runner` (process-group kill); the shell tool's per-command timeout.

**Done when.** Removing the Selection Label mid-step, crossing each of the four ceilings, and SIGTERM during a long command each end the Attempt as contract §11 says, with the work pushed and one comment. The M7 rows are green.

**Not in this slice.** Recovery after a process loss (M8).

---

### M8: Continuation and recovery

**Scope.**

- The volume lock.
- Startup recovery from the Run Ledger, with the recovery table of contract §17.
- The ledger readers.
- Continuation: a new Attempt from the previous pushed branch, rebased, opening with the verified Continuation Snapshot read back from the terminal comment.
- The redaction choke point across both log streams, tool arguments and results, and child environments.
- The volume free-space check.

**Modules.** `worker/` (lock, startup recovery, free-space check); `ledger/` (readers); `runtime/graph/attempt.py` (recovery entry points, Continuation in `readiness`); the redaction registry.

**Handoff document.** `docs/runbooks/ledger-loss.md`: what holds, what does not, and how a human releases an orphaned assignment (contract §17).

**Done when.** For every write kind, an injected crash after the write and before its receipt causes no duplicate. A crash at each point of the recovery table recovers as the table says, with no model call. Re-applying the Selection Label continues from the pushed work. The M8 rows are green.

---

## Completion criteria for the MVP

All of the following. None is optional, and nothing outside the list is a gate.

1. **Every row of the MVP acceptance matrix is green,** `met (pre-MVP)` rows included.
2. **One `delivered` on the sandbox repository,** through `agent run` with the `implement` Workflow of contract §2.1 (L4-1). Its recorded cost is written down (L4-3).
3. **Every Terminal Outcome and every reason in contract §14 is reached at least once** in L3.
4. **For every write kind,** idempotency is demonstrated by an injected crash after the write and before its receipt (L3-DEL-15).
5. **All three languages pass L2** inside the image, and **both providers pass L1**.
6. **The handoff documents exist:** the Workflow Configuration guide (M5) and the ledger-loss runbook (M8).

**Deliberately not completion criteria:** per-Attempt cost, wall-clock performance, image size, model behaviour rates ([#69](https://github.com/lbacik/coding-agent/issues/69)), and every tuning constant. They are set against observed runs.

## After the MVP

The slices become ordinary GitHub issues in this repository, as sub-issues of [#85](https://github.com/lbacik/coding-agent/issues/85), ordered by native blocked-by links. They are created ahead of their slice only up to the first real run (L4-1, in M6), because the plan will change after it, and one document ages better than a wall of stale issues. Slices beyond that point stay a draft breakdown on #85 until the slice before them closes.

**A slice ticket carries `ready-for-agent`** when the agent can implement it through `agent implement --issue N`, as it did for M1. A ticket whose acceptance needs a paid live run or a human's credentials carries `ready-for-human` instead. Until M6 the label is triage information only: `agent implement` takes the issue number directly, and no Selection runs against this repository.

From M6, `ready-for-agent` is also the parity Workflow's Selection Label. Pointing `agent run` at this repository is therefore a deliberate act, not a default. It means committing a `docs/agents/project-profile.yml` and a Workflow Configuration for this repository. It also authorises the Worker to claim every labelled ticket whose blockers are closed, so the label is removed first from any ticket that must stay human-driven.
