"""M7 hard ceilings: a crossed ceiling stops the Attempt inside the node.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Hard ceilings":
after every model response and every tool result the four ceilings — wall
clock, cost, tokens and tool calls — are checked; after a crossing there is
no further model call and no model-initiated tool call; usage is flushed to
the Run Ledger first; then a deterministic finalization commits the
existing edits and pushes the branch with no final checks, ending
``failed-limit`` naming the limit; the wall clock is measured from the
Attempt's opening in the Run Ledger, never from process start, so review
time counts too; after the Delivery Intent publication completes, a meter
cannot stop it), §14 (the Implementation Outcome → Terminal Outcome table)
and §15 (the terminal comment carries the limit crossed, the spend and the
Continuation Snapshot).

MVP row ids: C-LIM-1, P-LIM-1, L3-IMP-7.

Every push here goes to a local bare remote; no test touches the network.
Time-based crossings drive the production ledger seam
(``RunLedger.attempt_elapsed_seconds``) through a monkeypatched clock: the
fake stands in for review time passing, while the check under test still
reads the elapsed from the ledger rather than from process start.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

import pytest

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import BudgetSnapshot, LedgerRecord, RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import LoopCeilings, UsageTotals
from coding_agent.runtime.graph.attempt import (
    AttemptResult,
    run_attempt,
)
from coding_agent.runtime.graph.context import AttemptContext
from coding_agent.runtime.step_progress import StepProgress
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import REVIEW_SKILL_TEXT

IDENTITY = Identity(account_id=999, login="coding-agent")

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

EXPECTED_BRANCH = "agent/7/1-add-a-greeting"

GREEN_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

_PRICE = TokenPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    (src / "CODING_STANDARDS.md").write_text("# Standards\n", encoding="utf-8")
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(GREEN_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _activate(
    call_id: str = "call-act-1", *, usage: dict[str, object] | None = None
) -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[_call("activate_implement", {}, call_id)],
        usage_metadata=usage,
    )


def _write(
    path: str = "greeting.txt",
    write_id: str = "call-write-1",
    *,
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content="writing",
        tool_calls=[_call("write_file", {"path": path, "content": "hello\n"}, write_id)],
        usage_metadata=usage,
    )


def _write_and_test(
    write_id: str = "call-write-1",
    test_id: str = "call-test-1",
    *,
    usage: dict[str, object] | None = None,
) -> AIMessage:
    return AIMessage(
        content="writing then testing",
        tool_calls=[
            _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id),
            _call("test_targeted", {"path": "tests/test_greet.py"}, test_id),
        ],
        usage_metadata=usage,
    )


def _review(call_id: str = "call-rev-1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[_call("activate_code_review", {}, call_id)],
    )


def _spawn_pair(s1: str = "call_spawn_1", s2: str = "call_spawn_2") -> AIMessage:
    return AIMessage(
        content="reviewing with subagents",
        tool_calls=[
            _call("spawn_subagent", {"description": "review standards", "instructions": "review the change against the Target Issue"}, s1),
            _call("spawn_subagent", {"description": "review spec", "instructions": "review the change against the Target Issue"}, s2),
        ],
    )


def _report(call_id: str, report: str = "no findings") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[_call("report_subagent", {"report": report}, call_id)],
    )


def _completion(call_id: str) -> AIMessage:
    return AIMessage(
        content="the change is complete",
        tool_calls=[_call("report_completion", {"AC-1": ["pkg::test_ok"]}, call_id)],
    )


def _run_limits(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    model: ScriptedModel,
    bare_remote: Path,
    ceilings: LoopCeilings,
    price: TokenPrices | None = None,
) -> tuple[AttemptResult, FakeTracker]:
    """Run one Attempt against the local bare remote with explicit ceilings."""
    (skills_dir / "code-review" / "SKILL.md").write_text(
        REVIEW_SKILL_TEXT, encoding="utf-8"
    )
    tracker = FakeTracker()
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
        remote_url=str(bare_remote),
        agent_identity=IDENTITY,
        ceilings=ceilings,
        price=price,
    )
    return result, tracker


def _remote_branches(bare_remote: Path) -> list[str]:
    output = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], bare_remote
    )
    return sorted(line for line in output.splitlines() if line.strip())


def _events(ledger: RunLedger, kind: str) -> list[LedgerRecord]:
    return [record for record in ledger.records("#7/1") if record.kind == kind]


def _terminal_body(ledger: RunLedger) -> str:
    stored = ledger.get_outcome("#7/1")
    assert stored is not None
    return stored.terminal_comment_body


def _assert_limit_finalization(
    *,
    ledger: RunLedger,
    tracker: FakeTracker,
    bare_remote: Path,
    limit: str,
) -> None:
    """The deterministic §11 finalization after a crossed ceiling (C-LIM-1).

    The branch is pushed with no pull request, no final checks run, the
    terminal comment names the limit with the spend and the Continuation
    Snapshot, ``ready-for-human`` is added, and the crossing stopped the
    loop with the spend already durable as a readable per-bucket record.
    """
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert ledger.get_effect("#7/1", "delivery-snapshot-push") is not None
    assert ledger.get_effect("#7/1", "open-pull-request") is None
    assert "open_pull_request" not in tracker.call_names()
    assert _events(ledger, "final_checks") == []
    assert "add_ready_for_human" in tracker.call_names()
    body = _terminal_body(ledger)
    assert f"limit crossed: {limit}" in body
    assert "Continuation Snapshot" in body
    usages = _events(ledger, "usage")
    assert usages, "the ceiling finalization flushes usage first"
    payload = usages[-1].payload["usage"]
    assert isinstance(payload, dict)
    for key in (
        "tokens",
        "effective_tokens",
        "cost_usd",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_input_usd",
        "cost_output_usd",
        "cost_cache_read_usd",
        "cost_cache_write_usd",
    ):
        assert key in payload, f"readable usage record carries {key}"


def test_p_lim_1_tool_calls_ceiling(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-LIM-1: a tool-call crossing ends ``failed-limit`` naming the limit."""
    model = ScriptedModel([_activate(), _write()])
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_tool_calls=1),
    )
    assert (result.outcome, result.reason) == ("failed-limit", "tool_calls")
    assert len(model.invocations) == 2
    assert "2 tool calls" in _terminal_body(ledger)
    _assert_limit_finalization(
        ledger=ledger, tracker=tracker, bare_remote=bare_remote, limit="tool_calls"
    )


