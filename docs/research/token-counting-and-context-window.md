# Token counting and the Model Context Window: providers and the Model Gateway

Research for [#555](https://github.com/lbacik/coding-agent/issues/555), part of map [#553](https://github.com/lbacik/coding-agent/issues/553).
Read on 2026-11-20 against source baseline `801c47d` (`v0.4.6`).

It builds on [#319](https://github.com/lbacik/coding-agent/issues/319)
([note](https://github.com/lbacik/coding-agent/blob/research/litellm-model-identity/docs/research/2026-11-12-issue-319-litellm-model-identity.md)),
which covers what LiteLLM `/model/info` reports and where those values come from. This note does not repeat that work.

No paid or live API calls were made. Every claim cites a documentation URL (as served on 2026-11-20) or a versioned source file.
Claims marked **unverified** could not be confirmed from a primary source.

## Pins and versions in scope

| What | Value | Source |
| --- | --- | --- |
| Direct Anthropic pin | `anthropic` / `claude-sonnet-5`, endpoint `messages` | `src/coding_agent/provider/config.py` `PINNED_MODELS` |
| Direct OpenAI pin | `openai` / `gpt-5.6-luna`, endpoint `responses` | same |
| Gateway pins | `ChatOpenAI` against the LiteLLM Proxy Responses path (`use_responses_api=True`, `store=False`, `extra_body={"disable_fallbacks": True, "num_retries": 0}`) | `src/coding_agent/provider/pinned_model.py` (`build` path for `pin.gateway_url`) |
| Current Conversation Context estimate | `len(text.encode("utf-8")) // 4` | `src/coding_agent/runtime/context_handoff.py` `estimate_tokens` |
| `anthropic` | 1.5.0 | `uv.lock`, `.venv` |
| `openai` | 3.13.0 | `uv.lock`, `.venv` |
| `langchain-anthropic` | 1.7.12 | `uv.lock`, `.venv` |
| `langchain-openai` | 1.8.5 | `uv.lock`, `.venv` |
| `langchain-core` | 1.8.4 | `uv.lock` |
| `tiktoken` | 0.14.0 | `uv.lock`, `.venv` |
| LiteLLM | 1.109.1 (tag `v1.109.1`, the version #319 pinned) | `raw.githubusercontent.com/BerriAI/litellm/v1.109.1/...` |

Source paths below are relative to `.venv/lib/python3.13/site-packages/` for installed packages, and to the LiteLLM repository at tag `v1.109.1`.

---

## 1. Counting tokens before sending

### 1a. Anthropic: `POST /v1/messages/count_tokens`

Source: <https://platform.claude.com/docs/en/build-with-claude/token-counting>

- **Input shape.** "The token counting endpoint accepts the same structured list of inputs for creating a message, including support for system prompts, tools, images, and PDFs." The response is `{"input_tokens": N}`.
- **Thinking.** It accepts a `thinking` config. "Thinking blocks from **previous** assistant turns count toward your input tokens on models that keep all prior turns". `claude-sonnet-5` is one of those models, since the context-windows page lists "Claude Sonnet 4.6 and later Sonnet models" as keeping prior thinking blocks.
- **Inputs it rejects.** It returns `invalid_request_error` for server tools (except the advisor tool), the MCP connector, and `image`/`document` blocks with a `url` or `file` source. The repo's runtime uses client tools only, so none of these applies.
- **Accuracy.** "The token count is an **estimate**. In some cases, the actual number of input tokens used when creating a message might differ by a small amount." Counts "might include tokens added automatically by Anthropic for system optimizations. **You are not billed for system-added tokens**."
- **Caching.** "token counting provides an estimate without using caching logic". `cache_control` is accepted but has no effect.
- **Cost.** "Token counting is **free to use**".
- **Rate limits.** Requests per minute by usage tier: Start 5,000, Build 10,000, Scale 20,000. "Token counting and message creation have separate and independent rate limits."
- **Models.** "All active models support token counting"; `claude-sonnet-5` is in the page's `supportedModels`. The count uses the tokenizer of the `model` passed, so it must be the pinned model.
- **SDK.** `anthropic` 1.5.0 has `client.messages.count_tokens(...)` (`anthropic/resources/messages/messages.py:1243`).
- **LangChain.** `langchain-anthropic` 1.7.12 `ChatAnthropic.get_num_tokens_from_messages(messages, tools=...)` calls this endpoint (`langchain_anthropic/chat_models.py:3123-3219`).
  - It formats `system` and `messages` with the same `_format_messages` it uses to send a request.
  - It converts tools with `convert_to_anthropic_tool`, dropping the same schemas `bind_tools` drops ("so counting tokens and sending a request agree on which tools the API will accept").
  - It forwards `context_management` and `betas`.
  - It does **not** forward `thinking` on its own. A caller has to pass it through `**kwargs` if the count should match a request with thinking set.

### 1b. OpenAI: `POST /v1/responses/input_tokens`

Sources: <https://developers.openai.com/api/docs/guides/token-counting>, <https://developers.openai.com/api/reference/resources/responses/subresources/input_tokens/methods/count>, `openai` 3.13.0 `openai/resources/responses/input_tokens.py`.

- **Input shape.** The guide: "Pass text, messages, images, files, tools, or conversations". "Use the same payload you would send to `responses.create` and get an accurate count." The guide also says "the API returns the exact count the model will receive".
- **Parameters.** The SDK 3.13.0 `client.responses.input_tokens.count(...)` accepts `model`, `input`, `instructions` (the system/developer message), `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning`, `text`, `truncation`, `conversation` and `previous_response_id` (`input_tokens.py:46-62`). The response is `{"object": "response.input_tokens", "input_tokens": N}`.
- **Instructions and tools.** Both are counted the way the real request counts them.
- **Local tokenizers.** "Local tokenizers like tiktoken work for plain text, but they have limitations". Character heuristics such as "`characters / 4` are inaccurate" for images and files. The guide's summary says local estimates miss the formatting tokens that encode message structure.
- **Cost.** **Unverified.** The developers.openai.com pages say nothing about price or rate limits for this endpoint. A search snippet quotes the OpenAI Help Center ("Understanding and counting tokens") saying that counting is free and does not use inference quota. The page answered HTTP 403 to this agent, so the claim could not be read at the source.
- **Rate limits.** **Unverified.** No per-endpoint limit is published on the pages read.
- **Tokenizer for `gpt-5.6-luna`.** OpenAI does not publish it.
  - `tiktoken` 0.14.0 maps any `gpt-5*` name to `o200k_base` by prefix (`tiktoken/model.py:12`, `"gpt-5": "o200k_base"`). The library notes that prefix matching "can match on non-existent models".
  - Whether `o200k_base` is the real tokenizer for `gpt-5.6-luna` is **unverified**.
- **LangChain.** `langchain-openai` 1.8.5 `ChatOpenAI.get_num_tokens_from_messages` never calls this endpoint. It is a local tiktoken approximation (`langchain_openai/chat_models/base.py:2458-2581`):
  - it warns "Counting tokens in tool schemas is not yet supported. Ignoring tools.";
  - it uses Chat Completions framing constants ("tokens_per_message = 3");
  - it counts function-call tokens as "an approximation";
  - it ignores file inputs.
- **Consequence.** To get the provider count from the OpenAI pin (direct or gateway), the runtime would call the `openai` SDK's `responses.input_tokens.count` itself, for example through the client `ChatOpenAI` already holds. There is no LangChain method for it.

### 1c. LiteLLM Proxy (the Model Gateway)

Sources: <https://docs.litellm.ai/docs/count_tokens>; LiteLLM `v1.109.1` source:

- `litellm/proxy/response_api_endpoints/endpoints.py` (`responses_input_tokens`, lines 1724-1853);
- `litellm/proxy/anthropic_endpoints/endpoints.py` (`/v1/messages/count_tokens`, line 348);
- `litellm/proxy/proxy_server.py` (`/utils/token_counter` at 15436, `count_request_tokens`, `_count_request_tokens`, `_provider_token_count`, `_get_provider_token_counter`);
- `litellm/llms/openai/responses/count_tokens/token_counter.py`;
- `litellm/llms/anthropic/count_tokens/token_counter.py`;
- `litellm/types/token_count.py`.

**Routes.** The proxy exposes four counting routes:

| Route | Shape | Counts with the provider? |
| --- | --- | --- |
| `POST /v1/responses/input_tokens` (also `/responses/input_tokens`, `/openai/v1/responses/input_tokens`) | OpenAI Responses count request and response | Always tries (`call_endpoint=True`, `endpoints.py:1840`) |
| `POST /v1/messages/count_tokens` | Anthropic count request and response | Always tries |
| `POST /utils/token_counter` | LiteLLM `TokenCountRequest {model, prompt, messages, contents, tools, system}` | Only with `?call_endpoint=true`. Without it, it counts locally. |
| Google `:countTokens` | Gemini shape | n/a here |

`/v1/responses/input_tokens` is on the same base path the gateway pins already call, and it accepts the same body. That makes it the natural gateway counterpart of the OpenAI endpoint.

**How the Responses route counts** (`endpoints.py:1764-1853`, `proxy_server.py:_count_request_tokens`):

- It requires `model` and `input`.
- It counts `instructions` (merged into the counted messages by `_responses_input_as_token_count_messages`) and `tools` (`resolve_counted_tool_fields`).
- It routes the alias to a deployment with the router, the same way inference does.
- The deployment's provider then decides the method:
  - **`openai` deployment.** `OpenAITokenCounter` forwards the Responses request natively to the upstream `/responses/input_tokens` (`_native_responses_count`, `token_counter.py:253`). It applies only when `custom_llm_provider == "openai"` (`token_counter.py:133`).
  - **`anthropic` deployment (a non-native upstream for a Responses client).** The Responses request is bridged to chat form (`chat_count_request`), then `AnthropicTokenCounter` calls Anthropic `count_tokens`. It applies only when `custom_llm_provider == "anthropic"` (`anthropic/count_tokens/token_counter.py:38`).
  - **Other providers with a count API.** The docs list "OpenAI, Anthropic, Mistral, Vertex AI (Claude and Gemini), Bedrock (Claude), Gemini, and Azure AI (Claude)". These use their own counter through `ProviderConfigManager.get_provider_model_info(...).get_token_counter()`.
  - **Providers with no count API.** "Local tiktoken count, or the Hugging Face tokenizer of the model if you set one" (docs). In source, `_local_token_estimate` calls `litellm.token_counter`.
- The v1.109.0 release notes say: "Count `/v1/responses/input_tokens` the way inference sends the request" (PR #47363), which covers function tools. Source: <https://docs.litellm.ai/release_notes/v1.109.0/v1-109-0>.

**Provenance is reported.** The response carries `litellm_count_source`, one of `"provider"`, `"token_count_model"` or `"local_estimate"`, plus `litellm_count_model` for `token_count_model` (`litellm/types/token_count.py`; `count_source_fields` in `litellm/proxy/common_utils/token_count_settings.py:188`). `/utils/token_counter` names the field `count_source`. A client can therefore tell whether a gateway figure came from the provider or is a local estimate.

**When the provider count fails.** Since v1.108.0 the count routes "do not return a local tiktoken estimate". They return the provider's status and message (docs). The source has two narrow exceptions:

- an operator-configured `token_count_model`;
- the `token_count_*framing` keys in `model_info`, which turn only a provider 400/404 into a framed local estimate.

Both are in `_provider_token_count`, `proxy_server.py:15017-15067`. `litellm.disable_token_counter` with no provider result answers 503.

**Cost and rate limits.** LiteLLM charges nothing itself. Provider counting calls inherit the provider's cost and limits (Anthropic: free, with separate RPM limits; OpenAI: see 1b). The docs say local counting is throttled per worker: "Each worker process counts at most `TOKEN_COUNTER_MAX_CONCURRENT_COUNTS` payloads at a time (default 4)." Whether a count request goes through key or team RPM limits or spend logging was **not verified**.

**Version caveat.** All of this depends on the LiteLLM version the owner's proxy runs. #319 pinned 1.109.1, and the routes above are confirmed for that tag only. Whether the owner's proxy runs 1.109.1 or later is **unverified**.

### 1d. Summary for question 1

| | Anthropic direct | OpenAI direct | LiteLLM gateway (Responses) |
| --- | --- | --- | --- |
| Endpoint | `/v1/messages/count_tokens` | `/v1/responses/input_tokens` | `/v1/responses/input_tokens` on the proxy |
| Cost | Free (documented) | Free per Help Center snippet (**unverified**) | Provider's cost; no LiteLLM charge |
| Rate limit | 5k/10k/20k RPM by tier, separate from Messages | **Unverified** | Provider's; local count capped at 4 concurrent per worker |
| Counts system/instructions | Yes | Yes | Yes (`instructions` merged) |
| Counts tools | Yes (client tools) | Yes | Yes (since v1.109.0) |
| Exactness | "estimate", "might differ by a small amount"; may include system-added tokens | "exact count the model will receive" | Provider's figure when `litellm_count_source == "provider"`, otherwise a local estimate |
| LangChain path | `ChatAnthropic.get_num_tokens_from_messages` (API) | none (local tiktoken, ignores tools) | none |
| Non-native upstream | n/a | n/a | Anthropic deployments are bridged to Anthropic `count_tokens`; providers without a count API fall back to a local estimate, flagged in the response |

---

## 2. Model Context Window and maximum output

### 2a. Anthropic `claude-sonnet-5`

| Fact | Value | Source |
| --- | --- | --- |
| Context window | 1M tokens | <https://platform.claude.com/docs/en/about-claude/models/overview> ("All models" table); <https://platform.claude.com/docs/en/build-with-claude/context-windows> |
| Max output (`max_tokens`) | 128K tokens (synchronous Messages API) | Same pages: "A single request to any of them can generate up to 128k output tokens" |
| Batch max output | up to 300k with the `output-300k-2026-03-24` beta | models overview footnote |
| Beta header for 1M | none: "1M is the default: you don't need a beta header" | context-windows page |

**What counts toward the window.** "Everything in the request counts toward the context window: the system prompt, every message in `messages` (including tool results, images, and documents), and your tool definitions. The output Claude generates for the turn, including its extended thinking, counts too." With prompt caching, "the input count is split across `input_tokens`, `cache_read_input_tokens`, and `cache_creation_input_tokens`, and all three count toward the window" (context-windows page). Measured after the fact, the Anthropic Conversation Context is therefore the sum of those three usage fields.

**Context awareness.** `claude-sonnet-5` gets its budget injected by the API: `<budget:token_budget>` in the system prompt and `<system_warning>Token usage: …` after each tool call. "The budget matches the context window available to your request: 1M tokens for Claude Sonnet 5" (context-windows page). This happens on the provider side and is not configurable.

**Programmatic source: the Models API.** "You can query model capabilities and token limits programmatically with the Models API. The response includes `max_input_tokens`, `max_tokens`, and a `capabilities` object for every available model" (models overview). In `anthropic` 1.5.0 `anthropic/types/model_info.py`:

- `max_input_tokens: Optional[int]`: "Maximum input context window size in tokens for this model."
- `max_tokens: Optional[int]`: "Maximum value for the `max_tokens` parameter when using this model."

Both are `Optional`. The values the API returns for `claude-sonnet-5` were **not checked**, because that needs a live call.

**LangChain profile.** `langchain-anthropic` 1.7.12 `langchain_anthropic/data/_profiles.py` has `"claude-sonnet-5": {"max_input_tokens": 1000000, "max_output_tokens": 128000, ...}`. The file is "Auto-generated … derived from the models.dev project". It is third-party data frozen at the package version. `ChatAnthropic` uses it to default `max_tokens` when none is set (`chat_models.py:1722-1731`).

### 2b. OpenAI `gpt-5.6-luna`

| Fact | Value | Source |
| --- | --- | --- |
| Context window | 1,050,000 | <https://developers.openai.com/api/docs/models/gpt-5.6-luna> |
| Maximum input tokens | 922,000 | same page |
| Max output tokens | 128,000 | same page |
| Snapshot | `gpt-5.6-luna` only | same page |
| Responses endpoint | Supported | same page |

922,000 + 128,000 = 1,050,000, so OpenAI publishes a separate input ceiling: the window minus the maximum output. "This max tokens number includes input, output, and reasoning tokens" (<https://developers.openai.com/api/docs/guides/conversation-state>).

**Usage accounting.** `ResponseUsage.input_tokens` is a total. `input_tokens_details.cached_tokens` and `cache_write_tokens` break it down; they are not added on top (`openai` 3.13.0 `openai/types/responses/response_usage.py`). Anthropic is different: its cache fields are separate buckets and must be summed. Reading "OpenAI's `input_tokens` already includes cached tokens" from the field docstrings is an inference. The docstrings do not state it outright, so it is **unverified** against live data.

**No programmatic source from OpenAI.** The `openai` 3.13.0 `Model` object has only `id`, `created`, `object`, `owned_by` and `shutdown_date` (`openai/types/model.py`). The Models API does not report a context window or output cap.

**LangChain profile disagrees with OpenAI.** `langchain-openai` 1.8.5 `langchain_openai/data/_profiles.py` has `"gpt-5.6-luna": {"max_input_tokens": 1050000, "max_output_tokens": 128000}`, from models.dev. Its `max_input_tokens` is OpenAI's **context window**, not OpenAI's "Maximum input tokens" (922,000). Using the profile as the input ceiling would overstate it by 128,000.

### 2c. LiteLLM `/model/info` for these models

#319 established the general behaviour, which still applies:

- operator `model_info` wins and LiteLLM's model map fills in the rest;
- the map is fetched from GitHub `main` at startup by default, so it is not pinned to the release;
- no marker shows which limits the operator set;
- values can be null.

Map values for the two pinned names, as read on 2026-11-20:

| Key | `max_input_tokens` | `max_output_tokens` | `max_tokens` | Map |
| --- | --- | --- | --- | --- |
| `claude-sonnet-5` (`litellm_provider: anthropic`) | 1000000 | 128000 | null | `model_prices_and_context_window.json` at `v1.109.1` **and** at `main` |
| `gpt-5.6-luna` (`litellm_provider: openai`) | 922000 | 128000 | null | same |

- Neither map has an `anthropic/` or `openai/` prefixed key for these names.
- For these two models, LiteLLM's values match the provider docs: `gpt-5.6-luna` uses OpenAI's 922,000 input ceiling, not the 1.05M window.
- Because `main` moves, this agreement is a snapshot, not a guarantee.
- The v1.109.0 release notes show the map's `max_input_tokens` being corrected often ("Correct `max_input_tokens` to 922000 on Bedrock OpenAI GPT rows", and others). That supports #319's conclusion that the map is not a stable source.
- What `/model/info` returns for a deployment whose `litellm_params.model` is a non-map name (a custom alias or another provider's model) depends on the operator's `model_info`. This was not tested live.

This does not change the binding prior decision [#324](https://github.com/lbacik/coding-agent/issues/324): a gateway pin's limits come from the Workflow Configuration only.

---

## 3. Recognising a request that exceeds the Model Context Window

### 3a. Anthropic (direct)

Source: <https://platform.claude.com/docs/en/build-with-claude/context-windows#context-window-overflow-behavior>.

- **Input alone is over the window.** "If the input alone already exceeds the model's context window, the API returns a 400 `invalid_request_error` ("prompt is too long") on every model."
- **Input plus `max_tokens` is over the window.** This is not an error on Claude 4.5 and newer, which includes `claude-sonnet-5`. "If input tokens plus `max_tokens` exceeds the context window size, the API accepts the request. If generation then reaches the context window limit, it stops with `stop_reason: "model_context_window_exceeded"`."
  - The handling guide calls it a valid, truncated response (<https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons>).
  - `anthropic` 1.5.0 types it in the non-beta `StopReason` (`anthropic/types/stop_reason.py`), although the docs say it is "currently typed only in the SDK's `beta` namespace".
  - `langchain-anthropic` 1.7.12 maps it to the shared finish reason `"context_exceeded"` (`chat_models.py:3410`).
- **Error shape.** `{"type": "error", "error": {"type": "invalid_request_error", "message": "..."}, "request_id": "..."}` (<https://platform.claude.com/docs/en/api/errors#error-shapes>). The same page says "Catch the SDK's typed classes rather than string-matching error messages". However, `invalid_request_error` covers many causes and has no overflow-specific type or code, so only the message text distinguishes overflow.
- **413 is a different error.** `request_too_large` is about the request's size in bytes (128 MB for Messages), not tokens.
- **LangChain classification.** `langchain-anthropic` 1.7.12 `_handle_anthropic_bad_request` raises `AnthropicContextOverflowError(anthropic.BadRequestError, langchain_core.exceptions.ContextOverflowError)` when `"prompt is too long" in e.message` (`chat_models.py:1246, 1304-1309`). It does this on the invoke and stream paths (`chat_models.py:2309, 2360, 2684, 2705`).

### 3b. OpenAI Responses (direct)

- **Documented.** The `truncation` parameter: "`disabled` (default): If the input size will exceed the context window size for a model, the request will fail with a 400 error." With `auto`, the API silently drops items "from the beginning of the conversation". Sources: `openai` 3.13.0 `openai/types/responses/response_create_params.py:319-327`, generated from OpenAI's API spec. The repo does not set `truncation`, so it gets the 400.
- **Code and message.** **Partly verified.**
  - OpenAI's Agents API error guide lists the code `context_length_exceeded`, meaning "Input exceeds the model's context window" (<https://developers.openai.com/api/docs/guides/agents-api/errors>). The `openai` 3.13.0 beta `SessionTurnError` enumerates the same code (`openai/types/beta/session_turn_error.py:14`).
  - The OpenAI error-codes guide does not document the Responses overflow body.
  - Third-party reports (not primary) describe the Responses 400 body as `type: invalid_request_error`, `code: context_length_exceeded`, `param: input`, with a message containing "exceeds the context window". They also report that a **streamed** overflow can end in an error event with an empty message.
- **LangChain classification.** `langchain-openai` 1.8.5 encodes the expected shapes (`chat_models/base.py:646-653, 710-753`):
  - `_handle_openai_bad_request` raises `OpenAIContextOverflowError(openai.BadRequestError, ContextOverflowError)` when the error contains `"context_length_exceeded"`, `"Input tokens exceed the configured limit"`, `"prompt is too long"` or `"ContextWindowExceededError"`.
  - `_handle_openai_api_error` raises `OpenAIAPIContextOverflowError(openai.APIError, ContextOverflowError)` on `"exceeds the context window"`. This catches non-HTTP stream errors.
  - Both run on the invoke and stream paths (`base.py:1816-1818` and similar).
- **Input plus output over the window.** For OpenAI, what happens when input ≤ 922,000 but input + `max_output_tokens` > 1,050,000 is **unverified**. The `incomplete_details.reason` values in `openai` 3.13.0 are `max_output_tokens`, `max_messages`, `content_filter` and `steered` (`openai/types/responses/response.py:54`), with no context-window reason.

### 3c. LiteLLM Proxy (gateway pins)

Sources: LiteLLM `v1.109.1` `litellm/exceptions.py:608-640`, `litellm/litellm_core_utils/exception_mapping_utils.py`, `litellm/proxy/utils.py:9470-9488`, `litellm/proxy/common_utils/openai_error_payload.py`.

- **Upstream mapping.** The proxy maps upstream overflow errors to `litellm.ContextWindowExceededError` (a `BadRequestError`, `status_code = 400`). It prefixes the message: `self.message = f"litellm.ContextWindowExceededError: {self.message}"`.
  - Anthropic: `"prompt is too long"`, `"prompt: length"` or a generic match (`exception_mapping_utils.py:618-626`).
  - OpenAI and OpenAI-compatible providers: `ExceptionCheckers.is_error_str_context_window_exceeded`, which matches substrings including `'"code": "context_length_exceeded"'`, "this model's maximum context length is" and "input tokens exceed the configured limit" (`exception_mapping_utils.py:88-120, 372-379`).
  - The v1.109.0 release notes: "Map OpenAI `context_length_exceeded` errors to `ContextWindowExceededError` instead of `BadRequestError`" (PR #47954).
- **Client-facing body.** `handle_exception_on_proxy` builds an OpenAI-shaped `ProxyException`: HTTP 400, `error.type` taken from the exception or else `"invalid_request_error"` for a 4xx, `error.param` nullable, and `error.code` the status as a string unless an `openai_code` is set (`_types.py:3870-3905`; `openai_error_payload.py:openai_error_type_for_status`). The message keeps the `litellm.ContextWindowExceededError:` prefix and the upstream text.
- **Classification through the gateway.** `langchain-openai` 1.8.5 explicitly matches `"ContextWindowExceededError"` in a `BadRequestError` message, so a gateway overflow on a non-streaming call is raised as `OpenAIContextOverflowError` (a `ContextOverflowError`).
- **Not verified.** The exact JSON body a gateway pin receives on a **streamed** Responses overflow was not verified in source and needs an empirical check. Whether the proxy preserves `error.code = "context_length_exceeded"` from an OpenAI upstream (as opposed to `"400"`) is also unverified.

### 3d. Summary for question 3

| | Input over window | Input + max output over window | Stable classifier |
| --- | --- | --- | --- |
| Anthropic direct | 400 `invalid_request_error`, message "prompt is too long" | Accepted; `stop_reason: "model_context_window_exceeded"` | Message substring only; LangChain wraps as `ContextOverflowError` |
| OpenAI direct (Responses, `truncation` unset) | 400 (documented); code `context_length_exceeded` (Agents API docs, third-party reports) | **Unverified** | `code == "context_length_exceeded"`; LangChain wraps as `ContextOverflowError` |
| LiteLLM gateway | 400, message prefixed `litellm.ContextWindowExceededError:` | Depends on upstream | Message prefix; LangChain wraps as `ContextOverflowError` |

`langchain_core.exceptions.ContextOverflowError` (`langchain-core` 1.8.4, `exceptions.py:129`) is the one cross-provider type the installed stack already raises for every route above. The repo does not catch it today: there are no matches for `ContextOverflowError` in `src/`.

---

## Facts that bear on the two downstream decisions

**How the Conversation Context is measured**

1. All three routes offer a provider-side count before sending: Anthropic `count_tokens`, OpenAI `responses/input_tokens`, and LiteLLM `responses/input_tokens`. Each counts the system prompt or instructions and the tool definitions.
2. The current `bytes/4` estimate has no counterpart in any provider. OpenAI calls `characters / 4` "inaccurate". Anthropic says "Claude 4.7 and later models … use a newer tokenizer" that yields "approximately 30 percent more tokens" for the same text, and advises to "Recount prompts against the model you plan to use". The wording appears to cover `claude-sonnet-5`, which was released after Opus 4.7, but the page does not name Sonnet 5 explicitly.
3. The Anthropic count is free and has its own rate limit. It is documented as an estimate that "might differ by a small amount" and may include unbilled system-added tokens. The OpenAI count is documented as exact. Its cost and rate limit are unverified.
4. A provider count is one extra HTTP round trip per measurement. LiteLLM forwards it to the provider, or answers with a local estimate flagged `litellm_count_source: "local_estimate"`.
5. LangChain gives an API-backed count only for Anthropic. The OpenAI and gateway routes need a direct `openai` SDK call. `ChatOpenAI.get_num_tokens_from_messages` ignores tools and uses Chat Completions framing.
6. Post-hoc usage per bucket:
   - Anthropic Conversation Context = `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (documented);
   - OpenAI `input_tokens` is already the total, with cached tokens broken out in `input_tokens_details`. That reading is inferred from the SDK docstrings.

**Where the Model Context Window comes from**

1. The providers publish these values in their docs:
   - `claude-sonnet-5`: window 1,000,000; max output 128,000. For Anthropic, input alone may fill the whole window.
   - `gpt-5.6-luna`: window 1,050,000; max input 922,000; max output 128,000.
2. Only Anthropic has a machine-readable source: Models API `max_input_tokens` and `max_tokens`, both optional. OpenAI's Models API reports no limits.
3. The bundled LangChain profiles come from models.dev and are frozen per package version. The OpenAI profile gives `gpt-5.6-luna` a `max_input_tokens` of 1,050,000, which is the window, not OpenAI's 922,000 input ceiling.
4. LiteLLM's map currently matches the provider docs for both pins. It is unpinned (#319), and #324 has already ruled the gateway out as a source.
5. The two providers model the limit differently:
   - **Anthropic:** one window shared by input and output. Exceeding it with input plus output is a truncated success, not an error.
   - **OpenAI:** a separate published input ceiling.

   Any single "Model Context Window" figure has to say which one it means.

## Open empirical checks (not done: they need live or paid calls)

1. What the Anthropic Models API returns for `claude-sonnet-5` (`max_input_tokens`, `max_tokens`).
2. The exact Responses overflow body from OpenAI direct, non-streaming and streaming, and whether input ≤ 922k with input + `max_output_tokens` > 1.05M fails or truncates.
3. The overflow body a gateway pin receives through the owner's LiteLLM proxy, non-streaming and streaming. Does `error.code` stay `context_length_exceeded`, or become `"400"`?
4. The LiteLLM version the owner's proxy runs, and whether `/v1/responses/input_tokens` returns `litellm_count_source: "provider"` for each gateway alias.
5. How far the Anthropic `count_tokens` figure is from the billed `usage` for a typical repo request with tools and thinking.
6. OpenAI `responses/input_tokens` cost and rate limit, from the Help Center page this agent could not read.
