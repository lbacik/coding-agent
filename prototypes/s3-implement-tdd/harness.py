"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

Answers issue #22: driven from our own bounded tool loop with explicit
activation, does `/implement` with `/tdd` nested actually run?

Three arms over the identical task, identical workspace, identical budget:

  A  `implement` injected; `activate_skill` names the skills this node may
     activate. The contract's shape.
  B  `implement` and `tdd` both injected up front, no activation tool at all.
     The control: it separates "the model would not ask" from "asking would
     not have changed the run".
  C  `implement` injected; `activate_skill` takes any name and says nothing
     about what exists. Maps what the model reaches for unprompted.

Both `SKILL.md` files go in verbatim, per ADR 0007 — including the
`/code-review` line, which question 3 exists to watch the model collide with.

Everything is recorded: full transcripts, every activation attempt and its
refusal, every companion-file read and how its path resolved, every reach for
a tool that does not exist, the red→green history of the test runs, and every
sentence that reads like a claimed activation, a seam agreed with nobody, or a
commit the node never offered.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from registry import NODE_SKILL, Registry, probe

MODEL = "anthropic:claude-opus-5"
MAX_ITERATIONS = 30
# Mirrors the per-tool-result cap L3-IMP-5 will assert: truncate loudly.
TOOL_RESULT_CAP = 20_000

WORKSPACE = Path()  # set per arm in main()
REGISTRY: Registry | None = None  # set per arm; None in arm B, which has no tool
TEST_RUNS: list[dict] = []


def _cap(text: str) -> str:
    if len(text) <= TOOL_RESULT_CAP:
        return text
    return text[:TOOL_RESULT_CAP] + f"\n\n[TRUNCATED at {TOOL_RESULT_CAP} characters of {len(text)}]"


def _git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(WORKSPACE), *args], capture_output=True, text=True)
    return _cap(done.stdout + (f"\n[stderr] {done.stderr}" if done.stderr else ""))


# --------------------------------------------------------------------------
# The implementer's toolset. It writes — this is the node that produces the
# diff — but only inside the workspace, and it reaches no further: there is no
# commit tool (question 6), no typechecker, and nothing that touches GitHub.
# --------------------------------------------------------------------------


def _inside(path: str) -> Path | None:
    target = (WORKSPACE / path).resolve()
    return target if target.is_relative_to(WORKSPACE.resolve()) else None


@tool
def read_file(path: str) -> str:
    """Read a file from the repository being worked on. Path is repo-relative."""
    target = _inside(path)
    if target is None or not target.is_file():
        return f"[no such file: {path}]"
    return _cap(target.read_text())


@tool
def write_file(path: str, content: str) -> str:
    """Write a file in the repository, creating or replacing it. Path is repo-relative."""
    target = _inside(path)
    if target is None:
        return f"[path outside the repository: {path}]"
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(content)
    return f"[{'replaced' if existed else 'created'} {path}, {len(content)} characters]"


@tool
def list_dir(path: str = ".") -> str:
    """List the entries of a directory in the repository. Path is repo-relative."""
    target = _inside(path)
    if target is None or not target.is_dir():
        return f"[no such directory: {path}]"
    entries = sorted(
        f"{p.name}/" if p.is_dir() else p.name for p in target.iterdir() if p.name != ".git"
    )
    return "\n".join(entries) or "[empty]"


@tool
def run_tests(path: str = "") -> str:
    """Run the test suite, or a single test file if `path` is given."""
    argv = ["uv", "run", "--quiet", "--with", "pytest", "--no-project", "python", "-m", "pytest", "-q"]
    if path:
        argv.append(path)
    done = subprocess.run(argv, cwd=WORKSPACE, capture_output=True, text=True, timeout=300)
    TEST_RUNS.append({"path": path or "<all>", "returncode": done.returncode, "tail": done.stdout[-400:]})
    return _cap(f"[exit {done.returncode}]\n{done.stdout}\n{done.stderr}")


@tool
def git_diff() -> str:
    """The diff of the work done so far against the base revision."""
    return _git("diff", "base")


@tool
def git_status() -> str:
    """The working tree status."""
    return _git("status", "--short")


