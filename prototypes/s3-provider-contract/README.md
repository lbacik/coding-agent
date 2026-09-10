# THROWAWAY prototype — the provider contract through `init_chat_model`

This directory answers [issue #24](https://github.com/lbacik/coding-agent/issues/24)
and nothing else. It is **not** the S3 provider adapter, it writes no
production code, and it adds nothing to the graph. It lives only on the branch
`prototype/s3-provider-contract`; `main` keeps the decision, not the harness.

It is the third of this map's prototypes, after
[`prototypes/s4-review-fanout`](https://github.com/lbacik/coding-agent/tree/prototype/s4-review-fanout/prototypes/s4-review-fanout)
and [`prototypes/s3-implement-tdd`](https://github.com/lbacik/coding-agent/tree/prototype/s3-implement-tdd/prototypes/s3-implement-tdd),
and the first to spend money on the provider layer itself.

## The question

`L1-1…6` are the rows S3 owes at the provider layer, and not one of them had
been run against either provider. Do both pins pass the runtime contract
through `init_chat_model`, and what can a preflight capability assertion
actually assert?

## The two pins

| | Model | How it spells "medium effort" |
| --- | --- | --- |
| **anthropic** | `claude-sonnet-5` | `thinking: {type: adaptive}` + `output_config: {effort: medium}` |
| **openai** | `gpt-5.6-terra` | `reasoning_effort: medium`, **and `use_responses_api=True`** |

Both chosen by the project owner. Sonnet 5 rejects
`thinking: {type: enabled, budget_tokens: N}` with a 400 that says so, and the
OpenAI pin does not run at all on `init_chat_model`'s default endpoint — see
`L1-1` below.

## Everything is recorded, nothing is asserted

A probe that "fails" here is a finding, not a broken test. The ticket asks for
`L1-1…6` either confirmed as assertable exactly as written or named with the
amendment they need, and a guess dressed as a pass would defeat that. So each
probe writes what it saw — exception class, status, headers of interest,
response body, full token detail — and the reading happens in the resolution.

## The probes

| File | Rows | What it does |
| --- | --- | --- |
| `providers.py` | — | The two pins, and the one place that knows each provider's spelling |
| `probe.py` | `L1-1…5` | Six probes at L1's own definition of the layer: real API, smallest possible prompts |
| `implement.py` | `L1-6` | One small real implementation task per provider, through our own bounded tool loop |
| `cost.py` | `L1-3` | The arithmetic on what the others recorded. Spends nothing |

`probe.py`'s six, and why each earns its place:

- **`param_channel`** — a pin that is *silently dropped* is worse than one
  refused: the run looks correct and is not the run the Attempt recorded.
  Sending an effort value the provider must reject proves the channel without
  paying for a completion.
- **`tool_call`** (`L1-1`) — binds the implementer's toolset, asks for a call
  with a list and a mapping in one argument object, then **completes the
  round trip**. The round trip is the half that matters: getting a well-formed
  `tool_calls` entry says the provider can speak, but feeding it back and
  having the provider accept its own history is what says one tool loop serves
  both.
- **`structured_output`** (`L1-2`) — both `json_schema` and `function_calling`,
  against a schema shaped like §6's rate-limit table, the closest thing the
  contract has to a classification.
- **`usage_and_cost`** (`L1-3`) — the same long prefix twice, to see which
  token buckets move on a cache hit.
- **`error_surface`** (`L1-5`) — the errors that *can* be summoned: unknown
  model, broken credential, invalid request. `overloaded` cannot be summoned
  on demand and the probe says so rather than faking it.
- **`capability_surface`** (`L1-4`) — both routes to the same question: what
  the provider *declares* at `GET /v1/models/{id}`, and what *attempting*
  establishes, the latter run twice (with the pin's effort knobs and without),
  because the two routes disagree.

## Running it

```bash
./run.sh                                  # every probe, both pins
./run.sh --only tool_call                 # one probe
./run.sh --provider openai                # one pin
SCRIPT=implement.py ./run.sh              # L1-6
python3 cost.py                           # the arithmetic, free
```

Keys come from the repository `.env` if they are not already in the
environment. `transcripts/` holds what the runs recorded.

## What it found

The full reading is the [resolution comment on #24](https://github.com/lbacik/coding-agent/issues/24);
the short version, row by row:

- **`L1-1` — confirmed, with an amendment about the endpoint.** The argument
  object arrives well-formed on both (`paths` a real list, `env` a real
  mapping, `id` present, no `invalid_tool_calls`) and both accept their own
  history back. But **not through `init_chat_model`'s defaults**: the OpenAI
  pin returns `400 Function tools with reasoning_effort are not supported for
  gpt-5.6-terra in /v1/chat/completions`. Tools and a pinned effort are both
  non-negotiable for a work node, so the Responses API is the only shape that
  runs — and it makes the assistant turn carry a `reasoning` block with
  `encrypted_content` the loop must carry back. A loop that reconstructs the
  assistant turn from text plus `tool_calls` drops it.
- **`L1-2` — retired.** The mechanism works on both, but the contract has no
  model-driven classifier for it to serve: every classification §4 specifies
  is deterministic. Also worth keeping: `function_calling` structured output
  warns that it is not guaranteed while `thinking` is enabled, so `json_schema`
  is the method if a node ever needs one.
- **`L1-3` — confirmed on tokens, amended on cost.** Tokens are present and
  non-zero on both. Cost is **not** derivable from `usage_metadata`, which
  reports counts and never money, and the naive formula is wrong in both
  directions: `input_tokens` is the *whole* prompt including cache hits, so a
  cache read is over-charged **9.7x**, and an Anthropic cache write lands in
  `ephemeral_5m_input_tokens` while the normalised `cache_creation` stays zero,
  so it is under-charged at **0.8x**. `cost.py` fell into that second trap on
  its first run.
- **`L1-4` — the providers are asymmetric.** Anthropic declares a machine-
  readable capability tree; OpenAI declares four fields and nothing about
  capability. Neither declares tool calling. On OpenAI, "lacks a capability"
  and "does not exist" arrive as the *same* exception class, and the status
  even changes with the endpoint (404 on chat completions, 400 on Responses).
- **`L1-5` — not assertable as a run.** 401/400/404 all carry a clean,
  classifiable surface. `overloaded` is not summonable. And langchain surfaces
  **no response headers at all on a successful call**, so §10's rate-limit
  headroom check has no provider-side data to read.
- **`L1-6` — passes on both.** pytest exit 0 on an independent re-run, not the
  model's account of it; no model swap across 4 and 5 responses; the pinned
  model recorded beside the Fingerprint. $0.0103 for the Anthropic Attempt.

One thing found along the way that belongs to
[#25](https://github.com/lbacik/coding-agent/issues/25) rather than here: the
`L1-6` loops took **zero** cache hits, because no cache breakpoint was set, so
the re-sent history billed at the full input rate every turn. That is the
number #25's question 2 was waiting on — pinning material by re-sending it
verbatim costs full rate without a breakpoint, and about a tenth of it with
one.
