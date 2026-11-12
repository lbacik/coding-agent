# LiteLLM Proxy: model identity, served-model metadata, fallback and retry

Research ticket: [#319](https://github.com/lbacik/coding-agent/issues/319), part of the map
[#317](https://github.com/lbacik/coding-agent/issues/317). These are facts only. Whether the
no-fallback assertion can rest on them, and where per-pin limits come from, are separate
grilling tickets.

## Pinned version and sources

- **LiteLLM `1.109.1`**, the newest release on PyPI on 2026-11-12. Git tag `v1.109.1` is commit
  [`6c4ead4f`](https://github.com/BerriAI/litellm/commit/6c4ead4fc064d14e36b0c69d6795acc130c866bf).
  I read the source from the PyPI wheel `litellm-1.109.1-cp310-abi3-macosx_11_0_arm64.whl`. Four
  of the central files are byte-identical to the tag
  (`proxy/common_request_processing.py`, `router_python_core.py`, `proxy/proxy_server.py`,
  `proxy/litellm_pre_call_utils.py`), so the line links below point at the tag.
- **Docs**: the docs.litellm.ai pages as served on 2026-11-12. The docs site source is not in
  the main repository at `v1.109.1`, so the docs cannot be pinned to the release. Treat them as
  "current docs", not "1.109.1 docs".
- Markers used below:
  - **[doc]** means docs.litellm.ai states it.
  - **[src]** means I read it in the 1.109.1 source.
  - **[unverified]** means neither, or an inference. Check these on the owner's Raspberry Pi
    proxy before relying on them.
- I made no model calls and did not run the proxy.

`src` below is shorthand for `https://github.com/BerriAI/litellm/blob/v1.109.1/`.

## 1. What the response reports as the model

### Body `model` field: the alias the client sent, not the upstream id

- [src] On the non-streaming path, the proxy rewrites the body's `model` to the name the client
  requested (the `model_name` alias). It does so after every call:
  [`common_request_processing.py#L3446-L3455`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L3446-L3455),
  which calls `_override_openai_response_model`
  ([`#L1619`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L1619)).
  The function covers both response objects and dicts, so the OpenAI Chat Completions, Responses
  and Anthropic `/v1/messages` shapes are all restamped.
- [src] Streaming chunks are restamped the same way:
  - Chat Completions and Responses, including the nested `response.model` in Responses events:
    `_restamp_streaming_chunk_model`
    ([`proxy_server.py#L10061`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L10061)).
  - Anthropic `/v1/messages`: the `message_start` event is restamped
    (`proxy/anthropic_endpoints/streaming_model_restamp.py`).
- [src] The restamp is skipped in three cases (docstring at
  [`#L1640-L1646`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L1640-L1646)):
  - **After a fallback**, i.e. `x-litellm-attempted-fallbacks > 0`, the body keeps the model that
    actually answered. On streams it is set to the fallback model group name.
  - Azure Model Router requests.
  - `fastest_response` batch completions.

  An internal metadata flag also turns the restamp off:
  `_complexity_router_return_raw_model_name`
  ([`constants.py#L1685`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/constants.py#L1685)).
- [doc] The [response headers page](https://docs.litellm.ai/docs/proxy/response_headers) agrees.
  It says the body `model` is "often restamped to match the client" and that the headers
  identify the deployment that answered.
- **Consequence.** With no fallback, the body `model` always equals what the Worker sent.
  It carries no evidence about which upstream model answered. The upstream's own model string,
  such as a dated snapshot id, is overwritten. I found no response field that preserves it.
  [src; "no field preserves it" is unverified beyond the paths read]

### `x-litellm-*` response headers about identity, retries, fallbacks and cost

All of these are built in `get_custom_headers`
([`common_request_processing.py#L2204-L2299`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L2204-L2299)),
plus router-added `additional_headers`.

| Header | Meaning | Status |
|---|---|---|
| `x-litellm-call-id` | Per-request id. Also `litellm_call_id` in JSON error bodies when `general_settings.include_call_id_in_error_body: true`. | [doc][src] |
| `x-litellm-model-id` | Deployment id (`model_info.id`) that served the request. The docs add "or failed" it. | [doc][src] |
| `x-litellm-model-group` | The routed `model_name`, i.e. the alias. After a fallback it is the group that answered. Set in `Router.set_response_headers` ([`router_python_core.py#L13582`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L13582)). | [doc][src] |
| `x-litellm-model-name` | The deployment's `litellm_params.model`, e.g. `anthropic/claude-…`. This is the operator's configured upstream string, not what the upstream returned ([`#L2884`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L2884)). | [src] only, absent from the headers doc page |
| `x-litellm-model-api-base` | The deployment's API base, query string stripped. | [doc][src] |
| `x-litellm-version` | The LiteLLM version. | [doc][src] |
| `x-litellm-attempted-retries` | Retries made in the answering group. `0` on first-try success ([`router_python_core.py#L9532`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9532), [`#L9635`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9635)). | [doc][src] |
| `x-litellm-max-retries` | The retry budget. Sent only when at least one retry happened. | [doc][src] |
| `x-litellm-attempted-fallbacks` | Fallback hops made. Set to `0` explicitly on primary success ([`#L9415`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9415)). | [doc][src] |
| `x-litellm-fallback-errors` | JSON list of the errors that triggered fallbacks. Needs both the operator flag `general_settings.expose_fallback_errors_to_caller` and request `include_fallback_errors: true` ([`#L2490`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L2490)). | [src] |
| `x-litellm-fallback-reason` | Documented as `rate_limit:<scope>` for rate-limit fallbacks. | [doc]; **not found** as a literal in the 1.109.1 source [unverified] |
| `x-litellm-response-cost` (+ `-input`, `-output`, `-cache-read`, `-cache-creation`, `-reasoning`, `-tool-usage`, `-original`, `-discount-amount`, `-margin-*`) | The proxy-computed cost. See §4. | [doc][src] |
| `x-litellm-key-spend` | The key's total spend including this response. | [doc][src] |
| `x-litellm-cache-key` | Present when the proxy's own response cache was involved. | [src]; that it marks a cache hit is [unverified] |
| `llm_provider-*` | The upstream's own response headers, re-prefixed. | [doc] |

**Caveat on streaming headers.** The operator can set `sse_keepalive_ping_interval_seconds`
(default `None`, so off:
[`__init__.py#L250`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/__init__.py#L250)),
or a per-deployment `keepalive_seconds`. If the upstream is slow, the proxy then opens the SSE
response early with only `Cache-Control` and `X-Accel-Buffering` headers
([`common_request_processing.py#L1423-L1445`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L1423-L1445)),
so a streamed reply may lack every `x-litellm-*` header. [src] The Worker's provider layer
appears to use non-streaming `invoke` today, so this matters only if it ever streams.

## 2. Fallbacks, load balancing and retries

### How they are configured

The fallback lists and order-based failover are documented on the
[reliability](https://docs.litellm.ai/docs/proxy/reliability) page. Load balancing and the retry
budget are on the [load balancing](https://docs.litellm.ai/docs/proxy/load_balancing) page.

**Fallback lists** [doc]
- `fallbacks`: general errors such as rate limits and 5xx.
- `context_window_fallbacks`
- `content_policy_fallbacks`
- `default_fallbacks`: applies to every group that has no specific entry.
- All four are set in `router_settings` / `litellm_settings`.
- `max_fallbacks` bounds the chain. Source default is 5, env `ROUTER_MAX_FALLBACKS`.

**Order-based failover** [doc]
- Deployments in one `model_name` with `order: 1`, `order: 2`, … are tried level by level.

**Load balancing** [doc]
- Several deployments under one `model_name` form a group.
- The default strategy is `simple-shuffle`, a weighted random choice.
- Also available: `least-busy`, `usage-based-routing`, `latency-based-routing`, `cost-based-routing`.

**Weighted failover** [src]
- `enable_weighted_failover` (default `False`) re-picks across the same group on a retryable
  failure.
- It records the re-pick as a fallback
  ([`router_python_core.py#L1636`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L1636)).

**Retries**
- Configured with `num_retries` in `litellm_settings` / `router_settings`.
- **The default disagrees between docs and source:**
  - The load-balancing page says "default retries are 3". [doc]
  - In source, an unset `num_retries` falls back to `openai.DEFAULT_MAX_RETRIES`
    ([`router_python_core.py#L1870-L1875`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L1870-L1875)).
    That constant is `2` in openai-python. [src]
- **A retry may land on a different deployment of the same group.**
  - Each retry calls `make_call` again, which selects a deployment afresh.
  - Deployments that answered a non-retryable status are skipped
    ([`#L9480-L9495`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9480-L9495)).
    [src]
  - The docs add that a 429 puts the failed deployment on cooldown, so retries move to its
    siblings. [doc]

**Retries inside one router attempt** [src]
- Routers default the provider-SDK `max_retries` to `0`
  ([`#L1931`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L1931)).
- A deployment that sets `max_retries` in its `litellm_params` would pass it to the OpenAI SDK
  client. Those retries are not counted in `x-litellm-attempted-retries`. [unverified that the
  per-deployment value reaches the client on the proxy path]

### Can the client turn them off per request?

**Fallbacks: yes, with `"disable_fallbacks": true` in the request body.** [doc][src]
- The docs say it covers "every fallback the proxy would otherwise make", including mid-stream
  fallbacks on `/chat/completions`, `/v1/messages` and `/v1/responses`.
- In source, the router raises the original error before any fallback logic runs when the flag
  is set
  ([`router_python_core.py#L9388`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9388),
  [`#L9106-L9112`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9106-L9112)).
- That early raise also comes before the order-based fallbacks
  ([`#L9128`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9128))
  and weighted failover
  ([`#L9185`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9185)).
- **Per-key override, in either direction** [src]:
  - The key's `metadata.disable_fallbacks` is set via `/key/generate` [doc].
  - When the key carries it as a boolean, it **overwrites** the request body value
    ([`litellm_pre_call_utils.py#L2209-L2211`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/litellm_pre_call_utils.py#L2209-L2211)).
  - So a key with `disable_fallbacks: false` silently cancels a client's `true`.
  - A key with `true` makes the setting independent of the client.
- **Client-supplied fallbacks** [doc][src]:
  - A client can also *add* fallbacks per request (`"fallbacks": [...]` in the body) and
    override `context_window_fallbacks` / `content_policy_fallbacks`
    ([client-side fallbacks](https://docs.litellm.ai/docs/proxy/client_side_fallbacks);
    [`route_llm_request.py#L51-L63`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/route_llm_request.py#L51-L63)).
  - Values in the request body win over key- or team-level `router_settings` overrides
    ([`#L730-L741`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/route_llm_request.py#L730-L741)).

**Retries: yes, with `"num_retries": 0` in the body or the header `x-litellm-num-retries: 0`.**
[src] The docs do not document either form [doc: absent].
- The body value is popped by the router: a request value wins over a deployment's
  `num_retries`, and `0` also skips any retry policy
  ([`router_python_core.py#L9508-L9513`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9508-L9513),
  [`#L9565`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_python_core.py#L9565)).
- The header is parsed in
  [`litellm_pre_call_utils.py#L1689-L1696`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/litellm_pre_call_utils.py#L1689-L1696)
  and applied at
  [`#L1918-L1920`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/litellm_pre_call_utils.py#L1918-L1920).

**Load balancing: no per-request switch.**
- With several deployments under one `model_name`, every request (and every retry) may go to
  any of them.
- What a gateway model name maps to is decided only by the operator's `model_list`.
- `disable_fallbacks` and `num_retries: 0` do not narrow the group. [src; inference from the
  routing code, unverified empirically]

### Detecting them after the fact

**Fallback**
- `x-litellm-attempted-fallbacks > 0`, plus `x-litellm-model-group` naming the group that
  answered. [doc][src]
- On a non-streaming response the body `model` is also left unrestamped, so it differs from the
  requested alias. [src]
- On streams, a mid-stream fallback is signalled on the chunks: their `model` becomes the
  fallback group. A synthetic `litellm-fallback-metadata` SSE event is emitted only with the
  expose/include flags above
  ([`proxy_server.py#L10023-L10058`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L10023-L10058)).
  [src]

**Retry**
- `x-litellm-attempted-retries` and `x-litellm-max-retries`. [doc][src]

**Which deployment answered**
- `x-litellm-model-id`. Pass that value to `/model/info?litellm_model_id=<id>` to get that one
  deployment's record (see §3). [src docstring at
  [`proxy_server.py#L17883-L17890`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L17883-L17890)]
- The deployment id is either the operator's `model_info.id`, or a SHA-256 over the group name
  and every `litellm_params` key and value
  ([`router_api.py#L186-L200`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/router_api.py#L186-L200)).
  So it stays stable while the config entry is unchanged. [src]

**Limits of this detection**
- A retry that moved to a sibling deployment is visible only as `attempted-retries > 0` and a
  final `x-litellm-model-id`. Earlier deployment ids are not in the headers.
- The router records per-attempt deployments in request metadata (`previous_models`), which
  feeds the operator's spend logs, not the response
  ([`proxy/utils.py#L1281-L1416`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/utils.py#L1281-L1416)).
  [src]

**Configured fallbacks (before any request)**
- `GET /v1/models?include_metadata=true&fallback_type=general|context_window|content_policy`
  returns `metadata.fallbacks` per model.
- So a client can see whether the operator configured any fallback for a name
  ([`proxy/utils.py#L10063-L10161`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/utils.py#L10063-L10161)).
  [src; the query parameters are in the endpoint docstring, not on a docs page]

## 3. What `/model/info`, `/v1/model/info`, `/model_group/info` and `/v1/models` return

### `GET /model/info` and `GET /v1/model/info` (same handler)

Handler:
[`proxy_server.py#L17852-L18081`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L17852-L18081).

- Returns `{"data": [ … ]}` with one entry per **deployment**, not per model name. [src]
- Each entry holds:
  - `model_name`
  - `litellm_params`, with credentials removed or masked: `api_key`, cloud secrets, then the
    sensitive-data masker. `api_base` and `model` remain.
  - `model_info`
- Query parameters: `litellm_model_id` (one deployment), `include_team_models`, `teamId`,
  `healthy_only`. [src]
- `model_info` is a merge built by `_enrich_model_info_with_litellm_data`
  ([`#L16196-L16270`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L16196-L16270)):
  - **Operator-set `model_info` keys win.** Any key the operator wrote in `model_info` is kept
    as-is.
  - **LiteLLM's built-in model map fills the rest.** The lookup is
    `litellm.get_model_info(...)` on the deployment's `litellm_params.model`, then `base_model`
    (always for Azure), then the bare model name with the provider split off.
  - **Prices from `litellm_params` are copied in.** Per-deployment prices set there
    (`input_cost_per_token`, …) are stamped into `model_info`.
  - **Overrides are listed.** `model_info.pricing_overrides` lists which pricing fields were
    operator-set.
  - **The source is named.** `model_info.key` names the cost-map entry the built-in values came
    from
    ([`types/utils.py#L4270-L4337`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/types/utils.py#L4270-L4337)).
    [src]
  - **Non-pricing fields have no override marker.** For limits such as `max_input_tokens` a
    client cannot tell whether the operator set them or the map supplied them. [src]
- Fields available from the built-in map (`ModelInfoBase` / `ModelInfo`,
  [`types/utils.py#L315`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/types/utils.py#L315)):
  - Limits: `max_tokens`, `max_input_tokens`, `max_output_tokens`.
  - Prices: `input_cost_per_token`, `output_cost_per_token`, `cache_read_input_token_cost`,
    `cache_creation_input_token_cost`, plus tiered variants (`…_above_200k_tokens`,
    `…_above_1hr`, `…_flex`, `…_priority`, `…_batches`).
  - Capabilities: `supports_function_calling`, `supports_tool_choice`, `supports_reasoning`,
    `supports_prompt_caching`, `supports_vision`, `supports_response_schema`, …
  - Other: `supported_openai_params`, `mode`, `litellm_provider`, `deprecation_date`.
  - Each built-in field is filled only if the bundled map has an entry for that model. [src]
  - Example map entry: `claude-sonnet-4-5` in `model_prices_and_context_window.json` at the tag
    has:
    - `max_input_tokens: 200000` and `max_output_tokens: 64000`
    - input, output, cache-read and cache-write prices
    - `supports_function_calling: true` and `supports_reasoning: true`
- **The built-in map is not pinned to the release by default.**
  - At import, LiteLLM loads the map from `LITELLM_MODEL_COST_MAP_URL`. The default is
    `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`
    ([`__init__.py#L411-L414`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/__init__.py#L411-L414)).
  - It falls back to the bundled copy only on failure, or when `LITELLM_LOCAL_MODEL_COST_MAP=True`.
  - Admin-only `/model/cost_map/source` reports which source was loaded.
  - So the same proxy version can report different limits and prices after a restart. [src]
- Docs coverage [doc]:
  - The [model management](https://docs.litellm.ai/docs/proxy/model_management) page says only
    that `/model/info` "returns the full model list with API keys masked".
  - It also says custom `model_info` fields "pass straight through … alongside the mapped cost
    and context data".
  - It lists no response fields and gives no auth rules.
  - [Custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing) documents the
    `model_info` price overrides.

### Who can read them

- `/model/info`, `/v1/model/info` and `/model_group/info` are in `model_info_routes`, which is
  part of `llm_api_routes`
  ([`_types.py#L547-L565`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/_types.py#L547-L565)).
  So **any virtual key allowed to call the LLM routes can call them**, not only admins.
  [src]
- The list is filtered to the deployments the key or team may use, and to the caller's BYOK
  teams
  ([`proxy_server.py#L17733-L17812`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L17733-L17812),
  [`route_checks.py#L363-L365`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/auth/route_checks.py#L363-L365)).
  [src]
- A key whose `allowed_routes` exclude these paths cannot read them. Whether the owner's Pi
  keys are restricted that way is [unverified].

### `GET /model_group/info`

- One entry per model **name** (`ModelGroupInfo`,
  [`types/router.py#L1043-L1071`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/types/router.py#L1043-L1071)).
- Fields:
  - Limits: `max_input_tokens`, `max_output_tokens`.
  - Prices: `input_cost_per_token`, `output_cost_per_token`. **No cache prices.**
  - Capabilities: `supports_function_calling`, `supports_reasoning`,
    `supported_reasoning_efforts`, `supported_openai_params`, `supports_vision`, …
  - Other: `providers`, `mode`, `tpm`/`rpm`.
- Boolean `supports_*` fields default to `false` when unknown. That is indistinguishable from
  "known not to support". [src]

### `GET /v1/models` and `GET /models`

Handler:
[`proxy_server.py#L12397`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/proxy_server.py#L12397).

- The OpenAI list shape: `id`, `object`, `created`, `owned_by`. [src]
- 1.109.1 adds `mode`, `max_input_tokens` and `max_output_tokens` where known.
- With `include_metadata=true` it also returns `metadata.fallbacks`. [src]
- **When a name mixes models, the reported limit is the widest one.** If a name has
  deployments of different models, the limit reported is the **largest** any deployment
  declares. The source argues this deliberately
  ([`proxy/utils.py#L10041-L10056`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/utils.py#L10041-L10056)).
  `/model_group/info` aggregates the same way. [src]
- The docs say `/v1/models` exists "for compatibility" and point to `/model/info` for details.
  [src docstring]

## 4. Does the proxy report its own cost?

**Yes, per response, in headers.** [doc][src]
- Headers: `x-litellm-response-cost`, plus a breakdown into input, output, cache-read,
  cache-creation, reasoning and tool-usage.
- The docs say the components are sent only on non-streaming responses. Cache and reasoning
  components are sent only when non-zero. Reasoning is already inside output.
  ([response headers](https://docs.litellm.ai/docs/proxy/response_headers),
  [`common_request_processing.py#L1875-L1902`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L1875-L1902)).

**Response body**
- It carries no cost by default.
- LiteLLM has `include_cost_in_streaming_usage` (default `False`,
  [`__init__.py#L356`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/__init__.py#L356)).
- The docs pages I read do not document it. [src; behaviour unverified]

**How the cost is computed**
- It is **LiteLLM's own computation, not a provider-billed figure.**
- Inputs: the usage the upstream returned, priced with the cost map overridden by the
  deployment's `model_info` / `litellm_params` prices
  ([custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing): "the model cost map,
  overridden by `model_info` values").
- It therefore inherits the unpinned remote cost map (§3) unless the operator overrides prices.
  [doc][src]

**It can differ from what the provider bills** [src]
- **Proxy cache hit.** The header reports `0.0` when the response came from the proxy's own
  cache
  ([`#L1876-L1877`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/common_request_processing.py#L1876-L1877)).
- **Billed earlier attempts.** Earlier retry attempts that the provider also billed, notably
  timeouts, are written as separate spend-log rows. They are not included in the response's
  cost header
  ([`proxy/utils.py#L1281-L1416`](https://github.com/BerriAI/litellm/blob/v1.109.1/litellm/proxy/utils.py#L1281-L1416)).
  That they are excluded from the header is my reading of the code [unverified].

**Usage itself**
- The proxy passes through the upstream usage, translated to the wire format the client spoke.
- Which cache-token fields survive translation for a non-native upstream (e.g. an Anthropic
  model behind `/v1/chat/completions`) was not examined here [unverified].
- That question belongs to the Price Table item in the map's "Not yet specified" list.

## Open points to check empirically on the owner's proxy

These need no paid calls beyond one or two cheap requests:

1. Send one request to the gateway name and record:
   - the full response headers
   - the body `model`
   - `x-litellm-model-id` / `-model-group` / `-model-name`
2. Confirm that `"disable_fallbacks": true` and `x-litellm-num-retries: 0` are accepted. Check
   whether the Pi's keys carry `metadata.disable_fallbacks`.
3. With the Worker's key, call:
   - `GET /model/info?litellm_model_id=<id>`
   - `GET /model_group/info`
   - `GET /v1/models?include_metadata=true`

   Confirm that the key may read them and which limit and price fields are filled.
4. Check whether the Pi proxy loads the remote cost map: whether `LITELLM_LOCAL_MODEL_COST_MAP`
   is set.
5. Check whether any `model_name` on the Pi has more than one deployment, which means load
   balancing is live.