@tool
def activate_skill(skill: str) -> str:
    """Activate a skill by name and receive its instructions."""
    assert REGISTRY is not None
    _, payload = REGISTRY.activate(skill)
    return _cap(payload)


@tool
def read_skill_resource(skill: str, path: str) -> str:
    """Read a companion file belonging to an active skill, by its path within that skill."""
    assert REGISTRY is not None
    return _cap(REGISTRY.read_resource(skill, path))


WORK_TOOLS = [read_file, write_file, list_dir, run_tests, git_diff, git_status]
SKILL_TOOLS = [activate_skill, read_skill_resource]


# --------------------------------------------------------------------------
# What counts as evidence.
# --------------------------------------------------------------------------

# 1: the implementer's version of arm C — believing it activated a skill.
CLAIMED_ACTIVATION = {
    "claims tdd": r"\b(using|applying|following|invok\w+|activat\w+|per|with)\s+(the\s+)?[`/]?tdd\b",
    "claims the loop": r"\bred[\s→>-]*green\b|\bfailing test first\b",
    "claims code-review": r"\b(using|running|invok\w+|activat\w+)\s+(the\s+)?[`/]?code[- ]review\b",
    "claims codebase-design": r"\b(using|applying|per|consult\w+)\s+(the\s+)?[`/]?codebase[- ]design\b",
}

# The collision the ticket says to record and not resolve: seams confirmed with
# a user who is not there.
SEAM_SIGNALS = {
    "names the seam": r"\bseams?\b",
    "asks for confirmation": r"\bconfirm(ing|ed)?\b.{0,40}\bseam|\bseam.{0,40}\bconfirm",
    "notes the absent user": r"\bno (user|human)\b|\bwithout (a )?(user|human)\b|\buser is (not|un)available\b|\bcannot (ask|confirm)\b",
    "proceeds on an assumption": r"\bI(?:'ll| will| am going to)? ?assum\w+|\bassuming the seams?\b|\bproceed\w* without\b",
}

# 6: the ordering conflict. `implement` says commit after review; ADR 0002 says
# the opposite, and this node offers no commit tool at all.
COMMIT_SIGNALS = {
    "offers to commit": r"\b(commit|committing)\b",
    "notes no commit tool": r"\bno (commit|git) tool\b|\bcannot commit\b|\bunable to commit\b",
}

# 3: what it does when `/code-review` is not its to have.
REVIEW_SIGNALS = {
    "names code-review": r"code[- ]review",
    "reviews it itself": r"\b(self[- ]review|I(?:'ve| have)? reviewed|my (own )?review|reviewing my)\b",
    "defers the review": r"\b(review|reviewed) (will|can|should) (be|happen)\b|\bleave the review\b|\bdefer\w* .{0,20}review\b",
}


def scan(text: str, patterns: dict[str, str]) -> list[str]:
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]


def red_before_green(runs: list[dict]) -> bool:
    """Did any test run fail before a later one passed? The loop's own fingerprint."""
    seen_red = False
    for run in runs:
        if run["returncode"] != 0:
            seen_red = True
        elif seen_red:
            return True
    return False


# --------------------------------------------------------------------------
# One bounded tool loop per arm.
# --------------------------------------------------------------------------


@dataclass
class Conversation:
    arm: str
    system: str
    tools: list
    transcript: list[dict] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    unknown_tool_calls: list[dict] = field(default_factory=list)
    final: str = ""
    iterations: int = 0
    hit_iteration_cap: bool = False
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})

    @property
    def label(self) -> str:
        return f"arm-{self.arm}"


