# LiteLLM Proxy wire surfaces for a Pinned Model (issue #318)

Research for [#318](https://github.com/lbacik/coding-agent/issues/318), part of the map
[#317](https://github.com/lbacik/coding-agent/issues/317). Facts only: choosing the protocol is a
separate grilling ticket.

**Question.** Through a LiteLLM Proxy, which of `/v1/messages`, `/v1/responses` and
`/chat/completions` can carry a Worker request without changing what it means, for a native
upstream (Anthropic) and for non-native ones (Gemini, Ollama)? The request carries tools plus a
forced `tool_choice`, a pinned effort and the ADR 0009 invalid-effort probe, an assistant turn
carried back verbatim (ADR 0010), usage including cache fields, and an output cap.

## Sources and pins

| Source | Pin |
|---|---|
| LiteLLM source | tag [`v1.109.1`](https://github.com/BerriAI/litellm/tree/v1.109.1), commit `6c4ead4fc064d14e36b0c69d6795acc130c866bf`, the latest non-prerelease on 2026-11-12 |
| LiteLLM docs (docs.litellm.ai) | repo [`BerriAI/litellm-docs`](https://github.com/BerriAI/litellm-docs) at commit `4e45546ebf6ff77a0734cc7981e0930b30ce61c4` (2026-11-12). The docs site has no versions, so this commit is the pin. |
| LangChain adapters (this repo's `.venv`) | `langchain` 1.4.0, `langchain-anthropic` 1.7.2, `langchain-openai` 1.6.2, `langchain-core` 1.6.3, `anthropic` 1.5.0, `openai` 3.13.0 |
| This repo | `65a5dce` (`v0.3.0`): `src/coding_agent/provider/pinned_model.py`, `src/coding_agent/provider/capability.py` |

Each claim carries one evidence tag:

- **[D]** documented on docs.litellm.ai (linked page; content as of the docs pin).
- **[S]** read in source at the pin (linked file and line). This is behaviour that is implemented but
  not necessarily documented or promised.
- **[U]** unverified: an inference or a provider-side behaviour this research did not observe. No
  model calls were made, paid or free.

The Pi proxy's LiteLLM version is unknown here. Every [S] fact applies to `v1.109.1` only. Several
features below are recent (`supported_endpoints` passthrough for `/v1/messages` since `v1.92.0`, for
`/v1/responses` since `v1.101.0`, `reasoning_effort_levels` since `v1.108.0`), so check the
proxy's version before treating these facts as true for it.

Links use these short names:

- `LL` = `https://github.com/BerriAI/litellm/blob/v1.109.1`
- `DOCS` = `https://docs.litellm.ai/docs`
- `LC-A` = `.venv/.../langchain_anthropic/chat_models.py`
- `LC-O` = `.venv/.../langchain_openai/chat_models/base.py`

---

## 1. Summary

| | `/v1/messages` + `ChatAnthropic` | `/v1/responses` + `ChatOpenAI(use_responses_api=True)` | `/chat/completions` + `ChatOpenAI` |
|---|---|---|---|
| **Anthropic upstream** (`anthropic/…`) | **Native forward.** The body reaches Anthropic largely unchanged. Forced `tool_choice`, `output_config.effort`, thinking blocks with signatures and cache usage all pass through. An invalid effort reaches Anthropic and Anthropic refuses it (adaptive-thinking models). | **Bridged** through LiteLLM's chat-completions layer. `reasoning.effort` becomes `reasoning_effort`, then `thinking` + `output_config.effort`. LiteLLM itself refuses an invalid effort with a 400. Signed thinking blocks travel back as JSON inside `reasoning.encrypted_content`, and LangChain replays them. Cache read and write are both reported. | Same LiteLLM mapping as the bridge, so an invalid effort gets the same LiteLLM 400. **Not verbatim:** `ChatOpenAI` drops `thinking_blocks`/`reasoning_content`, so signed thinking is lost on replay. With `modify_params`, LiteLLM then silently drops `thinking`. |
| **Gemini upstream** (`gemini/…`) | **Translated** through the chat-completions adapter. Forced tool → `mode: ANY` + `allowed_function_names`. **`output_config.effort` is used only when `thinking.type == "adaptive"`**, and otherwise is silently discarded. The thought signature comes back in a non-standard `tool_use.provider_specific_fields`, which `ChatAnthropic` drops on replay. On Gemini 3+, LiteLLM then injects a dummy signature. | Bridged. Effort maps to `thinkingConfig`. An invalid effort gets a LiteLLM 400 (`UnsupportedParamsError`) whatever `drop_params` is set to. The thought signature rides inside the tool-call id (`…__thought__<sig>`), which LangChain keeps verbatim. | As the bridge: forced tool honoured, invalid effort refused by LiteLLM, and the signature carried in the tool-call id. |
| **Ollama upstream** (`ollama_chat/…`) | Translated. **Forced `tool_choice` is always removed.** Effort becomes `think: true/false`, and an invalid effort becomes `think: false` with only a log warning. | The same Ollama mapping: forced tool removed, invalid effort accepted. | The same. |
| **Usage** | Anthropic: verbatim, including `cache_creation_input_tokens`/`cache_read_input_tokens`. Adapter: cache fields are **absent when zero**, and `input_tokens` excludes the cached part. | `input_tokens_details.cached_tokens` plus a non-standard `cache_write_tokens`, both of which LangChain reads. | `prompt_tokens_details.cached_tokens`/`cache_write_tokens`, both of which LangChain reads. |
| **Output cap** | `max_tokens` is forwarded (Gemini `maxOutputTokens`, Ollama `num_predict`). | `max_output_tokens` → `max_tokens`. | `max_completion_tokens`/`max_tokens` mapped per provider. |

Several proxy-side settings can silently change a request on **every** surface: `drop_params`,
`modify_params`, a deployment's `reasoning_effort_levels`, the forced-tool downgrade, and
`encrypted_content_affinity`. They are covered in §5.

---

## 2. Which adapter points at which surface

| LangChain adapter | Base URL to set | Path the SDK appends | Proxy route |
|---|---|---|---|
| `ChatAnthropic` (`init_chat_model("anthropic:<m>", base_url=…)`) | `http://<proxy>:4000`, with no `/v1` | `/v1/messages` (`anthropic/resources/messages/messages.py:979`) [S] | `/v1/messages` ([LL/litellm/proxy/anthropic_endpoints/endpoints.py#L164](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/anthropic_endpoints/endpoints.py#L164)) [S] |
| `ChatOpenAI(use_responses_api=True)` | `http://<proxy>:4000/v1` | `/responses` (`openai/resources/responses/responses.py:1042`) [S] | `/v1/responses` and `/responses` ([LL/litellm/proxy/response_api_endpoints/endpoints.py#L298](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/response_api_endpoints/endpoints.py#L298)) [S] |
| `ChatOpenAI` (no Responses API) | `http://<proxy>:4000/v1` | `/chat/completions` [S] | `/v1/chat/completions` and `/chat/completions` (`litellm/proxy/proxy_server.py:12856`) [S] |

Adapter facts:

- **`ChatAnthropic` base URL** comes from the `base_url` field (alias of `anthropic_api_url`), then
  `ANTHROPIC_API_URL`, then `ANTHROPIC_BASE_URL` (`LC-A:1127`, `_resolve_gateway` at `LC-A:1414`)
  [S]. The Anthropic SDK sends the key as `x-api-key`. The proxy accepts that header as a LiteLLM
  key ([LL/litellm/proxy/auth/user_api_key_auth.py#L232](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/auth/user_api_key_auth.py#L232))
  [S].
- **`ChatOpenAI` base URL** comes from `base_url` (alias `openai_api_base`), then `OPENAI_API_BASE`,
  then `OPENAI_BASE_URL` (`LC-O:794`) [S]. Setting it turns off the default `stream_usage`, which
  matters only for streaming (`LC-O:1356`) [S].
- **`ChatAnthropic` keys several defaults on the model name** through its bundled model profiles
  (`_get_default_model_profile`, `LC-A:121`) [S]. A gateway model name that is not a known Anthropic
  model id (a proxy alias, or any Gemini/Ollama name) has no profile, and this has two effects:
  - `max_tokens` defaults to `4096` (`_FALLBACK_MAX_OUTPUT_TOKENS`, `LC-A:136`, `LC-A:1390`) [S].
    The Worker's explicit cap overrides this.
  - **The default `thinking: {"type": "adaptive", "display": "summarized"}` is not added.** That
    default is added only when `reasoning_effort` is set *and* the profile lists `xhigh`
    (`LC-A:1689`–`1700`) [S]. The request then carries `output_config.effort` with no `thinking`
    block, and on the Gemini/Ollama path of `/v1/messages` that loses the effort (§3.2).
- **`ChatAnthropic` and forced `tool_choice`.** `bind_tools(..., tool_choice="<name>")` sends
  `{"type": "tool", "name": …}`. If the *constructor's* `thinking` is `enabled`/`adaptive`,
  `bind_tools` deletes a forced `tool_choice` with a warning. The code comment's reason is that
  "Anthropic API rejects forced tool use when thinking is enabled" (`LC-A:2490`–`2510`) [S]. The
  adaptive default added in the payload (above) is not `self.thinking`, so it does **not** trigger
  that drop. Whether Anthropic accepts forced tool use together with that payload-level adaptive
  thinking was not checked here [U]. This is an existing direct-path fact, and the native
  `/v1/messages` path reproduces it unchanged.
- **`ChatAnthropic` rebuilds `tool_use` blocks** from `AIMessage.tool_calls` (`name`, `input`, `id`)
  when replaying. Extra keys on a `tool_use` content block are dropped (`LC-A:600`–`636`) [S].
  `thinking` blocks are replayed as received.
- **`ChatOpenAI` on chat completions targets the official OpenAI schema only.** "Non-standard
  response fields added by third-party providers (e.g., `reasoning_content`, `reasoning_details`)
  are **not** extracted or preserved" (`LC-O` module docstring, lines 1–11) [S]. Tool-call ids are
  kept.
- **`ChatOpenAI` on the Responses API** renames `max_tokens` to `max_output_tokens`, turns
  `reasoning_effort` into `reasoning: {effort}`, and turns a forced tool into
  `{"type": "function", "name": …}` (`_construct_responses_api_payload`, `LC-O:4504`–`4570`) [S].
  It replays `reasoning` items, with their `id` and any `encrypted_content`, whenever `store` is not
  `False`, which is the default `None` (`LC-O:4775`–`4800`, `4898`) [S].
- **The capability probe's error type works on both adapters.** An HTTP 400 from the proxy becomes
  `AnthropicInvalidRequestError` or `OpenAIInvalidRequestError`, both subclasses of
  `langchain_core.exceptions.ModelInvalidRequestError` (`LC-A:968`, `LC-O:588`) [S]. That is the
  exception `capability.py` catches.

---

## 3. `/v1/messages`

### 3.1 Routing

LiteLLM picks a native "Anthropic Messages config" by provider. `anthropic/` always gets
`AnthropicMessagesConfig`. `bedrock/`, `vertex_ai/` (Claude), `azure_ai/` (Claude), Databricks,
MiniMax, DeepSeek and some others also have native configs.
**Gemini and Ollama have none** ([LL/litellm/utils.py#L10113](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/utils.py#L10113))
[S]. Everything without a native config goes through `LiteLLMAnthropicMessagesAdapter`:
Anthropic → OpenAI chat → `litellm.completion` → provider, and back
(`litellm/llms/anthropic/messages/architecture.md`) [S]. `openai/` is the exception and goes through
the Responses API instead ([LL/…/native/handler.py#L65](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/native/handler.py#L65))
[S].

[D] [`DOCS/anthropic_unified/native_passthrough`](https://docs.litellm.ai/docs/anthropic_unified/native_passthrough)
says the same: "translation only keeps what the target API can express: `cache_control` blocks are
dropped, `thinking` is mapped to the provider's own reasoning parameter, and other Anthropic-only
request details are approximated or lost". An OpenAI-compatible deployment whose server *itself*
speaks Anthropic Messages can opt into an untranslated forward with
`model_info.supported_endpoints: ["/v1/messages"]` (since `v1.92.0`). That opt-in matters for an
upstream that itself serves `/v1/messages` (for example a vLLM server), not for `gemini/`.

### 3.2 Anthropic upstream (native)

- **Tools and forced `tool_choice`:** forwarded as sent. The forced-tool downgrade (§5.3) lives in
  the chat-completions transformation and Bedrock Converse, not on this path [S]. `tools` pass
  through `tools_for_upstream`, which drops only "custom" tool types under `drop_params`
  (`native/transformation.py:210`) [S].
- **Effort.** `output_config` and `thinking` are supported params (`native/transformation.py:118`–`140`)
  [S]. For **adaptive-thinking models** (the model map's `supports_adaptive_thinking`: Opus 4.6+,
  Sonnet 4.6+ and later), `thinking` and `output_config.effort` are "left untouched"
  ([LL/…/native/transformation.py#L529](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/native/transformation.py#L529))
  [S]. An invalid `output_config.effort` therefore reaches Anthropic, and Anthropic's own refusal
  comes back. That Anthropic names `effort` in the refusal, which `capability.py` requires, is the
  current direct-path behaviour; through the proxy it is [U]. For **older models**, LiteLLM reshapes
  silently:
  - Opus 4.5 (`supports_output_config` but not adaptive) keeps the effort.
  - Thinking-capable pre-4.6 models get the effort mapped to a legacy `thinking.budget_tokens`, and
    `output_config.effort` is removed. An invalid effort then raises a 400 from LiteLLM's mapper
    ("Unmapped reasoning effort", [LL/…/chat/transformation.py#L1644](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/chat/transformation.py#L1644)).
  - Models without reasoning lose `thinking` with only a warning log.

  All of these choices read LiteLLM's model map by model name [S].
- **Deployment effort rounding** also applies here: `_with_deployment_output_effort`
  ([LL/…/native/handler.py#L167](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/native/handler.py#L167))
  [S] [D]. See §5.2.
- **Verbatim turn:** the request body is forwarded and the Anthropic response is returned in
  Anthropic shape [D] [S]. `thinking`/`redacted_thinking` blocks with signatures and `toolu_…` ids
  survive, and `ChatAnthropic` replays them as it does today. Whether the proxy rewrites any
  response fields on this path (it has a `streaming_model_restamp` module for the `model` field) was
  not exhaustively checked. Content blocks were not seen to be altered [U].
- **Usage:** Anthropic's own `usage` is returned, including `cache_creation_input_tokens` and
  `cache_read_input_tokens` ([`DOCS/anthropic_unified`](https://docs.litellm.ai/docs/anthropic_unified),
  "Response Format") [D].
- **Output cap:** `max_tokens` is forwarded [S]. `modify_params` can lower it (§5.4).

### 3.3 Gemini / Ollama upstream (adapter)

- **Tools and forced `tool_choice`.** `{"type": "tool", "name": X}` becomes
  `{"type": "function", "function": {"name": X}}`
  ([LL/…/adapters/transformation.py#L803](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/transformation.py#L803))
  [S]. What happens next depends on the upstream:
  - **Gemini:** becomes `functionCallingConfig: {mode: "ANY", allowed_function_names: [X]}`, which
    is honoured ([LL/…/vertex_and_google_ai_studio_gemini.py#L781](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py#L781))
    [S].
  - **Ollama:** `tool_choice` is **always popped**, with the comment "causes ollama requests to
    hang", whatever `drop_params` is set to. No error is raised, so a forced activation call becomes
    `auto` ([LL/…/ollama/chat/transformation.py#L186](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/ollama/chat/transformation.py#L186))
    [S].
- **Effort: carried only through an adaptive `thinking` block.**
  - `output_config` is in the adapter's "translatable" list, so it is never copied through as is
    ([LL/…/adapters/transformation.py#L455](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/transformation.py#L455)).
    The handler also strips it from the re-merged extras (`ANTHROPIC_ONLY_REQUEST_KEYS`,
    [LL/…/adapters/handler.py#L55](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/handler.py#L55))
    [S].
  - `_translate_thinking_to_openai` returns at once when the request has no `thinking`. It reads
    `output_config.effort` only when `thinking.type == "adaptive"`. For a non-Claude target, that
    effort (or `"medium"` when adaptive thinking has no effort, or a budget-derived level for
    `type: enabled`) becomes `reasoning_effort`
    ([LL/…/adapters/transformation.py#L1155](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/transformation.py#L1155))
    [S].
  - **Consequence [S]:** `ChatAnthropic` sends `output_config.effort` without `thinking` whenever
    the gateway model name has no Anthropic profile (§2), which covers every Gemini or Ollama name.
    The effort then never reaches the upstream. Nothing is raised, so the ADR 0009 invalid-effort
    probe would be *accepted* and the assertion would refuse the pin. The invalid value is simply
    discarded.
  - When an adaptive `thinking` block *is* sent, `reasoning_effort` reaches the provider mapping:
    - **Gemini** maps `minimal/low/medium/high/none/disable` to `thinkingLevel` (Gemini 3+) or
      `thinkingBudget`. **Any other value raises `UnsupportedParamsError` (HTTP 400, message
      "Invalid `reasoning_effort`: …")** ([LL/…#L191](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py#L191),
      [#L1717](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py#L1717))
      [S]. The refusal comes from **LiteLLM**, not Google.
    - **Ollama** maps `low/medium/high` to `think: true` and anything else (except `none`) to
      `think: false`, with a warning only. `gpt-oss` models get the raw value
      ([LL/…/ollama/common_utils.py#L304](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/ollama/common_utils.py#L304))
      [S]. An invalid effort is accepted.
- **Verbatim turn.**
  - *Anthropic-format thinking blocks* the client replays become chat `thinking_blocks` plus
    `reasoning_content` on the assistant message
    ([LL/…/adapters/transformation.py#L624](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/transformation.py#L624))
    [S]. Neither Gemini nor Ollama verifies them.
  - **Gemini thought signatures.** Gemini attaches a `thoughtSignature` to function calls, and by
    default LiteLLM embeds it in the tool-call id as `<id>__thought__<sig>`
    ([LL/…/vertex_and_google_ai_studio_gemini.py#L2093](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py#L2093),
    [factory.py#L94](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/litellm_core_utils/prompt_templates/factory.py#L94))
    [S]. On this surface the adapter **strips that suffix from the `tool_use.id`** and moves the
    signature into a non-standard `tool_use.provider_specific_fields.signature`
    ([LL/…/adapters/transformation.py#L1494](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/transformation.py#L1494))
    [S]. On replay it reads the signature only from there (`#L606`) [S]. `ChatAnthropic` rebuilds
    `tool_use` blocks from `tool_calls` and drops that key (§2) [S]. The signature is therefore
    lost. For Gemini 3+, LiteLLM then inserts a **dummy signature**
    (`skip_thought_signature_validator`), which Google documents as a last resort that "will
    negatively impact model performance"
    ([LL/…/factory.py#L1331](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/litellm_core_utils/prompt_templates/factory.py#L1331))
    [S]. The request succeeds, but the turn the model sees is not the one it produced.
  - Gemini/Ollama `reasoning_content` comes back as an unsigned `thinking` block (`signature: null`)
    (`#L1427`) [S].
- **Usage.** Chat usage is mapped back to Anthropic shape:
  `input_tokens = prompt_tokens − cache_read − cache_creation`. `cache_creation_input_tokens` and
  `cache_read_input_tokens` are **set only when > 0, and are otherwise absent**
  ([LL/…/adapters/transformation.py#L1586](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/messages/adapters/transformation.py#L1586))
  [S].
  - **Gemini:** the cache read comes from `cachedContentTokenCount` (implicit caching included).
    Cache creation is non-zero only with explicit context caching (`#L2305`) [S].
  - **Ollama:** reports `prompt_eval_count`/`eval_count` only, so there are never any cache fields.
    When Ollama omits a count, LiteLLM fills it with its **own token-counter estimate**
    ([LL/…/ollama/chat/transformation.py#L386](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/ollama/chat/transformation.py#L386))
    [S].
- **Output cap.** `max_tokens` is copied through untranslated and mapped per provider: Gemini
  `maxOutputTokens`, Ollama `num_predict` (`ollama/chat/transformation.py#L157`) [S].

---

## 4. `/v1/responses`

### 4.1 Routing

Only providers with their own Responses config are native. At the pin that is OpenAI, Azure,
Azure AI, xAI, OpenRouter, hosted vLLM, Databricks, Bedrock, Fireworks and a few others.
**Anthropic, Gemini and Ollama are not** on the list
([LL/litellm/utils.py#L10349](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/utils.py#L10349))
[S]. Every other provider is **bridged**: Responses → chat completions → provider → back
(`litellm/responses/litellm_completion_transformation/`) [S].
[`DOCS/response_api_native_passthrough`](https://docs.litellm.ai/docs/response_api_native_passthrough)
documents the bridge and the `supported_endpoints: ["/v1/responses"]` opt-in for OpenAI-compatible
servers that serve `/responses` themselves (since `v1.101.0`) [D].

The bridge accepts `input, model, instructions, max_output_tokens, metadata, parallel_tool_calls,
previous_response_id, reasoning, stream, temperature, text, tool_choice, tools, top_p, user`
(`transformation.py#L935`) [S]. It builds the chat request at `#L1275`–`1305`:

- `max_output_tokens` → `max_tokens`
- `reasoning.effort` → `reasoning_effort`
- `reasoning.summary` → `reasoning_summary`
- a forced tool → `{"type": "function", "function": {"name": X}}` (`_transform_tool_choice`, `#L958`)
- `tool_choice` is dropped only when no tools are sent

### 4.2 Anthropic upstream (bridged)

- **Forced `tool_choice`:** becomes Anthropic `{"type": "tool", "name": X}` (`_map_tool_choice`)
  and then goes through `_apply_forced_tool_choice`. That function downgrades it to `auto` under
  `drop_params` for models the map flags with `supports_forced_tool_use: false`, and otherwise
  raises a client-side 400 (§5.3) [S].
- **Effort.** `reasoning_effort` maps as follows
  ([LL/…/chat/transformation.py#L2024](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/chat/transformation.py#L2024))
  [S]:
  - *adaptive-thinking models:* `thinking: {type: adaptive, display: summarized}` plus
    `output_config: {effort: <low|medium|high|xhigh|max>}` (`minimal` becomes `low`). An
    unrecognised value raises **LiteLLM's** `BadRequestError` "Invalid reasoning_effort: …"
    (`_raise_invalid_reasoning_effort`, `#L783`).
  - *older models:* legacy `thinking.budget_tokens`. An unrecognised value raises "Unmapped
    reasoning effort: …" (`#L1644`).
  - Either way the invalid value is refused with HTTP 400 **by the proxy**, and **Anthropic never
    sees it**. The message contains "effort", so `capability.py`'s substring check would pass. What
    the refusal proves changes, though: it shows that LiteLLM read the parameter, not that it
    reached the upstream. Whether a *valid* level reached Anthropic follows only from reading the
    source [S].
  - `drop_params` does not suppress these errors: they are raised in the value mapping, not the
    unsupported-param filter [S]. Separately, `_apply_output_config` drops non-`format`
    `output_config` keys when `litellm.drop_params` is on **and** the model map says the model
    lacks effort support (`#L2576`) [S].
- **Verbatim turn.** The Anthropic chat response carries `thinking_blocks` with signatures. The
  bridge emits one `reasoning` item with a minted id `rs_<uuid4>`, the thinking text as
  `content[].output_text`, and **`encrypted_content` = the signed thinking blocks serialised as
  plain JSON**. This is not encrypted, and it is emitted whether or not the request asked for
  `include: ["reasoning.encrypted_content"]`
  ([LL/…/transformation.py#L3560](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/responses/litellm_completion_transformation/transformation.py#L3560),
  [reasoning_items.py#L26](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/responses/litellm_completion_transformation/reasoning_items.py#L26))
  [S].
  - LangChain replays reasoning items, including `encrypted_content` (§2).
  - On input, the bridge decodes `encrypted_content` back into signed `thinking_blocks` and merges
    the reasoning-only message into the following assistant message, "placed ahead of any thinking
    blocks the target message already carries, because Anthropic and Bedrock verify signatures
    against the original block order"
    ([#L1716](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/responses/litellm_completion_transformation/transformation.py#L1716))
    [S].
  - Tool-call ids (`call_id`) carry Anthropic's `toolu_…` id [S].
  - The design aims at a verbatim round trip of the signed blocks. Whether Anthropic accepts the
    replay end to end was not observed [U].
- **Usage.** LiteLLM's Anthropic usage gives `prompt_tokens = input + cache_creation + cache_read`,
  with `prompt_tokens_details.cached_tokens` (read) and `cache_creation_tokens`, which are mirrored
  to `cache_write_tokens` (`#L2858`; `PromptTokensDetailsWrapper`
  [types/utils.py#L1865](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/types/utils.py#L1865))
  [S]. The bridge turns these into `input_tokens` (inclusive), `input_tokens_details.cached_tokens`
  and a non-standard `input_tokens_details.cache_write_tokens`
  ([#L3817](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/responses/litellm_completion_transformation/transformation.py#L3817))
  [S]. `langchain-openai` reads `cached_tokens` as `cache_read` and `cache_write_tokens` as
  `cache_creation` (`_create_usage_metadata_responses`) [S]. Note that **`input_tokens` includes the
  cached tokens on this surface but excludes them on `/v1/messages`.**
- **Output cap:** `max_output_tokens` → `max_tokens` [S].

### 4.3 Gemini upstream (bridged)

- **Forced tool:** honoured, `mode: ANY` + `allowed_function_names` (§3.3) [S].
- **Effort:** `reasoning.effort` → `reasoning_effort` → `thinkingConfig`. An invalid value raises
  LiteLLM's `UnsupportedParamsError` 400 (§3.3) [S]. `reasoning_effort` is a supported param only
  when the model map says `supports_reasoning` or the model is Gemini 3+
  ([#L776](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py#L776)).
  For other models it is an *unsupported param*: a 400 without `drop_params`, **silently dropped**
  with it ([LL/litellm/utils.py#L4612](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/utils.py#L4612))
  [S] [D].
- **Verbatim turn:** the thought signature rides in the tool-call id (`…__thought__<sig>`) as the
  `call_id` of the `function_call` item. LangChain replays `call_id` verbatim, so the signature
  survives [S].
  [`DOCS/providers/gemini#thought-signatures`](https://docs.litellm.ai/docs/providers/gemini#thought-signatures)
  documents that LiteLLM preserves signatures when the full assistant message is appended [D].
  Gemini's reasoning text comes back as a `reasoning` item without `encrypted_content`, and is
  replayed as `reasoning_content` [S].
- **Usage:** cache read via `cached_tokens`. Cache write is zero or absent except with explicit
  context caching [S].

### 4.4 Ollama upstream (bridged)

The same Ollama mapping as §3.3: **forced `tool_choice` removed**, effort becomes `think`
true/false, an invalid effort is accepted, and there is no cache usage [S].

### 4.5 OpenAI upstream (for reference, native)

Sent to `{api_base}/responses` natively ([D] `response_api_native_passthrough`). `reasoning.effort`
reaches OpenAI, which judges an invalid value itself [U]. With `encrypted_content_affinity` on,
LiteLLM **rewrites output item ids and wraps `encrypted_content`** as
`litellm_enc:<base64 model id>;<original>`, and unwraps it on the way back in
([LL/litellm/responses/utils.py#L523](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/responses/utils.py#L523))
[S] [D]. The client gets the bytes back as it sent them, but they differ from what the provider
issued.

---

## 5. Proxy settings that silently change a request

### 5.1 `drop_params`

[D] [`DOCS/completion/drop_params`](https://docs.litellm.ai/docs/completion/drop_params): by
default LiteLLM raises when a param is not supported by the provider/model. With
`drop_params: true` (proxy `litellm_settings`, per deployment, or per request in the body) it drops
the param instead. **Requests from Claude Code and Codex, recognised by user agent, drop by default**
even when it is unset. The LangChain/OpenAI/Anthropic SDK user agents are not those [U].

The distinction that matters for ADR 0009 is between an *unsupported param* and an *invalid value*
[S]:

- An **unsupported param** is dropped under `drop_params`. Examples: `reasoning_effort` on a
  non-reasoning Gemini, or `output_config` on an Anthropic model without effort support.
- An **invalid value** for a supported param is still a 400 from LiteLLM. Examples:
  `not-an-effort-level` on Anthropic adaptive models and on Gemini thinking models.
- **Exception:** a deployment that declares `reasoning_effort_levels` *drops* an undeclared level
  under `drop_params` (§5.2), so the invalid probe value then **is** dropped.
- On Ollama an invalid effort is never an error, `drop_params` or not.

### 5.2 `reasoning_effort_levels` and rounding (since `v1.108.0`)

[D] [`DOCS/reasoning_content#reasoning-effort-levels`](https://docs.litellm.ai/docs/reasoning_content):
a deployment's `model_info.reasoning_effort_levels` makes the proxy send a declared level **in
place of** any undeclared one. It rounds up by default, or down with `reasoning_effort_rounding`,
and logs an INFO line. This happens "on `/v1/chat/completions` (streaming included),
`/v1/responses`, and `/v1/messages`, for every provider". On `/v1/messages` the effort taken from
`output_config.effort` or `thinking` is rounded too.

The docs also say: "`none` and any level name outside the strength order are sent unchanged ...
With `drop_params: true`, a level the deployment does not declare is dropped instead."
Source: [`resolve_deployment_reasoning_effort`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/litellm_core_utils/reasoning_effort_capability.py#L269),
[`nearest_declared_reasoning_effort`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/litellm_core_utils/reasoning_effort_strength.py#L20)
[S].

So with this setting a **valid** pinned effort (say `medium`) can be sent as `high` without any
error. The invalid-effort probe cannot detect that: `not-an-effort-level` is outside the strength
order, so it is still forwarded and refused. Only with `drop_params` is the probe value dropped.

### 5.3 Forced tool use downgraded to `auto`

`AnthropicModelInfo.forced_tool_use_downgraded`
([LL/…/anthropic/common_utils.py#L928](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/common_utils.py#L928))
covers models whose map entry has `supports_forced_tool_use: false`. The docstring says these 400
on `any`/`tool`. At the pin the flag is set on the `claude-fable-5-1`, `claude-mythos-5-1`,
`claude-opus-5-5` and `claude-sonnet-5-5` families across providers. For these models:

- **with `drop_params`:** a forced `tool_choice` is replaced by `auto` with no error.
- **without it:** `UnsupportedParamsError`.

This applies on chat completions and the Responses bridge (Anthropic chat transformation) and on
Bedrock Converse. It does not apply on native `/v1/messages`, which forwards the forced choice for
the upstream to judge [S]. That these models actually reject forced tool use is LiteLLM's model-map
claim, not checked against Anthropic here [U].

### 5.4 `modify_params` (default off)

[D] [`DOCS/proxy/config_settings`](https://docs.litellm.ai/docs/proxy/config_settings) (`LITELLM_MODIFY_PARAMS`)
lists what it changes, among other things:

- "lowering `max_tokens` to fit the model's limit", which silently changes the output cap.
- "dropping `thinking` on Claude models when the earlier assistant tool calls carry no thinking
  blocks". This is what happens on `/chat/completions` after `ChatOpenAI` loses the thinking blocks
  ([LL/…/chat/transformation.py#L2388](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/llms/anthropic/chat/transformation.py#L2388))
  [S]. With it off, the outcome is whatever Anthropic does with a tool-use turn missing its
  thinking block [U].

### 5.5 Gemini dummy thought signature

This is unconditional for Gemini 3+ when a function call has no signature (§3.3). No setting
turns it off, so a lost signature is never surfaced as an error [S].

---

## 6. `/chat/completions`

- **Routing:** every provider is native to LiteLLM's chat layer [S].
- **Tools and forced tool:**
  - Anthropic: `{"type": "tool", "name": X}`, subject to §5.3.
  - Gemini: `mode: ANY` + `allowed_function_names`.
  - Ollama: removed.

  [S]
- **Effort:** `reasoning_effort` with the same per-provider mapping and invalid-value behaviour as
  §4.2–§4.4. The refusal comes from LiteLLM for Anthropic and Gemini, and Ollama accepts the value
  [S]. The Worker's existing note that OpenAI chat completions 400s on tools + effort is about
  OpenAI's own endpoint. It does not carry over to LiteLLM's chat layer in front of Anthropic or
  Gemini, which maps `reasoning_effort` itself [S]. Whether OpenAI models behind the proxy still
  400 there was not checked [U].
- **Verbatim turn:**
  - LiteLLM returns Anthropic thinking as `message.thinking_blocks` (with signatures) plus
    `reasoning_content`, and Gemini signatures in `provider_specific_fields` *and* the tool-call id
    [S] [D].
  - **`ChatOpenAI` keeps none of `thinking_blocks`, `reasoning_content` or `provider_specific_fields`**
    (§2). Only the tool-call id survives. So:
    - Gemini signatures survive through the id.
    - Anthropic signed thinking is lost. LiteLLM then drops `thinking` (with `modify_params`), or
      the request goes out without the block [U for Anthropic's reaction].
  - A LangChain adapter that keeps these fields (for example `langchain-litellm`'s `ChatLiteLLM`)
    would wrap the LiteLLM SDK, which is out of scope for #317. This was not investigated.
- **Usage:**
  - `prompt_tokens` (inclusive), `prompt_tokens_details.cached_tokens` and `cache_write_tokens`
    (mirrored from `cache_creation_tokens`) [S].
  - `langchain-openai` `_create_usage_metadata` reads `cached_tokens` → `cache_read` and
    `cache_write_tokens` → `cache_creation` [S].
  - Ollama: no cache fields. Counts may be LiteLLM estimates [S].
- **Output cap:** `ChatOpenAI` sends `max_completion_tokens`
  (`LC-O:3703`) [S], and LiteLLM maps it per provider [S].

---

## 7. Facts that bear on ADR 0009 and ADR 0010 (no decisions)

1. **Who refuses the invalid effort depends on surface and upstream.**

   | Surface and upstream | Refused by |
   |---|---|
   | Native `/v1/messages` → Anthropic adaptive models | Anthropic [S, U for the message] |
   | Chat or Responses bridge → Anthropic or Gemini | LiteLLM (HTTP 400, message names `reasoning_effort`) [S] |
   | `/v1/messages` adapter → Gemini without an adaptive `thinking` block | nobody: the value is discarded [S] |
   | Any surface → Ollama | nobody: the value becomes `think: false` [S] |
   | Any surface, deployment with `reasoning_effort_levels` and `drop_params` | nobody: the value is dropped [D] [S] |

   Where nobody refuses, the current assertion would refuse the pin. Where LiteLLM refuses, the
   probe shows that the gateway read the parameter, not that the upstream received it.
2. **Valid efforts can be silently re-levelled** by deployment rounding (§5.2) or by LiteLLM's
   reshaping for older Claude models (§3.2), and the invalid-value probe cannot see either.
3. **A forced `tool_choice` can silently become `auto`:** always on Ollama, and under `drop_params`
   for the flagged Claude models on the chat/Responses paths (§5.3). The capability probe binds its
   tool without any `tool_choice` (`capability.py`), so the Provider Capability Assertion as written
   would not notice a forced-to-`auto` downgrade [S, this repo].
4. **Verbatim carry-back by surface and upstream:**
   - holds for native `/v1/messages` → Anthropic [S];
   - holds by design for the Responses bridge → Anthropic (signed blocks inside `encrypted_content`)
     and Gemini (signature in `call_id`) [S, end-to-end U];
   - **breaks** for `/v1/messages` adapter → Gemini (signature moved to a field `ChatAnthropic`
     drops, then replaced by a dummy) [S];
   - **breaks** for `/chat/completions` → Anthropic (`ChatOpenAI` drops `thinking_blocks`) [S].
5. **Usage semantics differ by surface:** `/v1/messages` `input_tokens` excludes the cached part,
   and cache fields are absent when zero. `/v1/responses` and `/chat/completions` input counts
   include the cached part, and cache write appears as `cache_write_tokens`. Ollama has no cache
   fields, and its counts can be LiteLLM estimates [S].

## 8. Open items to check empirically on the Pi proxy (minimal calls)

- The proxy's LiteLLM version, and whether `drop_params`, `modify_params`,
  `reasoning_effort_levels` and `encrypted_content_affinity` are set in its config.
- `/v1/messages` → Anthropic with `output_config.effort: "not-an-effort-level"`: the refusal text
  comes from Anthropic and names `effort`.
- `/v1/messages` → Gemini with `output_config.effort` and no `thinking`: the effort is absent from
  the upstream request (LiteLLM debug log). Also the dummy signature on the second turn.
- `/v1/responses` → Anthropic: a two-turn tool loop with signed thinking replays without error, and
  `cache_write_tokens` appears.
- Forced `tool_choice` + adaptive thinking on Anthropic (native forward), which is the same question
  as on the direct path.
