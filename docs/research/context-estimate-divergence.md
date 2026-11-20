# How far the local context estimate diverges from provider-reported input tokens

Measurement for [#554](https://github.com/lbacik/coding-agent/issues/554), part of map [#553](https://github.com/lbacik/coding-agent/issues/553).
Run on 2026-11-20 against source baseline `801c47d` (`v0.4.6`). It uses the facts of
[#555](https://github.com/lbacik/coding-agent/issues/555)
([note](https://github.com/lbacik/coding-agent/blob/research/token-counting-and-context-window/docs/research/token-counting-and-context-window.md))
and does not repeat them.

The scripts and the derived numbers (CSV/JSON, no request content) are in
[`context-estimate-divergence/`](context-estimate-divergence/). The raw transcripts are not committed: they are
Attempt evidence with repository content in them.

## What is measured

The estimate is `estimate_context_tokens` (`src/coding_agent/runtime/context_handoff.py`): the UTF-8 bytes of
`json.dumps({"content", "tool_calls", "additional_kwargs"}, sort_keys=True, separators=(",", ":"))` for every
message, plus `json.dumps([tool.args ...])` for the bound tools, integer-divided by 4. The JSON is
`ensure_ascii`, so every newline, quote and non-ASCII character is counted in its escaped form.

The scripts recompute that estimate on each recorded request, rebuilt from the Transcript (`kind: request`
records carry only the messages new since the last assistant turn, so the full list is rebuilt by
accumulation). The rebuilt list matched the recorded `message_count` on all 384 requests.
Two approximations: the Transcript drops `additional_kwargs` (counted as `{}`), and the tool `args` come
from the tool set built by the code at `801c47d`, not from the Worker image that ran the Attempt
(`dump_tools.py`; `run_check`'s schema is written by hand because the test dependencies do not bind it).

## Data

| Route | Model | Source of the provider figure | Requests | Provider input tokens |
| --- | --- | --- | --- | --- |
| Gateway pin (LiteLLM, `responses`, effort `high`) | `muse-spark-1.3-contributor` | recorded `usage.input_tokens`, 5 Attempts of the `agent-battles` instance on rpi5 (`lbacik/battles` issues 106 ×2, 111, 112, 113; 2026-11-18..20) | 319 (one Sub-agent conversation of 4) | 1,564 – 303,508 |
| Direct Anthropic (`messages`, effort `medium`) | `claude-sonnet-5` | recorded `usage.input_tokens`, 3 Attempts in `lbacik/coding-agent-sandbox` issue 44 (2026-11-14..17) | 65 | 3,252 – 9,487 |
| Direct Anthropic, long conversations | `claude-sonnet-5` | `messages.count_tokens` on the gateway requests, replayed | 69 sampled (every 5th + the last of each Attempt) | 3,034 – 424,496 |
| Direct OpenAI, long conversations | `gpt-5.6-luna` | `POST /v1/responses/input_tokens` on the same replayed requests | 69 | 1,495 – 264,179 |

No direct-OpenAI Attempt archive exists, and the sandbox Anthropic Attempts are short, so the long-conversation
figures for both direct providers come from replaying the gateway requests through the count endpoints
(`replay.py`, mode `neutral`: assistant turns reduced to their text and tool calls, the model-specific reasoning
items dropped, because Meta's encrypted reasoning cannot be sent to another provider). The estimate is
recomputed on the replayed list, so each ratio compares like with like.

**`count_tokens` is a trustworthy stand-in for the recorded Anthropic usage.** Replaying the 47 sandbox
requests with their original content blocks (thinking signatures included, `thinking: adaptive`,
`replay.py` mode `faithful`) gave `count_tokens = recorded input_tokens + 80` for every request bound to 15
tools and `+ 39` for every request bound to 10 tools: a constant offset per tool set, zero spread. Anthropic
documents the count as an estimate (#555); on these requests it is exact up to that constant.

## Results: estimate / provider input tokens

A ratio below 1 means the estimate is **under** the real figure (the handoff fires late); above 1, **over**
(the handoff fires early).

| Context size (provider tokens) | Gateway (Meta), recorded | Anthropic, recorded | Anthropic, count_tokens | OpenAI, input_tokens |
| --- | --- | --- | --- | --- |
| < 10K | 0.825 median (0.336 – 1.102), n=24 | 0.671 median (0.371 – 1.089), n=65 | 0.402 (0.380 – 0.426), n=6 | 0.857 (0.814 – 0.872), n=6 |
| 50K – 100K | 1.031 (0.959 – 1.194), n=51 | – | 0.610 (0.607 – 0.717), n=6 | 0.960 (0.891 – 1.193), n=11 |
| 100K – 200K | 1.125 (1.039 – 1.195), n=150 | – | 0.565 (0.545 – 0.676), n=10 | 1.025 (0.876 – 1.069), n=39 |
| 200K – 400K+ | 1.210 (1.091 – 1.253), n=93 | – | 0.644 (0.577 – 0.665), n=41 | 0.948 (0.943 – 1.027), n=12 |
| All | 1.123 (0.336 – 1.253), n=319 | 0.671 (0.371 – 1.089), n=65 | 0.617 (0.380 – 0.717), n=69 | 0.977 (0.814 – 1.193), n=69 |

Worst cases:

- **Anthropic, under**: the last request of `battles#106/2` is 424,496 tokens by `count_tokens` and 250,627 by
  the estimate, 173,869 tokens (41%) under. Every long Anthropic conversation ends 90K – 174K under.
  The worst ratio, 0.380, is a first request (3,355 vs 1,276).
- **Gateway, over**: the last request of `battles#106/2` is 303,508 reported and 373,236 estimated,
  69,728 tokens (23%) over. The worst under-estimate is the Sub-agent's first request, 0.336 (1,564 vs 525).
- **OpenAI**: 0.88 – 1.07 above 100K; the largest gap is 13,892 tokens under (`battles#106/2`).

## Request makeup and where the error comes from

Bytes per token by content type, counted on the recorded content of `battles` 106/2, 111/1 and 113/1 (each
category capped at 120 KB, counted as one user message minus a one-character baseline; `categories.py`):

| Content | Raw bytes/token, Anthropic | Raw bytes/token, OpenAI | Estimate / tokens, Anthropic | Estimate / tokens, OpenAI |
| --- | --- | --- | --- | --- |
| Code (`read_file`, `read_lines`, `read_change`, `read_result_slice` results) | 2.43 | 3.79 | 0.62 | 0.97 |
| Command and test output (`run_command`, `test_targeted`, `run_check`) | 1.81 | 2.91 | 0.47 | 0.76 |
| Search and listing results | 2.19 | 3.56 | 0.56 | 0.91 |
| Pinned Prefix prose (instructions, skills, input) | 2.78 | 4.11 | 0.71 | 1.05 |
| Assistant text | 3.31 | 5.64 | 0.87 | 1.48 |
| Tool-call arguments (JSON) | 2.15 | 3.47 | 0.56 | 0.91 |
| Tool schemas (JSON) | 2.68 | 4.45 | 0.75 | 1.25 |

For the gateway (no tokenizer reachable), a non-negative least-squares fit of the recorded usage on the
byte counts per category (`regress.py`, median fit error 1.1%) gives about 4.4 JSON bytes/token for code,
3.3 for command output, 5.9 for tool calls, and **13.6 for Meta's encrypted reasoning items**.

Makeup of the largest request of each long Attempt (share of estimate bytes): code results 37 – 78%,
reasoning items 9 – 28%, command output 3 – 25%, tool calls 3 – 10%; the Pinned Prefix and tool schemas are
under 1% at that size. In the short sandbox Attempts the prefix is 60 – 80% of the bytes.

The divergence has four separate causes:

1. **Tokenizer density differs by provider, by a factor of about 1.6.** `claude-sonnet-5` packs 1.8 – 2.8 raw
   bytes into a token on this content; `gpt-5.6-luna` 2.9 – 4.1. `bytes/4` is close to OpenAI on code and
   prose, and 13 – 53% under Anthropic on every content type (38% on code). JSON escaping adds 2 – 5% bytes (more for command output
   with many newlines and non-ASCII characters), which pushes the estimate up, not down.
2. **A fixed part the estimate never sees.** The estimate counts the tools' argument schemas only. The 14-tool
   main step set is 432 estimated tokens; providers charge 2,284 (Anthropic, including its tool-use system
   prompt) and 797 (OpenAI) for the names, descriptions and schemas (`tool_overhead.py`). On the first request
   of an Attempt this is most of the error: Anthropic 1.2 – 1.3K estimated vs 3.25 – 3.4K reported, gateway
   1.3 – 1.8K vs 2.6 – 3.2K.
3. **Opaque reasoning items are counted as bytes.** The Responses route carries each turn's encrypted
   reasoning (`include: reasoning.encrypted_content`) back in the history. Those blobs are 9 – 28% of the bytes
   of a long gateway request but cost under a third of what `bytes/4` charges for them. They are why the
   gateway estimate turns from under to over as the conversation grows: with the reasoning bytes removed, the
   gateway ratio in the 200K+ band is 0.890 (0.863 – 1.041) instead of 1.210. Anthropic thinking signatures
   behave the same way in the sandbox Attempts (fit: about 20 bytes/token).
4. **Content mix.** Command output tokenizes densest on both direct providers, assistant text sparsest, so
   two conversations of the same size can differ by 10 – 20% in their ratio (OpenAI's 0.81 – 1.19 spread).

## How the error grows with conversation length

- **Anthropic: proportional, so the absolute error grows linearly.** Past the first few requests the ratio
  settles at 0.55 – 0.72; the under-count is about 2K tokens at 3K and 90K – 174K at 250K – 425K.
- **OpenAI: small and roughly stable.** Under by 13 – 19% at the start (the tool overhead), then band medians of
  0.95 – 1.03 with single requests from 0.88 to 1.19; absolute error up to about 14K.
- **Gateway (Meta): the sign changes.** Under at the start (tool overhead), about even at 50K – 100K, then over,
  growing with the share of reasoning items: +18K to +70K above 200K.

## An anchored measure

Every response already reports `input_tokens` for the request it answered (normalised by `langchain-anthropic`
to the whole prompt, cache included; #555). Using the previous request's reported `input_tokens` plus the
`bytes/4` estimate of only what was added since (the response and the tool results after it) leaves the
estimate's error on the delta only (`anchored.py`):

| Route | Requests | Relative error, median | p5 – p95 | Worst | Median absolute error |
| --- | --- | --- | --- | --- | --- |
| Gateway, recorded | 313 | +0.24% | −0.28% – +8.86% | +30.9% (+11,251 tokens, one large addition) | 398 tokens |
| Anthropic, recorded | 57 | +3.29% | −0.47% – +5.87% | +9.2% (+731 tokens) | 219 tokens |

The first request of a conversation has no anchor; there the count endpoint (Anthropic `count_tokens`,
OpenAI and LiteLLM `responses/input_tokens`, #555) or a measured per-pin constant would be needed.

## Consequences for the current defaults (facts, not a decision)

- The code-owned Anthropic threshold is 50,000 with a usable threshold of 48,000 estimated tokens
  (`src/coding_agent/provider/config.py`). At the measured ratio of 0.55 – 0.65 that is about 74K – 87K real
  input tokens when the handoff fires, not 48K.
- The `battles` gateway pin has `context-threshold-tokens: 400000`. Its largest request was 303,508 reported
  and 373,236 estimated: the estimate was 23% closer to the threshold than the real context.
- The dashboard shows the provider-measured figure "of window" = threshold (#553 Notes), so on Anthropic it
  can read above 100% before a handoff fires, and on the gateway it reads below what the handoff compares.

## Reproduce

```sh
uv run python docs/research/context-estimate-divergence/scripts/dump_tools.py tools.json   # from the repo root
python3 scripts/analyse.py tools.json gateway.csv LABEL=path/to/transcript.jsonl ...
python3 scripts/summary.py gateway.csv
uv run --no-project --with numpy --with scipy python scripts/regress.py gateway.csv anthropic.csv
python3 scripts/anchored.py gateway.csv anthropic.csv
# count endpoints; need ANTHROPIC_API_KEY / OPENAI_API_KEY; run from the repo root with `uv run python`
scripts/replay.py faithful anthropic 1 out.csv tools.json LABEL=transcript.jsonl ...
scripts/run_replays.sh <dir holding data/rpi5/lbacik/battles and scripts/>
scripts/categories.py categories.json tools.json LABEL=transcript.jsonl ...
scripts/tool_overhead.py tools.json
```

Only the free count endpoints were called (Anthropic `messages/count_tokens`, OpenAI
`responses/input_tokens`); no model request was made.
