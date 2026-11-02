"""M1 walking skeleton: the fixed Attempt graph over the primary test seam.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (one fixed graph, no
merge node, no checkpointer, runtime context out of graph state), §6 (the
eleven nodes in order), §14 ("Recording before publishing") and §17
(one SQLite file, WAL with ``synchronous=FULL``, ``IF NOT EXISTS``, no
schema version).

MVP row ids: C-GRF-1, L3-SEL-11, L3-CLR-19, L3-IMP-9, L3-ERR-2.

Every assertion here is externally observable — outcome and reason, ledger
rows, tracker calls and the bytes sent to the scripted model — never node
internals or graph state shape beyond the structural node list itself. The
seam is the Attempt graph invoked in-process with the fake ``TrackerPort``
and the scripted model (both in ``tests/support/``), a real Run Ledger file
on a temporary state volume, and a local bare git remote.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger, digest_effective_configuration
from coding_agent.runtime.graph.attempt import (
    ATTEMPT_NODE_ORDER,
    AttemptResult,
    AttemptState,
    build_attempt_graph,
    run_attempt,
)
from coding_agent.runtime.graph import attempt as attempt_module
from coding_agent.runtime.graph import tools as tools_module
from coding_agent.runtime.graph.tools import (
    FORBIDDEN_MODEL_TOOL_NAMES,
    build_implement_step_tools,
)
from coding_agent.tracker.port import TrackerPort
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EXPECTED_NODE_ORDER = (
    "claim",
    "readiness",
    "baseline",
    "run_step",
    "gate",
    "delivery_snapshot",
    "final_checks",
    "publication_gate",
    "delivery_intent",
    "publish",
    "terminal_writes",
)

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}


def completion_message(summary: str = "implemented the greeting") -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"summary": summary},
                "id": "call_completion_1",
                "type": "tool_call",
            }
        ],
    )


HAPPY_PATH_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare git remote, built from the shared ``init_origin_repo``.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (the Project
    Profile is read at the Base Revision and pinned as the Validation
    Contract).

    MVP row ids: L2-17.
    """
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PATH_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    """A workspace checked out from the local bare remote."""
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def invoke_happy_path(
    ledger: RunLedger, workspace: Path, tracker: FakeTracker, model: ScriptedModel
) -> AttemptResult:
    return run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
    )


def test_attempt_graph_has_eleven_nodes_in_order_without_merge_or_checkpointer() -> None:
    graph = build_attempt_graph()
    assert ATTEMPT_NODE_ORDER == EXPECTED_NODE_ORDER
    drawn = graph.get_graph()
    names = [name for name in drawn.nodes if not name.startswith("__")]
    assert names == list(EXPECTED_NODE_ORDER)
    assert len(names) == 11
    edges = {(edge.source, edge.target) for edge in drawn.edges}
    expected_edges = {("__start__", "claim"), ("terminal_writes", "__end__")}
    expected_edges.update(zip(EXPECTED_NODE_ORDER, EXPECTED_NODE_ORDER[1:]))
    assert edges == expected_edges
    assert graph.checkpointer is None


def test_scripted_happy_path_records_attempt_and_outcome(
    ledger: RunLedger, workspace: Path
) -> None:
    tracker = FakeTracker()
    model = ScriptedModel([completion_message()])
    result = invoke_happy_path(ledger, workspace, tracker, model)
    assert result.outcome == "delivered"
    assert result.reason == "report-completion"
    attempt = ledger.get_attempt("#7/1")
    assert attempt.state == "outcome-recorded"
    assert attempt.effective_configuration == EFFECTIVE_CONFIGURATION
    assert (
        attempt.effective_configuration_digest
        == digest_effective_configuration(EFFECTIVE_CONFIGURATION)
    )
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert outcome.outcome == "delivered"
    assert outcome.reason == "report-completion"


def test_outcome_is_recorded_before_terminal_writes_call_the_tracker(
    ledger: RunLedger, workspace: Path
) -> None:
    seen: dict[str, Any] = {}

    def on_first_write(name: str) -> None:
        seen["call"] = name
        seen["outcome"] = ledger.get_outcome("#7/1")

    tracker = FakeTracker(on_first_write=on_first_write)
    model = ScriptedModel([completion_message()])
    invoke_happy_path(ledger, workspace, tracker, model)
    assert seen["call"] == "post_terminal_comment"
    recorded = seen["outcome"]
    assert recorded is not None
    assert recorded.outcome == "delivered"
    assert tracker.call_names() == [
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "release_assignment",
    ]


