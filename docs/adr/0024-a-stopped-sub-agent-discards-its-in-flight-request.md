---
status: accepted
---

# A stopped Sub-agent discards its in-flight request at the next stop point

A Sub-agent that observes the stop signal (SIGTERM, or a sibling Sub-agent's
provider failure) ends `no-report` / `stopped` at its next stop point — after
the current model response or tool result — even when a provider request is
already in flight. That request is not cancelled mid-flight: it runs to the
provider's own timeout and its result is discarded. Decided in
[Sub-agent interruption accounting](https://github.com/lbacik/coding-agent/issues/220),
which asked for the in-flight request to be "abandoned within
`shutdown_grace_seconds`" and instructs the implementation to record an ADR
rather than diverge silently where a decision does not hold.

## Context

Contract §11 asks a running Sub-agent to observe the stop signal after every
model response and every tool result, and to have its in-flight request
"abandoned within `shutdown_grace_seconds`". The caller loop (§11, P-CAN-3)
already defines what abandonment means in this runtime: no new model call
starts after the signal is observed, while a provider request already inside
its call runs to the provider's own timeout and its result is discarded at the
next stop point. Only the runtime's own commands are cancelled mid-flight, by
killing their process group (REC-SIG-1). The provider interface
(`invoke_with_retry` over one bound model) exposes no cancellation handle, so
there is nothing to cancel a blocked provider call with — for the caller or
for a Sub-agent.

## Decision

- **Stop points, not cancellation.** `run_subagent` polls `is_stopped` after
  every model response, after every tool result, at the loop top, and before
  accepting a report (the stop takes precedence over a ready report). A
  provider call blocked inside `invoke_with_retry` is never interrupted; its
  response is metered through `on_usage` and then discarded at the next stop
  point. This is the same shape as the caller loop, deliberately.
- **`shutdown_grace_seconds` bounds the runtime's own work, not the
  provider's.** The queued Sub-agents never start, the running ones stop at
  their next stop point, and the caller then takes the claim-confirmed
  interrupted path — all within the grace window. A provider call already in
  flight may outlast it; that overrun is the provider's timeout, not runtime
  work, and its spend is still metered.
- **No silence.** The `run_subagent` docstring states the discard shape, and
  contract §11 points here, so the "abandoned" wording is read with this
  meaning.

## Considered options

**Threading a real cancellation through the provider calls.** Rejected: the
pinned provider interface has no abort handle, and killing provider HTTP
calls from another thread would trade a documented, metered discard for
unmetered, half-read responses. The stop points already fire after every
response, so a cooperative provider answers promptly anyway.

**Blocking the group until the in-flight call returns before ending
`stopped`.** Rejected: that is what the code does for the in-flight call
itself, but the Sub-agent must still end `stopped` rather than continue its
loop once the response lands — otherwise a chatty provider could keep a
stopped Sub-agent alive indefinitely.

## Consequences

- **A pathological provider timeout can outlast the grace window** for one
  in-flight call per running Sub-agent. The group still settles: no new calls
  start, and every Sub-agent ends `stopped` (or `provider-failure` for the
  call that exhausted its retries).
- **Spend from the discarded response stays metered.** The response that
  precedes a stop is flushed to the Attempt-wide meters before the end event,
  so the recorded budgets already contain it.
- **Reversing this** means giving the provider interface a cancellation
  handle and defining what a half-cancelled response meters — a provider-layer
  change, not a runtime-loop change.
