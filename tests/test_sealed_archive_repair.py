"""A repeated terminal transition never rewrites a Sealed archive (#449).

A finished Attempt is finalised twice (the Supervisor's live finalisation
seals the archive, then the poll loop finalises it again), and the
infrastructure-failure path has the same shape. The repeated transition
must be a no-op, nothing may append to a Sealed archive, and archives
already carrying the trailing line are repaired by truncation.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from coding_agent.github.errors import GitHubFailure
from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import archive as archive_mod
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.worker.run import WorkerLoopConfig, run_worker_loop
from support.fake_step_tracker import FakeStepTracker

REPO = "example-owner/example-repo"
LABEL = "agent:implement"
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"

FINISHED = "attempt_finished"
RELEASED = "attempt_released"


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore, Path]:
    """A ledger under ``state/`` with its instance row, plus the state dir."""
    state_dir = tmp_path / "state"
    ledger_path = state_dir / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(target_repository=REPO)
    return ledger, store, state_dir


def _archive(state_dir: Path, attempt_id: str) -> Path:
    return archive_mod.archive_dir_for(state_dir, REPO, attempt_id)


def _event_lines(archive: Path) -> list[dict[str, Any]]:
    text = (archive / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _event_types(archive: Path) -> list[str]:
    return [
        str(record.get("event", record.get("type"))) for record in _event_lines(archive)
    ]


def _db_event_count(ledger_path: Path, event_type: str, attempt_id: str) -> int:
    connection = sqlite3.connect(str(ledger_path))
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM operator_events WHERE type = ? AND attempt_id = ?",
            (event_type, attempt_id),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _events_manifest_entry(archive: Path) -> dict[str, Any]:
    document = archive_mod.read_sealed_manifest(archive)
    assert document is not None
    raw_entries = document.get("entries")
    assert isinstance(raw_entries, list)
    for raw_entry in raw_entries:
        assert isinstance(raw_entry, dict)
        if raw_entry.get("path") == "events.jsonl":
            return raw_entry
    raise AssertionError("sealed manifest has no events.jsonl entry")


def _seal(
    ledger: RunLedger, state_dir: Path, attempt_id: str, *, outcome: str = "delivered"
) -> Path:
    """Open, record an outcome for and finish ``attempt_id``; return its archive."""
    ledger.open_attempt(attempt_id, effective_configuration={"repo": REPO})
    archive = _archive(state_dir, attempt_id)
    ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason="done")
    ledger.set_attempt_state(attempt_id, "finished")
    assert (archive / "manifest.json").is_file()
    return archive


def _verify_export_files(
    state_dir: Path, ledger_path: Path, attempt_id: str
) -> dict[str, Any]:
    """Stream the Attempt export and check every file against its manifest."""
    shown = attempt_archives.show_attempt(
        ledger_path=ledger_path, state_dir=state_dir, attempt_id=attempt_id
    )
    assert shown is not None
    assert shown["attempt"]["sealed"] is True
    assert isinstance(shown["attempt"]["sealed_at"], str)
    entries = [dict(entry) for entry in shown["manifest"]]
    document = attempt_archives.export_manifest_document(
        attempt=dict(shown["attempt"]), files=list(entries)
    )
    archive = archive_mod.archive_dir_for(state_dir, REPO, attempt_id)
    buffer = io.BytesIO()
    attempt_archives.write_export_tar(
        archive_dir=archive, document=document, entries=entries, fileobj=buffer
    )
    reader = tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r")
    try:
        assert reader.getnames()[0] == "manifest.json"
        for entry in entries:
            member = reader.getmember(str(entry["path"]))
            data = reader.extractfile(member)
            assert data is not None
            content = data.read()
            assert len(content) == int(entry["size"])
            assert hashlib.sha256(content).hexdigest() == str(entry["sha256"])
    finally:
        reader.close()
    return document


def test_repeated_finished_emits_once_with_sealed_last(tmp_path: Path) -> None:
    """Finishing twice leaves one line; ``sealed`` is last and matches."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        archive = _seal(ledger, state_dir, "#7/1")
        ledger.set_attempt_state("#7/1", "finished")

        assert _event_types(archive).count(FINISHED) == 1
        assert _event_types(archive)[-1] == "sealed"
        entry = _events_manifest_entry(archive)
        data = (archive / "events.jsonl").read_bytes()
        assert len(data) == int(entry["size"])
        assert hashlib.sha256(data).hexdigest() == str(entry["sha256"])
    finally:
        store.close()
        ledger.close()