def test_delivered_attempt_sends_no_ready_for_human(
    ledger: RunLedger, workspace: Path
) -> None:
    tracker = FakeTracker()
    invoke_happy_path(ledger, workspace, tracker, ScriptedModel([completion_message()]))
    assert "add_ready_for_human" not in tracker.call_names()


def test_graph_state_carries_no_runtime_context() -> None:
    annotations = {name.lower() for name in AttemptState.__annotations__}
    for forbidden in (
        "meter",
        "evidence",
        "tracker",
        "usage",
        "ledger",
        "model",
        "cost",
        "token",
        "budget",
        "review",
    ):
        assert all(forbidden not in name for name in annotations), forbidden


def test_nodes_require_runtime_context() -> None:
    graph = build_attempt_graph()
    with pytest.raises(Exception):
        graph.invoke({"attempt_id": "#7/1", "issue_number": 7, "attempt_number": 1})


def test_step_binds_no_github_mutation_tool(workspace: Path) -> None:
    tools = build_implement_step_tools(workspace)
    names = {tool.name for tool in tools}
    assert "report_completion" in names
    assert names.isdisjoint(FORBIDDEN_MODEL_TOOL_NAMES)
    for name in names:
        lowered = name.lower()
        assert "label" not in lowered
        assert "comment" not in lowered
        assert "assign" not in lowered
        assert "pull" not in lowered
        assert "issue" not in lowered


def test_no_node_applies_the_selection_label_or_edits_the_issue_body() -> None:
    sources = "".join(
        inspect.getsource(getattr(attempt_module, name)) for name in ATTEMPT_NODE_ORDER
    )
    assert "apply_selection_label" not in sources
    assert "edit_issue" not in sources
    assert "update_issue" not in sources
    members = {
        name for name in dir(TrackerPort) if not name.startswith("_")
    }
    assert members == {
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    }
    assert "apply_selection_label" not in members
    assert not any("issue" in member and "edit" in member for member in members)
    assert "FORBIDDEN" in inspect.getsource(tools_module)


def test_bound_tool_set_has_no_github_mutation_tool(
    ledger: RunLedger, workspace: Path
) -> None:
    model = ScriptedModel([completion_message()])
    invoke_happy_path(ledger, workspace, FakeTracker(), model)
    assert "report_completion" in model.bound_tool_names
    assert set(model.bound_tool_names).isdisjoint(FORBIDDEN_MODEL_TOOL_NAMES)


def test_issue_reaches_the_scripted_model(
    ledger: RunLedger, workspace: Path
) -> None:
    model = ScriptedModel([completion_message()])
    invoke_happy_path(ledger, workspace, FakeTracker(), model)
    assert model.invocations, "the model was never invoked"
    sent = json.dumps(
        [[getattr(message, "content", "") for message in call] for call in model.invocations]
    ).encode("utf-8")
    assert b"Add a greeting" in sent
    assert len(sent) > 0


def test_unclassified_exception_ends_failed_and_records_the_outcome(
    ledger: RunLedger, workspace: Path
) -> None:
    tracker = FakeTracker(fail_on="fetch_issue")
    model = ScriptedModel([completion_message()])
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
    )
    assert result.outcome == "failed"
    assert result.reason == "unclassified-error"
    assert ledger.get_attempt("#7/1").state == "outcome-recorded"
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("failed", "unclassified-error")


def test_recorded_outcome_survives_a_late_tracker_failure(
    ledger: RunLedger, workspace: Path
) -> None:
    tracker = FakeTracker(fail_on="remove_selection_label")
    model = ScriptedModel([completion_message()])
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
    )
    assert (result.outcome, result.reason) == ("delivered", "report-completion")
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("delivered", "report-completion")


def test_ledger_opens_wal_full_and_reopen_creates_nothing_new(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run-ledger.sqlite3"
    first = RunLedger(path)
    assert first.journal_mode() == "wal"
    assert first.synchronous() == "2"
    first.open_attempt("#7/1", effective_configuration=EFFECTIVE_CONFIGURATION)
    first.record_terminal_outcome("#7/1", outcome="delivered", reason="report-completion")
    tables_before = first.table_names()
    assert {"run_ledger_events", "attempts", "outcomes"} <= set(tables_before)
    first.close()

    second = RunLedger(path)
    try:
        assert second.table_names() == tables_before
        assert second.get_attempt("#7/1").effective_configuration == EFFECTIVE_CONFIGURATION
        outcome = second.get_outcome("#7/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == ("delivered", "report-completion")
    finally:
        second.close()
