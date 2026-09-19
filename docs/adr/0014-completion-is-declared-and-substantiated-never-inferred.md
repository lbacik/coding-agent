---
status: accepted
---

# Completion is declared and substantiated, never inferred

The implementation stage ends with an explicit, verified act by the model, and the evidence that act rests on is produced by the runtime rather than by the model. Decided in [issue #63](https://github.com/lbacik/coding-agent/issues/63), part of the [planning map #57](https://github.com/lbacik/coding-agent/issues/57).

## Context

A pilot run of `agent implement` explored the Target Repository, ran an existing passing test unrelated to the task, and returned a turn with no tool calls. The tool loop treats a response without tool calls as the end of the implementation stage, unconditionally, so the Attempt ended there with an unresolved issue and an empty diff.

Nothing in the loop was broken as written. The defect is that **"the model stopped calling tools" and "the model considers the work finished" were the same observable event**, so no rule could distinguish them. Every outcome the runtime then derived — a category, a terminal classification — was derived from that one ambiguous signal.

A second, quieter version of the same problem sat underneath it. The only signal that an acceptance criterion had been resolved was a regular expression over the model's own prose (`progress: acceptance_criterion`), and `{evidence_dir}` was an ordinary directory that the model's shell tool could write. An account of a test run is not evidence, as the contract already says; a JUnit file the model wrote by hand is that same account in another file format.

## Decision

**One dedicated tool ends the implementation stage.** `report_completion` is the only non-exceptional exit from the model's tool loop, mirroring the forced `activate_implement` that opens it ([#60](https://github.com/lbacik/coding-agent/issues/60)). It carries a **Completion Claim**: one entry per **Acceptance Criterion** of this Attempt, each either naming the test identifiers that cover it or explicitly marked uncovered.

**The runtime enumerates the criteria, not the model.** Acceptance Criteria are resolved deterministically from the Target Issue body at `evaluate_readiness`, as a Readiness Fact, the way the Seam Set already is ([ADR 0008](./0008-the-tdd-seam-gate-is-a-readiness-fact.md)). The tool's schema is generated from that list, so a Completion Claim cannot silently narrow the task's scope. A Target Issue carrying no criterion is a missing Readiness Fact and routes to clarification.

**The runtime owns the evidence.** `test_targeted` runs only through a runtime-owned tool that executes the Validation Contract's command, is the sole writer of `{evidence_dir}`, and returns the parsed result. Only evidence that tool produced is **Criterion Evidence**. The model may still run tests through its shell for its own iteration; such a run produces nothing that counts, and the Attempt Header says so.

**Silence is answered, not obeyed.** A turn with no tool calls and no accepted Completion Claim yields a deterministic continuation message naming the unmet obligations — which criteria lack evidence, whether a candidate diff exists, which targeted tests ran. The runtime composes it from the missing evidence; it is never a generic "carry on", because a generic nudge buys another turn of the exploration that caused the failure. The budget is **two continuations per work node**, monotonic, never reset by progress. No continuation is issued in the verification reserve phase or after a hard ceiling; there the node proceeds to deterministic finalization.

**A claim that fails verification is returned, twice.** Where a named test identifier is absent from Criterion Evidence or did not execute, the call is rejected with a tool result naming that claim. A rejection is not a progress event. After two rejections the claim is force-accepted and the outcome downgraded, so a mistyped identifier costs turns rather than the Attempt.

**The implementation stage never decides publication.** It produces exactly one **Implementation Outcome**, describing the working tree and the evidence gathered. That outcome is an input to the Publication Gate alongside Drift, base-branch movement, limits and the review status [#62](https://github.com/lbacik/coding-agent/issues/62) defines — never a substitute for it.

## Why the runtime runs the tests

Re-running each named test at acceptance time, outside the model's loop, would be equally sound: the runtime would trust only its own execution. It was rejected on cost, not on correctness — it pays for a second full run of every test on every Completion Claim, where a runtime-owned tool has already run it once and simply kept the result.

Trusting `{evidence_dir}` as written was rejected outright. It reopens, in file form, exactly the hole §8 closes in prose form.

The shape of the rule is the one this repository reaches for repeatedly: **a region nothing can reach, rather than a rule something must keep obeying**. The model has no GitHub mutation tool (§1), no tool resolving a path under a skill directory ([ADR 0012](./0012-companion-files-are-injected-or-refused-never-fetched.md)), and now no write access to the directory its own evidence is read from. In each case a compliance question became a structural one.

## Consequences

`no change produced` is renamed **`already satisfied`** and means a judgement rather than a diff shape: the issue was already satisfied at the Base Revision and every criterion is covered by verified Criterion Evidence. The unjustified cases gain their own category, **`completion not established`**, carrying a reason of `undeclared`, `claims-unverified` or `criteria-uncovered`. Under the old names the pilot would have reported `no change produced`, indistinguishable from an issue genuinely already done.

An explicitly uncovered criterion is accepted rather than refused. Refusing it would leave a model that did incomplete work with one path to an accepted claim — asserting coverage it does not have — and Criterion Evidence makes that assertion detectable but does not make the incentive disappear.

The bar on an input Target Issue rises: it must carry acceptance criteria in the body's checklist convention, or it goes to clarification. This is the same bar ADR 0008 set for the Seam Set and uses the same mechanism.

Continuation and rejection budgets are per work node. A fresh node after a Context-Window Handoff opens with both restored, because the Continuation Snapshot carries work facts and never runtime policy state ([#61](https://github.com/lbacik/coding-agent/issues/61)). Attempt-wide meters run through the handoff unbroken, so the restored budgets cannot be farmed by splitting work across nodes.

The regression row for the pilot lives at L3, against a scripted model. The failure was the runtime's, not the model's: the loop broke on silence and would have broken identically for a model that behaved impeccably.

## What this decision does not claim

This ADR was accepted on a deterministic argument and a throwaway logic prototype ([issue #64](https://github.com/lbacik/coding-agent/issues/64)), never on a measurement of model behaviour against a paid provider.

What is established is structural and negative: the runtime no longer has to guess. `report_completion` makes *the model considers itself done* a different observable from *the model stopped calling tools*, and that separation holds whatever the model does. What is **not** established is that any model will now produce better work, choose better tools, or reach `verified completion` more often. A model that gives up early still gives up early; what changes is that the Attempt says so, as `completion not established`, instead of reporting an empty diff as a result.

The pilot regression `L3-IMP-24` exercises the runtime's classification against a scripted model. It is not a measurement of the provider that produced the pilot. A paid, repeatable comparison — identical Target Issue fixtures, the same Pinned Model configuration, tool schema and Attempt budgets, several runs, and provider cache usage where the provider reports it — is tracked separately and is not a prerequisite of this decision.
