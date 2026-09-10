"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

Answers issue #25. Two arms over an identical task, workspace, budget and
toolset, differing in exactly one thing: how the conversation is compacted when
it crosses the threshold.

  KEEP  the pinned prefix (Attempt Header + the injected skills) lives outside
        the evictable region; eviction drops whole exchange units from the
        oldest end. The candidate design.
  TAIL  one flat message list truncated to its last N messages. The loop
        everybody writes first, and the control that shows what "never
        compacted" is actually buying.

Run on both pins, because compaction is history rewriting by another name and
ADR 0010 says the OpenAI Responses turn carries a `reasoning` block the
provider expects back.

The threshold is set far below any real context window. `contract §5` makes the
compaction threshold a tuning constant, so lowering it exercises the same
machinery at a hundredth of the cost — the mechanism is what is under test, not
the size of the window.

Everything is recorded: the compaction event log, the measured prompt size per
turn per provider, every truncated tool result and whether anyone read a slice
of it, the cache accounting for the re-sent prefix, and the recall probe that
asks the model to say the Seam Set back.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import workspace
from context import KeepPinned, NaiveTail, PinnedMaterialTooLarge
from pins import PINS

MAX_ITERATIONS = 30
THRESHOLD_TOKENS = 20_000  # a tuning constant (§5), set low on purpose
TAIL_KEEP = 9              # odd, so the naive cut can land mid-unit
TOOL_RESULT_CAP = 4_000    # characters; L3-IMP-5's cap
HEAD_TAIL = 1_500          # characters of head and of tail kept in a capped result

STATE: dict = {}


# --------------------------------------------------------------------------
# The per-tool-result cap: head + tail + a pointer to the artifact (L3-IMP-5).
# --------------------------------------------------------------------------


def _cap(text: str, origin: str) -> str:
    if len(text) <= TOOL_RESULT_CAP:
        return text
    artifacts = STATE["artifacts"]
    artifact_id = f"artifact-{len(artifacts) + 1}"
    artifacts[artifact_id] = {"origin": origin, "text": text}
    STATE["truncations"].append(
        {"step": STATE["step"], "artifact": artifact_id, "origin": origin, "chars": len(text)}
    )
    return (
        text[:HEAD_TAIL]
        + f"\n\n[... {len(text) - 2 * HEAD_TAIL} characters elided ...]\n"
        + f"[full result stored as {artifact_id}: {len(text)} characters from {origin}]\n\n"
        + text[-HEAD_TAIL:]
    )


def _inside(path: str) -> Path | None:
    target = (STATE["ws"] / path).resolve()
    return target if target.is_relative_to(STATE["ws"].resolve()) else None


@tool
def read_file(path: str) -> str:
    """Read a file from the repository. Path is repo-relative."""
    target = _inside(path)
    if target is None or not target.is_file():
        return f"[no such file: {path}]"
    return _cap(target.read_text(), f"read_file({path})")


@tool
def read_slice(artifact: str, start: int, end: int) -> str:
    """Read a range of characters from a stored result that was too large to return in full."""
    STATE["slice_reads"].append({"step": STATE["step"], "artifact": artifact, "start": start, "end": end})
    stored = STATE["artifacts"].get(artifact)
    if stored is None:
        return f"[no such artifact: {artifact}]"
    return _cap(stored["text"][start:end], f"read_slice({artifact})")


@tool
def write_file(path: str, content: str) -> str:
    """Write a file in the repository, creating or replacing it. Path is repo-relative."""
    target = _inside(path)
    if target is None:
        return f"[path outside the repository: {path}]"
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    STATE["writes"].append({"step": STATE["step"], "path": path, "chars": len(content), "replaced": existed})
    target.write_text(content)
    return f"[{'replaced' if existed else 'created'} {path}, {len(content)} characters]"


@tool
def list_dir(path: str = ".") -> str:
    """List the entries of a directory in the repository. Path is repo-relative."""
    target = _inside(path)
    if target is None or not target.is_dir():
        return f"[no such directory: {path}]"
    entries = sorted(f"{p.name}/" if p.is_dir() else p.name for p in target.iterdir() if p.name != ".git")
    return "\n".join(entries) or "[empty]"


