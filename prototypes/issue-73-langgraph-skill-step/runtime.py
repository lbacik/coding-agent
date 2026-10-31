"""PROTOTYPE (issue #73) -- throwaway, never imported by `src/`.

Question: what is the smallest LangGraph runtime that reproduces the agreed
reference `/implement` behaviour under the #71 parity and #72 workflow
contracts?

Shape under test:

* an outer **Attempt graph** whose nodes are the fixed, runtime-owned
  skeleton (#72 decision 1): claim -> readiness -> baseline -> [steps] ->
  delivery snapshot -> final checks -> publication gate -> (delivery intent
  -> publish) -> terminal writes;
* one **step subgraph** per Workflow Step, invoked fresh at each step (no
  conversation history crosses a step boundary, W-STP-5): open -> model ->
  tools | silence -> ... -> exit;
* one **review subgraph**, invoked from inside the `activate_code_review`
  tool, fanning out two isolated reviewer conversations with `Send`.

Attempt-wide meters, evidence and the fake tracker live in a `Runtime`
object handed through `config["configurable"]`, not in graph state: the
prototype deliberately has no checkpointer in the loop (process loss before
Delivery Intent is `failed`/`interrupted` in the MVP, and the recovery
mechanism belongs to #74).

Reused from `src/` unchanged: `build_file_tools`, `run_targeted_test`,
`run_validation_contract`, `parse_profile_yaml`, `derive_seam_set`,
`InMemoryUsageLedger`, `ceiling_crossed`, `apply_cache_breakpoints`,
`build_chat_model`, the price table.
"""

from __future__ import annotations

import json
import operator
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict
from xml.etree import ElementTree as ET

import yaml
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from coding_agent.implement.ceilings import InMemoryUsageLedger, LoopCeilings, ceiling_crossed
from coding_agent.implement.prompt_cache import apply_cache_breakpoints
from coding_agent.implement.seam import derive_seam_set
from coding_agent.implement.toolset import build_file_tools
from coding_agent.profile.parser import ProjectProfile, parse_profile_yaml
from coding_agent.provider.price_table import TokenPrices
from coding_agent.validate.harness import CommandContext, run_targeted_test, run_validation_contract
from coding_agent.validate.results import classify
from coding_agent.validate.runner import CredentialStrippedCommandRunner

SKILLS_DIR = Path.home() / ".agents" / "skills"
AGENT_LOGIN = "agent"
MARKER = "<!-- coding-agent:attempt {attempt} -->"
MAX_REVIEWS = 3
MAX_SILENCES = 2
MAX_CLAIM_RETURNS = 2
REVIEWER_MAX_TURNS = 20


# ---------------------------------------------------------------- tracker --


@dataclass
class FakeTracker:
    """In-memory stand-in for GitHub. Every write is logged in order."""

    number: int
    title: str
    body: str
    labels: set[str] = field(default_factory=lambda: {"ready-for-agent"})
    assignees: set[str] = field(default_factory=set)
    closed: bool = False
    comments: list[str] = field(default_factory=list)
    prs: list[dict[str, Any]] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)

    def assign(self, login: str) -> None:
        self.assignees.add(login)
        self.writes.append(f"assign {login}")

    def unassign(self, login: str) -> None:
        self.assignees.discard(login)
        self.writes.append(f"unassign {login}")

    def comment(self, text: str) -> None:
        self.comments.append(text)
        self.writes.append("comment")

    def remove_label(self, label: str) -> None:
        self.labels.discard(label)
        self.writes.append(f"remove-label {label}")

    def add_label(self, label: str) -> None:
        self.labels.add(label)
        self.writes.append(f"add-label {label}")

    def open_pr(self, pr: dict[str, Any]) -> None:
        self.prs.append(pr)
        self.writes.append(f"open-pr draft={pr['draft']} head={pr['head']}")


# ---------------------------------------------------------------- runtime --


@dataclass
class StepSpec:
    id: str
    skill: str | None = None
    prompt_file: Path | None = None


