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
  and answers with a `capability_probe` call under the pinned effort.

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
