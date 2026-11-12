"""Probe the LiteLLM gateway's /v1/responses through the Worker's adapter path.

Usage (from repo root): uv run python <this> <check> ; checks: loop | badeffort
Reads LITELLM_PROXY_URL / LITELLM_PROXY_API_KEY from env. Never prints the key.
"""

import json
import logging
import os
import sys
import warnings

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

warnings.simplefilter("always")
logging.basicConfig(level=logging.WARNING)
logging.captureWarnings(True)

OUT = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ["LITELLM_PROXY_URL"].rstrip("/")
KEY = os.environ["LITELLM_PROXY_API_KEY"]
MODEL = "muse-spark-1.3-contributor"
CAPTURES: list[dict] = []


def _redact(h):
    return {k: ("<redacted>" if k.lower() in ("authorization", "api-key", "x-api-key") else v) for k, v in h.items()}


def on_request(req: httpx.Request):
    body = req.content.decode() if req.content else None
    try:
        body = json.loads(body) if body else None
    except ValueError:
        pass
    CAPTURES.append({"request": {"method": req.method, "url": str(req.url), "headers": _redact(dict(req.headers)), "body": body}})


def on_response(resp: httpx.Response):
    resp.read()
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    CAPTURES[-1]["response"] = {"status": resp.status_code, "headers": dict(resp.headers), "body": body}
    print(f"HTTP {resp.status_code} {resp.request.url.path}")
    print("  x-litellm headers:", json.dumps({k: v for k, v in resp.headers.items() if k.lower().startswith("x-litellm")}))


def dump(name):
    p = os.path.join(OUT, f"{name}.json")
    with open(p, "w") as f:
        json.dump(CAPTURES, f, indent=2, default=str)
    s = open(p).read()
    assert KEY not in s
    print("wrote", p)


@tool
def lookup_capital(country: str) -> str:
    """Look up the capital city of a country."""
    return {"france": "Paris"}.get(country.lower(), "unknown")


def make_model(effort, **extra):
    client = httpx.Client(event_hooks={"request": [on_request], "response": [on_response]}, timeout=180)
    return init_chat_model(
        f"openai:{MODEL}",
        reasoning_effort=effort,
        api_key=KEY,
        use_responses_api=True,
        base_url=f"{BASE}/v1",
        http_client=client,
        max_retries=0,
        **extra,
    )


def summarize(msg):
    blocks = msg.content if isinstance(msg.content, list) else [msg.content]
    out = []
    for b in blocks:
        if isinstance(b, dict):
            out.append({k: (v if k in ("type", "id", "name") else (f"<{type(v).__name__} len={len(v)}>" if isinstance(v, (str, list)) else v)) for k, v in b.items()})
        else:
            out.append(f"<str len={len(b)}>")
    return {
        "type": type(msg).__name__,
        "content_blocks_summary": out,
        "tool_calls": getattr(msg, "tool_calls", None),
        "usage_metadata": getattr(msg, "usage_metadata", None),
        "response_metadata": msg.response_metadata,
        "additional_kwargs_keys": list(msg.additional_kwargs.keys()),
        "output_version": msg.response_metadata.get("output_version"),
    }


def loop():
    llm = make_model("medium")
    print("model class:", type(llm).__name__, "output_version:", getattr(llm, "output_version", None),
          "include:", getattr(llm, "include", None), "store:", getattr(llm, "store", None),
          "max_tokens:", getattr(llm, "max_tokens", None), "use_previous_response_id:", getattr(llm, "use_previous_response_id", None))
    bound = llm.bind_tools([lookup_capital])
    msgs = [HumanMessage("What is the capital of France? You must use the lookup_capital tool to find out, then answer in one sentence.")]
    ai1 = bound.invoke(msgs, max_tokens=1024)
    s1 = summarize(ai1)
    print("TURN1:", json.dumps(s1, indent=2, default=str))
    if not ai1.tool_calls:
        dump("loop")
        print("no tool call; stopping")
        return
    tc = ai1.tool_calls[0]
    msgs += [ai1, ToolMessage(lookup_capital.invoke(tc["args"]), tool_call_id=tc["id"])]
    try:
        ai2 = bound.invoke(msgs, max_tokens=1024)
        s2 = summarize(ai2)
        print("TURN2:", json.dumps(s2, indent=2, default=str))
        print("TURN2 text:", ai2.text if isinstance(ai2.text, str) else ai2.text())
    except Exception as e:
        print("TURN2 EXC:", type(e).__name__, type(e).__mro__[:4], str(e)[:2000])
        s2 = {"exception": type(e).__name__, "message": str(e)}
    with open(os.path.join(OUT, "loop_messages.json"), "w") as f:
        json.dump({"turn1": s1, "turn1_raw_content": ai1.content, "turn2": s2}, f, indent=2, default=str)
    dump("loop")


def badeffort():
    llm = make_model("medium")
    bound = llm.bind_tools([lookup_capital])
    try:
        r = bound.invoke("What is the capital of France? Use the lookup_capital tool.",
                         reasoning_effort="not-an-effort-level", max_tokens=1024)
        print("ACCEPTED:", json.dumps(summarize(r), indent=2, default=str))
    except Exception as e:
        from langchain_core.exceptions import ModelInvalidRequestError
        print("EXC:", type(e).__name__, "is ModelInvalidRequestError:", isinstance(e, ModelInvalidRequestError))
        print("MRO:", [c.__name__ for c in type(e).__mro__])
        print("MSG:", str(e)[:2000])
    dump("badeffort")


def encrypted():
    llm = make_model("medium", store=False, include=["reasoning.encrypted_content"])
    bound = llm.bind_tools([lookup_capital])
    msgs = [HumanMessage("What is the capital of France? You must use the lookup_capital tool to find out, then answer in one sentence.")]
    ai1 = bound.invoke(msgs, max_tokens=1024)
    s1 = summarize(ai1)
    print("TURN1:", json.dumps(s1, indent=2, default=str))
    if ai1.tool_calls:
        tc = ai1.tool_calls[0]
        msgs += [ai1, ToolMessage(lookup_capital.invoke(tc["args"]), tool_call_id=tc["id"])]
        try:
            ai2 = bound.invoke(msgs, max_tokens=1024)
            print("TURN2:", json.dumps(summarize(ai2), indent=2, default=str))
        except Exception as e:
            print("TURN2 EXC:", type(e).__name__, str(e)[:2000])
    dump("encrypted")


def chat_badeffort():
    c = httpx.Client(event_hooks={"request": [on_request], "response": [on_response]}, timeout=180)
    c.post(f"{BASE}/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json={
        "model": MODEL, "max_completion_tokens": 1024, "reasoning_effort": "not-an-effort-level",
        "messages": [{"role": "user", "content": "Say hi."}]})
    dump("chat_badeffort")


if __name__ == "__main__":
    {"loop": loop, "badeffort": badeffort, "encrypted": encrypted, "chat_badeffort": chat_badeffort}[sys.argv[1]]()