@dataclass
class Runtime:
    """Attempt-wide facts and meters. Not graph state on purpose."""

    attempt_id: str
    workspace: Path
    evidence_dir: Path
    remote: Path
    tracker: FakeTracker
    steps: list[StepSpec]
    selection_label: str
    model_factory: Callable[[str], Any]
    price: TokenPrices
    ceilings: LoopCeilings
    live: bool = False
    hooks: dict[str, Callable[["Runtime", str], None]] = field(default_factory=dict)
    # filled at claim / readiness
    title: str = ""
    body: str = ""
    fingerprint: str = ""
    criteria: list[str] = field(default_factory=list)
    seam_set: tuple[str, ...] = ()
    profile: ProjectProfile | None = None
    base_revision: str = ""
    branch: str = ""
    # meters and evidence
    ledger: InMemoryUsageLedger = field(default_factory=InMemoryUsageLedger)
    started: float = field(default_factory=time.monotonic)
    criterion_evidence: dict[str, bool] = field(default_factory=dict)
    activations: dict[str, int] = field(default_factory=dict)
    review_invocations: int = 0
    reviews: list[dict[str, Any]] = field(default_factory=list)
    sigterm: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    ledger_records: list[dict[str, Any]] = field(default_factory=list)
    conversations: dict[str, list[BaseMessage]] = field(default_factory=dict)

    def event(self, actor: str, kind: str, **data: Any) -> None:
        self.events.append({"t": round(time.monotonic() - self.started, 2), "actor": actor, "kind": kind, **data})

    def limit_crossed(self) -> str | None:
        return ceiling_crossed(self.ledger.totals, time.monotonic() - self.started, self.ceilings)

    def fire(self, hook: str) -> None:
        if hook in self.hooks:
            self.hooks[hook](self, hook)


def rt_of(config: RunnableConfig) -> Runtime:
    return config["configurable"]["rt"]


