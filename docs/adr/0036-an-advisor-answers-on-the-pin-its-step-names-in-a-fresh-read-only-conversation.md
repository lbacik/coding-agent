---
status: accepted
---

# An Advisor answers on the pin its step names, in a fresh read-only conversation

An agent step may declare a sixth tool group, `advisor`, together with `agent.advisor: {model, prompt}`. The group binds one tool, `ask_advisor(question)`. Each call starts an **Advisor**: a fresh, read-only conversation answered by the pin `advisor.model` names, or by the Pinned Model where it names none, whose only return is its report. The calling step keeps its own conversation, so it can ask again without losing what it has explored. The Workflow's `model` stays the default pin: an element that may name another `models` entry overrides it for that element alone, and the Advisor is the only such element. A per-step `model` and a per-Sub-agent `model` stay rejected. Whether the caller follows an answer is the operator's policy, carried by the operator's prompts, recorded but never enforced ([ADR 0027](./0027-workflow-policy-is-separate-from-runtime-mechanics.md)). This amends §2.7, §4.1, §9.1, §9.4, §11.4 and §19 of the step-workflow runtime contract (#675). The decision records intent: it does not claim that the current code implements it.

## Why

- **Upstream skills ask a maintainer and wait.** `triage` says "Recommend … Wait for direction" and grills "a round of questions at a time". A Worker has no human in the loop, and under [ADR 0007](./0007-upstream-skill-text-is-answered-never-rewritten.md) the skill text is answered, never rewritten. Without an Advisor the step can only decide everything alone or end `blocked`.
- **The operator wants a split by cost.** A cheap model does the long work. A stronger model answers its questions as a maintainer proxy. Only the questions pay the stronger model's prices.
- **The proxy must be able to check what it is asked.** If the Advisor saw only the question, the weaker model would steer the stronger one through its own summary, and a wrong summary would be confirmed. So the Advisor gets the read-only Sub-agent tools and the same data the caller was given.
- **Asking must not cost the caller its exploration.** Every Step Execution is a fresh conversation, so an answer delivered through the graph would throw the caller's context away.
- **Every pin keeps every guarantee.** The Advisor's pin is a pin like any other, so no second model answers without the evidence the Pinned Model needs ([ADR 0029](./0029-the-answering-model-is-asserted-from-each-responses-evidence.md)).

## Considered options

- **A per-step `model`, with a question round-tripping through the graph (`triage → maintainer → triage`).** Rejected: a step cannot read its own earlier execution's result, so each round re-explores from nothing. The general rule stays open: allowing a per-step `model` later is a decision about that element, not a new mechanism. It is not taken here, because an inheriting Route would then carry verbatim assistant turns across pins ([ADR 0010](./0010-the-assistant-turn-is-carried-back-verbatim.md)).
- **Two Workers exchanging the issue through labels and comments.** Rejected: an end label equal to a Selection Label is refused by design, and working around it with two files only defeats the guard.
- **One request per question, without tools.** Rejected: the Advisor could not verify the caller's account of the code.
- **One persistent Advisor session per Step Execution.** Deferred, not rejected. It would save re-exploration across rounds, but it adds a second long-lived conversation to a step, with its own Context Threshold, round counting and recovery. It can be added later as an opt-in without breaking fresh consultations.
- **A separate Advisor budget.** Rejected: the operator already bounds a step through its Step Allowance.
- **The runtime enforcing the answers**, for example by requiring the Step Result to cite them. Rejected as mechanics acting for policy.

## Consequences

- **Configuration.** The `advisor` group and the `agent.advisor` mapping are each refused without the other. `advisor.model` is optional and must name a `models` entry; it may name the Workflow's own pin. `advisor.prompt` is required: one prompt file under the trust rules of §2.8. Instructions lists and skills are not accepted there.
- **Who asks.** Only a step's main conversation. A Sub-agent and an Advisor bind no `ask_advisor`. An inheriting entry treats historical `ask_advisor` exchanges as data, like any other historical tool call.
- **The tool.** `ask_advisor` takes only `question`: required, non-blank, at most 64 KiB. Every call is a barrier, so calls in one assistant turn run one at a time in call order. The operator guide recommends sending a whole round of questions in one call and quoting earlier questions and answers in the next round.
- **The opening.** Worker-authored framing, then items 1–3 of the caller's Pinned Prefix (the Attempt facts, the input framed as data, and the caller's declared inputs), then the operator's advisor prompt, then the question verbatim. The caller's Step Result contract is not included.
- **Mechanics as for a Sub-agent.** The Advisor binds exactly the Sub-agent's read-only set, with its own report tool in place of the Sub-agent report, and no skill activation. The 8-round bound, the 32 KiB report rule and the Sub-agent endings apply, including `pin-violation`. A model failure ends the Advisor, never the caller (#570). A pin violation halts the Worker, as on the Pinned Model.
- **Pins.** The Provider Capability Assertion runs at startup for every distinct pin the selected Workflow references. `status.provider_capability` becomes a map from pin id to report, a breaking change to the operator-contract projection. The identity and effort assertions and the gateway suppression apply to every Advisor request.
- **Spend and context.**
  - The Advisor's spend counts against the calling Step Execution, its Step Allowance, the Work Pool or reserve, and the Attempt Ceiling, priced at the Advisor pin's prices. Each request passes Admission individually.
  - The Advisor's guard compares its own Conversation Context with the Advisor pin's Usable Threshold.
  - Static validation's per-step maximum for a step with `advisor` includes the Advisor pin's largest admissible request.
  - Each `ask_advisor` call is a tool call of the caller. There is no cap on the number of questions.
- **Effective Configuration.** The digest covers the Advisor pin and the bytes of `advisor.prompt`.
- **Records.**
  - The Run Ledger records `advisor_started` and `advisor_ended`, with the fields of `subagent_ended`, plus the pin id and the question's length and digest.
  - The Transcript records the whole Advisor conversation.
  - The Execution Summary lists each consultation with the start of its question, its ending and its pin.
- **Workflows that do not declare `advisor`** keep one pin, and their records are unchanged except for the status map.
