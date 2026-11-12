# The owner's LiteLLM Proxy as the test gateway (issue #320)

Empirical checks for the map
[Design: LiteLLM Proxy as a Model Gateway for Pinned Models](https://github.com/lbacik/coding-agent/issues/317),
run on 2026-11-12 against the owner's LiteLLM Proxy (Raspberry Pi 5). The questions come from the
open points of the research notes for
[issue #319](https://github.com/lbacik/coding-agent/blob/research/litellm-model-identity/docs/research/2026-11-12-issue-319-litellm-model-identity.md)
and §8 of the note for
[issue #318](https://github.com/lbacik/coding-agent/blob/research/litellm-wire-surfaces/docs/research/2026-11-12-issue-318-litellm-wire-surfaces.md).
The answers and their consequences are recorded in the resolution comment of issue #320.

## Gateway under test

- LiteLLM `1.104.0` (`x-litellm-version` header and `/openapi.json`). The research notes read
  `1.109.1`; features newer than `1.104.0` (e.g. `reasoning_effort_levels`, `1.108.0`) are absent.
- One model, `muse-spark-1.3-contributor`, one deployment, no fallbacks:

  ```yaml
  model_list:
    - model_name: muse-spark-1.3-contributor
      litellm_params:
        model: openai/muse-spark-1.3-contributor
        api_base: https://api.meta.ai/v1
        api_key: os.environ/META_API_KEY

  litellm_settings:
    drop_params: true
  ```

  No `router_settings`, `modify_params`, `reasoning_effort_levels`, `encrypted_content_affinity`,
  `num_retries` or `fallbacks`. `LITELLM_LOCAL_MODEL_COST_MAP` is not set, so the remote cost map
  is loaded. A PostgreSQL database is attached (virtual keys, spend logs); `store_model_in_db` is not
  set, so models come only from the config file.
- Virtual key `coding-agent-map-317`, limited to that model, `max_budget` 5 USD, no metadata
  `disable_fallbacks`.

## Reproduce

```shell
set -a; . ~/.config/coding-agent/litellm.env; set +a   # LITELLM_PROXY_URL, LITELLM_PROXY_API_KEY
python3 -I probe.py identity out
python3 -I probe.py no_fallback out
python3 -I probe.py info out <x-litellm-model-id>
python3 -I probe.py forced_tool out
python3 -I probe.py effort out
python3 -I probe.py tool_loop out
```

`responses/` holds the recorded responses. Reasoning blobs are elided and the key spend header is
dropped; nothing else is changed.