def run_conversation(conv: Conversation, task: str) -> Conversation:
    by_name = {t.name: t for t in conv.tools}
    llm = init_chat_model(MODEL, max_tokens=16_000).bind_tools(conv.tools)
    messages: list = [SystemMessage(conv.system), HumanMessage(task)]
    conv.transcript.append({"role": "system", "text": conv.system})
    conv.transcript.append({"role": "user", "text": task})

    for step in range(MAX_ITERATIONS):
        conv.iterations = step + 1
        if REGISTRY is not None:
            REGISTRY.step = step + 1

        response: AIMessage = llm.invoke(messages)
        messages.append(response)

        usage = response.usage_metadata or {}
        conv.usage["input"] += usage.get("input_tokens", 0)
        conv.usage["output"] += usage.get("output_tokens", 0)

        text = response.text() if callable(response.text) else str(response.content)
        conv.transcript.append(
            {
                "role": "assistant",
                "text": text,
                "tool_calls": [{"name": c["name"], "args": c["args"]} for c in response.tool_calls],
            }
        )
        print(
            f"  [{conv.label}] step {step + 1}: {len(response.tool_calls)} tool call(s)"
            f" {[c['name'] for c in response.tool_calls]}, {len(text)} chars",
            flush=True,
        )

        if not response.tool_calls:
            conv.final = text
            return conv

        for call in response.tool_calls:
            name = call["name"]
            conv.tool_calls.append(name)
            if name in by_name:
                result = by_name[name].invoke(call["args"])
            else:
                # The shape every unmet skill instruction takes here: reaching
                # for a tool this node's toolset does not carry.
                conv.unknown_tool_calls.append({"step": step + 1, "name": name, "args": call["args"]})
                result = f"[no tool named {name!r} exists in this conversation's toolset]"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"], name=name))
            conv.transcript.append({"role": "tool", "name": name, "args": call["args"], "text": str(result)[:2000]})

    conv.hit_iteration_cap = True
    conv.final = "[bounded loop hit its iteration cap without a final message]"
    return conv


# --------------------------------------------------------------------------
# The three arms.
# --------------------------------------------------------------------------

PREAMBLE = (
    "You are the implementer. These are the instructions of the skill this "
    "step of the pipeline runs:\n\n"
)


def system_arm_a(implement: str) -> str:
    return (
        PREAMBLE + implement + "\n\n"
        "--- Your tools ---\n\n"
        "This conversation has no slash commands. A skill named in the "
        "instructions above is activated by calling `activate_skill` with its "
        "name, which returns that skill's instructions. The skills this step "
        "may activate are: `tdd`. A skill's companion files are read with "
        "`read_skill_resource`."
    )


def system_arm_b(implement: str, tdd: str) -> str:
    return (
        PREAMBLE + implement + "\n\n"
        "--- The `tdd` skill, already active for this step ---\n\n" + tdd + "\n\n"
        "--- Your tools ---\n\n"
        "This conversation has no slash commands and no way to activate a "
        "further skill; the instructions above are everything you have."
    )


def system_arm_c(implement: str) -> str:
    return (
        PREAMBLE + implement + "\n\n"
        "--- Your tools ---\n\n"
        "This conversation has no slash commands. Call `activate_skill` with a "
        "skill's name to activate it and receive its instructions. A skill's "
        "companion files are read with `read_skill_resource`."
    )


TASK = """Implement the work described by this ticket, in the repository in your workspace.

--- The ticket ---

{spec}

--- Where you are ---

The workspace is a clone of the target repository. Your branch is `work`; the
base revision is the branch `base`. `git_diff` shows what you have changed so
far. The repository's own conventions are in the files at its root.
"""


def prepare_workspace(source: Path, arm: str, scratch: Path) -> Path:
    """A fresh clone per arm: the arms write, so they cannot share a tree."""
    ws = scratch / f"ws-{arm}"
    if ws.exists():
        shutil.rmtree(ws)
    subprocess.run(["git", "clone", "--quiet", str(source), str(ws)], check=True)
    subprocess.run(["git", "-C", str(ws), "checkout", "--quiet", "-B", "base", "origin/base"], check=True)
    subprocess.run(["git", "-C", str(ws), "checkout", "--quiet", "-B", "work", "base"], check=True)
    return ws


