# PROTOTYPE: can a LangGraph skill step reproduce the reference `/implement`?

Throwaway code for [Can a LangGraph skill step reproduce the reference /implement behavior?](https://github.com/lbacik/coding-agent/issues/73), on the map [Design: YAML-configured LangGraph MVP with simple-coding-agent parity](https://github.com/lbacik/coding-agent/issues/70). Nothing here is imported by `src/`. The verdict and the decisions it settled live on the issue.

## Question

What is the smallest LangGraph runtime that reproduces the agreed reference `/implement` behaviour under the [#71](https://github.com/lbacik/coding-agent/issues/71) parity and [#72](https://github.com/lbacik/coding-agent/issues/72) workflow contracts? It is judged on the actual tool calls and outputs, not on narrated activation.

## Shape under test (`runtime.py`)

- **Attempt graph.** This is the fixed, runtime-owned skeleton, with 11 nodes: `claim → readiness → baseline → run_step ⇄ gate → delivery_snapshot → final_checks → publication_gate → delivery_intent → publish → terminal_writes`.
  - YAML steps are **data** consumed by the single `run_step` node, which loops through `gate`. The graph topology never depends on the YAML.
- **Step subgraph.** Its nodes are `open → model → tools | silence → … → exit`.
  - It is invoked **fresh** per step, with no history, opening from the Pinned Prefix plus a runtime-composed Continuation Snapshot.
  - `open` forces `activate_implement` or `activate_step`.
  - The exit tool is offered by position: `report_step` or `report_completion`.
  - Silence draws two deterministic continuations, then the node ends.
- **Review subgraph.** It is invoked from *inside* the `activate_code_review` tool.
  - The tool commits a review snapshot.
  - Two isolated reviewer conversations fan out through `Send`.
  - Each reviewer receives a Reviewer Brief: Seat Assignment + verbatim Role Block + inputs.
  - Read-only tools only.
- **Outside graph state.** Attempt-wide meters, Criterion Evidence and a fake tracker live in a `Runtime` object passed through `config["configurable"]`. No checkpointer is used.
- **Reused unchanged from `src/`:**
  - `build_file_tools`
  - `run_targeted_test`
  - `run_validation_contract`
  - `parse_profile_yaml`
  - `derive_seam_set`
  - `InMemoryUsageLedger` and `ceiling_crossed`
  - `apply_cache_breakpoints`
  - `build_chat_model`
  - the price table

The fixture is `fixtures/l2/python` with its deliberately red test removed. The issue is `fixture/.issues/1.md`: add `slugify`, with three Acceptance Criteria and one seam.

## How to run

The `.work/` directory holds scratch workspaces (PROTOTYPE, wipe me).

```sh
PY=/path/to/coding-agent/.venv/bin/python
$PY run_scripted.py                                 # 13 deterministic scenarios, scripted model, real git/pytest
$PY run_live.py implement <label>                   # LangGraph arm, real claude-sonnet-5 (ANTHROPIC_API_KEY from .env)
SEAT_VARIANT=v2 HEADER_VARIANT=slash $PY run_live.py implement <label>   # with the two fixes below
/path/to/simple-coding-agent/.venv/bin/python run_reference.py <label>    # reference ModelExecutor, unmodified
```

## Results

### Deterministic (scripted model, real git and pytest), `transcripts/scripted-*.json`

| Scenario | Implementation Outcome | Terminal | Rows |
|---|---|---|---|
| happy | verified completion | delivered, ready PR | P-DEL-1 |
| no-review | verified completion (claim returned once, naming the review) | failed/review-missing | P-DEL-3 |
| fourth-review | verified completion; 4th review refused | delivered | P-DEL-4 |
| silent | completion not established after 2 continuations | failed/no-completion-evidence | ADR 0014 |
| unbacked-claim | returned twice, then downgraded: implemented but unverified | failed/unverified | ADR 0014, P-DEL-9 |
| tool-call-ceiling | saved partial work, branch pushed | failed-limit | P-LIM-1 |
| red-final-check | verified completion | failed/failing-validation | P-DEL-2 |
| drift | verified completion | failed/drift | P-DEL-6 |
| no-criteria | no model call | failed/missing Readiness Fact | P-RDY-1 |
| multi-step | `report_step` then `report_completion` | delivered | W-STP-2, W-STP-5 |
| multi-cannot-continue | later step never starts | failed/no-completion-evidence | W-STP-1 |
| multi-cancel-between-steps | Gate between steps | abandoned | W-STP-3, P-CAN-1 |
| multi-sigterm-between-steps | Gate between steps | failed/interrupted | P-CAN-3 |

### Live, same model (`claude-sonnet-5`), same fixture

| Run | Arm | `tdd` activated | Red test run before green | Review | Standards seat actually reviewed | Outcome | Cost / time |
|---|---|---|---|---|---|---|---|
| reference | Claude Agent SDK | yes | yes (3 slices) | self-initiated, 2 `Agent` subagents with model-written briefs | yes | would publish | $0.63 / 158 s |
| live, live2, live3 | LangGraph | no | no | only after `report_completion` was returned for a missing review | 1 of 3 | delivered | ~$0.07 / ~31 s |
| live-multi | LangGraph, survey → implement | no | no | after the return | no | delivered | $0.09 / 34 s |
| seat-v2-a, seat-v2-b | LangGraph + Seat Assignment v2 | no | no | after the return | **yes (2/2)** | delivered | $0.07 / 30 s |
| slash-a, slash-b | LangGraph + v2 + slash answer in Attempt Header | **yes (2/2)** | no (test written first, implementation written before the first run) | **self-initiated (2/2)** | yes | delivered | $0.07 / 29 s |

Each arm's sample is small (1 reference run, 2–3 LangGraph runs per variant). The figures show which way each effect points. They are not rates.

## Findings

1. **The skeleton is the cheap part.** A fixed 11-node graph plus one step subgraph reproduces every deterministic #71/#72 row exercised above. YAML never shapes the topology, so the application builds one graph, not one graph per workflow.
2. **The Standards Role Block is read as a request to write a prompt.** The block says "should include:", and 3 of 4 runs answered with a prompt, not a review. The runtime still counted it as a **completed** review, so the review gate was satisfied by a non-review. Spec reviewed correctly in 4/4. A runtime-authored Seat Assignment sentence (the `SEAT_VARIANT=v2` text) fixed it in 2/2 without touching upstream text (ADR 0007).
3. **The reference's `/tdd` and `/code-review` references are answered by Claude Code and by nothing in our runtime.** Without an answer, the LangGraph arm activated `tdd` 0/6 times and reviewed only when the claim was returned. One runtime-authored Attempt Header sentence mapping each slash command to its `activate_*` tool made both happen 2/2.
4. **Red → green is still not reproduced.** The model activates `tdd` and writes the test first, but it writes the implementation before running the test. Nothing in the contract observes this: the Seam Set authorises and never verifies (ADR 0008).
5. **The reference has an unbounded tool surface.** Claude Code offered its own `ScheduleWakeup` tool, and the reference model used it to wait for its subagents. Our arm's tool set is closed by construction.
6. **A review snapshot is an external write inside a step.** ADR 0002 rejects "commit before review, push only at delivery". So `activate_code_review` must push, which puts a Gate inside a tool call, below the step boundary. The prototype only commits locally.
7. **Criterion Evidence needs executed test ids.** `JUnitResult` keeps only `failure_ids`, so the prototype parses JUnit again to bind passing ids.
8. **Per-turn checkpointing is optional, not required, for MVP.** Every assistant turn of every live conversation round-trips through LangGraph's `JsonPlusSerializer` unchanged (ADR 0010). Model resumption is deferred, though, and meters and evidence sit outside graph state. A checkpointer is therefore a #74 decision, not something this skeleton needs.
9. **LangGraph detail.** A node receives `config` only when the parameter is annotated `RunnableConfig`.

## Decided with the owner (2026-10-31)

- **"Completed review" is declared.** A reviewer conversation completes only through a `report_review` call: a list of findings, or an explicit "no findings". The Seat Assignment carries the v2 sentence.
- **The Attempt Header answers slash-command references:** `/tdd` maps to `activate_tdd`, `/code-review` to `activate_code_review`, `/codebase-design` to `activate_codebase_design`.
- **Red → green is disclosed, not enforced.** The runtime keeps the Criterion Evidence history, and the PR states whether each claimed test was ever observed red.
- **`activate_code_review` commits and pushes its snapshot,** keeping ADR 0002 unchanged. A Gate runs before that push.
- **The step is a subgraph,** reusing `run_tool_loop`'s helpers rather than wrapping the loop. The build order is in the issue resolution.
