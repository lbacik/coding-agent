# Forced-tool capability probe on direct pins and through the owner's gateway (#323)

Evidence for [How does the Provider Capability Assertion keep its meaning through a Model Gateway?](https://github.com/lbacik/coding-agent/issues/323).
Run 2026-11-12 from `main` at `a391fc7` (langchain-anthropic 1.7.10, langchain-openai 1.7.4), LiteLLM Proxy 1.104.0 on the owner's LAN. Six paid calls.

## The check under test

The probe tool is bound with a **forced** `tool_choice` naming it, and the prompt asks for **no** tool call:
`Reply with the single word OK. Do not call any tool.`
A `capability_probe` call can only come from a forced choice that reached the model and was honoured. A forced choice dropped on the way (`drop_params`, an Ollama upstream) would yield text instead. A control call sends the same prompt with no `tool_choice`. It must yield text, which shows the prompt discriminates.

Script: [`probe_forced_tool.py`](probe_forced_tool.py). Records: [`anthropic.json`](anthropic.json), [`openai.json`](openai.json), [`gateway.json`](gateway.json) (gateway address redacted).

## Results

| Pin | Forced (contrary prompt) | Control, `auto` |
| --- | --- | --- |
| `anthropic:claude-sonnet-5@messages`, effort `medium`, via `build_chat_model` | `capability_probe(city="OK")`, no text, 630 tokens | text `OK`, no tool call, 474 tokens |
| `openai:gpt-5.6-luna@responses`, effort `medium`, via `build_chat_model` | `capability_probe(city="unknown")`, no text, 111 tokens | text `OK`, no tool call, 80 tokens |
| `litellm:muse-spark-1.3-contributor@responses` (Meta upstream), effort `medium`, `store: false` + encrypted reasoning | **HTTP 400** from Meta via LiteLLM: "only `auto` is supported for `tool_choice` … named function choices are not currently supported"; no `x-litellm-*` headers | text `OK`, no tool call, 724 tokens |

## Observations

- **The contrary prompt discriminates on every pin that answered.** Each model answered the prompt as asked when not forced, and called the tool when forced. A forced choice silently downgraded to `auto` would therefore show up as text, and the assertion would refuse it.
- **Anthropic accepts a forced tool together with the payload-level adaptive thinking.** `ChatAnthropic` sends `thinking: {type: adaptive, display: summarized}` for `claude-sonnet-5` (not the constructor's `thinking`, so `bind_tools` keeps the forced choice), and the API honours both. #318 left this open. It also confirms that the production activation call (contract MVP §8) works on the direct Anthropic pin.
- **Meta behind the gateway fails the forced check loudly**, as in #320. Under the new rule this pin is refused at its first probe call. Today's probe (unforced, prompt asking for the tool) would pass it on tool calling and only refuse it later, on the invalid-effort step (#322).
- **Not run:** a native upstream behind the gateway (Anthropic is not registered on it, see #320), and an upstream whose forced choice the gateway drops silently (Ollama, or a model the map flags `supports_forced_tool_use: false` under `drop_params`). The silent-drop case is the one the contrary prompt exists for, and it is covered here only by the control calls.
