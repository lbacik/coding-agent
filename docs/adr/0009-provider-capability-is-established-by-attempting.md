---
status: accepted
---

# Provider capability is established by attempting, not by reading a declaration

The Worker refuses to start against a model that cannot do what a work node needs, and it decides that by **making one small call**, not by reading what the provider says about itself. The Required Capability list is four items long, the assertion runs once at Worker startup, and its refusal is a nonzero process exit with no Attempt opened. Decided in [Do both providers pass the runtime contract through init_chat_model, and what does the preflight capability assertion assert?](https://github.com/lbacik/coding-agent/issues/24).

## The observation that rules out the obvious design

The obvious design is declarative: ask each provider what its model supports, compare against a list, refuse on a mismatch. It costs nothing and runs before any Attempt, which is exactly what `L1-4` asks for.

[The provider prototype](https://github.com/lbacik/coding-agent/issues/24) asked both providers. They do not answer the same question:

| | `GET /v1/models/{id}` returns |
| --- | --- |
| `anthropic:claude-sonnet-5` | `max_input_tokens`, `max_tokens`, and a capability tree — `structured_outputs`, `effort` with each level, `thinking.types.enabled.supported: false`, `batch`, `citations`, `context_management`, `code_execution`, `image_input`, `pdf_input` |
| `openai:gpt-5.6-terra` | `id`, `object`, `created`, `owned_by`, `shutdown_date` |

So a declarative check is not a thing one can write once: it would refuse on facts for one pin and abstain for the other, and abstention is the answer that matters — a check that passes because it had nothing to read is indistinguishable from a check that passed.

**And neither provider declares tool calling.** It is absent from Anthropic's tree, which is otherwise generous, and OpenAI declares nothing at all. Tool calling is the one capability every work node depends on: `implement` is a bounded tool loop, `validate` reads its evidence from tools, the reviewers are given read-only toolsets. The capability the design rests on hardest is the one no declaration covers.

The prototype also probed the other direction — bind a toolset to a model that lacks something and see what comes back — and found that on OpenAI **a missing capability and a missing model arrive as the same exception class**, `OpenAIModelNotFoundError`, distinguishable only by message prose, with the status varying by endpoint (404 on chat completions, 400 on Responses). A classifier over that surface would be classifying English.

§10 already settled the identical shape for the credential:

> No endpoint enumerates a fine-grained token's permissions, so capability is established by attempting the write, not by reading a grant.

That section split the concerns by **how often they need to run**, because attempting every write at every start would litter the Target Repository. A model call litters nothing. It only costs — and the prototype measured the cost: a refused binding came back in 0.7 s, and the cheapest successful probe was a fraction of a cent. So the reason §10 kept its write probes out of startup does not apply here, and the analogy carries with the frequency restriction lifted.

## The rule, in four parts

**1. The Required Capability list is four items.** Each earns its place by something that breaks without it, not by being available:

- **Tool calling** — every work node is a tool loop. Neither provider declares it.
- **The effort pin, at the pinned level.** The prototype's cheapest probe sent a deliberately misspelled effort value to prove the parameter reaches the provider at all; both refused it with a 400 naming the parameter and enumerating the valid set. That probe exists because the failure it guards against is invisible: an effort pin silently dropped produces a run that looks correct and is **not the run the Attempt recorded**.
- **Non-zero usage reporting.** §5 counts cost and tokens from `usage_metadata` and flushes after every model response. A provider that reports zeros makes every ceiling in §5 unenforceable while every Attempt still reads as within budget.
- **A Price Table entry for the Pinned Model.** See consequences below.

**Context window is deliberately not on the list.** Anthropic declares `max_input_tokens`; OpenAI declares nothing, so it is assertable on one pin and not the other — and what it would be asserted *against* is the size of the pinned material, which is [#25](https://github.com/lbacik/coding-agent/issues/25)'s third question and not yet decided. Adding it here would mean asserting on one provider a fact the other cannot supply, against a threshold nobody has set.

**2. The Provider Capability Assertion runs once, at Worker startup.** The **Pinned Model** is deployment configuration: nothing in the contract lets a Target Issue choose a model, the Project Profile describes the *Target Repository's* toolchain rather than our runtime, and §10 makes the credential Worker-wide for the same reason. "Pinned for the Attempt" therefore means *immutable for the Attempt's duration*, and recording it beside the Fingerprint is evidence rather than variability. One startup assertion covers every Attempt the Worker runs.

**3. Where a provider does declare, the declaration is read as a second sieve — never as the first.** Anthropic's tree is worth reading: it is free, and it explains a refusal in advance rather than after a 400. It runs *after* the attempt has passed, and it can only ever add a refusal, never authorise one the attempt did not.

**4. Refusal is a nonzero process exit, and no Attempt is opened.** This follows [ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md)'s precedent for a start-time failure on its stated grounds — *"no Attempt is open, so there is nothing to classify"* — and it is the opposite of [ADR 0008](0008-the-tdd-seam-gate-is-a-readiness-fact.md)'s choice for `confirm_seam`, which fails the Attempt because `claim_labels` has already run and exiting would strand a labelled issue. Here nothing has been claimed and no issue has been touched, so there is nothing to strand and nobody to explain it to on GitHub. A misconfigured deployment is not a property of any Target Issue.

## Considered options

**A static capability table we maintain, keyed by model id.** Rejected on the same ground §10 rejected reading a grant: it describes our idea of the model rather than the model. It is also the option that fails silently and late — a pin added to configuration without a table row either refuses a model that works or, worse, passes a model that does not, and the table's staleness is invisible until a work node is halfway through an Attempt.

**Declaration only.** Rejected by the observation: for the OpenAI pin there is nothing to read, and for tool calling there is nothing to read on either.

**A capability check per Attempt rather than per Worker.** Rejected: it pays a call per issue for a variability nobody asked for. It becomes the right answer the moment a Target Issue may name its own model, and this ADR is what would have to be reopened.

**Fold the model-side check into `agent preflight`, beside the GitHub write probes.** Rejected: §10 put those probes in an operator command precisely because they mutate a repository, and a model call does not. Putting the capability assertion there would mean a Worker can start against a model nobody checked, on the strength of an operator having checked a different deployment at some point in the past.

**No check at all** — let the missing capability surface as a failed Attempt, the way §10 lets a missing token permission surface *"as a permission refusal when it bites"*. Rejected on the asymmetry between the two: a token permission bites on one write kind and fails that Attempt alone, whereas a model that cannot call tools fails **every** Attempt identically and explains none of them well. That is the premise `Credential Failure` uses to justify halting the Worker, and the same reasoning says do not start it.

## Consequences

**`L1-4` is amended into two rows.** *"Preflight against a model lacking a required capability → the Worker refuses to start; no Attempt is opened"* stands, but it is only assertable once the list and the mechanism exist, and it needs a sibling stating that **the assertion is a real call, not a table lookup** — otherwise a static-table implementation satisfies the row as written while reintroducing everything this ADR rejected.

**`L1-2` is retired.** The row asks for structured output from *"the classifier role"*, and there is no such role: every classification §4 specifies is deterministic — rate limits by status and header value (§6), the Workflows refusal by write kind and target path (§10), failure transience by class, `evaluate_readiness` by parsing the profile, and `confirm_seam` explicitly *"performs no model call"*. The mechanism was probed anyway and works on both pins, so nothing is lost by retiring the row. Two facts are worth keeping for whoever adds a classifier later: **`json_schema` is the method**, because `function_calling` warns that structured output is not guaranteed while `thinking` is enabled; and `review_join` reading the finding taxonomy out of §4's own labels is **not** structured output — [ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md) forbids the schema that would make it so.

**The Price Table is a declared configuration input, and a pin without an entry refuses to start.** `usage_metadata` reports token counts and never money; no provider exposes a rate through any API, and the prototype could not find one for `gpt-5.6-terra` in anything it could read. `L4-3` makes the observed cost the first honest input to the per-Attempt ceiling, so a pin that may start without a price is a pin whose cost ceiling is silent exactly when it is needed.

**The table is priced per bucket, not per direction.** The prototype found the naive formula wrong in both directions on the same pin: `input_tokens` is the **whole** prompt including cache hits rather than the uncached remainder, so a cache read is over-charged **9.7x**; and an Anthropic cache write lands in `ephemeral_5m_input_tokens` while the normalised `cache_creation` stays zero, so it is under-charged at **0.8x**. The prototype's own cost script fell into the second trap on its first run, which is the argument for the buckets being named in configuration rather than inferred.

**A token ceiling in §5 is not portable across pins.** The two providers counted the identical prompt at 6818 and 4414 tokens. A single tuning constant means two different budgets.

**§10's rate-limit headroom check has no provider-side data.** It reads *"Startup therefore checks for headroom"*, and langchain surfaces **no response headers at all** on a successful call — `response_metadata` carries the model, the stop reason and usage, and nothing from the wire. Headroom remains checkable for GitHub and unavailable for the provider, and `L1-5`'s *overloaded* row is not assertable as a run either: it cannot be summoned on demand, so it becomes a unit-level rule over a synthetic response, exactly the amendment [#22](https://github.com/lbacik/coding-agent/issues/22) forced on `L3-IMP-2` and `L3-IMP-3`.

**"No cross-model fallback" is now structurally assertable.** The prototype recorded the answering model on every response of every run — `models_seen` in the Run Ledger row, one entry per flush — and no run swapped. Paired with the absence of `with_fallbacks` in the adapter, that is an assertion on the toolset rather than on the model's account of itself, which is the pattern this map keeps arriving at.

Reversing this decision means going back to declarations. Any replacement must say what it reads to establish **tool calling**, which neither provider declares, and how a check that passed because it had nothing to read is told apart from one that passed.
