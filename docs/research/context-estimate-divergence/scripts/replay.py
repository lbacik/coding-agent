"""Replay recorded requests through the providers' token-count endpoints.

For a sample of requests from v2-format transcripts, rebuild the LangChain
message list and count it with:
- Anthropic ``messages.count_tokens`` (via ChatAnthropic.get_num_tokens_from_messages),
- OpenAI ``POST /v1/responses/input_tokens`` (payload built by ChatOpenAI).

Modes:
- ``faithful``: assistant messages keep their recorded content blocks (used for
  Anthropic transcripts replayed to Anthropic, to check count_tokens against the
  recorded usage of the very same request).
- ``neutral``: assistant messages are reduced to their text plus tool calls
  (provider-specific reasoning blocks dropped), so a transcript recorded on one
  model can be counted by another provider.

The runtime estimate is recomputed on the replayed message list, exactly as
``estimate_context_tokens`` does, so the ratio compares like with like.

Usage: python replay.py MODE PROVIDER EVERY OUT_CSV TOOLS_JSON LABEL=TRANSCRIPT ...
Keys come from ANTHROPIC_API_KEY / OPENAI_API_KEY in the environment.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

sys.path.insert(0, os.path.dirname(__file__))
from analyse import dumps, message_json  # noqa: E402

ANTHROPIC_MODEL = "claude-sonnet-5"
OPENAI_MODEL = "gpt-5.6-luna"


def neutral_ai(msg: dict) -> dict:
    content = msg["content"]
    if not isinstance(content, str):
        content = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return {"type": "ai", "content": content, "tool_calls": msg.get("tool_calls") or []}


def to_lc(msg: dict):
    t = msg["type"]
    if t == "system":
        return SystemMessage(content=msg["content"])
    if t == "human":
        return HumanMessage(content=msg["content"])
    if t == "tool":
        return ToolMessage(content=msg["content"], tool_call_id=msg["tool_call_id"])
    calls = [
        {"name": c["name"], "args": c.get("args") or {}, "id": c["id"], "type": "tool_call"}
        for c in msg.get("tool_calls") or []
    ]
    return AIMessage(content=msg["content"], tool_calls=calls)


def iter_requests(path: str, mode: str):
    """Yield (conversation label, seq, recorded usage, message dicts, tool names)."""
    convs: dict[tuple, list[dict]] = {}
    last_key: dict[object, tuple] = {}
    pending: dict[tuple, tuple] = {}
    for line in open(path):
        rec = json.loads(line)
        if rec["kind"] == "request":
            key = (rec.get("conversation"), tuple(rec.get("tools") or ()))
            last_key[rec.get("conversation")] = key
            new = rec.get("new_messages") or []
            if any(m["type"] == "system" for m in new) or key not in convs:
                convs[key] = []
            convs[key].extend(new)
            pending[key] = (rec["seq"], list(convs[key]), list(rec["tools"]))
        elif rec["kind"] == "response":
            key = last_key[rec.get("conversation")]
            msg = rec["message"]
            seq, msgs, tools = pending.pop(key)
            label = f"{key[0]}/{len(key[1])}tools"
            yield label, seq, msg.get("usage") or {}, msgs, tools
            convs[key].append(msg if mode == "faithful" else neutral_ai(msg))


def main() -> None:
    mode, provider, every, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    schemas = {t["name"]: t for t in json.load(open(sys.argv[5]))}
    schemas.setdefault("run_check", {"name": "run_check", "args": {"name": {"title": "Name", "type": "string"}},
                                     "openai": {"type": "function", "function": {"name": "run_check", "description": "Run one Validation Contract check, diagnostic only.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}}})
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        model = ChatAnthropic(model=ANTHROPIC_MODEL, max_retries=6)
    else:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(model=OPENAI_MODEL, use_responses_api=True, store=False,
                           include=["reasoning.encrypted_content"], reasoning_effort="medium")
        client = httpx.Client(timeout=120, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    rows = []
    for spec in sys.argv[6:]:
        label, path = spec.split("=", 1)
        reqs = list(iter_requests(path, mode))
        picks = set(range(0, len(reqs), every)) | {len(reqs) - 1}
        for i, (conv, seq, usage, msgs, tool_names) in enumerate(reqs):
            if i not in picks:
                continue
            if mode == "neutral":
                msgs = [neutral_ai(m) if m["type"] == "ai" else m for m in msgs]
            est_text = "".join(message_json(m) for m in msgs) + dumps([schemas[n]["args"] for n in tool_names])
            estimate = max(1, len(est_text.encode()) // 4)
            lc = [to_lc(m) for m in msgs]
            tools = [schemas[n]["openai"] for n in tool_names]
            if provider == "anthropic":
                kwargs = {"thinking": {"type": "adaptive"}} if mode == "faithful" else {}
                counted = model.get_num_tokens_from_messages(lc, tools=tools, **kwargs)
                source = "count_tokens"
            else:
                payload = model._get_request_payload(lc, tools=tools)
                body = {k: v for k, v in payload.items() if k in ("model", "input", "instructions", "tools", "reasoning", "tool_choice", "text")}
                for attempt in range(6):
                    resp = client.post("https://api.openai.com/v1/responses/input_tokens", json=body)
                    if resp.status_code != 429:
                        break
                    time.sleep(5 * (attempt + 1))
                if resp.status_code != 200:
                    print(label, seq, resp.status_code, resp.text[:500], file=sys.stderr)
                    continue
                counted = resp.json()["input_tokens"]
                source = "responses/input_tokens"
            row = {
                "source": label, "conversation": conv, "seq": seq, "messages": len(msgs),
                "bytes": len(est_text.encode()), "estimate": estimate,
                "counted": counted, "count_source": source,
                "recorded_input_tokens": usage.get("input_tokens", ""),
                "ratio_est_to_counted": round(estimate / counted, 4),
                "bytes_per_token": round(len(est_text.encode()) / counted, 3),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
            time.sleep(0.3)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
