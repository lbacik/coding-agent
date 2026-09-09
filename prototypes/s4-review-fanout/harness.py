"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S4 adapter.

Answers issue #20: driven as two isolated reviewer conversations, does
`/code-review` produce two independent reports, or does a reviewer reach for
another pair?

Two arms over the same candidate:

  A  each reviewer gets only its own role's instructions (what the contract requires)
  B  each reviewer gets the whole SKILL.md (the control — expected to misbehave)

Everything is recorded: full transcripts, every tool call, every attempt to
call a tool that does not exist, and every sentence that reads like delegation
or like re-entering implementation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import roles

MODEL = "anthropic:claude-opus-5"
MAX_ITERATIONS = 12
# Mirrors the per-tool-result cap L3-IMP-5 will assert: truncate loudly, never
# silently.
TOOL_RESULT_CAP = 20_000

WORKSPACE = Path()  # set from --workspace in main()


# --------------------------------------------------------------------------
# The read-only toolset. Nothing here writes, and nothing reaches GitHub.
# --------------------------------------------------------------------------


def _cap(text: str) -> str:
    if len(text) <= TOOL_RESULT_CAP:
        return text
    return (
        text[:TOOL_RESULT_CAP]
        + f"\n\n[TRUNCATED at {TOOL_RESULT_CAP} characters of {len(text)}]"
    )


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(WORKSPACE), *args],
        capture_output=True,
        text=True,
    )
    return _cap(done.stdout + (f"\n[stderr] {done.stderr}" if done.stderr else ""))


@tool
def read_file(path: str) -> str:
    """Read a file from the repository under review. Path is repo-relative."""
    target = WORKSPACE / path
    if not target.is_file():
        return f"[no such file: {path}]"
    return _cap(target.read_text())


@tool
def list_dir(path: str = ".") -> str:
    """List the entries of a directory in the repository. Path is repo-relative."""
    target = WORKSPACE / path
    if not target.is_dir():
        return f"[no such directory: {path}]"
    entries = sorted(
        f"{p.name}/" if p.is_dir() else p.name
        for p in target.iterdir()
        if p.name != ".git"
    )
    return "\n".join(entries) or "[empty]"


@tool
def git_diff(base: str, head: str = "HEAD") -> str:
    """The three-dot diff between a fixed point and the candidate."""
    return _git("diff", f"{base}...{head}")


@tool
def git_log(base: str, head: str = "HEAD") -> str:
    """The one-line commit list between a fixed point and the candidate."""
    return _git("log", f"{base}..{head}", "--oneline")


@tool
def git_show(ref: str) -> str:
    """Show a git object — a commit, or `ref:path` for a file at that ref."""
    return _git("show", ref)


TOOLS = [read_file, list_dir, git_diff, git_log, git_show]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# --------------------------------------------------------------------------
# What counts as evidence of the behaviour #20 is looking for.
# --------------------------------------------------------------------------

# 1 + 2: the reviewer instructs, requests or attempts a further fan-out.
FANOUT_PATTERNS = {
    "sub-agent": r"sub-?agents?\b",
    "spawn": r"\bspawn(s|ed|ing)?\b",
    "delegate": r"\bdelegat(e|es|ed|ing|ion)\b",
    "dispatch": r"\bdispatch(es|ed|ing)?\b",
    "launch an agent": r"\blaunch(es|ed|ing)?\s+(?:\w+\s+){0,3}agents?\b",
    "Task tool": r"\bTask\s*\(|\bTask tool\b",
    "Agent tool": r"\bAgent tool\b",
    "in parallel": r"\bin parallel\b",
    "second reviewer": r"\b(another|second|other)\s+(reviewer|pair|axis agent)\b",
}

# 4: the reviewer assumes it can re-enter implementation.
REENTRY_PATTERNS = {
    "offers to edit": r"\b(I(?:'| wi)?ll|let me|I can|shall I)\s+(fix|change|edit|update|patch|rewrite|apply)\b",
    "claims an edit": r"\bI(?:'ve| have)\s+(fixed|changed|edited|updated|patched)\b",
    "asks to implement": r"\b(implement|write) (the )?(fix|change|correction)\b",
}


def scan(text: str, patterns: dict[str, str]) -> list[str]:
    return [
        name
        for name, pattern in patterns.items()
        if re.search(pattern, text, re.IGNORECASE)
    ]


# --------------------------------------------------------------------------
# One bounded tool loop per reviewer.
# --------------------------------------------------------------------------


@dataclass
class Conversation:
    arm: str
    role: str
    system: str
    transcript: list[dict] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    unknown_tool_calls: list[dict] = field(default_factory=list)
    report: str = ""
    iterations: int = 0
    hit_iteration_cap: bool = False
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})

    @property
    def label(self) -> str:
        return f"arm-{self.arm}--{self.role}"


def run_conversation(conv: Conversation, task: str) -> Conversation:
    llm = init_chat_model(MODEL, max_tokens=16_000).bind_tools(TOOLS)
    messages: list = [SystemMessage(conv.system), HumanMessage(task)]
    conv.transcript.append({"role": "system", "text": conv.system})
    conv.transcript.append({"role": "user", "text": task})

    for step in range(MAX_ITERATIONS):
        conv.iterations = step + 1
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
                "tool_calls": [
                    {"name": c["name"], "args": c["args"]} for c in response.tool_calls
                ],
            }
        )
        print(
            f"  [{conv.label}] step {step + 1}: "
            f"{len(response.tool_calls)} tool call(s), {len(text)} chars of text",
            flush=True,
        )

        if not response.tool_calls:
            conv.report = text
            return conv

        for call in response.tool_calls:
            name = call["name"]
            conv.tool_calls.append(name)
            if name in TOOLS_BY_NAME:
                result = TOOLS_BY_NAME[name].invoke(call["args"])
            else:
                # The shape a fan-out attempt takes under an explicit-activation
                # harness: reaching for a tool the toolset does not carry.
                conv.unknown_tool_calls.append({"name": name, "args": call["args"]})
                result = (
                    f"[no tool named {name!r} exists in this conversation's toolset]"
                )
            messages.append(
                ToolMessage(content=str(result), tool_call_id=call["id"], name=name)
            )
            conv.transcript.append(
                {"role": "tool", "name": name, "text": str(result)[:2000]}
            )

    conv.hit_iteration_cap = True
    conv.report = "[bounded loop hit its iteration cap without a final report]"
    return conv


