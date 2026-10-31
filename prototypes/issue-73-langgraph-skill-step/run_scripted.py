"""PROTOTYPE (issue #73): deterministic scenarios against a scripted model.

Each scenario drives the real graph, real file tools, real pytest and real
git against a fresh copy of the fixture; only the model is scripted. The
provider honours a forced `tool_choice`, like both pinned providers do (#60).

    uv run --project <repo> python run_scripted.py [scenario ...]
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage

import runtime as R
from coding_agent.implement.ceilings import LoopCeilings
from coding_agent.provider.config import DEFAULT_PRICE_TABLE, PINNED_MODELS

HERE = Path(__file__).parent
WORK = HERE / ".work"  # PROTOTYPE, wipe me
ISSUE = (HERE / "fixture/.issues/1.md").read_text()
TITLE, BODY = ISSUE.split("\n", 1)[0].removeprefix("# "), ISSUE.split("\n", 1)[1].strip()
_ids = itertools.count()

TESTS = '''from l2_fixture_py.text import slugify


def test_hello_world() -> None:
    assert slugify("Hello, World!") == "hello-world"


def test_collapses_runs() -> None:
    assert slugify("a  --  b") == "a-b"


def test_strips_edges() -> None:
    assert slugify("  -Hi-  ") == "hi"
'''
IMPL = '''from __future__ import annotations

import re


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''
FAILING = "def test_unrelated() -> None:\n    assert 1 == 2\n"
CLAIM = {"criteria": [{"criterion": "AC-1", "tests": ["tests/test_text.py::test_hello_world"]},
                      {"criterion": "AC-2", "tests": ["tests/test_text.py::test_collapses_runs"]},
                      {"criterion": "AC-3", "tests": ["tests/test_text.py::test_strips_edges"]}],
         "summary": "slugify added test-first."}


def call(name: str, **args: Any) -> tuple[str, str, dict[str, Any]]:
    return ("call", name, args)


def text(t: str) -> tuple[str, str]:
    return ("text", t)


TDD = [call("activate_tdd"),
       call("write_file", path="tests/test_text.py", content=TESTS),
       call("test_targeted", path="tests/test_text.py"),
       call("write_file", path="src/l2_fixture_py/text.py", content=IMPL),
       call("test_targeted", path="tests/test_text.py")]
SURVEY = [call("list_directory", path="."), call("read_file", path="src/l2_fixture_py/greeting.py"),
          call("report_step", status="done", justification="Package lives in src/l2_fixture_py; tests in tests/ use plain pytest.")]


class ScriptedModel:
    def __init__(self, moves: list[Any]) -> None:
        self.moves = list(moves)
        self.offered: list[list[str]] = []

    def bind_tools(self, tools: list[Any], tool_choice: str | None = None) -> "ScriptedModel._Bound":
        return ScriptedModel._Bound(self, [t.name for t in tools], tool_choice)

    class _Bound:
        def __init__(self, owner: "ScriptedModel", names: list[str], forced: str | None) -> None:
            self.owner, self.names, self.forced = owner, names, forced

        def invoke(self, _messages: Any) -> AIMessage:
            self.owner.offered.append(self.names)
            if self.forced:
                return AIMessage(content="", tool_calls=[{"name": self.forced, "args": {}, "id": f"c{next(_ids)}"}])
            if not self.owner.moves:
                return AIMessage(content="I believe the work is complete.")
            move = self.owner.moves.pop(0)
            if move[0] == "text":
                return AIMessage(content=move[1])
            return AIMessage(content="", tool_calls=[{"name": move[1], "args": move[2], "id": f"c{next(_ids)}"}])


class ScriptedReviewer:
    def __init__(self, seat: str) -> None:
        self.seat = seat

    def bind_tools(self, _tools: list[Any], tool_choice: str | None = None) -> "ScriptedReviewer":
        return self

    def invoke(self, messages: Any) -> AIMessage:
        assert "seat of a two-axis code review" in messages[0].content
        return AIMessage(content=f"No findings on the {self.seat} axis. (scripted)")


def scenario(name: str, moves: list[Any], *, workflow: str = "implement", body: str = BODY,
             hooks: dict[str, Callable[[R.Runtime, str], None]] | None = None,
             ceilings: LoopCeilings | None = None) -> dict[str, Any]:
    root = WORK / name
    workspace, remote, _base = R.prepare_workspace(HERE / "fixture", root)
    steps, label, _limits = R.load_workflow(HERE / "workflows.yaml", workflow)
    implementer = ScriptedModel(moves)
    pin = PINNED_MODELS["anthropic"]
    rt = R.Runtime(
        attempt_id="#1/1", workspace=workspace, evidence_dir=root / "evidence", remote=remote,
        tracker=R.FakeTracker(number=1, title=TITLE, body=body, labels={label}),
        steps=steps, selection_label=label,
        model_factory=lambda role: implementer if role == "implementer" else ScriptedReviewer(role.split(":")[1]),
        price=DEFAULT_PRICE_TABLE[pin.key],
        ceilings=ceilings or LoopCeilings(max_wall_clock_seconds=3600, max_cost_usd=20, max_tool_calls=600),
        hooks=hooks or {})
    final = R.run_attempt(rt)
    exits = {name: [names for names in implementer.offered if name in names] for name in ("report_step", "report_completion")}
    record = {
        "scenario": name, "workflow": workflow, "trace": final["trace"],
        "implementation_outcome": final.get("implementation_outcome"), "verdict": final["verdict"],
        "tracker_writes": rt.tracker.writes, "labels_after": sorted(rt.tracker.labels),
        "prs": rt.tracker.prs, "ledger": rt.ledger_records, "reviews": rt.reviews,
        "tool_calls_metered": rt.ledger.totals.tool_calls,
        "offered_per_turn": [sorted(n for n in names if n.startswith(("report_", "activate_step"))) for names in implementer.offered],
        "step_reports": final.get("step_reports"),
        "events": rt.events,
    }
    record["_exit_tools_offered"] = {k: len(v) for k, v in exits.items()}
    (HERE / "transcripts").mkdir(exist_ok=True)
    (HERE / "transcripts" / f"scripted-{name}.json").write_text(json.dumps(record, indent=2, default=str))
    return record


def remove_label(rt: R.Runtime, _hook: str) -> None:
    rt.tracker.labels.discard(rt.selection_label)


def edit_body(rt: R.Runtime, _hook: str) -> None:
    rt.tracker.body += "\n\nEdited by a human during the Attempt."


def sigterm(rt: R.Runtime, _hook: str) -> None:
    rt.sigterm = True


REVIEW = [call("activate_code_review")]
DONE = [call("report_completion", **CLAIM)]
SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "happy": lambda: scenario("happy", TDD + REVIEW + DONE),
    "no-review": lambda: scenario("no-review", TDD + DONE + DONE),
    "fourth-review": lambda: scenario("fourth-review", TDD + REVIEW * 4 + DONE),
    "silent": lambda: scenario("silent", [text("Looks fine to me."), text("Done."), text("Really done.")]),
    "unbacked-claim": lambda: scenario("unbacked-claim", TDD[:2] + [call("write_file", path="src/l2_fixture_py/text.py", content=IMPL)] + REVIEW + DONE * 3),
    "tool-call-ceiling": lambda: scenario("tool-call-ceiling", TDD + REVIEW + DONE, ceilings=LoopCeilings(max_tool_calls=4)),
    "red-final-check": lambda: scenario("red-final-check", TDD + [call("write_file", path="tests/test_other.py", content=FAILING)] + REVIEW + DONE),
    "drift": lambda: scenario("drift", TDD + REVIEW + DONE, hooks={"publication_gate": edit_body}),
    "no-criteria": lambda: scenario("no-criteria", TDD, body=BODY.split("## Acceptance criteria")[0]),
    "multi-step": lambda: scenario("multi-step", SURVEY + TDD + REVIEW + DONE, workflow="survey-then-implement"),
    "multi-cannot-continue": lambda: scenario("multi-cannot-continue", [call("report_step", status="cannot-continue", justification="The issue names a module that cannot exist.")] + TDD, workflow="survey-then-implement"),
    "multi-cancel-between-steps": lambda: scenario("multi-cancel-between-steps", SURVEY + TDD, workflow="survey-then-implement", hooks={"step_gate": remove_label}),
    "multi-sigterm-between-steps": lambda: scenario("multi-sigterm-between-steps", SURVEY + TDD, workflow="survey-then-implement", hooks={"step_gate": sigterm}),
}


def main(names: list[str]) -> None:
    for name in names or list(SCENARIOS):
        r = SCENARIOS[name]()
        v = r["verdict"]
        print(f"{name:28} outcome={r['implementation_outcome']!s:28} terminal={v['terminal']}"
              f"{'/' + v['reason'] if v.get('reason') else ''} pr={v['pr']} reviews={[x['status'] for x in r['reviews']]}"
              f" tool_calls={r['tool_calls_metered']}")
        print(f"{'':28} trace={' > '.join(r['trace'])}")
        print(f"{'':28} writes={r['tracker_writes']}")


if __name__ == "__main__":
    main(sys.argv[1:])
