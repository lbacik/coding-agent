---
status: accepted
---

# The answering model is asserted from each response's evidence

A direct pin's response model must equal the pinned model id exactly, asserted per response from that response's own evidence. A reported difference — including a vendor-resolved snapshot — is a pin violation: the response is discarded, its usage charged, the Attempt ends `interrupted` with reason `pin-violation`, and the Worker halts. Decided under [How are step transitions, terminal outcomes and external effects recorded and recovered?](https://github.com/lbacik/coding-agent/issues/283), part of [Design: declarative step workflows compiled from YAML to LangGraph](https://github.com/lbacik/coding-agent/issues/276); contract §9.1, §12 and §16.3 of the step-workflow runtime contract.

This is an accepted decision for the step-workflow runtime, and it is about identity, not capability. ADR 0009 establishes that the provider honours the pin (forced tool choice, usage, effort reach the wire); this ADR establishes that the answering model *is* the pin, from each response's own metadata. Direct pins assert the provider-reported response model; gateway pins assert the gateway's own identity headers through the same verdict.

## Context

Both current pins echo the pinned id with no snapshot suffix. If a vendor resolves the pinned name to a snapshot, the response names another model while the run otherwise looks correct — the Attempt would be recorded under a model that never answered it. Capability checks cannot catch this: the provider honoured every parameter and still answered as someone else.

## Decision

- **One function owns the identity rules.** Given a pin and one response it returns the observed identity and a verdict: `check_direct_pin` reads the response metadata's `model` (falling back to `model_name`, the two adapters' names for the same evidence) for a direct pin, and the gateway branch for a gateway pin; it reports `observed` plus whether it `violated`. There is no second seam.
- **A gateway response is from the Pinned Model only when the `x-litellm-model-group` header equals the pinned name and `x-litellm-attempted-fallbacks` is `0`.** Any deployment the gateway spreads that name over counts as the Pinned Model. `x-litellm-model-id`, `x-litellm-model-name`, the call id and the observed `x-litellm-version` are recorded, never asserted; the body `model` is never read for identity. A successful response missing either identity header fails closed, as does any attempted retry above `0` (a missing retry header means `0`) or a present-but-unparseable count. Only the identity check covers successful responses: an error without identity headers is classified by its status as today, but retry or fallback evidence above `0` on an error is still a violation — a retry or fallback happened despite the per-request suppression.
- **Every gateway request carries the per-request suppression** (`disable_fallbacks: true`, `num_retries: 0`): the Worker's own provider retry is then the only retry, and each of its tries passes Admission and counts against the Attempt Ceiling. The reaction to a gateway violation is the same as for a direct pin, including refusal of a violating probe at startup.
- **Exact equality.** A direct pin's response model must equal the pinned model id exactly. Any reported difference — including a vendor-resolved snapshot suffix — is a violation. When a vendor resolves the pinned name to a snapshot, the operator pins the name the vendor reports.
- **Unreported is not a violation.** A response whose metadata carries no model records `response_model: null`: it carries no evidence either way, and the run continues as before.
- **A violation discards the response.** It is never added to the conversation, never carried back to the model, and never retried by the provider retry. Its usage is charged at actual meter spend. Its observed identity and the violation are recorded in the Run Ledger beside the response's own `model_response` event.
- **The Attempt ends `interrupted` / `pin-violation` through the §12 finalisation**: the in-flight request is discarded, Sub-agents end stopped, and nothing is committed or pushed. The Workflow never routes a pin violation — an operator configuration fault would repeat in every Attempt — so the Worker halts at once with a non-zero exit, as on a Credential Failure. After the halt, a restart's recovery finishes the Attempt's terminal writes. The set of Terminal Outcomes stays closed; only this reason is new.
- **The startup probe goes through the same check.** A probe response naming another model refuses startup with no Attempt opened — the operator pins the name the vendor reports before any work is claimed.

## Considered options

**Warn and continue.** Rejected: the Attempt record would attribute work to a model that did not do it, and every later Attempt would repeat the misattribution silently.

**Route the violation through the Workflow.** Rejected: routing is process policy for work outcomes, and a misconfigured pin is not a work outcome — it would fail identically in every Attempt the Workflow opens.

**Normalise snapshot suffixes.** Rejected: stripping a `-20260101` suffix assumes the vendor's snapshot is the pinned model under another name, which is exactly the claim being asserted. The operator sees the observed name in the ledger and pins it explicitly.

**Fall back to another model.** Rejected: there is no fallback anywhere in the runtime (§9.1), and a silent substitution would make the Attempt's model record false.

## Consequences

- A vendor that starts resolving a pinned name to a snapshot halts the Worker at the next probe or response; the ledger names the observed model so the operator knows what to pin.
- Responses that report no model behave exactly as before — recorded as `null`, never a violation — so adapters that omit the field are unaffected.
- Reversing this means deciding again what counts as evidence that the pinned model answered, and where that check lives.
