---
status: accepted
---

# The assistant turn is carried back verbatim, and the adapter picks the endpoint that permits it

Our tool loop appends the message object the provider returned and sends it back unchanged. It never rebuilds an assistant turn from the text and the tool calls it can see. Where a provider offers more than one endpoint, the adapter picks the one on which a work node's requirements — tools **and** a pinned effort — are both accepted. Decided in [Do both providers pass the runtime contract through init_chat_model, and what does the preflight capability assertion assert?](https://github.com/lbacik/coding-agent/issues/24).

## The observation

`init_chat_model("openai:gpt-5.6-terra", reasoning_effort="medium")` constructs without complaint, answers a plain prompt, and then refuses the first thing a work node asks of it:

> `400 Function tools with reasoning_effort are not supported for gpt-5.6-terra in /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'.`

Tools and a pinned effort are both non-negotiable for a work node — the first because every one of them is a tool loop, the second because [ADR 0009](0009-provider-capability-is-established-by-attempting.md) makes the effort pin a Required Capability. So the endpoint is not a preference. On the default endpoint **the pin does not run at all**, and the only alternative that keeps both is the Responses API.

Switching to it works, and changes the shape of the assistant turn. On `/v1/chat/completions` the turn is text plus tool calls. On the Responses API it is `['reasoning', 'function_call']`, where the `reasoning` block carries an opaque `encrypted_content` blob the provider expects to see again on the next request. The Anthropic pin has the same shape available for the same reason: adaptive thinking emits `thinking` blocks bound to the producing model.

The prototype's round trip passed on both pins — because it appended the `AIMessage` object it received. A loop that rebuilt the turn from `.text` and `.tool_calls`, which is the natural thing to write and reads correctly, would have dropped the `reasoning` block on the floor.

## Why this is an ADR and not a comment in the adapter

Because breaking it is invisible in the output. A loop that discards the reasoning block does not crash: the provider accepts the truncated history, the model answers, the run completes, the diff looks like a diff. What is lost is the model's own chain across tool calls, and nothing downstream can tell a run that kept it from a run that did not.

That is the same class of failure this map has now hit three times, and the reason each was settled structurally rather than behaviourally: [#20](https://github.com/lbacik/coding-agent/issues/20)'s reviewer collapse reported *"Both axes ran in parallel"* when nothing had; [#22](https://github.com/lbacik/coding-agent/issues/22)'s arms wrote tests at an unconfirmed seam and not one of them said so; and here a degraded loop reports nothing at all. An invariant whose violation leaves no trace has to be held by construction.

## The rule, in three parts

**1. The assistant turn goes back as the object the provider returned.** No reconstruction, no re-serialisation through our own message type, no dropping of block types the loop does not understand. A block the loop cannot interpret is still a block the provider may require.

**2. The adapter owns the endpoint choice, and it is part of the Pinned Model, not a tuning constant.** `openai:gpt-5.6-terra` means the Responses API. Recording the pin beside the Fingerprint (`L1-6`) records the endpoint with it, because the same model id on the other endpoint is a different set of accepted requests.

**3. Anything that rewrites history inherits this rule.** `L3-IMP-4` compacts the oldest turns when context fills. A compactor that summarises an assistant turn, or rebuilds one, breaks the chain the same way the naive loop does — and does it mid-Attempt, where it is even harder to see.

## Considered options

**Set `reasoning_effort: 'none'` and stay on the default endpoint.** The 400's own second suggestion, and the prototype confirmed it works: tools arrive well-formed, the round trip is accepted. Rejected because it abandons the pin the project owner chose. The point of `medium` is that it is a decision about how much the implementer thinks, and trading it away to avoid an endpoint switch is paying for the wrong thing.

**Reconstruct the turn but strip nothing** — rebuild an assistant message carrying every block. Rejected as a distinction without a difference that costs a maintainer's attention forever: it is the same object with an opportunity to drop a field, and the failure mode it preserves is exactly the invisible one.

**A provider-neutral internal message shape**, converted on the way in and out. Rejected: it is the naive loop with more code. Any neutral shape has to enumerate the block types it carries, and `encrypted_content` is opaque by construction — the first provider-specific block nobody thought to add to the enumeration is dropped silently, which is the failure this ADR exists to prevent.

**Treat the endpoint as an implementation detail of the adapter.** Rejected: it determines which requests are accepted, so two deployments with an identical `pinned_model` in the Run Ledger could behave differently and the Ledger would not say so.

## Consequences

**`L1-1` stands, with the endpoint named.** *"Bind the implementer's toolset and ask for a tool call → the call arrives with a well-formed argument object"* is confirmed on both pins: `paths` arrives as a real list, `env` as a real mapping, `id` present, `invalid_tool_calls` empty, and each provider accepts its own history back. The row needs the amendment that this holds **on the endpoints the adapter pins**, since it is false for the OpenAI pin on `init_chat_model`'s default.

**One tool loop does serve both, on one condition.** The condition is this rule. The loop is otherwise identical across the two pins — same `bind_tools`, same `tool_calls` shape, same `ToolMessage` with `tool_call_id`, same round trip — and that is the answer to `L1-1`'s second half.

**[#25](https://github.com/lbacik/coding-agent/issues/25) inherits a constraint before it starts.** Its first question is what the compaction strategy is, and whether summarisation is used at all. Part 3 above narrows it: a summariser that rewrites assistant turns is not merely a second model call with its own failure mode, it breaks a provider requirement. Eviction of whole turns does not; rewriting them does.

Reversing this decision means letting the loop build its own assistant turns. Any replacement must say how a dropped provider-specific block becomes **visible**, given that the provider accepts the truncated history and the run completes normally.
