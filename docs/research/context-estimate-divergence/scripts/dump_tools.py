"""Dump the main step tool set (args schema + provider schema) as built by the runtime."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from langchain_core.utils.function_calling import convert_to_openai_tool
from coding_agent.runtime.graph.agent_step import build_agent_step_tools
from coding_agent.workflow.schema import Agent, RouteTarget, WorkflowStep, FieldType
from support.agent_step_deps import make_agent_step_deps
from support.scripted_model import ScriptedModel
import inspect
sig = inspect.signature(make_agent_step_deps)
print("make_agent_step_deps params:", list(sig.parameters), file=sys.stderr)
tmp = Path(tempfile.mkdtemp()); ws = tmp / "work"; ws.mkdir(); (ws / "README.md").write_text("x\n")
from test_step_workflow_agent import _facts
deps = make_agent_step_deps(model=ScriptedModel([]), workspace=ws, tmp_path=tmp, attempt=_facts())
step = WorkflowStep(id="process", agent=Agent(instructions=(), tools=("workspace-read","workspace-write","shell","tests","subagents"), inputs=(), results={"done": {}}), max_executions=1, routes={"done": RouteTarget(end="completed")})
tools = build_agent_step_tools(step, deps)
out = [{"name": t.name, "args": t.args, "description": t.description, "openai": convert_to_openai_tool(t)} for t in tools]
json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)
print([t.name for t in tools])

# The Sub-agent's own exit tool (its other tools are already in the main set).
from coding_agent.runtime.graph.subagent import SubagentRecord, build_report_subagent_tool
sub = build_report_subagent_tool(SubagentRecord())
out.append({"name": sub.name, "args": sub.args, "description": sub.description, "openai": convert_to_openai_tool(sub)})
json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)
print("added", sub.name)