def test_p_lim_1_cost_ceiling(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-LIM-1: a cost crossing ends ``failed-limit`` naming the limit."""
    crossed: dict[str, object] = {
        "input_tokens": 250_000,
        "output_tokens": 100_000,
        "total_tokens": 350_000,
    }
    model = ScriptedModel([_activate(), _write(usage=crossed)])
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_cost_usd=1.0),
        price=_PRICE,
    )
    assert (result.outcome, result.reason) == ("failed-limit", "cost")
    assert len(model.invocations) == 2
    body = _terminal_body(ledger)
    assert "spend $1.5" in body
    _assert_limit_finalization(
        ledger=ledger, tracker=tracker, bare_remote=bare_remote, limit="cost"
    )
    usages = _events(ledger, "usage")
    assert usages[-1].payload["usage"]["cost_usd"] == 1.5  # type: ignore[index]


def test_p_lim_1_tokens_ceiling(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-LIM-1: an effective-token crossing ends ``failed-limit`` naming it."""
    opening: dict[str, object] = {"input_tokens": 60, "output_tokens": 10, "total_tokens": 70}
    crossed: dict[str, object] = {"input_tokens": 60, "output_tokens": 50, "total_tokens": 110}
    model = ScriptedModel([_activate(usage=opening), _write(usage=crossed)])
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_effective_tokens=100),
    )
    assert (result.outcome, result.reason) == ("failed-limit", "tokens")
    assert len(model.invocations) == 2
    assert "180 effective tokens (180 total)" in _terminal_body(ledger)
    _assert_limit_finalization(
        ledger=ledger, tracker=tracker, bare_remote=bare_remote, limit="tokens"
    )


def test_p_lim_1_wall_clock_ceiling(
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    bare_remote: Path,
    monkeypatch: Any,
) -> None:
    """P-LIM-1: a wall-clock crossing ends ``failed-limit`` naming the limit.

    The clock under test is the ledger seam, not process start: elapsed
    stays zero until the write lands, then jumps past the ceiling.
    """
    real_elapsed = RunLedger.attempt_elapsed_seconds

    def _fake_elapsed(self: RunLedger, attempt_id: str, *, now: Any = None) -> float:
        _ = (real_elapsed, now)
        if (workspace / "greeting.txt").exists():
            return 3600.0
        return 0.0

    monkeypatch.setattr(RunLedger, "attempt_elapsed_seconds", _fake_elapsed)
    model = ScriptedModel([_activate(), _write()])
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_wall_clock_seconds=60.0),
    )
    assert (result.outcome, result.reason) == ("failed-limit", "wall_clock")
    assert len(model.invocations) == 2
    assert "limit crossed: wall_clock" in _terminal_body(ledger)
    _assert_limit_finalization(
        ledger=ledger, tracker=tracker, bare_remote=bare_remote, limit="wall_clock"
    )


