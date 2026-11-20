"""Compare the runtime's local context estimate with provider-reported input tokens.

Replays each v2-format transcript.jsonl (request/response records), rebuilds every
conversation's message list, computes the estimate exactly as
``coding_agent.runtime.context_handoff.estimate_context_tokens`` does (UTF-8 bytes
of the sorted, compact JSON of content/tool_calls/additional_kwargs per message,
plus the bound tools' ``args``, integer-divided by 4), and pairs it with the
``input_tokens`` of the response that answered the request.

Usage: python analyse.py TOOLS_JSON OUT_CSV LABEL=TRANSCRIPT [LABEL=TRANSCRIPT ...]

Known approximations (documented in the report):
- The transcript drops ``additional_kwargs``; the estimate uses ``{}`` for it.
- Registered secrets were redacted before the transcript was written.
- ``run_check`` is not built by the test deps; its args schema is added by hand.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict

CODE_TOOLS = {"read_file", "read_lines", "read_change", "read_result_slice"}
SEARCH_TOOLS = {"search_text", "find_files", "list_directory"}
EXEC_TOOLS = {"run_command", "test_targeted", "run_check"}

CATEGORIES = [
    "prefix",  # opening messages up to the first assistant turn (the Pinned Prefix)
    "runtime_msg",  # later human/system messages the runtime injects
    "ai_text",
    "ai_reasoning",  # reasoning / thinking blocks incl. signatures and encrypted content
    "ai_calls",  # tool-call blocks and the tool_calls list
    "tool_code",
    "tool_search",
    "tool_exec",
    "tool_other",
    "tool_schemas",
]


def dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def message_json(msg: dict) -> str:
    tool_calls: object = ()
    if msg["type"] == "ai":
        tool_calls = [
            {"name": c.get("name"), "args": c.get("args"), "id": c.get("id"), "type": "tool_call"}
            for c in msg.get("tool_calls") or []
        ]
    return dumps({"content": msg["content"], "tool_calls": tool_calls, "additional_kwargs": {}})


def ai_split(msg: dict) -> dict[str, int]:
    """Bytes of one AI message, split by block kind (sums to the message bytes)."""
    total = len(message_json(msg).encode())
    content = msg["content"]
    out = {"ai_text": 0, "ai_reasoning": 0, "ai_calls": 0}
    if isinstance(content, str):
        out["ai_text"] = len(dumps(content).encode())
    else:
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            n = len(dumps(block).encode())
            if kind in ("reasoning", "thinking", "redacted_thinking"):
                out["ai_reasoning"] += n
            elif kind in ("tool_use", "function_call"):
                out["ai_calls"] += n
            else:
                out["ai_text"] += n
    out["ai_calls"] += total - sum(out.values())  # the tool_calls list and framing
    return out


def tool_category(name: str | None) -> str:
    if name in CODE_TOOLS:
        return "tool_code"
    if name in SEARCH_TOOLS:
        return "tool_search"
    if name in EXEC_TOOLS:
        return "tool_exec"
    return "tool_other"


class Conversation:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.cats: list[str | None] = []  # category per message (AI messages split separately)
        self.call_names: dict[str, str] = {}
        self.seen_ai = False

    def add(self, msg: dict) -> None:
        if msg["type"] == "ai":
            self.seen_ai = True
            for c in msg.get("tool_calls") or []:
                self.call_names[c.get("id")] = c.get("name")
            self.cats.append(None)
        elif msg["type"] == "tool":
            self.cats.append(tool_category(self.call_names.get(msg.get("tool_call_id"))))
        else:
            self.cats.append("runtime_msg" if self.seen_ai else "prefix")
        self.messages.append(msg)

    def makeup(self) -> dict[str, int]:
        out = defaultdict(int)
        for msg, cat in zip(self.messages, self.cats):
            if cat is None:
                for k, v in ai_split(msg).items():
                    out[k] += v
            else:
                out[cat] += len(message_json(msg).encode())
        return out


def main() -> None:
    tools = {t["name"]: t for t in json.load(open(sys.argv[1]))}
    tools.setdefault(
        "run_check",
        {"name": "run_check", "args": {"name": {"title": "Name", "type": "string"}}},
    )
    tools.setdefault(
        "report_subagent",
        {"name": "report_subagent", "args": {"report": {"title": "Report", "type": "string"}}},
    )
    rows = []
    for spec in sys.argv[3:]:
        label, path = spec.split("=", 1)
        convs: dict[tuple, Conversation] = {}
        pending: dict[tuple, dict] = {}
        conv_index: dict[tuple, int] = defaultdict(int)
        last_key: dict[object, tuple] = {}
        for line in open(path):
            rec = json.loads(line)
            if rec["kind"] == "request":
                key = (rec.get("conversation"), tuple(rec.get("tools") or ()))
                last_key[rec.get("conversation")] = key
                new = rec.get("new_messages") or []
                if any(m["type"] == "system" for m in new) or key not in convs:
                    convs[key] = Conversation()
                    conv_index[key] += 1
                conv = convs[key]
                for m in new:
                    conv.add(m)
                missing = [n for n in rec["tools"] if n not in tools]
                if missing:
                    raise SystemExit(f"{label}: no schema for {missing}")
                schema_text = dumps([tools[n]["args"] for n in rec["tools"]])
                msg_text = "".join(message_json(m) for m in conv.messages)
                estimate = max(1, len((msg_text + schema_text).encode()) // 4)
                mk = conv.makeup()
                mk["tool_schemas"] = len(schema_text.encode())
                pending[key] = {
                    "source": label,
                    "conversation": f"{rec.get('conversation')}/{len(rec['tools'])}tools#{conv_index[key]}"
                    + ("/sub" if "report_subagent" in rec["tools"] else ""),
                    "seq": rec["seq"],
                    "message_count": rec.get("message_count"),
                    "rebuilt_count": len(conv.messages),
                    "estimate": estimate,
                    "bytes": len((msg_text + schema_text).encode()),
                    "non_ascii_chars": sum(1 for ch in msg_text if ord(ch) > 127),
                    **{c: mk.get(c, 0) for c in CATEGORIES},
                }
            elif rec["kind"] == "response":
                key = last_key[rec.get("conversation")]
                req = pending.pop(key, None)
                conv = convs[key]
                msg = rec["message"]
                conv.add(msg)
                usage = msg.get("usage") or {}
                if req is None or "input_tokens" not in usage:
                    continue
                details = usage.get("input_token_details") or {}
                req.update(
                    input_tokens=usage["input_tokens"],
                    cache_read=details.get("cache_read", 0),
                    cache_creation=details.get("cache_creation", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    ratio=round(req["estimate"] / usage["input_tokens"], 4),
                    bytes_per_token=round(req["bytes"] / usage["input_tokens"], 3),
                )
                rows.append(req)
    with open(sys.argv[2], "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    mism = [r for r in rows if r["message_count"] != r["rebuilt_count"]]
    print(f"{len(rows)} requests; message_count mismatches: {len(mism)}")


if __name__ == "__main__":
    main()
