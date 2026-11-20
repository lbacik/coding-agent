"""Tokens per byte by content type, counted by each provider's count endpoint.

Collects the recorded content of each category from v2 transcripts, caps every
category at CAP bytes, counts it as one user message (minus the count of an
empty-ish baseline message), and reports bytes/token both for the raw text and
for the JSON-escaped form the runtime estimate measures.

Usage: python categories.py OUT_JSON TOOLS_JSON LABEL=TRANSCRIPT ...
"""

from __future__ import annotations

import json
import os
import sys

import httpx
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

sys.path.insert(0, os.path.dirname(__file__))
from analyse import CODE_TOOLS, EXEC_TOOLS, SEARCH_TOOLS, dumps  # noqa: E402

CAP = 120_000


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def collect(paths: list[str], tools_json: str) -> dict[str, str]:
    cats: dict[str, list[str]] = {k: [] for k in ("tool_code", "tool_exec", "tool_search", "prefix_prose", "ai_text", "ai_call_args")}
    seen_prefix = set()
    for path in paths:
        names: dict[str, str] = {}
        for line in open(path):
            rec = json.loads(line)
            if rec["kind"] == "response":
                m = rec["message"]
                cats["ai_text"].append(text_of(m["content"]))
                for c in m.get("tool_calls") or []:
                    names[c["id"]] = c["name"]
                    cats["ai_call_args"].append(dumps(c.get("args")))
                continue
            for m in rec.get("new_messages") or []:
                if m["type"] == "tool":
                    n = names.get(m.get("tool_call_id"))
                    cat = "tool_code" if n in CODE_TOOLS else "tool_exec" if n in EXEC_TOOLS else "tool_search" if n in SEARCH_TOOLS else None
                    if cat:
                        cats[cat].append(text_of(m["content"]))
                elif m["type"] in ("system", "human"):
                    t = text_of(m["content"])
                    if t not in seen_prefix:
                        seen_prefix.add(t)
                        cats["prefix_prose"].append(t)
    out = {}
    for k, parts in cats.items():
        s = "\n".join(p for p in parts if p)
        out[k] = s.encode()[:CAP].decode(errors="ignore")
    tools = json.load(open(tools_json))
    out["tool_schemas_json"] = dumps([t["openai"] for t in tools])
    return out


def main() -> None:
    out_path, tools_json = sys.argv[1], sys.argv[2]
    samples = collect([s.split("=", 1)[1] for s in sys.argv[3:]], tools_json)
    anthropic = ChatAnthropic(model="claude-sonnet-5", max_retries=6)
    openai = ChatOpenAI(model="gpt-5.6-luna", use_responses_api=True, store=False)
    client = httpx.Client(timeout=120, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})

    def count_openai(text: str) -> int:
        body = {"model": "gpt-5.6-luna", "input": [{"role": "user", "content": text}]}
        r = client.post("https://api.openai.com/v1/responses/input_tokens", json=body)
        r.raise_for_status()
        return r.json()["input_tokens"]

    def count_anthropic(text: str) -> int:
        return anthropic.get_num_tokens_from_messages([HumanMessage(content=text)])

    base = {"anthropic": count_anthropic("."), "openai": count_openai(".")}
    result = {"baseline": base, "categories": {}}
    for k, text in samples.items():
        raw = len(text.encode())
        escaped = len(json.dumps(text).encode())  # what the estimate sees (ensure_ascii, escapes)
        row = {"raw_bytes": raw, "json_bytes": escaped, "non_ascii_chars": sum(1 for ch in text if ord(ch) > 127)}
        for prov, fn in (("anthropic", count_anthropic), ("openai", count_openai)):
            tok = fn(text) - base[prov]
            row[prov] = {"tokens": tok, "raw_bytes_per_token": round(raw / tok, 3), "json_bytes_per_token": round(escaped / tok, 3), "estimate_over_tokens": round((escaped // 4) / tok, 3)}
        result["categories"][k] = row
        print(k, json.dumps(row), flush=True)
    json.dump(result, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
