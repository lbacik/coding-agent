"""M3 review attribution: the activation plus the first group after it.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (a Review is the
``activate_code_review`` activation plus the first ``spawn_subagent`` group
after it, in the same work node, provided the tree at the start of that
group equals X; a later group, a step boundary or a Context-Window Handoff
before the group leaves the activation without a Review; completed when the
group holds ≥ 1 Sub-agent and every one ended ``reported``; status
``not-invoked`` / ``incomplete`` / ``fresh`` / ``stale``) and §17 (the
``review_status`` ledger event holds the status, activation count, gate
verdict, latest completed ordinal, per-activation X and attribution, and
per-Sub-agent description, end status and reason, report, ``instructions``
and budget snapshot).

Scenarios 13, 14, 15, 16, 24. Every push here goes to a local bare remote;
no test touches the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.review_snapshot import (
    ATTRIBUTION_GROUP,
    ATTRIBUTION_NO_GROUP,
    ATTRIBUTION_TREE_CHANGED,
    REVIEW_STATUS_KIND,
    ReviewSnapshotState,
)
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

PROFILE = """\
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

BACKED = {"AC-1": ["pkg::test_ok"]}


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _activate(call_id: str = "call-act-1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[_call("activate_implement", {}, call_id)],
    )


def _write_and_test(
    write_id: str = "call-write-1",
    test_id: str = "call-test-1",
    content: str = "hello\n",
) -> AIMessage:
    return AIMessage(
        content="working",
        tool_calls=[
            _call("write_file", {"path": "greeting.txt", "content": content}, write_id),
            _call("test_targeted", {"path": "tests/test_greet.py"}, test_id),
        ],
    )


def _rewrite_only(call_id: str = "call-rewrite-1") -> AIMessage:
    return AIMessage(
        content="revising after the review snapshot",
        tool_calls=[
            _call(
                "write_file",
                {"path": "greeting.txt", "content": "hello again\n"},
                call_id,
            )
        ],
    )


def _read_only(
    read_id: str = "call-read-1", list_id: str = "call-list-1"
) -> AIMessage:
    return AIMessage(
        content="re-reading without changing the tree",
        tool_calls=[
            _call("read_file", {"path": "greeting.txt"}, read_id),
            _call("list_directory", {"path": "."}, list_id),
        ],
    )


def _review(call_id: str = "call-rev-1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[_call("activate_code_review", {}, call_id)],
    )


def _spawn_pair(
    first: str = "call-spawn-1", second: str = "call-spawn-2"
) -> AIMessage:
    """One turn with two ``spawn_subagent`` calls: one group of two."""
    return AIMessage(
        content="reviewing with subagents",
        tool_calls=[
            _call(
                "spawn_subagent",
                {
                    "description": "review standards",
                    "instructions": "review the change against the Target Issue",
                },
                first,
            ),
            _call(
                "spawn_subagent",
                {
                    "description": "review spec",
                    "instructions": "review the change against the Target Issue",
                },
                second,
            ),
        ],
    )


def _spawn_single(call_id: str = "call-spawn-3") -> AIMessage:
    return AIMessage(
        content="reviewing again with one subagent",
        tool_calls=[
            _call(
                "spawn_subagent",
                {
                    "description": "follow-up reviewer",
                    "instructions": "review the change against the Target Issue",
                },
                call_id,
            ),
        ],
    )


def _report(call_id: str, report: str = "no findings") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[_call("report_subagent", {"report": report}, call_id)],
    )


def _completion(call_id: str) -> AIMessage:
    return AIMessage(
        content="done",
        tool_calls=[_call("report_completion", dict(BACKED), call_id)],
    )


def _init_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _run(
    tmp_path: Path, skills_dir: Path, model: ScriptedModel, bare: Path
) -> tuple[AttemptResult, RunLedger]:
    workspace = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workspace)], cwd=tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            remote_url=str(bare),
            agent_identity=Identity(account_id=999, login="coding-agent"),
        )
    except Exception:
        ledger.close()
        raise
    return result, ledger


def _review_payload(ledger: RunLedger) -> dict[str, Any]:
    """The single ``review_status`` ledger event payload (§17)."""
    events = [r for r in ledger.records("#7/1") if r.kind == REVIEW_STATUS_KIND]
    assert len(events) == 1
    assert isinstance(events[0].payload, dict)
    return dict(events[0].payload)


def test_scenario_13_write_between_activation_and_group_is_no_review(
    tmp_path: Path, skills_dir: Path
) -> None:
    """§10, scenario 13: ``activate → write → [spawn, spawn]`` is no Review.

    The write after the activation moves the tree past X, so the first
    group starts on a changed tree: the activation is left
    ``tree-changed``, the status is ``incomplete``, and the claim is
    returned once naming the tree before the Attempt ends
    ``failed`` / ``review-missing``.
    """
    bare = _init_remote(tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _rewrite_only(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-completion-1"),
            _completion("call-completion-2"),
        ]
    )
    result, ledger = _run(tmp_path, skills_dir, model, bare)
    try:
        assert (result.outcome, result.reason) == ("failed", "review-missing")
        payload = _review_payload(ledger)
        assert payload["status"] == "incomplete"
        assert payload["activations"] == 1
        assert payload["review_gate_met"] is False
        assert payload["latest_completed_ordinal"] is None
        (review,) = payload["reviews"]
        assert review["attribution"] == ATTRIBUTION_TREE_CHANGED
        assert review["completed"] is False
        assert review["cause"] == "tree changed since X"
        assert review["subagents"] == []
        returned = [
            message
            for message in model.invocations[8]
            if isinstance(message, ToolMessage)
            and message.tool_call_id == "call-completion-1"
        ]
        assert len(returned) == 1
        assert "tree changed since X" in str(returned[0].content)
    finally:
        ledger.close()


