"""Tokens the bound tool set costs per request vs what the estimate counts for it.

The estimate counts json.dumps([tool.args ...]) / 4: argument schemas only, no
names, no descriptions, no provider tool-use framing. Usage: python tool_overhead.py TOOLS_JSON
"""
import json, os, sys
import httpx
from langchain_core.messages import HumanMessage
from langchain_anthropic import ChatAnthropic
tools = json.load(open(sys.argv[1]))
schemas = [t["openai"] for t in tools]
args_est = len(json.dumps([t["args"] for t in tools], sort_keys=True, default=str, separators=(",", ":")).encode()) // 4
a = ChatAnthropic(model="claude-sonnet-5")
msg = [HumanMessage(content="Hello.")]
a0, a1 = a.get_num_tokens_from_messages(msg), a.get_num_tokens_from_messages(msg, tools=schemas)
c = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
def oc(extra):
    r = c.post("https://api.openai.com/v1/responses/input_tokens", json={"model": "gpt-5.6-luna", "input": "Hello.", **extra}); r.raise_for_status(); return r.json()["input_tokens"]
ot = [{"type": "function", "name": s["function"]["name"], "description": s["function"].get("description", ""), "parameters": s["function"]["parameters"]} for s in schemas]
o0, o1 = oc({}), oc({"tools": ot})
print(json.dumps({"tools": len(tools), "estimate_for_tool_args": args_est, "anthropic_tool_tokens": a1 - a0, "anthropic_bare_message": a0, "openai_tool_tokens": o1 - o0, "openai_bare_message": o0}))
