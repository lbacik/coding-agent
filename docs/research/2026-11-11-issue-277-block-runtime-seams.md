# Declarative Workflow Blocks: current runtime guarantees and seams

Research for [Which runtime guarantees and architectural seams must declarative Workflow Blocks address?](https://github.com/lbacik/coding-agent/issues/277), a child of [Design: declarative block workflows compiled from YAML to LangGraph](https://github.com/lbacik/coding-agent/issues/276).

## Scope and evidence

Investigated on 2026-11-11 against coding-agent commit `815de8c3358e0d02b1f84260ab91c78b79b7f931`. At investigation time, `v0.2.0^{commit}` resolves to that same commit. Thus the current planning baseline and the owner's fallback tag are equal; future implementation should branch from the chosen current baseline rather than assume a separate older tag snapshot. The owner waived old-YAML compatibility, conversion and dual execution, not durable-state preservation.

This is a source inventory, not a design decision. Workflow Block is a proposed term. The current glossary defines Workflow as an ordered list confined to implementation, Workflow Step as a fresh work conversation with a positional report, and Pinned Model as one provider/model/endpoint for the whole Attempt. Replacing those meanings needs explicit owner decisions. [Glossary][glossary]

Evidence includes source, accepted ADRs, representative tests, the open operator-adoption specification and its pinned external normative contract. No production code, accepted ADR or glossary was changed; no paid model or live deployment was contacted. Statements about possible reuse below are engineering inferences, not accepted interfaces.

## Main finding

A configurable graph is compatible with many existing mechanisms, but cannot be obtained merely by extending the YAML parser. Process topology, work-node reports, tool exposure, accounting, review attribution and terminal publication currently meet inside an implementation-specific `run_step`. The outer Attempt graph is a fixed eleven-node chain. The existing inner conversation graph is already conditional, with `open`, `model`, `tools` and `silence` nodes. This is direct evidence that the missing piece is the product contract and generic orchestration above the loop, rather than an absence of branching in LangGraph. [Attempt graph][attempt-graph], [inner step graph][inner-graph]

## Ownership and change inventory

| Area | Observed owner and guarantee | Reuse or necessary change | Representative existing tests |
| --- | --- | --- | --- |
| Configuration and trust | `workflow.parser` rejects unknown fields, duplicate keys and YAML aliases/tags; prompt paths are contained in the operator configuration directory. `workflow.effective` reads prompt bytes, fills defaults and creates a stable secret-free canonical payload/digest. | Keep strict loading, trusted operator source and immutable Effective Configuration as reusable mechanisms. Replace the ordered `steps` shape and include every new behavior-affecting graph/instruction/schema/model field in the pinned record. No `start`, event routes, block-local limits or output schemas exist today. [Schema/parser][schema], [parser fields][parser], [effective payload][effective] | `test_workflow_config.py`; `test_workflow_effective.py`; `test_mvp_workflow_run.py::test_editing_the_yaml_and_prompt_after_claim_changes_nothing` |
| Work selection and Claim | The Supervisor selects GitHub issues, not arbitrary graph inputs. Claim records an Attempt before assignment, reads the issue back and releases only the Worker's own assignment on guard failure. Selection excludes open issues with an existing marked PR. | Reuse exclusive ownership and authorization mechanisms where the chosen product boundary permits. Standalone review of an existing PR needs an explicit target/selection contract; a new `code-review` prompt alone does not supply it. [Selection][selection], [Claim][claim] | `test_worker_claim.py`; `test_tracker_selection_contract.py` |
| Readiness and baseline | The fixed graph resolves profile/toolchain/acceptance facts and rebases a Continuation; baseline executes pinned commands before work-model calls. A refusal becomes a terminal classification without asking an AI Preflight to fix it. | Existing probes, profile resolution and command/evidence runners are candidate registered runtime operations. Whether they remain mandatory, graph-visible or process-specific is an owner decision. [Readiness/baseline][readiness] | `test_readiness_node.py`; `test_baseline_node.py`; `test_mvp_continuation.py` |
| Model construction and capability | `configured_pin` constructs one pin/price set. Startup creates one model and runs its Provider Capability Assertion; `AttemptContext` supplies that same model, price and context configuration throughout the Attempt. | One model per block would change effective pinning, startup capability checks, cost lookup, transcript identification and Sub-agent attribution. Separate conversations using one model already work; per-block model identity is not already supported. [Supervisor][supervisor], [AttemptContext][context] | `test_mvp_workflow_run.py`; `test_provider.py`; `test_mvp_subagents.py::test_scenario_6_every_ended_event_names_the_pinned_model` |
| Conversation and skill activation | `WorkflowStep` accepts exactly `implement` or prompt bytes. Runtime forces activation on the first request and makes fresh conversations at step boundaries; the successor receives a Continuation Snapshot, not the predecessor's message history. Nested activation tools expose the pinned Skill Bundle. | Reuse activation, whole assistant-turn handling, isolated conversation state, result capping/redaction and artifact readers. General block instruction bundles, standalone top-level `code-review` and data schemas require a new contract; adding allowlist entries alone is insufficient. [Step declaration][step-decl], [tool construction][tools] | `test_mvp_step_opening.py`; `test_mvp_step_contract.py::test_two_steps_share_no_message_list`; `test_workflow_effective.py::test_code_review_is_refused_as_outside_the_allowlist` |
| Block exit versus completion | Non-final steps get `report_step` (`done` or `cannot-continue`). The final step gets an Attempt-specific `report_completion`, whose acceptance is checked against runtime-owned Criterion Evidence; malformed/unbacked claims have bounded returns. Silence is not completion. | Declared, validated reporting is reusable. Positional exits and Implementation Outcome classification must be separated from generic block results so review/handoff/preparation can end without claiming implementation coverage. [Positional tools][tools], [completion][completion], [list result mapping][step-result] | `test_mvp_step_contract.py::test_positional_exit_tools_on_the_bound_tool_set`; `test_completion.py`; `test_mvp_step_silence.py` |
| Tool authority and evidence | Main-step tools include workspace reads/writes, a credential-free shell, runtime targeted-test/check tools, skill activation and Sub-agents. No direct GitHub mutation tool is bound. Criterion Evidence records actual targeted-test execution, not arbitrary shell reports. Ref Guard restores branch/HEAD; Execution Footprint discloses executed-code changes. | Tool builders and evidence mechanisms are reusable, but a generic agent block must not inherit implementation write capabilities accidentally. Define its allowed tools and the evidence meaning of each result. Evidence proves the named tests ran/passed; it does not prove that their semantics establish the criterion. [Tools][tools], [evidence][evidence], [execution tools][toolset] | `test_mvp_attempt_graph.py::test_bound_tool_set_has_no_github_mutation_tool`; `test_run_check.py`; `test_footprint.py`; `test_run_command.py` |
| Review and Sub-agents | Review activation commits/pushes X before reviewers. The first following Sub-agent group in the same work node belongs to the Review only if the tree still matches X. Reports are required, but review axes and findings are not adjudicated; findings do not block publication. Sub-agents have fixed read-only tools and can run concurrently under an Attempt-wide limit. | Review snapshot/evidence machinery can be reused if its meaning is retained. A review block, new block boundary or new selection target must explicitly establish revision identity and completion attribution. Workflow-level fan-out is separate from existing Sub-agent concurrency. [Review state][review-state], [ADR 0021][adr-review], [Sub-agent tools][subagent] | `test_mvp_review_attribution.py`; `test_mvp_review_snapshot.py`; `test_mvp_subagents.py::test_scenario_27_two_calls_at_limit_one_do_not_overlap` |
| Metering and hard limits | One `StepProgress` per Attempt survives all steps and Context-Window Handoffs. Usage records include raw/cache-inclusive tokens, effective tokens excluding cache reads, bucket costs and tool calls. Work, Sub-agent and handoff spend is folded into the same meters. Hard cutoff stops further loop work and ends `failed-limit`; deterministic tail preserves edits without final checks/PR. | Keep accounting math and durable flushes. Add an explicit definition of block/visit/Attempt scopes and handler allowance. Existing 70% progress gate/80% verification reserve restricts implementation work; it is not an independently protected budget for an AI Handoff block. [Meters][meters], [progress][progress], [limit finalization][limit-tests] | `test_mvp_limits.py`; `test_mvp_step_contract.py::test_meters_run_attempt_wide_across_steps`; `test_ceilings.py` |
| Context-Window Handoff | Before a request exceeding the usable context estimate, the node requests a handoff. Runtime builds a fixed fact prompt, invokes the same model without tools and expects a versioned JSON Continuation Snapshot. It persists/verifies the snapshot, accounts spend and reopens the same step. Failure ends `handoff-failure`; the snapshot cap and max handoffs are code-owned. | Reuse fact collection, redaction, artifact addressing and verification. A configurable AI Handoff after Process budget exhaustion is a different control-flow contract: the current handoff continues work and has no separate budget. [Handoff implementation][handoff], [context policy][context-policy], [ADR 0013][adr-handoff] | `test_step_handoff.py`; `test_mvp_step_contract.py::test_handoff_in_a_prompt_file_step_reopens_with_identical_bytes` |
| Publication and terminal effects | Runtime judges Drift, completed Review, final validation and clean tree. `delivery_intent` records terminal result/rendered bodies and planned writes before publication. Effects use Attempt/write-kind identity and intent/receipt lookup. Other terminal outcomes are recorded before terminal writes. | Pure verdict/rendering functions and effect idempotency are useful seams. Graph topology must not accidentally turn arbitrary model `done` into permission to publish. A standalone review write, multiple publish blocks or repeated visits need distinct operation identity and a decided terminal contract. [Publication verdict][publication], [terminal recording][terminal], [effects][ledger] | `test_mvp_publication.py::test_delivery_intent_exists_before_publish_performs_anything`; `test_worker_outcome_recovery.py::test_l3_del_15_crash_after_each_terminal_write_writes_once` |
| Cancellation, interruption and recovery | Cancellation is issue closure/removal of the Selection Label. Pre-Intent SIGTERM interrupts and stops new model calls; runtime kills command process groups. After Intent publication completes. Recovery validates pinned configuration and finishes recorded work with no model; no framework checkpointer is used. | These mechanics do not inherently require a fixed process graph. Resuming arbitrary blocks or spending AI budget on restart would reopen ADR 0017. Waiving YAML compatibility does not make old unfinished ledgers readable under a new shape; `pinned_recovery_config` explicitly reads current paths. [Gates][gates], [recovery][recovery], [ADR 0017][adr-recovery] | `test_mvp_sigterm.py`; `test_worker_recovery.py`; `test_worker_outcome_recovery.py::test_recovery_takes_no_model` |

## Existing external-operation surface

`TrackerPort` currently exposes issue reads/selection/assignment/labels, terminal comments, marked-PR lookup/creation and base-revision reads. It has no first-class operation for selecting a PR as the work target, publishing a PR review, approving, enabling auto-merge or merging. Existing `open_pull_request` always creates a non-draft PR. Thus the standalone-review acceptance example needs a decided target and minimal publication operation set, not just generic node compilation. Shell access is credential-stripped and is not a substitute for these registered operations. [TrackerPort][tracker], [tools][tools]

## ADR and contract conflicts to resolve deliberately

1. **ADR 0016:** the outer skeleton and its guarantees are explicitly outside YAML. A whole-process declarative graph conflicts with that accepted scope. The decision must say which lifecycle remains mandatory and where its guarantees are enforced. [ADR 0016][adr-workflow]
2. **ADR 0013:** Context-Window Handoff has a fixed continuation purpose, same Pinned Model, no tools, shared Attempt budgets and a terminal failure classification. An AI-assisted workflow handoff can reuse its mechanisms but cannot silently inherit or redefine all those semantics. [ADR 0013][adr-handoff]
3. **ADR 0014:** the final implementation declaration is verified against runtime-owned evidence. Generic block reporting must preserve that distinction when implementation completion is claimed, while allowing other kinds of result. [ADR 0014][adr-completion]
4. **ADR 0021:** Review is attributed by activation/group/tree position, and findings do not gate delivery. A separately configured review block or a changed finding policy must state its new meaning explicitly. [ADR 0021][adr-review]
5. **ADR 0003 and ADR 0017:** recorded Delivery Intent and immutable terminal results make publication recovery independent of ephemeral graph state. Replaying a configurable graph or resuming the model is a policy change, not a consequence of using LangGraph. [ADR 0003][adr-intent], [ADR 0017][adr-recovery]
6. **ADR 0023:** the Worker owns the model/tool loop. An agent block is compatible with this if it uses that loop; a third-party agent owning its own loop remains a separate scope decision. [ADR 0023][adr-loop]
7. **Glossary/Pinned Model:** per-block model choice is a proposed expansion of a single Attempt-wide pin. A different conversation is not a different model pin. [Glossary][glossary], [supervisor][supervisor]

These are facts about conflicts, not recommendations to retain or remove the old policies.

## Operator-contract implications: specification versus implementation

[Spec: coding-agent serves operator contract major 1 as a producer](https://github.com/lbacik/coding-agent/issues/245) is open at investigation time. It targets the [normative operator contract pinned at 06ee46c](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/docs/spec.md). Its published solution retains the Run Ledger, existing outcomes/Attempt IDs, Publication Gate and Delivery Intent, effective-token arithmetic and SIGTERM interruption; contract records are read-only projections of runtime facts. It advertises only `next-issue` and `stop-after`, excluding the distinct `operator-handoff` capability and model recovery commands. The new graph planning must either preserve these exposed meanings or explicitly coordinate amendments; calling a configured block Handoff does not advertise operator-handoff.

This baseline has no `agentctl` entry point, operator control package, Status Snapshot producer or archive-export implementation. The CLI dispatch and Compose command remain the existing Worker path; therefore the operator feature set above is a specification dependency, not observed shipped behavior. New block identity/usage/terminal artifacts need projection decisions for that adoption effort without introducing a second lifecycle authority. [CLI][cli], [Compose][compose], [ledger][ledger]

The normative external contract's sections 7, 9 and 11 cover figures and their basis, observation and archive sealing. New per-block counters should not change the basis of existing Attempt-wide figures silently; artifacts written by new blocks must be complete before an archive is declared sealed. Those are integration consequences of the contract, not an implementation proposal. [Pinned external contract](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/docs/spec.md)

## Candidate implementation seams (inference)

- **Configuration normalization/pinning:** extend the existing strict parser/effective module responsibilities to the resolved graph schema. Validate first and freeze all referenced artifacts before Claim; do not make process code reach back to mutable files.
- **Block executor:** extract the reusable conversation loop and validated report handling from the implementation-specific orchestration. Supply model/tools/evidence policy through runtime context; return a small block result and artifact references rather than the entire prior conversation.
- **Graph compiler/router:** map a validated definition into node factories and conditional routes. The current `build_attempt_graph` is the replaceable outer seam; `run_work_node` demonstrates the reusable inner loop, but its caller currently combines many implementation policies.
- **Accounting/stop policy:** retain UsageTotals, ledger flushes and stop hooks; introduce the agreed scopes and handler authorization around them rather than starting fresh meters at each loop visit.
- **Registered runtime operations:** wrap existing profile/baseline, snapshot, publication and terminal helpers where selected by the owner. Keep external-effect verification in runtime-owned code.
- **Durable execution identities:** generalize intent/receipt keys and transition/artifact records to repeated block executions. Whether this remains finish-only recovery or becomes work resumption must be decided before selecting persistence mechanisms.

No package layout or new interface is selected by this research. [Config][effective], [inner graph][inner-graph], [run_step][step-result], [ledger][ledger]

## Open owner decisions this investigation enables

- Which lifecycle remains mandatory around the graph, and which checks/publication rules remain non-bypassable operations?
- What is the minimal execution/result contract for an implementation, read-only review, AI Handoff and deterministic terminal block?
- Is one Pinned Model retained per Attempt, or are provider/model/effort, capability checks and prices pinned per block?
- What are the scopes and precedence of per-conversation, per-visit, per-block and Attempt limits? How is handler capacity protected without reopening work budget?
- What new PR-target selection and review publication behavior is essential for the standalone-review example?
- How are repeated external operations identified, existing unfinished Attempts handled and operator projections preserved? Does recovery still finish recorded outcomes without model work?

These questions belong to the map's human decision tickets; no answer is inferred from illustrative YAML.

## Verification and documentation discrepancies

Ran in the isolated checkout with `PYTHONPATH=src`, using the existing Python environment:

```text
python -m pytest -q tests/test_mvp_attempt_graph.py tests/test_mvp_limits.py \
  tests/test_step_handoff.py tests/test_worker_outcome_recovery.py
51 passed, 1 warning in 17.98s
```

The warning was a sandbox-denied attempt to create pytest's cache directory in the managed checkout, not a failed product assertion. These scripted/local-remote tests establish representative current behavior, not future graph correctness or paid-provider behavior. Other named tests in the inventory were inspected as relevant regression surfaces, not all rerun here.

Two source comments are stale and should not guide the new architecture: `attempt.py`'s opening docstring still describes several later-ticket nodes as pass-through even though their functions are implemented, and `ceilings.InMemoryUsageLedger` says no Run Ledger exists even though production uses the SQLite ledger. The research above follows executable functions and tests rather than those comments. [Stale attempt introduction](https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/attempt.py#L50-L68), [stale meter comment](https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/ceilings.py#L282-L290)

## Source index

All coding-agent links below are pinned to the investigated revision.

[glossary]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/CONTEXT.md
[schema]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/workflow/schema.py#L85-L132
[parser]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/workflow/parser.py#L63-L76
[effective]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/workflow/effective.py#L432-L546
[attempt-graph]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/attempt.py#L292-L351
[inner-graph]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/step.py#L877-L923
[readiness]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/attempt.py#L393-L603
[supervisor]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/worker/supervisor.py#L399-L544
[context]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/context.py#L59-L144
[step-decl]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/step.py#L130-L171
[tools]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/tools.py#L313-L445
[completion]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/completion.py#L432-L567
[step-result]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/attempt.py#L2180-L2348
[evidence]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/completion.py#L113-L166
[toolset]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/toolset.py#L497-L774
[review-state]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/review_snapshot.py#L72-L128
[subagent]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/subagent.py#L399-L448
[meters]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/ceilings.py#L34-L76
[progress]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/step_progress.py#L117-L127
[limit-tests]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/tests/test_mvp_limits.py#L237-L278
[handoff]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/context_handoff.py#L550-L652
[context-policy]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/provider/config.py#L47-L72
[publication]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/publication.py#L97-L164
[terminal]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/graph/attempt.py#L3529-L3675
[ledger]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/ledger/store.py
[selection]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/tracker/selection.py#L30-L72
[claim]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/worker/claim.py
[tracker]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/tracker/port.py#L132-L242
[gates]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/runtime/gates.py
[recovery]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/worker/recovery.py#L175-L242
[cli]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/src/coding_agent/cli.py
[compose]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/compose.yaml
[adr-workflow]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md
[adr-handoff]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0013-context-window-handoff.md
[adr-completion]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0014-completion-is-declared-and-substantiated-never-inferred.md
[adr-review]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0021-a-review-is-the-first-sub-agent-group-after-the-code-review-activation.md
[adr-intent]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0003-delivery-intent.md
[adr-recovery]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md
[adr-loop]: https://github.com/lbacik/coding-agent/blob/815de8c3358e0d02b1f84260ab91c78b79b7f931/docs/adr/0023-the-worker-drives-only-models-whose-tool-loop-it-owns.md
