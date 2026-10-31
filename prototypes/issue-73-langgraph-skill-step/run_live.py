"""PROTOTYPE (issue #73): the LangGraph arm against the real Pinned Model.

    /path/to/coding-agent/.venv/bin/python run_live.py [workflow] [label]

Reads ANTHROPIC_API_KEY from the repository's `.env` (never printed).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import messages_to_dict
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

import runtime as R
from coding_agent.implement.ceilings import LoopCeilings
from coding_agent.provider.config import DEFAULT_PRICE_TABLE, PINNED_MODELS
from coding_agent.provider.pinned_model import build_chat_model

HERE = Path(__file__).parent
REPO = Path("/Volumes/Sources/python/ai/coding-agent")
ISSUE = (HERE / "fixture/.issues/1.md").read_text()
TITLE, BODY = ISSUE.split("\n", 1)[0].removeprefix("# "), ISSUE.split("\n", 1)[1].strip()


def serde_roundtrip_ok(messages: list) -> dict:
    """Would a per-turn checkpointer carry every assistant turn back
    verbatim (ADR 0010)? Round-trip each through LangGraph's serializer."""
    serde = JsonPlusSerializer()
    checked, mismatched = 0, []
    for i, m in enumerate(messages):
        if m.type != "ai":
            continue
        checked += 1
        back = serde.loads_typed(serde.dumps_typed(m))
        if back.content != m.content or back.tool_calls != m.tool_calls or back.additional_kwargs != m.additional_kwargs:
            mismatched.append(i)
    return {"assistant_turns": checked, "mismatched": mismatched}


def main(workflow: str = "implement", label: str = "live") -> None:
    load_dotenv(REPO / ".env")
    pin = PINNED_MODELS["anthropic"]
    root = HERE / ".work" / f"live-{label}"
    workspace, remote, _ = R.prepare_workspace(HERE / "fixture", root)
    steps, sel, limits = R.load_workflow(HERE / "workflows.yaml", workflow)
    rt = R.Runtime(
        attempt_id="#1/1", workspace=workspace, evidence_dir=root / "evidence", remote=remote,
        tracker=R.FakeTracker(number=1, title=TITLE, body=BODY, labels={sel}), steps=steps, selection_label=sel,
        model_factory=lambda role: build_chat_model(pin), price=DEFAULT_PRICE_TABLE[pin.key],
        ceilings=LoopCeilings(max_wall_clock_seconds=3600, max_cost_usd=10.0,
                              max_tool_calls=limits.get("tool_calls", 600)),
        live=True)
    started = time.monotonic()
    error = None
    try:
        final = R.run_attempt(rt)
    except Exception as exc:  # keep whatever evidence exists
        final, error = {}, f"{type(exc).__name__}: {exc}"
    totals = rt.ledger.totals
    record = {
        "arm": "langgraph", "workflow": workflow, "model": pin.key, "error": error,
        "elapsed_s": round(time.monotonic() - started, 1),
        "trace": final.get("trace"), "implementation_outcome": final.get("implementation_outcome"),
        "verdict": final.get("verdict"), "final_checks": final.get("final_checks"),
        "step_reports": final.get("step_reports"), "reviews": rt.reviews, "review_invocations": rt.review_invocations,
        "activations": {k: v for k, v in rt.activations.items() if "|" not in k},
        "criterion_evidence": rt.criterion_evidence,
        "usage": {"tokens": totals.tokens, "effective_tokens": totals.effective_tokens,
                  "cost_usd": round(totals.cost_usd, 4), "tool_calls": totals.tool_calls},
        "tracker_writes": rt.tracker.writes, "prs": rt.tracker.prs,
        "git_log": subprocess.run(["git", "log", "--format=%h %s", f"{rt.base_revision}..HEAD"], cwd=workspace, capture_output=True, text=True).stdout,
        "diff": subprocess.run(["git", "diff", rt.base_revision, "HEAD"], cwd=workspace, capture_output=True, text=True).stdout,
        "serde_roundtrip": {actor: serde_roundtrip_ok(msgs) for actor, msgs in rt.conversations.items()},
        "events": rt.events,
    }
    out = HERE / "transcripts"
    (out / f"live-{label}.json").write_text(json.dumps(record, indent=2, default=str))
    (out / f"live-{label}.conversations.json").write_text(json.dumps(
        {actor: messages_to_dict(msgs) for actor, msgs in rt.conversations.items()}, indent=1, default=str))
    print(json.dumps({k: record[k] for k in ("error", "elapsed_s", "trace", "implementation_outcome", "verdict",
                                             "review_invocations", "activations", "usage", "serde_roundtrip")}, indent=1, default=str))


if __name__ == "__main__":
    main(*sys.argv[1:])
