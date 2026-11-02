"""Scratch probe for handoff threshold tuning (deleted before finishing)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.context_handoff import ContextWindowConfig
from coding_agent.runtime.graph.attempt import run_attempt
from conftest import init_origin_repo, run_git
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from test_step_bounded_context import _commit_step_profile


def _ai(content: str = "", calls: list[dict[str, object]] | None = None) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=list(calls or []),
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _run(
    threshold: int, outcomes: list[AIMessage], skills_dir: Path
) -> tuple[str, str, int]:
    tracker = FakeTracker()
    model = ScriptedModel(list(outcomes))
    with tempfile.TemporaryDirectory() as tmp:
        tmpp = Path(tmp)
        ledger = RunLedger(tmpp / "ledger.sqlite3")
        try:
            src = tmpp / "origin-src"
            init_origin_repo(src)
            _commit_step_profile(src)
            bare = tmpp / "remote.git"
            subprocess.run(
                ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
            )
            workdir = tmpp / "workspace"
            run_git(["clone", "-q", str(bare), str(workdir)], cwd=tmpp)
            result = run_attempt(
                issue_number=7,
                attempt_number=1,
                tracker=tracker,
                ledger=ledger,
                model=model,
                workspace=workdir,
                effective_configuration={"repo": "x/y", "issue": 7},
                skills_dir=skills_dir,
                context_window=ContextWindowConfig(
                    threshold_tokens=threshold,
                    safety_margin_tokens=0,
                    request_overhead_tokens=0,
                ),
            )
            return result.outcome, result.reason, len(model.invocations)
        finally:
            ledger.close()


def test_probe_thresholds(skills_dir: Path) -> None:
    _ = skills_dir
    outcomes = [
        _ai(calls=[_call("activate_implement", {}, "c-act")]),
        _ai(content="thinking..."),
        _ai(content="thinking again..."),
        _ai(content="still thinking..."),
    ]
    for threshold in [700, 800, 850, 900, 950, 1000, 1040, 1060, 1080, 1100]:
        outcome, reason, invocations = _run(threshold, outcomes, skills_dir)
        print(
            f"\nTHRESHOLD {threshold}: outcome={outcome} reason={reason[:80]!r} "
            f"invocations={invocations}"
        )