@tool
def run_tests(path: str = "") -> str:
    """Run the test suite, or a single test file if `path` is given."""
    argv = ["uv", "run", "--quiet", "--with", "pytest", "--no-project", "python", "-m", "pytest", "-q"]
    if path:
        argv.append(path)
    done = subprocess.run(argv, cwd=STATE["ws"], capture_output=True, text=True, timeout=300)
    STATE["test_runs"].append({"step": STATE["step"], "path": path or "<all>", "returncode": done.returncode,
                               "tail": done.stdout[-300:]})
    return _cap(f"[exit {done.returncode}]\n{done.stdout}\n{done.stderr}", "run_tests")


@tool
def git_diff() -> str:
    """The diff of the work done so far against the base revision."""
    done = subprocess.run(["git", "-C", str(STATE["ws"]), "diff", "base"], capture_output=True, text=True)
    return _cap(done.stdout or "[no changes]", "git_diff")


TOOLS = [read_file, read_slice, write_file, list_dir, run_tests, git_diff]


# --------------------------------------------------------------------------
# The pinned prefix: the Attempt Header, then the skills, verbatim (ADR 0007).
# --------------------------------------------------------------------------

ATTEMPT_HEADER = """\
--- Attempt Header ---

Attempt: #1 of 3 for issue orders#412.
Base Revision: branch `base`. Working branch: `work`.
Pinned Model: {model}
Seam Set ({seam_id}), resolved by the Worker before this conversation opened:

  - `orders.pricing.apply_discount(price: float, discount: float) -> float`

The Seam Set is the set of interfaces this Attempt is authorised to work
against. It was derived from the Target Issue and is a settled fact of this
Attempt: where the instructions below tell you to confirm the seams with the
user, they are already confirmed, and this Header is that confirmation. There
is no user in this conversation to ask.

Carried Decisions: none (this is the first Attempt on this issue).
"""

PREAMBLE = "\n--- The skills this step of the pipeline runs ---\n\n"


def pinned_system(bundle: Path, model: str) -> str:
    parts = [ATTEMPT_HEADER.format(model=model, seam_id=workspace.SEAM_ID), PREAMBLE]
    for skill in ("implement", "tdd", "codebase-design"):
        parts.append(f"### skill: {skill}\n\n" + (bundle / skill / "SKILL.md").read_text() + "\n\n")
    parts.append(
        "--- Your tools ---\n\n"
        "This conversation has no slash commands and no way to activate a further "
        "skill; the instructions above are everything you have. There is no commit "
        "tool: the pipeline commits your work after this step ends.\n"
    )
    return "".join(parts)


TASK = """\
Implement the work described by this ticket, in the repository in your workspace.

--- The ticket ---

{ticket}

--- Where you are ---

The workspace is a clone of the target repository. Your branch is `work`; the
base revision is the branch `base`. `git_diff` shows what you have changed so
far. The repository's own conventions are in the files at its root.
"""

RECALL_PROBE = """\
Before you finish, one question about this Attempt itself, answered from what \
you have in front of you and not from what you remember doing:

State the Seam Set identifier you were given for this Attempt, and the symbols \
it names. If you do not have it, say so plainly.
"""


# --------------------------------------------------------------------------
# The loop.
# --------------------------------------------------------------------------


@dataclass
class Run:
    arm: str
    provider: str
    model: str
    transcript: list[dict] = field(default_factory=list)
    compactions: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    unknown_tool_calls: list[dict] = field(default_factory=list)
    final: str = ""
    recall: str = ""
    iterations: int = 0
    hit_iteration_cap: bool = False
    refused_to_open: str = ""
    error: str = ""
    usage: dict[str, int] = field(default_factory=lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})

    @property
    def label(self) -> str:
        return f"{self.arm}-{self.provider}"


def _account(run: Run, response: AIMessage) -> dict:
    usage = response.usage_metadata or {}
    details = usage.get("input_token_details", {}) or {}
    # #24: a cache write may land in a provider-specific field while the
    # normalised one stays zero. Read both rather than trusting either.
    write = details.get("cache_creation", 0) or details.get("ephemeral_5m_input_tokens", 0) \
        or details.get("ephemeral_1h_input_tokens", 0)
    read = details.get("cache_read", 0)
    run.usage["input"] += usage.get("input_tokens", 0)
    run.usage["output"] += usage.get("output_tokens", 0)
    run.usage["cache_read"] += read
    run.usage["cache_write"] += write
    return {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
            "cache_read": read, "cache_write": write}