def git(rt: Runtime, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=rt.workspace, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def has_diff_against_base(rt: Runtime) -> bool:
    git(rt, "add", "-A")
    return bool(git(rt, "diff", "--cached", "--name-only", rt.base_revision))


def enumerate_criteria(body: str) -> list[str]:
    """#66: direct checklist items under one `## Acceptance criteria`."""
    match = re.search(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not match:
        return []
    return [m.group(1).strip() for m in re.finditer(r"^- \[ \] (.+)$", match.group(1), re.M)]


def truncate(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit})"


# ------------------------------------------------------------- skill text --


def skill_text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def frontmatter_description(name: str) -> str:
    meta = skill_text(name).split("---", 2)[1]
    return str(yaml.safe_load(meta)["description"])


def activation_body(name: str) -> str:
    """Skill body verbatim; `tdd` carries its two injected companions
    (ADR 0012); `codebase-design`'s companions are refused."""
    text = skill_text(name)
    if name == "tdd":
        for companion in ("mocking.md", "tests.md"):
            text += f"\n\n----- tdd/{companion} -----\n" + (SKILLS_DIR / "tdd" / companion).read_text(encoding="utf-8")
    return text


def role_blocks() -> dict[str, str]:
    """Verbatim cuts of `code-review` at structural anchors (ADR 0007)."""
    text = skill_text("code-review")
    standards = text[text.index("**Standards sub-agent prompt**") : text.index("**Spec sub-agent prompt**")]
    spec = text[text.index("**Spec sub-agent prompt**") : text.index("If the spec is missing")]
    baseline = text[text.index("On top of whatever the repo documents") : text.index("### 4. Spawn both")]
    return {"standards": standards.strip() + "\n\n" + baseline.strip(), "spec": spec.strip()}


# ------------------------------------------------------------ attempt hdr --


def attempt_header(rt: Runtime) -> str:
    criteria = "\n".join(f"- AC-{i}: {c}" for i, c in enumerate(rt.criteria, 1))
    seams = ", ".join(f"`{s}`" for s in rt.seam_set)
    return (
        "# Attempt Header\n\n"
        f"Attempt: {rt.attempt_id}\nBase Revision: {rt.base_revision}\n"
        f"Target Project language: {rt.profile.language if rt.profile else '?'}\n\n"
        f"Seam Set (ADR 0008), confirmed before this conversation opened: {seams}.\n\n"
        f"Acceptance Criteria, enumerated by the runtime:\n{criteria}\n\n"
        "The runtime owns commits, pushes, validation and the pull request: you have no git tool, "
        "and the work you leave in the working tree is what the runtime commits. Tests count as "
        "evidence only when run through `test_targeted`. Reviews run through `activate_code_review`, "
        "which runs isolated reviewers against a runtime commit of your working tree."
        + (
            "\n\nWhere an activated skill names another skill as a slash command (`/tdd`, `/code-review`, "
            "`/codebase-design`), that command is its `activate_*` tool here: `/tdd` is `activate_tdd`, "
            "`/code-review` is `activate_code_review`, `/codebase-design` is `activate_codebase_design`."
            if os.environ.get("HEADER_VARIANT") == "slash" else ""
        )
    )


def target_issue_message(rt: Runtime) -> str:
    return f"# Target Issue\n\n#{rt.tracker.number}: {rt.title}\n\n{rt.body}"


# ------------------------------------------------------------------ tools --


class CriterionAnswer(BaseModel):
    criterion: str = Field(description="Acceptance Criterion id, e.g. `AC-1`.")
    tests: list[str] = Field(default_factory=list, description="Test identifiers exactly as `test_targeted` reported them.")
    uncovered_reason: str | None = Field(default=None, description="Set instead of `tests` when no test covers the criterion.")


class CompletionArgs(BaseModel):
    criteria: list[CriterionAnswer]
    summary: str = Field(description="At most five sentences.")


class StepReportArgs(BaseModel):
    status: str = Field(description="`done` or `cannot-continue`.")
    justification: str = Field(description="At most five sentences, carried to the next step.")


def junit_cases(path: Path) -> list[tuple[str, bool]]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return []
    cases = []
    for case in root.iter("testcase"):
        if case.find("skipped") is not None:
            continue
        ident = f"{case.get('classname', '')}::{case.get('name', '')}"
        cases.append((ident, case.find("failure") is None and case.find("error") is None))
    return cases


def normalise_test_id(ident: str) -> str:
    """`tests/test_x.py::test_a` and `tests.test_x::test_a` are one id."""
    ident = ident.strip()
    if "::" not in ident:
        return ident
    module, name = ident.split("::", 1)
    module = module.removesuffix(".py").replace("/", ".")
    return f"{module}::{name}"


def build_tools(rt: Runtime, actor: str, position: str, step: StepSpec, state_box: dict[str, Any]) -> list[BaseTool]:
    """One tool set for every step (#72 decision 15), plus the exit tool by
    position and `activate_step` for a `prompt_file` step."""
    tools: list[BaseTool] = list(build_file_tools(rt.workspace))

    def test_targeted(path: str) -> str:
        assert rt.profile is not None
        ctx = CommandContext(CredentialStrippedCommandRunner(["GITHUB_TOKEN"]), rt.workspace, rt.evidence_dir, workspace_root=rt.workspace)
        raw = run_targeted_test(rt.profile, ctx, path)
        cases = junit_cases(rt.evidence_dir / "test_targeted.xml")
        for ident, passed in cases:
            rt.criterion_evidence[normalise_test_id(ident)] = passed
        lines = [f"exit code {raw.exit_code}; {len(cases)} test(s) executed"]
        lines += [f"{'PASS' if ok else 'FAIL'} {ident}" for ident, ok in cases]
        lines.append(truncate((raw.stdout + raw.stderr)[-3000:], 3000))
        return "\n".join(lines)

    tools.append(StructuredTool.from_function(test_targeted, name="test_targeted", description=(
        "Run the Validation Contract's targeted test command against one test file and record the result "
        "as Criterion Evidence. `path` is relative to the repository root.")))

    for skill in ("implement", "tdd", "codebase-design"):
        tools.append(StructuredTool.from_function(
            (lambda s: (lambda: activate(rt, actor, s)))(skill),
            name=f"activate_{skill.replace('-', '_')}",
            description=frontmatter_description(skill)))
    tools.append(StructuredTool.from_function(
        lambda: review(rt, actor), name="activate_code_review", description=frontmatter_description("code-review")))

    if step.prompt_file is not None:
        prompt_bytes = step.prompt_file.read_text(encoding="utf-8")
        tools.append(StructuredTool.from_function(
            lambda: prompt_bytes, name="activate_step", description="Load this step's instructions."))

    if position == "final":
        tools.append(StructuredTool.from_function(
            lambda criteria, summary: report_completion(rt, state_box, criteria, summary),
            name="report_completion", args_schema=CompletionArgs, description=(
                "Declare the implementation stage finished. Answer every Acceptance Criterion with the test "
                "identifiers covering it (as `test_targeted` reported them) or an explicit uncovered reason.")))
    else:
        tools.append(StructuredTool.from_function(
            lambda status, justification: report_step(state_box, status, justification),
            name="report_step", args_schema=StepReportArgs, description=(
                "End this workflow step: `done` to continue to the next step, `cannot-continue` to stop.")))
    return tools


def activate(rt: Runtime, actor: str, skill: str) -> str:
    """Idempotent per conversation (#60): full body once, then an ack."""
    rt.activations[skill] = rt.activations.get(skill, 0) + 1
    key = f"{actor}|{skill}"
    if key in rt.activations:
        return f"`{skill}` is already active in this conversation; its instructions are above."
    rt.activations[key] = 1
    return activation_body(skill)


def report_step(box: dict[str, Any], status: str, justification: str) -> str:
    if status not in ("done", "cannot-continue"):
        return "Refused: status must be `done` or `cannot-continue`."
    box["result"] = {"kind": "report_step", "status": status, "justification": justification[:800]}
    return "Step report accepted."


def report_completion(rt: Runtime, box: dict[str, Any], criteria: list[Any], summary: str) -> str:
    answers = {(a.criterion if hasattr(a, "criterion") else a["criterion"]): a for a in criteria}
    get = lambda a, k: getattr(a, k) if hasattr(a, k) else a.get(k)  # noqa: E731
    expected = [f"AC-{i}" for i in range(1, len(rt.criteria) + 1)]
    problems = [f"{c} is not answered" for c in expected if c not in answers]
    covered, uncovered = [], []
    for cid in expected:
        a = answers.get(cid)
        if a is None:
            continue
        tests = get(a, "tests") or []
        if not tests:
            uncovered.append(cid)
            continue
        bad = [t for t in tests if rt.criterion_evidence.get(normalise_test_id(t)) is not True]
        if bad:
            problems.append(f"{cid}: no passing Criterion Evidence for {', '.join(bad)}")
        else:
            covered.append(cid)
    diff = has_diff_against_base(rt)
    completed_reviews = [r for r in rt.reviews if r["status"] == "completed"]
    if problems and box["claim_returns"] < MAX_CLAIM_RETURNS:
        box["claim_returns"] += 1
        return "Completion Claim returned: " + "; ".join(problems)
    if diff and not completed_reviews and not box["review_returned"]:
        box["review_returned"] = True
        return "Completion Claim returned: no completed `activate_code_review` in this Attempt; a pull request requires one."
    if not diff:
        outcome = "already satisfied" if covered and not uncovered and not problems else "completion not established"
    elif problems or uncovered:
        outcome = "implemented but unverified"
    else:
        outcome = "verified completion"
    box["result"] = {"kind": "completion", "outcome": outcome, "covered": covered, "uncovered": uncovered,
                     "problems": problems, "summary": summary[:1000]}
    return f"Completion Claim accepted: {outcome}."


# ----------------------------------------------------------------- review --


class ReviewState(TypedDict):
    revision: str
    inputs: str
    reports: Annotated[list[tuple[str, str]], operator.add]
    joined: str


def review(rt: Runtime, actor: str) -> str:
    rt.review_invocations += 1
    if rt.review_invocations > MAX_REVIEWS:
        rt.reviews.append({"status": "refused", "invocation": rt.review_invocations})
        return f"Refused: at most {MAX_REVIEWS} review invocations per Attempt."
    if not has_diff_against_base(rt):
        rt.reviews.append({"status": "incomplete", "reason": "empty diff"})
        return "Nothing to review: the working tree does not differ from the Base Revision."
    if git(rt, "status", "--porcelain"):
        git(rt, "commit", "-q", "-m", f"Review snapshot for {rt.attempt_id}\n\nAttempt: {rt.attempt_id}")
    revision = git(rt, "rev-parse", "HEAD")
    diff = git(rt, "diff", f"{rt.base_revision}...HEAD")
    commits = git(rt, "log", "--oneline", f"{rt.base_revision}..HEAD")
    inputs = (
        f"Diff command: `git diff {rt.base_revision}...HEAD`\n\nCommits:\n{commits}\n\n"
        f"Diff:\n```diff\n{diff}\n```\n\nStandards sources found in the repository: none documented.\n\n"
        f"Spec (the Target Issue):\n\n{target_issue_message(rt)}"
    )
    out = REVIEW_GRAPH.invoke({"revision": revision, "inputs": inputs, "reports": [], "joined": ""},
                              config={"configurable": {"rt": rt}})
    rt.reviews.append({"status": "completed", "revision": revision, "invocation": rt.review_invocations})
    rt.event(actor, "review_completed", revision=revision[:12])
    return out["joined"]


def fan_out(state: ReviewState) -> list[Send]:
    return [Send("reviewer", {**state, "seat": seat}) for seat in ("standards", "spec")]


def reviewer(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    seat = state["seat"]
    actor = f"reviewer:{seat}#{rt.review_invocations}"
    seat_assignment = (
        f"You occupy the {seat.capitalize()} seat of a two-axis code review, and only that seat. "
        "No other reviewer will see your conversation. Your final message is your report.")
    if os.environ.get("SEAT_VARIANT") == "v2":
        seat_assignment += (
            " The block below was written for an orchestrator: it lists what a reviewer's instructions contain. "
            "You are that reviewer and the Inputs section is those instructions' content, so carry the review out "
            "yourself; never write a prompt for someone else.")
    brief = f"{seat_assignment}\n\n{role_blocks()[seat]}\n\n# Inputs\n\n{state['inputs']}"
    tools = [t for t in build_file_tools(rt.workspace) if t.name in ("read_file", "list_directory", "find_files", "search_text", "read_lines")]
    model = rt.model_factory(f"reviewer:{seat}").bind_tools(tools)
    messages: list[BaseMessage] = [HumanMessage(content=brief)]
    by_name = {t.name: t for t in tools}
    report = "(reviewer produced no report)"
    for _ in range(REVIEWER_MAX_TURNS):
        response = model.invoke(messages)
        rt.ledger.flush_model_response(getattr(response, "usage_metadata", None), rt.price)
        messages.append(response)
        if not response.tool_calls:
            report = text_of(response)
            break
        for call in response.tool_calls:
            rt.ledger.flush_tool_call()
            result = by_name[call["name"]].invoke(call["args"]) if call["name"] in by_name else "unknown tool"
            rt.event(actor, "tool_call", name=call["name"], args=truncate(call["args"], 200), result=truncate(result, 200))
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    rt.conversations[actor] = messages
    rt.event(actor, "report", text=truncate(report, 600))
    return {"reports": [(seat, report)]}


def join(state: ReviewState) -> dict[str, Any]:
    reports = dict(state["reports"])
    return {"joined": f"Review of {state['revision'][:12]}\n\n## Standards\n\n{reports.get('standards', '')}\n\n## Spec\n\n{reports.get('spec', '')}"}


def build_review_graph():
    g = StateGraph(ReviewState)
    g.add_node("reviewer", reviewer)
    g.add_node("join", join)
    g.add_conditional_edges(START, fan_out, ["reviewer"])
    g.add_edge("reviewer", "join")
    g.add_edge("join", END)
    return g.compile()


REVIEW_GRAPH = build_review_graph()


def text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


# ------------------------------------------------------------- step graph --


class StepState(TypedDict):
    step_index: int
    snapshot: str
    messages: list[BaseMessage]
    force: str | None
    silences: int
    box: dict[str, Any]


def step_ctx(state: StepState, config: RunnableConfig) -> tuple[Runtime, StepSpec, str, str]:
    rt = rt_of(config)
    step = rt.steps[state["step_index"]]
    position = "final" if state["step_index"] == len(rt.steps) - 1 else "non-final"
    return rt, step, position, f"implementer:{step.id}"


def open_node(state: StepState, config: RunnableConfig) -> dict[str, Any]:
    rt, step, position, actor = step_ctx(state, config)
    issue = target_issue_message(rt)
    if state["snapshot"]:
        issue += f"\n\n# Continuation Snapshot (composed by the runtime)\n\n{state['snapshot']}"
    messages = [SystemMessage(content=attempt_header(rt)), HumanMessage(content=issue)]
    force = "activate_implement" if step.skill == "implement" else "activate_step"
    rt.event(actor, "node", name="open", position=position, force=force)
    return {"messages": messages, "force": force}


def model_node(state: StepState, config: RunnableConfig) -> dict[str, Any]:
    rt, step, position, actor = step_ctx(state, config)
    tools = build_tools(rt, actor, position, step, state["box"])
    model = rt.model_factory("implementer")
    bound = model.bind_tools(tools, tool_choice=state["force"]) if state["force"] else model.bind_tools(tools)
    to_send = apply_cache_breakpoints(state["messages"], prefix_length=2) if rt.live else state["messages"]
    response = bound.invoke(to_send)
    rt.ledger.flush_model_response(getattr(response, "usage_metadata", None), rt.price)
    text = text_of(response)
    if text.strip():
        rt.event(actor, "text", text=truncate(text, 500))
    return {"messages": [*state["messages"], response], "force": None}


def tools_node(state: StepState, config: RunnableConfig) -> dict[str, Any]:
    rt, step, position, actor = step_ctx(state, config)
    tools = {t.name: t for t in build_tools(rt, actor, position, step, state["box"])}
    last = state["messages"][-1]
    out: list[BaseMessage] = []
    for call in last.tool_calls:
        rt.ledger.flush_tool_call()
        if call["name"] not in tools:
            result = f"Unknown tool `{call['name']}` at this position."
        else:
            try:
                result = tools[call["name"]].invoke(call["args"])
            except Exception as exc:  # prototype: surface, don't hide
                result = f"Tool error: {exc}"
        rt.event(actor, "tool_call", name=call["name"], args=truncate(call["args"], 300), result=truncate(result, 300))
        out.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    crossed = rt.limit_crossed()
    if crossed and state["box"].get("result") is None:
        state["box"]["result"] = {"kind": "limit", "which": crossed}
    return {"messages": [*state["messages"], *out]}


def silence_node(state: StepState, config: RunnableConfig) -> dict[str, Any]:
    rt, step, position, actor = step_ctx(state, config)
    silences = state["silences"] + 1
    rt.event(actor, "silence", count=silences)
    if silences > MAX_SILENCES:
        state["box"]["result"] = {"kind": "silent"}
        return {"silences": silences}
    if position == "final":
        unmet = ", ".join(f"AC-{i}" for i in range(1, len(rt.criteria) + 1))
        review = "a completed review" if not any(r["status"] == "completed" for r in rt.reviews) else ""
        nudge = (f"You ended your turn without a tool call. The implementation stage ends only through "
                 f"`report_completion`, answering {unmet}" + (f"; the Attempt still lacks {review}" if review else "") + ".")
    else:
        nudge = "You ended your turn without a tool call. This step ends only through `report_step`."
    return {"silences": silences, "messages": [*state["messages"], HumanMessage(content=nudge)]}


def after_model(state: StepState, config: RunnableConfig) -> str:
    rt = rt_of(config)
    crossed = rt.limit_crossed()
    if crossed:
        state["box"]["result"] = {"kind": "limit", "which": crossed}
        return "exit"
    return "tools" if state["messages"][-1].tool_calls else "silence"


def after_tools(state: StepState) -> str:
    return "exit" if state["box"].get("result") else "model"


def after_silence(state: StepState) -> str:
    return "exit" if state["box"].get("result") else "model"


def exit_node(state: StepState, config: RunnableConfig) -> dict[str, Any]:
    rt, step, position, actor = step_ctx(state, config)
    rt.conversations[actor] = state["messages"]
    rt.event(actor, "node", name="exit", result=state["box"].get("result"))
    return {}


def build_step_graph():
    g = StateGraph(StepState)
    for name, fn in (("open", open_node), ("model", model_node), ("tools", tools_node),
                     ("silence", silence_node), ("exit", exit_node)):
        g.add_node(name, fn)
    g.add_edge(START, "open")
    g.add_edge("open", "model")
    g.add_conditional_edges("model", after_model, ["tools", "silence", "exit"])
    g.add_conditional_edges("tools", after_tools, ["model", "exit"])
    g.add_conditional_edges("silence", after_silence, ["model", "exit"])
    g.add_edge("exit", END)
    return g.compile()


STEP_GRAPH = build_step_graph()


# ---------------------------------------------------------- attempt graph --


class AttemptState(TypedDict, total=False):
    trace: Annotated[list[str], operator.add]
    step_index: int
    snapshot: str
    step_reports: Annotated[list[dict[str, Any]], operator.add]
    implementation_outcome: str | None
    stop: str | None
    refusal: str | None
    delivery_sha: str | None
    final_checks: dict[str, Any] | None
    verdict: dict[str, Any]


def claim(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    rt.tracker.assign(AGENT_LOGIN)
    assert rt.tracker.assignees == {AGENT_LOGIN}, "read-back: another assignee"
    rt.title, rt.body = rt.tracker.title, rt.tracker.body
    rt.fingerprint = f"{hash((rt.title, rt.body)) & 0xFFFFFFFF:08x}"
    rt.ledger_records.append({"kind": "claim", "attempt": rt.attempt_id, "fingerprint": rt.fingerprint})
    return {"trace": ["claim"]}


def readiness(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    rt.criteria = enumerate_criteria(rt.body)
    rt.seam_set = derive_seam_set(rt.title, rt.body)
    profile = parse_profile_yaml((rt.workspace / "docs/agents/project-profile.yml").read_text())
    missing = [n for n, ok in (("Acceptance Criteria", rt.criteria), ("Seam Set", rt.seam_set),
                               ("Project Profile", isinstance(profile, ProjectProfile))) if not ok]
    if isinstance(profile, ProjectProfile):
        rt.profile = profile
    return {"trace": ["readiness"], "refusal": f"missing Readiness Fact: {', '.join(missing)}" if missing else None}


def baseline(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    assert rt.profile is not None
    boot = subprocess.run(rt.profile.bootstrap, shell=True, cwd=rt.workspace, capture_output=True, text=True)
    if boot.returncode != 0:
        return {"trace": ["baseline"], "refusal": f"bootstrap failed: {boot.stderr[-300:]}"}
    ctx = CommandContext(CredentialStrippedCommandRunner(["GITHUB_TOKEN"]), rt.workspace, rt.evidence_dir / "baseline")
    (rt.evidence_dir / "baseline").mkdir(parents=True, exist_ok=True)
    red = [r.name for r in run_validation_contract(rt.profile, ctx) if classify(r) != "passed"]
    return {"trace": ["baseline"], "refusal": f"red baseline: {', '.join(red)}" if red else None}


def route_refusal(state: AttemptState) -> str:
    return "terminal_writes" if state.get("refusal") else "next"


def run_step(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    index = state.get("step_index", 0)
    box: dict[str, Any] = {"result": None, "claim_returns": 0, "review_returned": False}
    STEP_GRAPH.invoke({"step_index": index, "snapshot": state.get("snapshot", ""), "messages": [],
                       "force": None, "silences": 0, "box": box},
                      config={"configurable": {"rt": rt}, "recursion_limit": 2000})
    result = box["result"] or {"kind": "silent"}
    update: dict[str, Any] = {"trace": [f"step:{rt.steps[index].id}"], "step_reports": [result]}
    final = index == len(rt.steps) - 1
    if result["kind"] == "limit":
        update.update(stop="limit", implementation_outcome="saved partial work")
    elif result["kind"] == "silent":
        update.update(stop="silent", implementation_outcome="completion not established")
    elif result["kind"] == "report_step" and result["status"] == "cannot-continue":
        update.update(stop="cannot-continue", implementation_outcome="completion not established")
    elif result["kind"] == "completion":
        update["implementation_outcome"] = result["outcome"]
    elif final:
        update.update(stop="no-completion", implementation_outcome="completion not established")
    return update


def step_gate(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    """A Gate: re-read the tracker, re-evaluate limits, maybe stop."""
    rt = rt_of(config)
    rt.fire("step_gate")
    update: dict[str, Any] = {"trace": ["gate"]}
    if state.get("stop"):
        return update
    if rt.tracker.closed or rt.selection_label not in rt.tracker.labels:
        update.update(stop="abandoned", implementation_outcome=state.get("implementation_outcome") or "saved partial work")
    elif rt.sigterm:
        update.update(stop="interrupted", implementation_outcome=state.get("implementation_outcome") or "saved partial work")
    elif rt.limit_crossed():
        update.update(stop="limit", implementation_outcome="saved partial work")
    elif state.get("step_index", 0) < len(rt.steps) - 1 and not state.get("implementation_outcome"):
        update["step_index"] = state.get("step_index", 0) + 1
        update["snapshot"] = compose_snapshot(rt, state["step_reports"][-1])
    return update


def route_after_gate(state: AttemptState, config: RunnableConfig) -> str:
    rt = rt_of(config)
    if state.get("stop") or state.get("implementation_outcome"):
        return "delivery_snapshot"
    return "run_step"


def compose_snapshot(rt: Runtime, last_report: dict[str, Any]) -> str:
    """ADR 0013-style work facts, composed by the runtime, never by the model."""
    git(rt, "add", "-A")
    changed = git(rt, "diff", "--cached", "--stat", rt.base_revision) or "(no changes)"
    evidence = "\n".join(f"- {'PASS' if ok else 'FAIL'} {i}" for i, ok in rt.criterion_evidence.items()) or "(none yet)"
    return (f"Changed files against the Base Revision:\n{changed}\n\nCriterion Evidence so far:\n{evidence}\n\n"
            f"Previous step report ({last_report.get('status')}): {last_report.get('justification', '')}")


def delivery_snapshot(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    if not has_diff_against_base(rt):
        return {"trace": ["delivery_snapshot:no-diff"], "delivery_sha": None}
    if git(rt, "status", "--porcelain"):
        git(rt, "commit", "-q", "-m", f"Delivery Snapshot for {rt.attempt_id}\n\nAttempt: {rt.attempt_id}")
    sha = git(rt, "rev-parse", "HEAD")
    git(rt, "push", "-q", str(rt.remote), f"HEAD:refs/heads/{rt.branch}")
    rt.tracker.writes.append(f"push {rt.branch}@{sha[:12]}")
    return {"trace": ["delivery_snapshot"], "delivery_sha": sha}


def route_after_snapshot(state: AttemptState) -> str:
    ok = state.get("implementation_outcome") == "verified completion" and not state.get("stop")
    return "final_checks" if ok else "publication_gate"


def final_checks(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    assert rt.profile is not None
    ctx = CommandContext(CredentialStrippedCommandRunner(["GITHUB_TOKEN"]), rt.workspace, rt.evidence_dir / "final")
    (rt.evidence_dir / "final").mkdir(parents=True, exist_ok=True)
    results = run_validation_contract(rt.profile, ctx)
    green = all(classify(r) == "passed" for r in results)
    dirty = bool(git(rt, "status", "--porcelain"))
    summary = {r.name: {"exit": r.exit_code, "executed": r.junit.executed if r.junit else None} for r in results}
    return {"trace": ["final_checks"], "final_checks": {"green": green, "dirty": dirty, "commands": summary}}


def publication_gate(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    rt.fire("publication_gate")
    outcome = state.get("implementation_outcome")
    stop = state.get("stop")
    checks = state.get("final_checks") or {}
    drift = (rt.tracker.title, rt.tracker.body) != (rt.title, rt.body)
    cancelled = rt.tracker.closed or rt.selection_label not in rt.tracker.labels
    reviewed = any(r["status"] == "completed" for r in rt.reviews)
    if stop == "abandoned" or cancelled:
        verdict = {"terminal": "abandoned", "pr": False}
    elif stop == "interrupted" or rt.sigterm:
        verdict = {"terminal": "failed", "reason": "interrupted", "pr": False}
    elif stop == "limit":
        verdict = {"terminal": "failed-limit", "pr": False}
    elif outcome != "verified completion":
        reason = {"implemented but unverified": "unverified", "already satisfied": "already-satisfied"}.get(outcome or "", "no-completion-evidence")
        verdict = {"terminal": "failed", "reason": reason, "pr": False}
    elif drift:
        verdict = {"terminal": "failed", "reason": "drift", "pr": False}
    elif not reviewed:
        verdict = {"terminal": "failed", "reason": "review-missing", "pr": False}
    elif not checks.get("green"):
        verdict = {"terminal": "failed", "reason": "failing-validation", "pr": False}
    elif checks.get("dirty"):
        verdict = {"terminal": "failed", "reason": "dirty-tree", "pr": False}
    else:
        verdict = {"terminal": "delivered", "pr": True}
    return {"trace": ["publication_gate"], "verdict": verdict}


def route_after_publication_gate(state: AttemptState) -> str:
    return "delivery_intent" if state["verdict"]["pr"] else "terminal_writes"


def delivery_intent(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    rt.ledger_records.append({"kind": "delivery_intent", "attempt": rt.attempt_id, "branch": rt.branch,
                              "head": state.get("delivery_sha"), "verdict": state["verdict"],
                              "writes": ["open-pr", "comment", "remove-label", "unassign"]})
    return {"trace": ["delivery_intent"]}


def publish(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    review_status = "fresh" if rt.reviews and rt.reviews[-1].get("revision") == state.get("delivery_sha") else "stale"
    rt.tracker.open_pr({"title": rt.title, "head": rt.branch, "draft": False,
                        "body": f"Closes #{rt.tracker.number}\n{MARKER.format(attempt=rt.attempt_id)}\nreview: {review_status}, "
                                f"{rt.review_invocations} invocation(s)"})
    return {"trace": ["publish"]}


def terminal_writes(state: AttemptState, config: RunnableConfig) -> dict[str, Any]:
    rt = rt_of(config)
    verdict = state.get("verdict") or {"terminal": "failed", "reason": state.get("refusal"), "pr": False}
    rt.tracker.comment(f"{MARKER.format(attempt=rt.attempt_id)}\nAttempt result: {verdict}")
    rt.tracker.remove_label(rt.selection_label)
    rt.tracker.unassign(AGENT_LOGIN)
    if not verdict["pr"]:
        rt.tracker.add_label("ready-for-human")
    return {"trace": ["terminal_writes"], "verdict": verdict}


def build_attempt_graph():
    g = StateGraph(AttemptState)
    for name, fn in (("claim", claim), ("readiness", readiness), ("baseline", baseline), ("run_step", run_step),
                     ("gate", step_gate), ("delivery_snapshot", delivery_snapshot), ("final_checks", final_checks),
                     ("publication_gate", publication_gate), ("delivery_intent", delivery_intent),
                     ("publish", publish), ("terminal_writes", terminal_writes)):
        g.add_node(name, fn)
    g.add_edge(START, "claim")
    g.add_edge("claim", "readiness")
    g.add_conditional_edges("readiness", route_refusal, {"terminal_writes": "terminal_writes", "next": "baseline"})
    g.add_conditional_edges("baseline", route_refusal, {"terminal_writes": "terminal_writes", "next": "run_step"})
    g.add_edge("run_step", "gate")
    g.add_conditional_edges("gate", route_after_gate, ["run_step", "delivery_snapshot"])
    g.add_conditional_edges("delivery_snapshot", route_after_snapshot, ["final_checks", "publication_gate"])
    g.add_edge("final_checks", "publication_gate")
    g.add_conditional_edges("publication_gate", route_after_publication_gate, ["delivery_intent", "terminal_writes"])
    g.add_edge("delivery_intent", "publish")
    g.add_edge("publish", "terminal_writes")
    g.add_edge("terminal_writes", END)
    return g.compile()


ATTEMPT_GRAPH = build_attempt_graph()


# ------------------------------------------------------------------ setup --


def load_workflow(config_path: Path, workflow_id: str) -> tuple[list[StepSpec], str, dict[str, Any]]:
    data = yaml.safe_load(config_path.read_text())
    wf = data["workflows"][workflow_id]
    steps = [StepSpec(id=s["id"], skill=s.get("skill"),
                      prompt_file=(config_path.parent / s["prompt_file"]) if "prompt_file" in s else None)
             for s in wf["steps"]]
    return steps, wf["selector"]["label"], wf.get("limits", {})


def prepare_workspace(fixture: Path, root: Path) -> tuple[Path, Path, str]:
    """Fresh git repo from the fixture plus a bare 'remote'. Returns
    (workspace, remote, base_revision)."""
    import shutil

    if root.exists():
        shutil.rmtree(root)
    workspace, remote = root / "workspace", root / "remote.git"
    shutil.copytree(fixture, workspace)
    run = lambda *a, cwd=workspace: subprocess.run(a, cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "add", "-A")
    run("git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture")
    run("git", "config", "user.name", "coding-agent prototype")
    run("git", "config", "user.email", "agent@example.invalid")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()
    return workspace, remote, base


def run_attempt(rt: Runtime) -> dict[str, Any]:
    rt.branch = f"agent/{rt.tracker.number}-{rt.attempt_id.split('/')[-1]}"
    rt.base_revision = git(rt, "rev-parse", "HEAD")
    rt.evidence_dir.mkdir(parents=True, exist_ok=True)
    final = ATTEMPT_GRAPH.invoke({"trace": [], "step_index": 0, "snapshot": "", "step_reports": []},
                                 config={"configurable": {"rt": rt}, "recursion_limit": 200})
    return final