# --------------------------------------------------------------------------
# Building the two arms.
# --------------------------------------------------------------------------


def render_role_prompt(role_block: str, evidence: str) -> str:
    """Turn a role block into an actual system prompt.

    The upstream skill states what each sub-agent prompt *should include*; it
    does not carry the prompt itself. Something has to render one, and this
    minimal preamble is that something — deliberately as thin as it can be, so
    the reviewer's behaviour comes from the extracted block and not from us.
    """
    return (
        "You are one reviewer in a code review. These are your instructions:\n\n"
        f"{role_block}\n\n"
        "--- The concrete inputs your instructions refer to ---\n\n"
        f"{evidence}\n\n"
        "Use your read-only tools to gather what you need, then write your "
        "report as your final message."
    )


def render_whole_skill_prompt(skill: str, role: str, evidence: str) -> str:
    """Arm B: the same reviewer, handed the whole skill instead."""
    return (
        f"You are the {role} reviewer in a code review. This is the skill you "
        "are working from:\n\n"
        f"{skill}\n\n"
        "--- The concrete inputs the skill refers to ---\n\n"
        f"{evidence}\n\n"
        "Use your read-only tools to gather what you need, then write your "
        "report as your final message."
    )


def main() -> int:
    global WORKSPACE

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="clone of the sandbox repo")
    parser.add_argument("--skill", required=True, help="pinned code-review SKILL.md")
    parser.add_argument("--spec", required=True, help="the spec the candidate answers")
    parser.add_argument("--base", required=True, help="the fixed point")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--out", default="transcripts")
    parser.add_argument("--arms", default="A,B")
    args = parser.parse_args()

    WORKSPACE = Path(args.workspace).resolve()
    skill_md = Path(args.skill)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    diff_command = f"git diff {args.base}...{args.head}"
    commit_list = subprocess.run(
        ["git", "-C", str(WORKSPACE), "log", f"{args.base}..{args.head}", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    common = (
        f"Diff command: `{diff_command}` (call git_diff with base="
        f"{args.base!r} and head={args.head!r}).\n"
        f"Commit list:\n{commit_list}\n"
    )
    standards_evidence = common + (
        "\nStandards sources found in this repository: `CODING_STANDARDS.md`.\n"
    )
    spec_evidence = common + (
        "\nThe originating spec, fetched in full:\n\n"
        "```\n" + Path(args.spec).read_text().strip() + "\n```\n"
    )
    evidence = {roles.STANDARDS: standards_evidence, roles.SPEC: spec_evidence}

    task = (
        "Review the candidate against the fixed point and write your report. "
        "Do not modify anything: your tools are read-only."
    )

    conversations: list[Conversation] = []
    for arm in [a.strip().upper() for a in args.arms.split(",") if a.strip()]:
        for role in (roles.STANDARDS, roles.SPEC):
            if arm == "A":
                system = render_role_prompt(
                    roles.role_instructions(skill_md, role), evidence[role]
                )
            else:
                system = render_whole_skill_prompt(
                    roles.whole_skill(skill_md), role, evidence[role]
                )
            conversations.append(Conversation(arm=arm, role=role, system=system))

    for conv in conversations:
        print(f"\n=== running {conv.label} ===", flush=True)
        run_conversation(conv, task)
        payload = {
            "arm": conv.arm,
            "role": conv.role,
            "model": MODEL,
            "iterations": conv.iterations,
            "hit_iteration_cap": conv.hit_iteration_cap,
            "tool_calls": conv.tool_calls,
            "unknown_tool_calls": conv.unknown_tool_calls,
            "usage": conv.usage,
            "fanout_signals_in_report": scan(conv.report, FANOUT_PATTERNS),
            "reentry_signals_in_report": scan(conv.report, REENTRY_PATTERNS),
            "fanout_signals_anywhere": scan(
                " ".join(t.get("text", "") for t in conv.transcript if t["role"] == "assistant"),
                FANOUT_PATTERNS,
            ),
            "report": conv.report,
            "transcript": conv.transcript,
        }
        (out / f"{conv.label}.json").write_text(json.dumps(payload, indent=2))
        (out / f"{conv.label}.report.md").write_text(conv.report)

    print("\n" + "=" * 72)
    print(f"{'conversation':<28} {'iters':>5} {'tools':>5} {'unknown':>7} "
          f"{'fanout':>7} {'reentry':>7} {'words':>6}")
    print("=" * 72)
    for conv in conversations:
        print(
            f"{conv.label:<28} {conv.iterations:>5} {len(conv.tool_calls):>5} "
            f"{len(conv.unknown_tool_calls):>7} "
            f"{len(scan(conv.report, FANOUT_PATTERNS)):>7} "
            f"{len(scan(conv.report, REENTRY_PATTERNS)):>7} "
            f"{len(conv.report.split()):>6}"
        )
    print("=" * 72)
    print(f"transcripts written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
