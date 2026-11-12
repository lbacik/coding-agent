# Redacted Model Gateway recordings (LiteLLM Proxy)

Replayed by the fake gateway (`tests/support/fake_gateway.py`) through the
real adapter. Shapes follow traffic observed against
`research/litellm-test-gateway`; every value here is redacted:

- hosts are `gateway.test`, never a real address;
- credentials never appear — the gateway key travels only in the test's
  `Authorization` header, never in a fixture;
- model names are `gateway-test-model`, never the owner's model;
- reasoning blobs are ASCII placeholders, never provider ciphertext;
- ids and timestamps are fixed sentinels.

`litellm_probe_ok_200.json`
: The mutated happy variant: the gateway honours the forced `tool_choice`
  and answers with a `capability_probe` call under the pinned effort,
  carrying the pinned model group with attempted fallbacks and retries `0`.

`litellm_effort_refused_400.json`
: The mutated happy variant's second leg: the gateway refuses the invalid
  effort with a 400 naming `reasoning.effort`.

`litellm_meta_forced_tool_400.json`
: Recorded: a Meta model behind the gateway refuses the forced tool choice
  with a 400. The Provider Capability Assertion must refuse the pin.

`litellm_effort_accepted_200.json`
: Recorded: the gateway accepts the deliberately invalid effort with a 200.
  The Provider Capability Assertion must refuse the pin — the effort setting
  does not reach the provider.

`litellm_turn1_200.json`, `litellm_turn2_200.json`
: A two-turn tool loop. Turn one carries a `reasoning` block with
  `encrypted_content`; turn two answers with text.

`litellm_step_report_200.json`
: Mutated variant answering with a `report_step_result` call and valid
  identity headers (pinned group, fallbacks `0`, retries `0`, model id,
  model name, call id and version).

`litellm_step_report_no_identity_200.json`,
`litellm_step_report_wrong_group_200.json`,
`litellm_step_report_fallbacks_1_200.json`,
`litellm_step_report_retries_2_200.json`
: Mutated variants of the step report: identity headers missing, a group
  other than the pinned name, attempted fallbacks `1`, attempted retries
  `2`. Each is a pin violation through the real adapter.

`litellm_spawn_ok_200.json`, `litellm_subagent_report_200.json`
: Mutated variants with valid identity headers: a `spawn_subagent` call
  and the Sub-agent's `report_subagent` answer.

`litellm_error_retry_500.json`, `litellm_error_fallback_500.json`,
`litellm_error_plain_500.json`
: Synthesised (`"_synthesised": true`), not recorded: no real gateway
  recording of an error carrying retry or fallback evidence exists yet
  (issue #337). The retry variant carries attempted retries `2` under the
  pinned group; the fallback variant carries attempted fallbacks `1` under
  another group; the plain variant carries no identity headers and is
  classified by its status, never a violation. Each replaces with a real
  recording when one is vendored.
