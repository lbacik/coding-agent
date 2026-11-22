---
status: accepted
---

# Token limits are declared per pin in the Workflow Configuration

Decided in [issue #574](https://github.com/lbacik/coding-agent/issues/574), slicing [the token-accounting design](https://github.com/lbacik/coding-agent/issues/553) per [issue #569](https://github.com/lbacik/coding-agent/issues/569), under the authorities [where the Model Context Window comes from](https://github.com/lbacik/coding-agent/issues/557#issuecomment-7039063339) and [what the token limits rest on](https://github.com/lbacik/coding-agent/issues/559#issuecomment-7040147612) (decision 2). This amends decisions 5 and 6 of [issue #324](https://github.com/lbacik/coding-agent/issues/324): gateway pins are no longer the only pins that declare their token budget, and the context window and output cap for direct pins no longer stay in code.

No release is tagged between this slice and the anchoring slice ([ADR 0031](./0031-the-conversation-context-is-anchored-on-provider-reported-input.md)): the recommended thresholds below assume the anchored Conversation Context measure.

## Decision

- **Every pin declares all three token keys.** Each `models.<id>` entry — direct or gateway — carries `context-window-tokens`, `context-threshold-tokens` and `output-cap-tokens`, all required positive integers, like `prices-usd-per-mtok`. The Model Context Window is the window a request and its response share.
- **Two static rules, one error shape, every pin, offline.** Validation refuses `context-threshold-tokens + output-cap-tokens > context-window-tokens`, and it applies the old gateway-only floor — the threshold must exceed the safety margin plus request overhead — to every pin. Both are checked at `agent config validate` and at startup before any network call, with the same fault shape whatever the pin kind.
- **No code-owned budget tables.** `DEFAULT_CONTEXT_WINDOWS`, `DEFAULT_OUTPUT_CAPS` and `DEFAULT_PRICE_TABLE` are removed. The only values that stay code-owned are the two mechanism constants — safety margin (1,000) and request overhead (1,000) — because they describe the runtime's own framing, not any model's budget.
- **The Workflow Configuration is the only prices authority, and `provider-check` reads it.** `agent provider-check --config <file> <pin>` loads the file, reads the pin's prices and output cap from it, and sends its probe with `max_tokens` set to that output cap — so an output cap above the model's maximum is refused there, by attempting, not by reading a declaration ([ADR 0009](./0009-provider-capability-is-established-by-attempting.md)).
- **The token meter's key is `effective-tokens`.** `tokens` becomes `effective-tokens` in the Workflow `limits` (Attempt Ceiling) and in step `limits`; a v4 file naming `tokens` is refused as an unknown key. Enforcement is unchanged: the meter still counts Effective Tokens, cache reads excluded.
- **The Pinned Model record carries the window.** The Effective Configuration's `pinned_model` section records `model_context_window_tokens` beside `output_cap_tokens` and `context_threshold_tokens`, so the window is in the digest. The record version is bumped (§16.4): a new Worker refuses an unfinished Attempt written by the older release, and the operator drains with the compatible release first.
- **`version: 3` is refused with a migration diagnostic.** Exit 1, before any install, network call, Claim or GitHub write. The diagnostic names exactly what to change: `version: 4`; the three token keys on every pin (direct pins newly); `tokens` → `effective-tokens`. There is no converter and no side-by-side reading. `version: 2` keeps its own refusal.

## Why per pin, not per table

A code-owned table answers the wrong question. It says what the vendor allows; the operator needs to say what the Attempt may spend. The gateway path already proved the declared form: the pin that names the budget is the pin the digest covers, the probe asserts and the meters enforce. Extending that form to direct pins removes the special case — one rule, one error shape, every pin — and removes the table the special case needed. The recommended values stay recommendations, documented in the operator guide, not defaults the code silently applies:

- `claude-sonnet-5`: window 1,000,000, threshold 80,000, output cap 32,000;
- `gpt-5.6-luna`: window 1,050,000, threshold 100,000, output cap 32,000.

The threshold counts provider-measured tokens since the anchored measure, so 80,000 on `claude-sonnet-5` keeps roughly the old real crossing point.

## Consequences

- [Issue #324](https://github.com/lbacik/coding-agent/issues/324) decisions 5 (gateway-only token keys; direct pins must not name them) and 6 (windows stay in code) are superseded by this record.
- The generated and committed JSON Schema is v4; `examples/` are v4; existing v3 fixtures are ported to v4 and dedicated v3-refusal tests keep the refusal pinned.
- The next slice (the static floor for limits) builds on the declared keys; modelling a separate input ceiling or a model's maximum output is explicitly out of scope here.
