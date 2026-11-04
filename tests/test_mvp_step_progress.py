"""M2 step contract: progress, soft stall, the verification reserve, cut-offs.

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Progress Gates
and the verification reserve": the Run Ledger records a ``progress_event``
only when a model response resolves an Acceptance Criterion or Seam Set
uncertainty, changes the candidate diff, or produces new Criterion Evidence
after such a change; five consecutive responses without one record a
``soft_stall``; a suppressed, unchanged targeted diagnostic enters the same
soft-stall path; at 70% of any Attempt-wide meter the ledger records one
durable ``Gate`` with a concrete next action and a remaining-budget snapshot;
at 80% it records ``reserve_entry``; broad exploration ends, the phase only
moves forward, and no silence continuation is issued in the reserve) and §9
(Implementation Outcomes come from the step subgraph alone; the category
follows the moment of acceptance, not the moment of measurement).

MVP row ids: L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-30, L1-3,
C-LIM-1, P-LIM-1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from langchain_core.messages import AIMessage, ToolMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import LoopCeilings, UsageTotals
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.silence import is_silence_continuation
from coding_agent.runtime.step_progress import (
    NARROW_NEXT_ACTION,
    StepProgress,
)
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

HAPPY_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_targeted.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

FAILING_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_flaky\\"><failure message=\\"boom\\"/></testcase></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_targeted.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

ATTEMPT_ID = "#7/1"


def _snapshot(tool_calls: int) -> BudgetSnapshot:
    return BudgetSnapshot(
        elapsed_seconds=0.0, usage=UsageTotals(tool_calls=tool_calls)
    )


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {"name": "activate_implement", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _targeted_message(call_id: str) -> AIMessage:
    return AIMessage(
        content="running the existing suite",
        tool_calls=[
            {
                "name": "test_targeted",
                "args": {"path": "tests/test_greet.py"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _write_message(call_id: str = "call_write_1") -> AIMessage:
    return AIMessage(
        content="working",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "greeting.txt", "content": "hello\n"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _write_and_test_message(
    write_id: str = "call_write_1", test_id: str = "call_test_1"
) -> AIMessage:
    return AIMessage(
        content="writing then testing",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "greeting.txt", "content": "hello\n"},
                "id": write_id,
                "type": "tool_call",
            },
            {
                "name": "test_targeted",
                "args": {"path": "tests/test_greet.py"},
                "id": test_id,
                "type": "tool_call",
            },
        ],
    )


def _completion_message(
    covering: object = ("pkg::test_ok",), call_id: str = "call_completion_1"
) -> AIMessage:
    args: dict[str, object] = {"AC-1": covering}
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {"name": "report_completion", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _silent_message(text: str = "still thinking") -> AIMessage:
    return AIMessage(content=text, tool_calls=[])


def _make_workspace(tmp_path: Path, name: str, profile_text: str) -> Path:
    src = tmp_path / name
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(profile_text, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    workdir = tmp_path / f"{name}-work"
    run_git(["clone", "-q", str(bare), str(workdir)], cwd=tmp_path)
    return workdir


def _kinds(ledger: RunLedger, attempt_id: str = ATTEMPT_ID) -> list[str]:
    return [record.kind for record in ledger.records(attempt_id)]


def _phase(progress: StepProgress) -> str:
    """The tracker's phase as a plain string (avoids literal narrowing)."""
    return progress.phase


def _tool_contents(model: ScriptedModel) -> list[str]:
    texts: list[str] = []
    for invocation in model.invocations:
        for message in invocation:
            if isinstance(message, ToolMessage):
                texts.append(str(message.content))
    return texts


