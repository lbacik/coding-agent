"""Probe the proposed forced-tool check of the Provider Capability Assertion (#323).

The proposed check binds the probe tool with a forced ``tool_choice`` and sends a
prompt that asks for *no* tool call. Only a forced choice that reaches the model
and is honoured produces a ``capability_probe`` call; a forced choice dropped on
the way (``drop_params``, an Ollama upstream) produces text. A control call sends
the same prompt with no ``tool_choice`` and should produce text, which shows the
prompt discriminates.

Usage (from repo root): uv run python <this> <pin> ; pins: anthropic | openai | gateway
Direct pins use the Worker's own adapter (``build_chat_model``) and the pins in
``coding_agent.provider.config``. The gateway pin reads LITELLM_PROXY_URL /
LITELLM_PROXY_API_KEY from env and uses the adapter shape decided in #322.
Never prints or records a key.
"""

import json
import logging
import os
import sys
import warnings

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool

from coding_agent.provider.config import PINNED_MODELS
from coding_agent.provider.pinned_model import build_chat_model

warnings.simplefilter("always")
logging.basicConfig(level=logging.WARNING)
logging.captureWarnings(True)

OUT = os.path.dirname(os.path.abspath(__file__))
GATEWAY_MODEL = "muse-spark-1.3-contributor"
CONTRARY_PROMPT = "Reply with the single word OK. Do not call any tool."
SECRETS = [v for k, v in os.environ.items() if k.endswith("API_KEY") and v]
WIRE: list[dict] = []


@tool
def capability_probe(city: str) -> str:
    """A harmless tool the Provider Capability Assertion asks the Pinned Model to call."""
    return f"probe received {city!r}"


def _on_request(req: httpx.Request) -> None:
    body = req.content.decode() if req.content else None
    try:
        body = json.loads(body) if body else None
    except ValueError:
        pass
    WIRE.append({"request": {"url": str(req.url), "body": body}})


def _on_response(resp: httpx.Response) -> None:
    resp.read()
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    WIRE[-1]["response"] = {
        "status": resp.status_code,
        "x-litellm": {k: v for k, v in resp.headers.items() if k.lower().startswith("x-litellm")},
        "body": body,
    }


def make_model(pin_name: str):
    if pin_name == "gateway":
        client = httpx.Client(event_hooks={"request": [_on_request], "response": [_on_response]}, timeout=180)
        return "medium", init_chat_model(
            f"openai:{GATEWAY_MODEL}",
            reasoning_effort="medium",
            api_key=os.environ["LITELLM_PROXY_API_KEY"],
            base_url=os.environ["LITELLM_PROXY_URL"].rstrip("/") + "/v1",
            use_responses_api=True,
            store=False,
            include=["reasoning.encrypted_content"],
            http_client=client,
            max_retries=0,
        )
    pin = PINNED_MODELS[pin_name]
    key = os.environ["ANTHROPIC_API_KEY" if pin_name == "anthropic" else "OPENAI_API_KEY"]
    return pin.key, build_chat_model(pin, api_key=key)


def run(label: str, model, tool_choice):
    bound = model.bind_tools([capability_probe], tool_choice=tool_choice) if tool_choice else model.bind_tools([capability_probe])
    record: dict = {"case": label, "tool_choice_arg": tool_choice}
    try:
        payload = bound.bound._get_request_payload(CONTRARY_PROMPT, **bound.kwargs)
        record["payload_tool_choice"] = payload.get("tool_choice")
        record["payload_thinking"] = payload.get("thinking")
        record["payload_effort"] = payload.get("output_config") or payload.get("reasoning")
    except Exception as exc:  # payload introspection is best effort
        record["payload_error"] = repr(exc)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            msg = bound.invoke(CONTRARY_PROMPT)
            record["outcome"] = "response"
            record["tool_calls"] = [{"name": c["name"], "args": c["args"]} for c in msg.tool_calls]
            record["text"] = msg.text() if callable(getattr(msg, "text", None)) else str(msg.content)
            record["usage"] = msg.usage_metadata
            record["model"] = msg.response_metadata.get("model_name") or msg.response_metadata.get("model")
        except Exception as exc:
            record["outcome"] = "error"
            record["error_class"] = type(exc).__name__
            record["error"] = str(exc)[:600]
    record["warnings"] = [str(w.message) for w in caught]
    return record


def main() -> None:
    pin_name = sys.argv[1]
    key, model = make_model(pin_name)
    results = {
        "pin": key,
        "prompt": CONTRARY_PROMPT,
        "cases": [
            run("forced", model, "capability_probe"),
            run("control-auto", model, None),
        ],
    }
    if WIRE:
        results["wire"] = WIRE
    text = json.dumps(results, indent=2, default=str)
    for secret in SECRETS:
        assert secret not in text
    path = os.path.join(OUT, f"{pin_name}.json")
    with open(path, "w") as f:
        f.write(text + "\n")
    for case in results["cases"]:
        print(json.dumps({k: case.get(k) for k in ("case", "payload_tool_choice", "payload_thinking", "outcome", "tool_calls", "text", "error_class", "error", "warnings")}, default=str))
    print("wrote", path)


if __name__ == "__main__":
    main()
