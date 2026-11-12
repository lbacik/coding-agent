"""Empirical checks on the owner's LiteLLM Proxy for map #317 / ticket #320.

Reads LITELLM_PROXY_URL and LITELLM_PROXY_API_KEY from the environment. Never prints the key.
Usage: python3 probe.py <check> [out_dir]
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["LITELLM_PROXY_URL"].rstrip("/")
KEY = os.environ["LITELLM_PROXY_API_KEY"]
MODEL = "muse-spark-1.3-contributor"
OUT = sys.argv[2] if len(sys.argv) > 2 else "."

TOOL_ANTHROPIC = {
    "name": "record_answer",
    "description": "Record the final answer.",
    "input_schema": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
}
TOOL_OPENAI_CHAT = {
    "type": "function",
    "function": {
        "name": "record_answer",
        "description": "Record the final answer.",
        "parameters": TOOL_ANTHROPIC["input_schema"],
    },
}
TOOL_RESPONSES = {
    "type": "function",
    "name": "record_answer",
    "description": "Record the final answer.",
    "parameters": TOOL_ANTHROPIC["input_schema"],
}
TOOL_LOOKUP = {
    "name": "lookup",
    "description": "Look up the capital city of a country.",
    "input_schema": {
        "type": "object",
        "properties": {"country": {"type": "string"}},
        "required": ["country"],
    },
}


def call(method, path, body=None, headers=None):
    h = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, hdrs, raw = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, dict(e.headers), e.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw.decode(errors="replace")
    return status, hdrs, payload


def litellm_headers(hdrs):
    return {k: v for k, v in hdrs.items() if k.lower().startswith("x-litellm")}


def dump(name, status, hdrs, payload):
    rec = {"status": status, "headers": hdrs, "body": payload}
    with open(os.path.join(OUT, f"{name}.json"), "w") as f:
        json.dump(rec, f, indent=2)
    print(f"--- {name}: HTTP {status}")
    print(json.dumps(litellm_headers(hdrs), indent=2))


def summary_messages(payload):
    if not isinstance(payload, dict):
        return payload
    return {
        "model": payload.get("model"),
        "stop_reason": payload.get("stop_reason"),
        "content_types": [c.get("type") for c in payload.get("content", [])],
        "content": payload.get("content"),
        "usage": payload.get("usage"),
        "error": payload.get("error"),
    }


def check_identity():
    body = {
        "model": MODEL,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
    }
    s, h, p = call("POST", "/v1/messages", body)
    dump("identity_messages", s, h, p)
    print(json.dumps(summary_messages(p), indent=2))


def check_no_fallback():
    body = {
        "model": MODEL,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "disable_fallbacks": True,
    }
    s, h, p = call("POST", "/v1/messages", body, {"x-litellm-num-retries": "0"})
    dump("no_fallback_messages", s, h, p)
    print(json.dumps(summary_messages(p), indent=2))


def check_info(model_id):
    for name, path in [
        ("model_info", f"/model/info?litellm_model_id={model_id}"),
        ("model_info_all", "/model/info"),
        ("model_group_info", "/model_group/info"),
        ("v1_models_meta", "/v1/models?include_metadata=true"),
        ("key_info", "/key/info"),
    ]:
        s, h, p = call("GET", path)
        rec = {"status": s, "body": p}
        with open(os.path.join(OUT, f"{name}.json"), "w") as f:
            json.dump(rec, f, indent=2)
        text = json.dumps(p, indent=2)
        print(f"--- {name}: HTTP {s}\n{text[:3000]}")


def check_forced_tool():
    prompt = "Say hello to me in one short sentence."
    # 1. /v1/messages, forced tool
    s, h, p = call(
        "POST",
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 200,
            "tools": [TOOL_ANTHROPIC],
            "tool_choice": {"type": "tool", "name": "record_answer"},
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    dump("forced_tool_messages", s, h, p)
    print(json.dumps(summary_messages(p), indent=2))
    # 2. /v1/chat/completions, forced tool
    s, h, p = call(
        "POST",
        "/v1/chat/completions",
        {
            "model": MODEL,
            "max_tokens": 200,
            "tools": [TOOL_OPENAI_CHAT],
            "tool_choice": {"type": "function", "function": {"name": "record_answer"}},
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    dump("forced_tool_chat", s, h, p)
    if isinstance(p, dict) and p.get("choices"):
        ch = p["choices"][0]
        print(json.dumps({"model": p.get("model"), "finish_reason": ch.get("finish_reason"), "message": ch.get("message"), "usage": p.get("usage")}, indent=2))
    else:
        print(json.dumps(p, indent=2)[:2000])
    # 3. /v1/responses, forced tool
    s, h, p = call(
        "POST",
        "/v1/responses",
        {
            "model": MODEL,
            "max_output_tokens": 200,
            "tools": [TOOL_RESPONSES],
            "tool_choice": {"type": "function", "name": "record_answer"},
            "input": prompt,
        },
    )
    dump("forced_tool_responses", s, h, p)
    if isinstance(p, dict) and "output" in p:
        print(json.dumps({"model": p.get("model"), "status": p.get("status"), "output_types": [o.get("type") for o in p["output"]], "output": p["output"], "usage": p.get("usage")}, indent=2)[:3000])
    else:
        print(json.dumps(p, indent=2)[:2000])


def check_effort():
    # /v1/messages to a non-native upstream with output_config.effort and no thinking.
    s, h, p = call(
        "POST",
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 32,
            "output_config": {"effort": "not-an-effort-level"},
            "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        },
    )
    dump("effort_invalid_messages", s, h, p)
    print(json.dumps(summary_messages(p), indent=2))


def check_tool_loop():
    # Two-turn tool loop on /v1/messages with thinking enabled; the assistant turn is replayed verbatim.
    first = {
        "model": MODEL,
        "max_tokens": 2048,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "tools": [TOOL_LOOKUP],
        "messages": [{"role": "user", "content": "Use the lookup tool to find the capital of Poland, then answer in one word."}],
    }
    s, h, p = call("POST", "/v1/messages", first)
    dump("loop_turn1_messages", s, h, p)
    print(json.dumps(summary_messages(p), indent=2)[:3000])
    if s != 200 or not isinstance(p, dict):
        return
    tool_uses = [c for c in p.get("content", []) if c.get("type") == "tool_use"]
    if not tool_uses:
        print("no tool_use in turn 1")
        return
    second = dict(first)
    second["messages"] = first["messages"] + [
        {"role": "assistant", "content": p["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_uses[0]["id"], "content": "Warsaw"}]},
    ]
    s, h, p = call("POST", "/v1/messages", second)
    dump("loop_turn2_messages", s, h, p)
    print(json.dumps(summary_messages(p), indent=2)[:3000])


if __name__ == "__main__":
    check = sys.argv[1]
    if check == "info":
        check_info(sys.argv[3])
    else:
        globals()[f"check_{check}"]()
