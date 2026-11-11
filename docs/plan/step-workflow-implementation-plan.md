# Step-workflow implementation plan

**Status: current.** The ordered route from the MVP runtime to the step-workflow runtime. Behaviour is defined by [the step-workflow runtime contract](../contract/step-workflow-runtime-contract.md); this document says only in what order it gets built and how each slice proves itself. Companion: [the step-workflow acceptance matrix](./step-workflow-acceptance-matrix.md). [The MVP implementation plan](./mvp-implementation-plan.md) is a frozen design record.

The order, the cutover rule and the reuse notes were agreed in [What specification, acceptance scenarios and implementation order make step workflows ready to build?](https://github.com/lbacik/coding-agent/issues/285#issuecomment-6800378643) (**#285**, point 10). The "Reuse" column names modules observed on `main` at `b864702`; it is a starting point, not a constraint.

## Build strategy

- **Side by side, then cut over.** The new runtime is built as new modules beside the old one. Shared mechanisms are refactored in place only while the old graph still works.
- **One cutover slice** (S10) switches the Worker entry point and deletes the old engine, together with its configuration and tests.
- **`main` stays green and releasable throughout,** and no release contains two engines: until S10 the released Worker runs the MVP runtime, and the new modules are not reachable from `agent run`.
- **The legacy-Attempt startup refusal** ([contract §16.4](../contract/step-workflow-runtime-contract.md#164-legacy-attempts)) activates only in S10, because until then the old runtime legitimately shares the ledger. Every ledger change before S10 is additive.
- **A slice is done when its acceptance rows are green.** Every slice runs end to end against the scripted model before any live run.
- **Open points** in the contract, decided in [Which details did the step-workflow contract transcription leave open?](https://github.com/lbacik/coding-agent/issues/288), block the slices they touch: S1 (worker settings and defaults), S6 (the Selection Label actor check), S7b (published bodies) and S8 (`canceled` and SIGTERM).

## Slices

| # | Slice | Depends on | Reuse (observed modules) | Proven by |
| --- | --- | --- | --- | --- |
| S1 | `version: 2` configuration: schema; the full static validator (Route coverage, acyclic `limit-exceeded`, required-input must-analysis with the refusal-edge refinement, reserve / Work Pool arithmetic, input-type compatibility); `agent config validate` | — | strict loader in `workflow/parser.py` | the 35 invalid cases and `examples/workflows.yaml` |
| S2 | Ledger and pinning: schema version; Step Execution open/end, transitions, admission holds; Effect Intent key `(Attempt, step, n, ordinal)`; pinning of skill texts and of the model table with its output cap. All additive, so the old runtime keeps working | — | `ledger/store.py` (`RunLedger`, effects), `workflow/effective.py` digest | ledger tests |
| S3 | Graph compiler: a `StateGraph` from a validated Workflow, the executor registry, Execution Ceiling refusal, Workflow End nodes, the Terminal Outcome record; operations stubbed | S1, S2 | the `AttemptContext` pattern; replaces `build_attempt_graph` | the 13 traces at control-flow level (L3) |
| S4 | `agent` step: fresh conversation, instruction activation, tool groups, named Step Result report (payload validation, bounded correction, `evidence-ref`), Step Artifacts, Execution Summary (`<step>.facts`), the generic Attempt Header, Sub-agents without review attribution, a failed Context-Window Handoff becoming `error` | S3 | `runtime/graph/step.py` loop, `skillbundle/activation.py`, `runtime/toolset.py`, footprint, Ref Guard, `context_handoff.py`, `subagent.py` | L3, scripted model |
| S5 | Limits: four meters, Attempt Ceiling, Step Allowances, Reserved Step / Work Pool, worst-case admission with holds, the output cap, tool-call counting, deadlines, operations still running after the ceiling, `limit-exceeded` facts and precedence | S4 | metering in `runtime/ceilings.py` (today checked only after spend) | loops, Sub-agents, reserve exhaustion |
| S6 | Inputs and Claim: issue and pull-request `TrackerPort`, guards, filters, `drafts`, `skip-open-agent-pr`, `labeled`-event authorisation, Claim intent → assignment → Claim Comment → read-back, pinned Fingerprint / Base Revision / Input Head, Drift and withdrawal at every Gate, Attempt `n` rebuilt from markers after a ledger loss | S2 (in parallel with S3–S5) | `github/client.py`, `tracker/selection.py`, `runtime/fingerprint.py` | the 8 input scenarios and the fake-GitHub contract suite |
| S7a | Local operations: `check-readiness`, `run-validation`, `commit-and-push` (including the compare-and-swap to a pull-request head and `stale-head`), revision binding of evidence | S4, S6 | `profile/`, `validate/`, `runtime/git.py` | L3 and L2 |
| S7b | Publishing operations: `open-pull-request` (title template, `requires`), `publish-review` (the real review API; today a review lands only in the pull-request body), `post-comment`, each with intent and receipt | S7a | renderers and the Attempt Marker in `runtime/publication.py` | L3 |
| S8 | Finalisation and recovery: the terminal-comment template, end labels, Selection Label and assignment removal, the write-once outcome, restart settlement by key lookup, unconfirmed Claim release, Credential Failure, the halt after three failed recovery writes, cancel / SIGTERM / Drift endings | S7b | `runtime/interrupted.py`, `worker/recovery.py` | the 5 recovery scenarios and injected crashes |
| S9 | Operator projection ([contract §19](../contract/step-workflow-runtime-contract.md#19-operator-contract-projection)), `native.` step events, Step Artifacts and Execution Summaries in the Attempt Archive | S8 | `operator/`, `conformance/` | the producer conformance suite green |
| S10 | Cutover: the Worker entry point on the new runtime; legacy unfinished Attempts refuse startup; the old graph, `version: 1` configuration, Publication Gate, review snapshot and the `test_mvp_*` suites removed; the operator guide | all | — | full L3, `producer-suite.sh`, both L4 smokes before the release tag |

### Dependency graph

```
S1 ──┐
     ├─► S3 ─► S4 ─► S5
S2 ──┤          │
     └─► S6 ────┴─► S7a ─► S7b ─► S8 ─► S9 ─► S10
```

S10 also depends on S5 (it depends on every slice). S6 can run in parallel with S3–S5.

## Slice notes

- **S1** moves the prototype's [`invalid/`](https://github.com/lbacik/coding-agent/tree/dc9e93f3cde841af93a92a3543d1bc65cea48bda/prototypes/workflow-yaml/invalid) cases into `tests/fixtures/workflows/invalid/` with their `# expect:` lines, and adds CI validation of [`examples/workflows.yaml`](../../examples/workflows.yaml). The prototype itself stays on `research/workflow-yaml-prototype` and is cited by permalink. Rows: CFG-1 … CFG-39.
- **S2** rows: CFG-41, CFG-42, STEP-17, REC-1 (with S3), REC-3, REC-4 (with S7b).
- **S3** rows: CFG-40, ROUTE-1 … ROUTE-11, STEP-10, REC-2.
- **S4** rows: STEP-1 … STEP-14, STEP-16.
- **S5** rows: LIM-1 … LIM-17.
- **S6** rows: INP-1 … INP-18, OP-14 (with S7b), REC-22.
- **S7a** rows: OP-1 … OP-6, OP-13, STEP-15.
- **S7b** rows: OP-7 … OP-12, OP-14.
- **S8** rows: REC-5 … REC-10, REC-12 … REC-21, REC-23, REC-24.
- **S9** rows: OPC-1 … OPC-8. The native vocabulary is producer-owned, so all operator-contract changes are in this one slice.
- **S10** rows: REC-11, OPC-8, OP-15, OP-16. The retained-regression MVP rows keep running; rows whose mechanism is replaced are withdrawn with their suites.

## The cutover gate

S10 merges, and a release tag containing it is cut, only when all of these hold:

1. every L3 row of the matrix is green on the new runtime;
2. `conformance/producer-suite.sh` is green, including its `kill` mode;
3. both L4 smokes passed on the sandbox repository: an implementation (OP-15), then a review of the pull request it opened (OP-16);
4. [the operator guide](../ops/workflow-configuration.md) describes `version: 2` and the release notes tell operators to drain unfinished MVP Attempts with the last pre-change release before upgrading.

## Coordination points (outside this plan)

- [Spec: coding-agent serves operator contract major 1 as a producer](https://github.com/lbacik/coding-agent/issues/245): the new `outcome.native` set, receipt-based `delivery`, the Attempt id, the native phase, the budget projection, `next-issue` only for `issue` Workers. Delivered by S9.
- `agent-operator-contract`: the §16 outcome note (an upstream pull request that does not block the cutover); an optional minor field for published reviews (future).

## Handoff

The slices are tracked as sub-issues of a `Spec: step workflows replace the fixed Attempt graph` issue, with native blocking edges as above. That issue carries **no Selection Label**, so nothing runs until the owner applies one.