def test_c_lim_1_ceiling_stops_the_loop_mid_tool_loop(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """C-LIM-1: the crossing stops the loop with no further model or tool call."""
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _write(path="second.txt", write_id="call-write-2"),
            AIMessage(content="never reached"),
        ]
    )
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_tool_calls=2),
    )
    assert (result.outcome, result.reason) == ("failed-limit", "tool_calls")
    # The write-and-test turn crossed the ceiling: no further model call, so
    # the second write's model-initiated tool call never ran either.
    assert len(model.invocations) == 2
    assert (workspace / "greeting.txt").exists()
    assert not (workspace / "second.txt").exists()
    usages = _events(ledger, "usage")
    assert usages, "usage is flushed before the finalization"
    assert usages[-1].payload["usage"]["tool_calls"] >= 3  # type: ignore[index]
    _assert_limit_finalization(
        ledger=ledger, tracker=tracker, bare_remote=bare_remote, limit="tool_calls"
    )


def test_l3_imp_7_wall_clock_crossed_during_review(
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    bare_remote: Path,
    monkeypatch: Any,
) -> None:
    """L3-IMP-7: wall clock measured from the ledger opening ends the Attempt.

    Review time counts: the clock stays flat through the implementer's turns
    and jumps only once both review subagents have reported, so the Attempt
    stops inside the invoking node before the completion claim — a
    process-start clock would still read ~0s against a 60s ceiling.
    """
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1", "standards: no findings"),
            _report("sub-report-2", "spec: no findings"),
            _completion("call-rep-1"),
        ]
    )

    def _fake_elapsed(self: RunLedger, attempt_id: str, *, now: Any = None) -> float:
        _ = (self, attempt_id, now)
        if len(model.invocations) >= 6:
            return 3600.0
        return 0.0

    monkeypatch.setattr(RunLedger, "attempt_elapsed_seconds", _fake_elapsed)
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_wall_clock_seconds=60.0),
    )
    assert (result.outcome, result.reason) == ("failed-limit", "wall_clock")
    # Both subagents reported, then the node stopped: the completion claim
    # that would otherwise verify the Attempt never ran.
    assert len(model.invocations) == 6
    assert "limit crossed: wall_clock" in _terminal_body(ledger)
    _assert_limit_finalization(
        ledger=ledger, tracker=tracker, bare_remote=bare_remote, limit="wall_clock"
    )


def test_post_intent_crossing_does_not_stop_publication(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """§11: after the Delivery Intent a meter cannot stop publication.

    A verified Attempt with generous ceilings still publishes; and the
    ``gate`` node — the only ceiling-checking node past ``run_step`` —
    passes a decided Attempt through untouched even with a crossed ceiling
    on record (it would flip an undecided Attempt instead).
    """
    from coding_agent.runtime.graph.attempt import gate

    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1", "standards: no findings"),
            _report("sub-report-2", "spec: no findings"),
            _completion("call-rep-1"),
        ]
    )
    result, tracker = _run_limits(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(
            max_tool_calls=600,
            max_wall_clock_seconds=3600.0,
            max_cost_usd=10.0,
            max_effective_tokens=4_000_000,
        ),
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert ledger.get_effect("#7/1", "open-pull-request") is not None

    def _context(progress: StepProgress) -> RunnableConfig:
        context = AttemptContext(
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            ceilings=LoopCeilings(max_wall_clock_seconds=60.0),
            progress=progress,
        )
        return {"configurable": context.to_configurable()}

    crossed = StepProgress("#7/1", LoopCeilings(max_wall_clock_seconds=60.0), ledger=ledger)
    crossed.last_snapshot = BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    crossed.ceiling = "wall_clock"
    undecided = gate(
        {"attempt_id": "#7/1", "issue_number": 7, "attempt_number": 1},
        _context(crossed),
    )
    assert undecided == {"outcome": "failed-limit", "reason": "wall_clock"}

    decided_progress = StepProgress(
        "#7/1", LoopCeilings(max_wall_clock_seconds=60.0), ledger=ledger
    )
    decided_progress.last_snapshot = BudgetSnapshot(
        elapsed_seconds=0.0, usage=UsageTotals()
    )
    decided_progress.ceiling = "wall_clock"
    untouched = gate(
        {
            "attempt_id": "#7/1",
            "issue_number": 7,
            "attempt_number": 1,
            "outcome": "delivered",
            "reason": "verified-completion",
        },
        _context(decided_progress),
    )
    assert untouched == {}
    stored = ledger.get_outcome("#7/1")
    assert stored is not None
    assert (stored.outcome, stored.reason) == ("delivered", "verified-completion")