def test_soft_stall_next_action_is_narrow(tmp_path: Path) -> None:
    """L3-IMP-16 (unit): five quiet responses record exactly one ``soft_stall``.

    The next action offered is narrow implementation, snapshot preparation
    or verification: broad exploration is refused afterwards, while a sixth
    quiet response records nothing more.
    """
    ledger = RunLedger(tmp_path / "stall.sqlite3")
    try:
        progress = StepProgress("a/1", LoopCeilings(), ledger=ledger)
        assert progress.allows("exploration") is True
        for _ in range(4):
            assert progress.observe_response(_snapshot(0), progress=False) == ()
        assert progress.observe_response(_snapshot(0), progress=False) == ("soft_stall",)
        assert progress.observe_response(_snapshot(0), progress=False) == ()
        assert progress.policy_events == ["soft_stall"]
        kinds = _kinds(ledger, "a/1")
        assert kinds == ["soft_stall"]
        detail = ledger.records("a/1")[0].detail or ""
        assert NARROW_NEXT_ACTION in detail
        assert "narrow implementation" in detail
        assert progress.allows("implementation") is True
        assert progress.allows("verification") is True
        assert progress.allows("snapshot") is True
        assert progress.allows("exploration") is False
        assert progress.allows("read") is False
    finally:
        ledger.close()


def test_progress_resets_the_stall_counter(tmp_path: Path) -> None:
    """L3-IMP-16 (unit): a qualifying response resets the five-response clock."""
    ledger = RunLedger(tmp_path / "reset.sqlite3")
    try:
        progress = StepProgress("a/1", LoopCeilings(), ledger=ledger)
        for _ in range(4):
            progress.observe_response(_snapshot(0), progress=False)
        progress.record_progress("diff", _snapshot(0))
        for _ in range(4):
            assert progress.observe_response(_snapshot(0), progress=False) == ()
        assert progress.policy_events == []
        assert _kinds(ledger, "a/1") == ["progress_event"]
    finally:
        ledger.close()


def test_gate_then_reserve_are_durable_and_monotonic(tmp_path: Path) -> None:
    """L3-IMP-18 (unit): 70% records one Gate, 80% one reserve entry, forever.

    The Gate carries the narrow next action and the remaining-budget
    snapshot; the phase never goes back, so crossing another meter cannot
    restore broad exploration.
    """
    ceilings = LoopCeilings(max_tool_calls=10)
    ledger = RunLedger(tmp_path / "gate.sqlite3", ceilings=ceilings)
    try:
        progress = StepProgress("a/1", ceilings, ledger=ledger)
        assert progress.observe_budget(_snapshot(6)) == ()
        assert _phase(progress) == "normal"
        assert progress.allows("exploration") is True
        assert progress.observe_budget(_snapshot(7)) == ("gate",)
        assert _phase(progress) == "gate"
        assert progress.allows("implementation") is True
        assert progress.allows("exploration") is False
        assert progress.observe_budget(_snapshot(8)) == ("reserve_entry",)
        assert _phase(progress) == "verification_reserve"
        assert progress.observe_budget(_snapshot(9)) == ()
        assert progress.observe_budget(_snapshot(2)) == ()
        assert _phase(progress) == "verification_reserve"
        assert progress.policy_events == ["gate", "reserve_entry"]
        records = ledger.records("a/1")
        assert [record.kind for record in records] == ["gate", "reserve_entry"]
        assert NARROW_NEXT_ACTION in (records[0].detail or "")
        assert records[0].remaining_budget["tool_calls"] == 3
        assert records[1].remaining_budget["tool_calls"] == 2
        assert progress.allows("verification") is True
        assert progress.allows("snapshot") is True
        assert progress.allows("implementation") is False
        assert progress.allows("exploration") is False
    finally:
        ledger.close()