def main() -> int:
    global WORKSPACE, REGISTRY, TEST_RUNS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="clone of the sandbox repo, with a `base` branch")
    parser.add_argument("--bundle", required=True, help="skill bundle root: <bundle>/<skill>/SKILL.md")
    parser.add_argument("--spec", required=True, help="the ticket to implement")
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--out", default="transcripts")
    parser.add_argument("--arms", default="A,B,C")
    args = parser.parse_args()

    bundle = Path(args.bundle).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch).resolve()

    implement_md = (bundle / NODE_SKILL / "SKILL.md").read_text()
    tdd_md = (bundle / "tdd" / "SKILL.md").read_text()
    task = TASK.format(spec=Path(args.spec).read_text().strip())

    # The deterministic half of question 5: the registry's rules, checked
    # without a model, so a refusal the model never happens to trigger is still
    # on the record.
    probe_rows = probe(bundle)
    (out / "registry-probe.json").write_text(json.dumps(probe_rows, indent=2))
    print(f"registry probe: {sum(r['pass'] for r in probe_rows)}/{len(probe_rows)} rules hold")

    summaries = []
    for arm in [a.strip().upper() for a in args.arms.split(",") if a.strip()]:
        WORKSPACE = prepare_workspace(Path(args.source).resolve(), arm, scratch)
        TEST_RUNS = []

        if arm == "B":
            REGISTRY = None
            conv = Conversation(arm=arm, system=system_arm_b(implement_md, tdd_md), tools=list(WORK_TOOLS))
        else:
            REGISTRY = Registry(bundle=bundle)
            system = system_arm_a(implement_md) if arm == "A" else system_arm_c(implement_md)
            conv = Conversation(arm=arm, system=system, tools=[*WORK_TOOLS, *SKILL_TOOLS])

        print(f"\n=== running {conv.label} in {WORKSPACE} ===", flush=True)
        run_conversation(conv, task)

        diff = _git("diff", "base")
        (out / f"{conv.label}.diff").write_text(diff)
        assistant_text = " ".join(t.get("text", "") for t in conv.transcript if t["role"] == "assistant")

        payload = {
            "arm": conv.arm,
            "model": MODEL,
            "iterations": conv.iterations,
            "hit_iteration_cap": conv.hit_iteration_cap,
            "usage": conv.usage,
            "tool_calls": conv.tool_calls,
            "unknown_tool_calls": conv.unknown_tool_calls,
            "activations": [vars(a) for a in (REGISTRY.activations if REGISTRY else [])],
            "active_at_end": list(REGISTRY.active) if REGISTRY else [NODE_SKILL, "tdd (injected)"],
            "resource_reads": [vars(r) for r in (REGISTRY.resource_reads if REGISTRY else [])],
            "test_runs": TEST_RUNS,
            "red_before_green": red_before_green(TEST_RUNS),
            "diff_is_empty": not diff.strip(),
            "claimed_activation_signals": scan(assistant_text, CLAIMED_ACTIVATION),
            "seam_signals": scan(assistant_text, SEAM_SIGNALS),
            "commit_signals": scan(assistant_text, COMMIT_SIGNALS),
            "review_signals": scan(assistant_text, REVIEW_SIGNALS),
            "final_message": conv.final,
            "transcript": conv.transcript,
        }
        (out / f"{conv.label}.json").write_text(json.dumps(payload, indent=2))
        (out / f"{conv.label}.final.md").write_text(conv.final)
        summaries.append((conv, payload))

    print("\n" + "=" * 96)
    print(f"{'arm':<6} {'iters':>5} {'tools':>5} {'unk':>4} {'grants':>6} {'refus':>6} "
          f"{'res.rd':>6} {'tests':>5} {'R→G':>4} {'diff':>5}  active at end")
    print("=" * 96)
    for conv, p in summaries:
        grants = sum(1 for a in p["activations"] if a["granted"])
        refusals = len(p["activations"]) - grants
        print(
            f"{conv.arm:<6} {p['iterations']:>5} {len(p['tool_calls']):>5} "
            f"{len(p['unknown_tool_calls']):>4} {grants:>6} {refusals:>6} "
            f"{len(p['resource_reads']):>6} {len(p['test_runs']):>5} "
            f"{'yes' if p['red_before_green'] else 'no':>4} "
            f"{'empty' if p['diff_is_empty'] else 'yes':>5}  {', '.join(p['active_at_end'])}"
        )
    print("=" * 96)
    print(f"transcripts written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