def test_scenario_14_read_only_between_activation_and_group_completes_review(
    tmp_path: Path, skills_dir: Path
) -> None:
    """§10, scenario 14: ``activate → read/shell → [spawn, spawn]`` completes.

    Reads leave the tree equal to X, so the first group owns the Review;
    both Sub-agents report and the claim is accepted at once — the Attempt
    is ``delivered`` and the ``review_status`` event carries a ``fresh``
    completed Review at X.
    """
    bare = _init_remote(tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _read_only(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-completion-1"),
        ]
    )
    result, ledger = _run(tmp_path, skills_dir, model, bare)
    try:
        assert (result.outcome, result.reason) == ("delivered", "verified-completion")
        payload = _review_payload(ledger)
        assert payload["status"] == "fresh"
        assert payload["activations"] == 1
        assert payload["invocations"] == 1
        assert payload["review_gate_met"] is True
        assert payload["latest_completed_ordinal"] == 1
        (review,) = payload["reviews"]
        assert review["attribution"] == ATTRIBUTION_GROUP
        assert review["completed"] is True
        assert review["cause"] is None
        assert review["commit_sha"] and isinstance(review["commit_sha"], str)
        assert len(review["subagents"]) == 2
        for subagent in review["subagents"]:
            assert subagent["status"] == "reported"
            assert subagent["reason"] == "reported"
            assert subagent["report"] == "no findings"
            assert (
                subagent["instructions"]
                == "review the change against the Target Issue"
            )
            assert isinstance(subagent["description"], str) and subagent["description"]
            assert isinstance(subagent["budget"], dict)
            assert isinstance(subagent["call_id"], str)
    finally:
        ledger.close()


def test_scenario_15_only_the_first_group_belongs_to_the_review(
    tmp_path: Path, skills_dir: Path
) -> None:
    """§10, scenario 15: in ``activate → [spawn, spawn] → [spawn]`` only the
    first group owns the Review; the later group attributes nothing."""
    bare = _init_remote(tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _spawn_single(),
            _report("sub-report-3"),
            _completion("call-completion-1"),
        ]
    )
    result, ledger = _run(tmp_path, skills_dir, model, bare)
    try:
        assert (result.outcome, result.reason) == ("delivered", "verified-completion")
        payload = _review_payload(ledger)
        assert payload["status"] == "fresh"
        assert payload["latest_completed_ordinal"] == 1
        (review,) = payload["reviews"]
        assert review["attribution"] == ATTRIBUTION_GROUP
        assert review["completed"] is True
        assert len(review["subagents"]) == 2
        assert all(
            subagent["description"] in ("review standards", "review spec")
            for subagent in review["subagents"]
        )
    finally:
        ledger.close()


def test_scenario_16_handoff_before_the_group_leaves_no_review() -> None:
    """§10, scenario 16: a work-node boundary (step boundary or
    Context-Window Handoff) between the activation and the group leaves the
    activation with no Review; a Review completed before the boundary stays
    completed."""
    before = ReviewSnapshotState()
    before.record_activation(commit_sha="sha-1", branch_name="agent/7/1-x")
    before.current_work_node += 1
    before.close_work_node()
    assert before.attribute_group(group_ordinal=0, tree_equal=True) is None
    assert before.activations[0].attribution == ATTRIBUTION_NO_GROUP
    assert before.status == "incomplete"
    assert before.review_gate_met is False
    assert before.latest_completed_activation is None

    completed = ReviewSnapshotState()
    completed.record_activation(commit_sha="sha-1", branch_name="agent/7/1-x")
    activation = completed.attribute_group(group_ordinal=0, tree_equal=True)
    assert activation is not None
    completed.record_attributed_subagent(
        activation,
        call_id="call-1",
        description="reviewer",
        instructions="review the change",
        status="reported",
        reason="report",
        report="no findings",
        budget=None,
    )
    assert completed.status == "fresh"
    completed.current_work_node += 1
    completed.close_work_node()
    assert completed.status == "fresh"
    assert completed.review_gate_met is True
    assert completed.latest_completed_activation is not None
    assert completed.latest_completed_activation.ordinal == 1


def test_scenario_24_findings_change_neither_outcome_nor_status(
    tmp_path: Path, skills_dir: Path
) -> None:
    """§10, scenario 24: findings of any severity block nothing — a completed
    Review with alarming findings still leaves the Attempt ``delivered``
    with a ``fresh`` status."""
    bare = _init_remote(tmp_path)
    alarming = (
        "CRITICAL: this change must not ship — blocking finding, "
        "severity highest."
    )
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1", alarming),
            _report("sub-report-2", alarming),
            _completion("call-completion-1"),
        ]
    )
    result, ledger = _run(tmp_path, skills_dir, model, bare)
    try:
        assert (result.outcome, result.reason) == ("delivered", "verified-completion")
        payload = _review_payload(ledger)
        assert payload["status"] == "fresh"
        assert payload["review_gate_met"] is True
        (review,) = payload["reviews"]
        assert review["completed"] is True
        assert [s["report"] for s in review["subagents"]] == [alarming, alarming]
    finally:
        ledger.close()