def test_repeated_released_emits_once_with_sealed_last(tmp_path: Path) -> None:
    """Releasing twice leaves one line; ``sealed`` is last and matches."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration={"repo": REPO})
        archive = _archive(state_dir, "#7/1")
        ledger.set_attempt_state("#7/1", "released")
        ledger.set_attempt_state("#7/1", "released")

        assert _event_types(archive).count(RELEASED) == 1
        assert _event_types(archive)[-1] == "sealed"
        entry = _events_manifest_entry(archive)
        data = (archive / "events.jsonl").read_bytes()
        assert len(data) == int(entry["size"])
        assert hashlib.sha256(data).hexdigest() == str(entry["sha256"])
    finally:
        store.close()
        ledger.close()


def test_repeated_terminal_emits_a_single_database_row(tmp_path: Path) -> None:
    """The duplicate transition writes no second database event row."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        _seal(ledger, state_dir, "#7/1")
        ledger.set_attempt_state("#7/1", "finished")
        ledger_path = state_dir / "run-ledger.sqlite3"
        assert _db_event_count(ledger_path, FINISHED, "#7/1") == 1

        ledger.open_attempt("#7/2", effective_configuration={"repo": REPO})
        ledger.set_attempt_state("#7/2", "released")
        ledger.set_attempt_state("#7/2", "released")
        assert _db_event_count(ledger_path, RELEASED, "#7/2") == 1
    finally:
        store.close()
        ledger.close()


def _stop_after_request(request_id: str, after: int) -> bytes:
    body: dict[str, Any] = {
        "op": "stop",
        "request_id": request_id,
        "expect_repo": REPO,
        "after": after,
    }
    return (json.dumps(body) + "\n").encode("utf-8")


def _stop_plan(ledger_path: Path) -> dict[str, Any] | None:
    from coding_agent.operator import commands as operator_commands

    connection = operator_commands.open_ledger_connection(ledger_path, timeout_seconds=2.0)
    try:
        intake = operator_commands.read_intake(connection)
    finally:
        connection.close()
    plan = intake.get("stop_plan")
    assert plan is None or isinstance(plan, dict)
    return plan


