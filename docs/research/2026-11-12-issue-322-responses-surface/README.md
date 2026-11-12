# The Responses surface through the owner's LiteLLM Proxy (#322)

Evidence for [Which wire protocol does the Worker speak to a Model Gateway?](https://github.com/lbacik/coding-agent/issues/322).

- **Gateway:** the owner's LiteLLM Proxy `1.104.0`, set up in [#320](https://github.com/lbacik/coding-agent/issues/320). One model, `muse-spark-1.3-contributor` (Meta, LiteLLM provider `openai/`), `drop_params: true`. The host is redacted as `<gateway-host>`. The key never appears in these files.
- **Client:** the Worker's own adapter path, `init_chat_model("openai:<model>", reasoning_effort=…, api_key=…, use_responses_api=True, base_url=<gateway>/v1)`. Versions: langchain-openai 1.7.4, openai 3.13.0, langchain-core 1.8.0. The probe added `max_retries=0` and a call-time `max_tokens=1024`, which reached the wire as `max_output_tokens`.
- **Cost:** 6 paid calls. The run is `probe_responses.py`, with the env from `~/.config/coding-agent/litellm.env`.

## Findings

1. **Two-turn tool loop (`effort=medium`, `tool_choice` auto), default settings: 200 / 200** (`responses/loop.json`, `responses/loop_messages.json`).
   - Turn 1 content blocks: `reasoning`, `text` (`phase: "commentary"`), `function_call`.
   - The reasoning block carries **only an id**, with no `encrypted_content`. ChatOpenAI sends no `include` and no `store`, and the response echoes `store: true`.
   - Turn 2 sends the AIMessage back unchanged and the reasoning item byte-equal. Because that item is only an id, the reasoning lives in server-side state, not in the turn.
2. **The same loop with `store=False, include=["reasoning.encrypted_content"]`: 200 / 200** (`responses/encrypted.json`).
   - The turn-1 reasoning block carries `encrypted_content` (1426 chars), and turn 2 sends it back verbatim.
   - Turn-2 input is 766 tokens with the encrypted reasoning, against 660 with the id only. This suggests the reasoning is consumed.
   - With `store=False`, LangChain drops `id` from `response_metadata`.
3. **Invalid effort on `/v1/responses`: accepted, HTTP 200** (`responses/badeffort.json`).
   - Every `/v1/responses` reply echoes `reasoning.effort: "high"` whatever was sent.
   - On this version LiteLLM forwards `/v1/responses` for an `openai/` deployment to the upstream's own Responses endpoint. It does not bridge it through chat completions, so LiteLLM's own effort validation (described for 1.109.1 in #318) does not apply.
   - The Worker's Provider Capability Assertion would refuse this pin.
4. **Invalid effort on `/v1/chat/completions`: HTTP 400** (`responses/chat_badeffort.json`), from Meta itself: ``reasoning_effort`: unknown variant `not-an-effort-level` ``.
5. **Usage:** `usage_metadata` is non-zero. `cache_read` is reported, and there is no cache-write field. The gateway cost is `0.0`.
6. **Identity headers:**
   - On every 200: `x-litellm-model-name`, `x-litellm-model-group`, `x-litellm-model-id`, `x-litellm-model-api-base`, `x-litellm-attempted-fallbacks: 0`, `x-litellm-attempted-retries: 0` and `x-litellm-version`.
   - On a 400: absent.