def test_l3_imp_16_five_quiet_responses_record_one_soft_stall(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-16: five scripted responses with no qualifying progress.

    The activation plus four passing-but-diff-less targeted runs record
    exactly one ``soft_stall`` and no ``progress_event``; activity alone
    never resets the counter. The cutoff ends ``failed`` / ``undeclared``
    with no diff, and the stall names the narrow next action.
    """
    workspace = _make_workspace(tmp_path, "stall", HAPPY_PROFILE)
    ledger = RunLedger(tmp_path / "stall.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _targeted_message("call_test_1"),
                _targeted_message("call_test_2"),
                _targeted_message("call_test_3"),
                _targeted_message("call_test_4"),
                _silent_message("still thinking"),
                _silent_message("still thinking"),
                _silent_message("still silent"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert result.outcome == "failed"
        assert result.reason == "undeclared"
        kinds = _kinds(ledger)
        assert kinds.count("soft_stall") == 1
        assert "progress_event" not in kinds
        stall = next(
            record for record in ledger.records(ATTEMPT_ID) if record.kind == "soft_stall"
        )
        assert NARROW_NEXT_ACTION in (stall.detail or "")
        assert len(model.invocations) == 8
    finally:
        ledger.close()


def test_l3_imp_17_suppressed_diagnostic_keeps_the_stall_path(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-17: a suppressed, unchanged targeted diagnostic is not progress.

    Repeating the same failing targeted test with no relevant change
    suppresses the second diagnostic (no new Criterion Evidence, no diff),
    and that diagnostic enters the same soft-stall path: one ``soft_stall``,
    no ``progress_event``, and the activity around it resets nothing.
    """
    workspace = _make_workspace(tmp_path, "suppressed", FAILING_PROFILE)
    ledger = RunLedger(tmp_path / "suppressed.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _targeted_message("call_test_1"),
                _targeted_message("call_test_2"),
                _silent_message("still thinking"),
                _silent_message("still thinking"),
                _silent_message("still silent"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
        )
        assert any(
            '"suppressed": true' in content for content in _tool_contents(model)
        ), "the repeated failing targeted run must be suppressed"
        assert result.outcome == "failed"
        assert result.reason == "undeclared"
        kinds = _kinds(ledger)
        assert kinds.count("soft_stall") == 1
        assert "progress_event" not in kinds
    finally:
        ledger.close()


def test_l3_imp_18_reserve_entry_ends_broad_exploration_without_silence(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-18: 70% records one Gate, 80% one reserve entry, then no silence.

    With a five-call Attempt meter the fourth tool call crosses both
    thresholds at once; the silent turn after the reserve exits with no
    continuation, and the Gate precedes the reserve entry in the ledger.
    """
    workspace = _make_workspace(tmp_path, "reserve", HAPPY_PROFILE)
    ledger = RunLedger(tmp_path / "reserve.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _targeted_message("call_test_1"),
                _targeted_message("call_test_2"),
                _targeted_message("call_test_3"),
                _silent_message("still thinking"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            ceilings=LoopCeilings(max_tool_calls=5),
        )
        assert result.outcome == "failed"
        assert result.reason == "undeclared"
        kinds = _kinds(ledger)
        assert kinds.count("gate") == 1
        assert kinds.count("reserve_entry") == 1
        assert kinds.index("gate") < kinds.index("reserve_entry")
        assert "progress_event" not in kinds
        gate = next(
            record for record in ledger.records(ATTEMPT_ID) if record.kind == "gate"
        )
        assert NARROW_NEXT_ACTION in (gate.detail or "")
        assert len(model.invocations) == 5
        for invocation in model.invocations:
            assert not any(is_silence_continuation(message) for message in invocation)
    finally:
        ledger.close()


def test_l3_imp_30_ceiling_after_accepted_claim_stays_verified(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-30: a ceiling crossed after acceptance keeps the earned outcome.

    The write plus its passing test cross the Gate and enter the reserve, and
    the repeated completion turn (after the one review return, P-DEL-3)
    crosses the tool-call ceiling — but the accepted and
    validated claim still yields the ``verified completion`` Implementation
    Outcome: the category follows the moment of acceptance, not the moment
    of measurement. With no completed review the Publication Gate (§12)
    then ends the Attempt ``failed`` / ``review-missing`` instead of
    delivering it.
    """
    workspace = _make_workspace(tmp_path, "accepted", HAPPY_PROFILE)
    ledger = RunLedger(tmp_path / "accepted.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _write_and_test_message(),
                _completion_message(call_id="call_completion_1"),
                # Repeated after the one review return (P-DEL-3).
                _completion_message(call_id="call_completion_2"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            ceilings=LoopCeilings(max_tool_calls=4),
        )
        assert result.outcome == "failed"
        assert result.reason == "review-missing"
        kinds = _kinds(ledger)
        assert kinds.count("gate") == 1
        assert kinds.count("reserve_entry") == 1
        assert len(model.invocations) == 4
    finally:
        ledger.close()


def test_l3_imp_30_ceiling_before_acceptance_with_diff_is_saved_partial_work(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-30 + §11 hard ceilings: a ceiling cut with a diff and no claim.

    The Implementation Outcome ``saved partial work`` ends the Attempt
    ``failed-limit`` naming the limit (C-LIM-1, P-LIM-1).
    """
    workspace = _make_workspace(tmp_path, "partial", HAPPY_PROFILE)
    ledger = RunLedger(tmp_path / "partial.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _write_message(),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            ceilings=LoopCeilings(max_tool_calls=1),
        )
        assert (result.outcome, result.reason) == ("failed-limit", "tool_calls")
        assert len(model.invocations) == 2
    finally:
        ledger.close()


def test_l3_imp_30_ceiling_before_acceptance_without_diff_is_undeclared(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-30 + §11 hard ceilings: a ceiling cut with no diff, no claim.

    The Implementation Outcome ``completion not established`` ends the
    Attempt ``failed-limit`` naming the limit (C-LIM-1, P-LIM-1).
    """
    workspace = _make_workspace(tmp_path, "cutoff", HAPPY_PROFILE)
    ledger = RunLedger(tmp_path / "cutoff.sqlite3")
    try:
        tracker = FakeTracker()
        model = ScriptedModel(
            [
                _activation_message(),
                _targeted_message("call_test_1"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            ceilings=LoopCeilings(max_tool_calls=1),
        )
        assert (result.outcome, result.reason) == ("failed-limit", "tool_calls")
        assert len(model.invocations) == 2
    finally:
        ledger.close()


def _priced(message: AIMessage) -> AIMessage:
    """``message`` carrying a cached Anthropic-shaped response's usage.

    Built by langchain-anthropic's own converter: 0.25 M uncached input,
    0.1 M output, 5 M cache-read and 1 M cache-write (5-minute) tokens. At
    ``_PRICE`` that is exactly $5.00 per bucket-priced response.
    """
    from anthropic.types import CacheCreation, Usage
    from langchain_anthropic.chat_models import _create_usage_metadata

    message.usage_metadata = _create_usage_metadata(
        Usage(
            input_tokens=250_000,
            output_tokens=100_000,
            cache_read_input_tokens=5_000_000,
            cache_creation_input_tokens=1_000_000,
            cache_creation=CacheCreation(
                ephemeral_5m_input_tokens=1_000_000, ephemeral_1h_input_tokens=0
            ),
        )
    )
    return message


_PRICE = TokenPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)


def test_l1_3_cost_is_metered_per_bucket_and_binds_the_cost_ceiling(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L1-3 / §11: every response is costed per token bucket from the bound
    price, the cost meter drives the Gates, and crossing ``max_cost_usd``
    stops the loop with no further model call, ending ``failed-limit``
    naming the limit (C-LIM-1, P-LIM-1).

    The first response costs $5.00 of a $6.00 ceiling (83%): one Gate and
    one reserve entry, recording $1.00 remaining. The second crosses it.
    Reading ``cache_creation`` instead of the ephemeral bucket would price
    the write as uncached input — $4.50 per response, 75%: a Gate with
    $1.50 remaining and no reserve entry.
    """
    workspace = _make_workspace(tmp_path, "costed", HAPPY_PROFILE)
    ledger = RunLedger(tmp_path / "costed.sqlite3", ceilings=LoopCeilings(max_cost_usd=6.0))
    try:
        model = ScriptedModel(
            [
                _priced(_activation_message()),
                _priced(_targeted_message("call_test_1")),
                _silent_message("never reached"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            ceilings=LoopCeilings(max_cost_usd=6.0),
            price=_PRICE,
        )
        assert (result.outcome, result.reason) == ("failed-limit", "cost")
        assert len(model.invocations) == 2
        gate = next(record for record in ledger.records(ATTEMPT_ID) if record.kind == "gate")
        assert gate.remaining_budget["cost_usd"] == pytest.approx(1.0)
        assert _kinds(ledger).count("reserve_entry") == 1
    finally:
        ledger.close()


def test_attempt_policy_and_terminal_outcome_are_gone() -> None:
    """The v1 ``AttemptPolicy`` and ``progress.terminal_outcome`` are deleted.

    Their behaviour lives in the step subgraph now; importing either name
    must fail so no caller can reach the pre-M2 shape.
    """
    import importlib

    for name in ("coding_agent.runtime.progress",):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{name} still exists")
    import coding_agent.runtime.step_progress as step_progress

    assert hasattr(step_progress, "StepProgress")
