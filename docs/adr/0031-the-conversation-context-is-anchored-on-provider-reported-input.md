---
status: accepted
---

# The Conversation Context is anchored on provider-reported input

Decided in [issue #573](https://github.com/lbacik/coding-agent/issues/573), slicing [the token-accounting design](https://github.com/lbacik/coding-agent/issues/553) per [issue #569](https://github.com/lbacik/coding-agent/issues/569), under the authorities [how the Conversation Context is measured](https://github.com/lbacik/coding-agent/issues/556#issuecomment-7038756055) and [what the token limits rest on](https://github.com/lbacik/coding-agent/issues/559#issuecomment-7040147612) (decisions 3 and 4). This supersedes [ADR 0013](./0013-context-window-handoff.md)'s "never substituted" sentence: provider-reported `input_tokens` now anchors the Conversation Context, and every context decision and Admission hold compares that anchored measure.

The local `bytes/4` estimate of the whole request was 36–44% under the true context on `claude-sonnet-5` (and up to 21% over on the gateway pin), so the handoff fired late on the direct pin: the same code-owned thresholds moved the handoff from about 74K–87K real tokens to about 48K. The provider already reports the input size of the request it just served; anchoring on that number keeps the control signal deterministic — one pure function of the ledger-visible usage and the messages appended since — while closing most of the gap. Pre-send provider token counting was considered and rejected: the anchor is observed, never requested.

## Decision

One function measures the Conversation Context of the next request, and every context decision and Admission hold uses it — the main conversation's handoff decision, the Sub-agent context guard, the Pinned Prefix refusal, the handoff turn's check, Admission's hold and the spawn group's admission.

- **Measured.** The Conversation Context of the next request is the `usage_metadata.input_tokens` of the most recent request of the same conversation that reported usage, plus `bytes/4` of every message appended since — including the assistant turn that request produced and its tool results. `output_tokens` is never added. Basis `anchored`.
- **A response without usage does not reset the anchor.** The previous anchored value stands; only a response with complete usage metadata moves it.
- **Estimated.** With no measured response yet in the conversation — its opening, the first request after a Context-Window Handoff, a Sub-agent's first request — the measure is today's `bytes/4` of the whole request. Basis `estimated`, with no per-provider calibration.
- **A conversation is the main conversation between handoffs, or one Sub-agent.** Each holds its own anchoring state; a handoff or a new Step Execution starts a fresh state, and a Sub-agent is anchored on its own responses only.
- **The Usable Threshold is unchanged:** Handoff Threshold minus safety margin minus request overhead, for both bases.
- **Admission stays the strict worst case.** The whole Conversation Context is priced as uncached input — a gateway pin at max(`input`, `cache-write`) — plus the output cap, in both the Effective Tokens and the cost meter. No cache discount. Only the input value changed, from the whole-request estimate to this measure; the bound's shape is untouched.
- **The Run Ledger records what was compared.** Every `model_response` event (main conversation, Sub-agents, the handoff turn) carries the compared Conversation Context — value, basis, and for an anchored value the anchor's `input_tokens` with its request reference and the appended estimate — and the Usable Threshold in force. The `handoff` event and a Sub-agent's `no-report` / `context-window` ending carry the value (and basis) that crossed the threshold. Records written before these fields travelled read `unavailable`, never zero.
- **The operator snapshot is untouched here.** `native.context_estimate_tokens` keeps carrying exactly the value the loop compared — now this measure — with its basis; no other operator figure changes (operator contract 1.1 is a separate slice).

## Consequences

- The handoff fires at the same code-owned thresholds but at a truer context size; the thresholds, windows and output caps themselves are retuned by the follow-up Workflow schema v4 slice, and no release is tagged between the two.
- `bytes/4` of the whole request survives only as the `estimated`-basis fallback and as the appended-since-anchor delta; no context decision or Admission hold computes it directly any more.
- A provider that misreports `input_tokens` moves the handoff, as it already moves every meter; the anchor is ledger-visible per request, so a misreport is auditable.