def _system_message(text: str, cache: bool):
    """Anthropic's cache breakpoint is a content-block annotation. #24 measured
    zero cache hits without one, so the arm that re-sends the prefix every turn
    is measured both ways."""
    if not cache:
        return SystemMessage(text)
    return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])


def run_arm(run: Run, pin, system_text: str, task: str, cache: bool) -> Run:
    by_name = {t.name: t for t in TOOLS}
    llm = pin.build().bind_tools(TOOLS)
    pinned = [_system_message(system_text, cache), HumanMessage(task)]

    if run.arm == "KEEP":
        policy = KeepPinned(pinned=pinned)
    else:
        policy = NaiveTail(pinned=pinned, keep=TAIL_KEEP)

    run.transcript.append({"role": "system", "text": system_text})
    run.transcript.append({"role": "user", "text": task})

    scale = 1.0
    probe_sent = False

    for step in range(MAX_ITERATIONS):
        run.iterations = step + 1
        STATE["step"] = step + 1

        try:
            dropped = policy.compact(THRESHOLD_TOKENS, scale=scale)
        except PinnedMaterialTooLarge as exc:
            run.refused_to_open = str(exc)
            return run
        if dropped:
            run.compactions.append({"step": step + 1, "dropped": dropped,
                                    "messages_left": len(policy.messages()),
                                    "scale": round(scale, 3)})

        messages = policy.messages()
        try:
            response: AIMessage = llm.invoke(messages)
        except Exception as exc:  # the trap arm is expected to land here, or not
            run.error = f"{type(exc).__name__}: {exc}"
            run.turns.append({"step": step + 1, "sent_messages": len(messages), "error": run.error})
            return run

        counts = _account(run, response)
        local = policy.tokens(messages) or 1
        scale = (counts["input"] / local) if counts["input"] else scale
        run.turns.append({"step": step + 1, "sent_messages": len(messages), **counts,
                          "first_role_sent": messages[0].type})

        # ADR 0010: the object received is the object carried back. Never rebuilt.
        policy.open_unit(response)

        text = response.text() if callable(response.text) else str(response.content)
        run.transcript.append({"role": "assistant", "text": text,
                               "tool_calls": [{"name": c["name"], "args": c["args"]} for c in response.tool_calls]})
        print(f"  [{run.label}] step {step + 1}: sent {len(messages)} msgs / {counts['input']} tok,"
              f" {len(response.tool_calls)} call(s) {[c['name'] for c in response.tool_calls]}", flush=True)

        if not response.tool_calls:
            if not probe_sent:
                # The recall probe. Asked only once, after the work is done, so
                # it costs one turn and cannot steer the run that preceded it.
                probe_sent = True
                run.final = text
                policy.open_unit(HumanMessage(RECALL_PROBE))
                run.transcript.append({"role": "user", "text": RECALL_PROBE})
                continue
            run.recall = text
            return run

        for call in response.tool_calls:
            name = call["name"]
            if name in by_name:
                result = str(by_name[name].invoke(call["args"]))
            else:
                run.unknown_tool_calls.append({"step": step + 1, "name": name, "args": call["args"]})
                result = f"[no tool named {name!r} exists in this conversation's toolset]"
            policy.extend_unit(ToolMessage(content=result, tool_call_id=call["id"], name=name))
            run.transcript.append({"role": "tool", "name": name, "args": call["args"], "text": result[:1500]})

    # The recall probe is owed even here — arguably especially here, since a
    # loop that ran out of budget is exactly the long loop L3-IMP-4 is about.
    run.hit_iteration_cap = True
    if not run.recall:
        policy.open_unit(HumanMessage(RECALL_PROBE))
        run.transcript.append({"role": "user", "text": RECALL_PROBE})
        try:
            final = llm.invoke(policy.messages())
            _account(run, final)
            run.recall = final.text() if callable(final.text) else str(final.content)
            run.transcript.append({"role": "assistant", "text": run.recall})
        except Exception as exc:
            run.error = run.error or f"{type(exc).__name__}: {exc}"
    return run


# --------------------------------------------------------------------------
# What counts as evidence.
# --------------------------------------------------------------------------