def test_repeated_finish_counts_stop_plan_once(tmp_path: Path) -> None:
    """A duplicate finish does not consume a second ``stop --after`` slot."""
    from datetime import datetime, timezone

    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#40/1", effective_configuration={"repo": REPO})
        live = LiveView(
            process_started_at=datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
        )
        live.set_ready()
        reply = operator_listener._handle_one(
            _stop_after_request("req-after-1", 2),
            ledger_path=state_dir / "run-ledger.sqlite3",
            live=live,
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "accepted"

        ledger.record_terminal_outcome("#40/1", outcome="delivered", reason="first")
        ledger.set_attempt_state("#40/1", "finished")
        ledger.set_attempt_state("#40/1", "finished")

        plan = _stop_plan(state_dir / "run-ledger.sqlite3")
        assert plan is not None
        assert plan["remaining"] == 1
        assert plan["counted"] == 1
        assert ledger.intake_is_open() is True

        ledger.open_attempt("#40/2", effective_configuration={"repo": REPO})
        ledger.record_terminal_outcome("#40/2", outcome="delivered", reason="second")
        ledger.set_attempt_state("#40/2", "finished")
        assert _stop_plan(state_dir / "run-ledger.sqlite3") is None
        assert ledger.intake_is_open() is False
    finally:
        store.close()
        ledger.close()


def test_emit_after_seal_leaves_events_byte_identical(tmp_path: Path) -> None:
    """A late event still records its DB row but never touches the sealed file."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        archive = _seal(ledger, state_dir, "#7/1")
        sealed_bytes = (archive / "events.jsonl").read_bytes()

        ledger.emit_attempt_event("#7/1", "native.handoff", "a late handoff")

        assert (archive / "events.jsonl").read_bytes() == sealed_bytes
        assert _db_event_count(
            state_dir / "run-ledger.sqlite3", "native.handoff", "#7/1"
        ) == 1
    finally:
        store.close()
        ledger.close()


def test_repair_truncates_trailing_line_and_keeps_seal(tmp_path: Path) -> None:
    """The repair restores the sealed bytes; the seal itself never changes."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        archive = _seal(ledger, state_dir, "#7/1")
        sealed_bytes = (archive / "events.jsonl").read_bytes()
        manifest_before = (archive / "manifest.json").read_bytes()
        shown_before = attempt_archives.show_attempt(
            ledger_path=state_dir / "run-ledger.sqlite3",
            state_dir=state_dir,
            attempt_id="#7/1",
        )
        assert shown_before is not None
        trailing = (
            json.dumps(
                {
                    "attempt_id": "#7/1",
                    "event": FINISHED,
                    "text": "attempt finished #7/1",
                },
                sort_keys=True,
            )
            + "\n"
        )
        with (archive / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(trailing)
        assert len((archive / "events.jsonl").read_bytes()) > len(sealed_bytes)

        assert archive_mod.repair_sealed_archive(archive) is True

        assert (archive / "events.jsonl").read_bytes() == sealed_bytes
        assert (archive / "manifest.json").read_bytes() == manifest_before
        shown_after = attempt_archives.show_attempt(
            ledger_path=state_dir / "run-ledger.sqlite3",
            state_dir=state_dir,
            attempt_id="#7/1",
        )
        assert shown_after is not None
        assert (
            shown_after["attempt"]["sealed_at"]
            == shown_before["attempt"]["sealed_at"]
        )
        _verify_export_files(state_dir, state_dir / "run-ledger.sqlite3", "#7/1")

        assert archive_mod.repair_sealed_archive(archive) is False
        assert (archive / "events.jsonl").read_bytes() == sealed_bytes
    finally:
        store.close()
        ledger.close()


def test_repair_leaves_healthy_archive_untouched(tmp_path: Path) -> None:
    """A second run — and a healthy archive — change nothing."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        archive = _seal(ledger, state_dir, "#7/1")
        sealed_bytes = (archive / "events.jsonl").read_bytes()
        assert archive_mod.repair_sealed_archive(archive) is False
        assert (archive / "events.jsonl").read_bytes() == sealed_bytes
    finally:
        store.close()
        ledger.close()


def test_repair_leaves_unsealed_archive_untouched(tmp_path: Path) -> None:
    """An archive without a sealed manifest is never truncated."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration={"repo": REPO})
        archive = _archive(state_dir, "#7/1")
        before = (archive / "events.jsonl").read_bytes()
        assert archive_mod.repair_sealed_archive(archive) is False
        assert (archive / "events.jsonl").read_bytes() == before
    finally:
        store.close()
        ledger.close()


def test_repair_leaves_prefix_mismatch_untouched(tmp_path: Path) -> None:
    """A longer file whose prefix is not the sealed bytes is left alone."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        archive = _seal(ledger, state_dir, "#7/1")
        events_path = archive / "events.jsonl"
        sealed_bytes = events_path.read_bytes()
        tampered = b"X" + sealed_bytes[1:] + b'{"event": "x"}\n'
        events_path.write_bytes(tampered)
        assert archive_mod.repair_sealed_archive(archive) is False
        assert events_path.read_bytes() == tampered
    finally:
        store.close()
        ledger.close()


def test_repair_leaves_shorter_file_untouched(tmp_path: Path) -> None:
    """A file shorter than the manifest size is never truncated."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        archive = _seal(ledger, state_dir, "#7/1")
        events_path = archive / "events.jsonl"
        sealed_bytes = events_path.read_bytes()
        assert len(sealed_bytes) > 10
        shorter = sealed_bytes[:-10]
        events_path.write_bytes(shorter)
        assert archive_mod.repair_sealed_archive(archive) is False
        assert events_path.read_bytes() == shorter
    finally:
        store.close()
        ledger.close()


def test_repair_leaves_other_file_mismatch_untouched(tmp_path: Path) -> None:
    """Damage beyond the trailing line is not the shape the repair restores."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration={"repo": REPO})
        archive = _archive(state_dir, "#7/1")
        (archive / "notes.txt").write_text("sealed notes", encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="failed", reason="x")
        ledger.set_attempt_state("#7/1", "finished")
        sealed_events = (archive / "events.jsonl").read_bytes()
        with (archive / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"event": "attempt_finished"}\n')
        (archive / "notes.txt").write_text("tampered after seal", encoding="utf-8")
        damaged = (archive / "events.jsonl").read_bytes()

        assert archive_mod.repair_sealed_archive(archive) is False

        assert (archive / "events.jsonl").read_bytes() == damaged
        assert len(damaged) > len(sealed_events)
    finally:
        store.close()
        ledger.close()


def test_sweep_repairs_all_damaged_archives(tmp_path: Path) -> None:
    """The sweep scans every sealed archive and repairs each damaged one."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        damaged: list[Path] = []
        for attempt_id in ("#7/1", "#7/2"):
            archive = _seal(ledger, state_dir, attempt_id)
            with (archive / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"event": "attempt_finished"}\n')
            damaged.append(archive)
        healthy = _seal(ledger, state_dir, "#7/3")
        healthy_bytes = (healthy / "events.jsonl").read_bytes()

        summary = archive_mod.sweep_sealed_archive_repairs(state_dir)

        assert summary == {"scanned": 3, "repaired": 2}
        for archive in damaged:
            entry = _events_manifest_entry(archive)
            data = (archive / "events.jsonl").read_bytes()
            assert len(data) == int(entry["size"])
        assert (healthy / "events.jsonl").read_bytes() == healthy_bytes

        again = archive_mod.sweep_sealed_archive_repairs(state_dir)
        assert again == {"scanned": 3, "repaired": 0}
    finally:
        store.close()
        ledger.close()


def test_sweep_isolates_a_failing_archive(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """One archive raising mid-sweep neither fails the sweep nor hides others."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        first = _seal(ledger, state_dir, "#7/1")
        second = _seal(ledger, state_dir, "#7/2")
        for archive in (first, second):
            with (archive / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"event": "attempt_finished"}\n')
        real_repair = archive_mod.repair_sealed_archive

        def _flaky(archive_dir: Path) -> bool:
            if archive_dir == second:
                raise OSError("disk gone")
            return real_repair(archive_dir)

        monkeypatch.setattr(
            archive_mod, "repair_sealed_archive", _flaky
        )
        summary = archive_mod.sweep_sealed_archive_repairs(state_dir)
        assert summary == {"scanned": 2, "repaired": 1}
        entry = _events_manifest_entry(first)
        assert len((first / "events.jsonl").read_bytes()) == int(entry["size"])
    finally:
        store.close()
        ledger.close()


def _issue_input(number: int, created_at: str) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=f"Add a greeting {number}",
        body=BODY,
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = _issue_input(number, created_at)
        tracker.labeled_events[(number, LABEL)] = LabeledEvent(
            label=LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at="2026-10-01T00:00:00Z",
        )
    return tracker


def _claim_config() -> ClaimConfig:
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login="coding-agent",
        agent_id=999,
        effective_configuration={"repo": REPO},
    )


def _loop_config() -> WorkerLoopConfig:
    from coding_agent.workflow.schema import (
        Agent,
        AttemptCeiling,
        InputDecl,
        RouteTarget,
        WorkflowStep,
        Workflow,
    )

    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="work",
        defaults_routes={"error": RouteTarget(end="failed")},
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": RouteTarget(end="delivered")},
            )
        },
    )
    return WorkerLoopConfig(
        claim_config=_claim_config(), workflow=workflow, poll_interval_seconds=60.0
    )


def _assert_sealed_and_consistent(
    state_dir: Path, ledger: RunLedger, attempt_id: str
) -> None:
    """One ``attempt_finished``, ``sealed`` last, manifest hash matches."""
    archive = _archive(state_dir, attempt_id)
    assert _event_types(archive).count(FINISHED) == 1
    assert _event_types(archive)[-1] == "sealed"
    assert _db_event_count(state_dir / "run-ledger.sqlite3", FINISHED, attempt_id) == 1
    entry = _events_manifest_entry(archive)
    data = (archive / "events.jsonl").read_bytes()
    assert len(data) == int(entry["size"])
    assert hashlib.sha256(data).hexdigest() == str(entry["sha256"])
    _verify_export_files(state_dir, state_dir / "run-ledger.sqlite3", attempt_id)
    assert ledger.get_attempt(attempt_id).state == "finished"


def test_worker_loop_success_path_seals_consistently(tmp_path: Path) -> None:
    """The success path finalises twice yet seals a manifest-consistent archive."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

        def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
            ledger.record_terminal_outcome(
                claimed.attempt_id, outcome="delivered", reason="verified-completion"
            )
            tracker.remove_label(claimed.input_number, "issue", LABEL)
            tracker.unassign_input(claimed.input_number, "issue")
            # The Supervisor's live-Attempt finalisation seals the archive;
            # the poll loop finalises the same Attempt again below.
            ledger.set_attempt_state(claimed.attempt_id, "finished")
            return WorkflowResult(
                attempt_id=claimed.attempt_id,
                outcome="delivered",
                reason="verified-completion",
            )

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(),
            run_one_attempt=run_one,
            sleep=lambda seconds: None,
            max_attempts=1,
        )

        assert exit_code == 0
        _assert_sealed_and_consistent(state_dir, ledger, "implement#31/1")
    finally:
        store.close()
        ledger.close()


def test_worker_loop_infrastructure_failure_seals_consistently(
    tmp_path: Path,
) -> None:
    """The infrastructure-failure path finalises twice yet seals consistently."""
    ledger, store, state_dir = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with((31, "2026-10-01T00:00:00Z"))

        def run_one(claimed: ConfirmedClaim) -> WorkflowResult:
            ledger.record_terminal_outcome(
                claimed.attempt_id, outcome="failed", reason="setup boom"
            )
            tracker.remove_label(claimed.input_number, "issue", LABEL)
            tracker.unassign_input(claimed.input_number, "issue")
            ledger.set_attempt_state(claimed.attempt_id, "finished")
            raise GitHubFailure(
                "GET",
                "/repos/o/r/issues",
                502,
                reason="server-error",
                failure_class="github",
            )

        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(),
            run_one_attempt=run_one,
            sleep=lambda seconds: None,
            max_attempts=1,
        )

        assert exit_code == 0
        _assert_sealed_and_consistent(state_dir, ledger, "implement#31/1")
    finally:
        store.close()
        ledger.close()


def test_repair_entry_point_signature() -> None:
    """The repair sweep keeps its documented shape for the startup caller."""
    sweep: Callable[[Path], dict[str, int]] = (
        archive_mod.sweep_sealed_archive_repairs
    )
    assert sweep(Path("/nonexistent-state-dir")) == {"scanned": 0, "repaired": 0}


_RUN_CONFIG = """\
version: 3

target:
  kind: github
  repository: example-owner/example-repo
  base-branch: main
  token-env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}

worker:
  poll-interval-seconds: 5

workflows:
  implement:
    model: sonnet
    input: {type: issue, label: agent:implement}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      work:
        agent:
          tools: [workspace-read]
          results:
            done: {}
        routes: {done: {end: completed}}
"""


def test_worker_startup_repairs_sealed_archive_before_intake_opens(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """Startup truncates a damaged Sealed archive before the first Claim."""
    import json as _json

    from coding_agent.worker import supervisor
    from support.scripted_model import ScriptedModel
    from worker_fakes import make_worker_ports

    state_dir = tmp_path / "state"
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    store = OperatorStore(state_dir / "run-ledger.sqlite3")
    store.write_instance_metadata(target_repository=REPO)
    try:
        ledger.open_attempt("implement#7/1", effective_configuration={"repo": REPO})
        ledger.record_terminal_outcome(
            "implement#7/1", outcome="delivered", reason="done"
        )
        ledger.set_attempt_state("implement#7/1", "finished")
    finally:
        store.close()
        ledger.close()
    archive = archive_mod.archive_dir_for(state_dir, REPO, "implement#7/1")
    sealed_bytes = (archive / "events.jsonl").read_bytes()
    with (archive / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"event": "attempt_finished"}\n')

    (tmp_path / "workflow.yml").write_text(_RUN_CONFIG, encoding="utf-8")
    (tmp_path / "toolchain-matrix.json").write_text(
        _json.dumps(
            {
                "schema": 2,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_managers": {"uv": {"version": "0.12.8"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=999)
    fakes = make_worker_ports(model=ScriptedModel([]), tracker=tracker)

    exit_code = supervisor.run_worker(
        config=tmp_path / "workflow.yml",
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=supervisor.SkillDeploymentCheck(
            policy=tmp_path / "skill-policy.json",
            bundle_dir=tmp_path / "bundle",
            home=tmp_path / "home",
        ),
        toolchain_matrix_path=tmp_path / "toolchain-matrix.json",
        ports=fakes.ports,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 0
    assert (archive / "events.jsonl").read_bytes() == sealed_bytes
    err = capsys.readouterr().err
    repair_started = err.index("run: startup: archive-repair started")
    repair_ok = err.index("run: startup: archive-repair ok")
    recovery_started = err.index("run: startup: startup-recovery")
    assert repair_started < repair_ok < recovery_started
    assert "run: archive repair: 1 repaired of 1 archives scanned" in err
    _verify_export_files(state_dir, state_dir / "run-ledger.sqlite3", "implement#7/1")