LOST_HEADER = {
    "says it lacks the seam": r"\b(do not|don't|no longer|cannot|can't)\b.{0,40}\b(seam|header|identifier)\b"
                              r"|\bnot (given|provided)\b.{0,30}\bseam",
    "invents a seam identifier": r"\bSEAM-(?!7Q4M)[A-Z0-9]{3,}\b",
}


def verdict(run: Run) -> dict:
    recall = run.recall or ""
    return {
        "recalled_seam_id": workspace.SEAM_ID in recall,
        "recalled_symbol": "apply_discount" in recall,
        "signals_on_recall": [k for k, p in LOST_HEADER.items() if re.search(p, recall, re.IGNORECASE)],
        "read_a_slice": bool(STATE["slice_reads"]),
        "truncations": len(STATE["truncations"]),
        "compactions": len(run.compactions),
        "provider_error": run.error,
    }


def main() -> int:
    global THRESHOLD_TOKENS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="skill bundle root: <bundle>/<skill>/SKILL.md")
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--out", default="transcripts")
    parser.add_argument("--arms", default="KEEP,TAIL")
    parser.add_argument("--providers", default="anthropic,openai")
    parser.add_argument("--no-cache", action="store_true", help="run KEEP without a cache breakpoint")
    parser.add_argument("--threshold", type=int, default=THRESHOLD_TOKENS,
                        help="compaction threshold in tokens; a tuning constant (§5). The pins do "
                             "not count an identical prompt alike (#24: 6818 vs 4414), so the "
                             "threshold that makes one pin compact may leave the other untouched.")
    args = parser.parse_args()
    THRESHOLD_TOKENS = args.threshold

    bundle = Path(args.bundle).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch).resolve()

    summaries = []
    for arm in [a.strip().upper() for a in args.arms.split(",") if a.strip()]:
        for provider in [p.strip() for p in args.providers.split(",") if p.strip()]:
            pin = PINS[provider]
            ws = workspace.build(scratch / f"ws-{arm}-{provider}")
            STATE.clear()
            STATE.update(ws=ws, step=0, artifacts={}, truncations=[], slice_reads=[],
                         writes=[], test_runs=[])

            run = Run(arm=arm, provider=provider, model=pin.model)
            system_text = pinned_system(bundle, pin.model)
            cache = pin.supports_cache_breakpoint and not args.no_cache
            print(f"\n=== {run.label}: prefix {len(system_text)} chars, cache_breakpoint={cache} ===", flush=True)

            run_arm(run, pin, system_text, TASK.format(ticket=workspace.TICKET.strip()), cache)

            # The verdict pytest gives, not the one the model gives (#22's rule).
            done = subprocess.run(
                ["uv", "run", "--quiet", "--with", "pytest", "--no-project", "python", "-m", "pytest", "-q"],
                cwd=ws, capture_output=True, text=True, timeout=300)
            diff = subprocess.run(["git", "-C", str(ws), "diff", "base"], capture_output=True, text=True).stdout
            (out / f"{run.label}.diff").write_text(diff)

            payload = {
                "arm": run.arm, "provider": run.provider, "model": run.model,
                "prefix_chars": len(system_text), "threshold_tokens": THRESHOLD_TOKENS,
                "tool_result_cap": TOOL_RESULT_CAP, "tail_keep": TAIL_KEEP,
                "iterations": run.iterations, "hit_iteration_cap": run.hit_iteration_cap,
                "refused_to_open": run.refused_to_open, "error": run.error,
                "usage": run.usage, "turns": run.turns, "compactions": run.compactions,
                "truncations": STATE["truncations"], "slice_reads": STATE["slice_reads"],
                "writes": STATE["writes"], "test_runs": STATE["test_runs"],
                "unknown_tool_calls": run.unknown_tool_calls,
                "final_suite": {"returncode": done.returncode, "tail": done.stdout[-600:]},
                "verdict": verdict(run),
                "final": run.final, "recall": run.recall,
                "transcript": run.transcript,
            }
            (out / f"{run.label}.json").write_text(json.dumps(payload, indent=2, default=str))
            summaries.append({k: payload[k] for k in
                              ("arm", "provider", "iterations", "usage", "error", "verdict", "final_suite")})
            print(json.dumps(summaries[-1], indent=2, default=str), flush=True)

    (out / "summary.json").write_text(json.dumps(summaries, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
