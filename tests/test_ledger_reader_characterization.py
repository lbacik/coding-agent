"""Characterization tests locking operator read output before the reader refactor.

Builds a fixture Run Ledger with a current Attempt, a finished Attempt that
delivered a pull request, a released Attempt, a recorded halt, step
transitions and enough Attempts to page through. Compares exact ``json.dumps``
output of every operator read path against golden output recorded before the
refactor. Goldens must never change during the refactor: they prove the
refactor is byte-identical. Issue #474 adds ``provider_capability: null``
to both status goldens: the field is part of the read output now, and the
fixture predates any assertion run, so the recorded value is absent.
The fixture pins ``agent_version`` to ``FIXED_VERSION`` so a package version
bump does not change the goldens.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.ceilings import UsageTotals

FIXED_NOW = "2026-11-10T08:00:00Z"
FIXED_STARTED = "2026-11-09T08:00:00Z"
REPO = "example-owner/example-repo"
# Pinned so a package version bump does not change the goldens.
FIXED_VERSION = "0.0.0-test"

GOLDEN_STATUS_LIVE = "{\"current_attempt\": {\"attempt_id\": \"implement#7/1\", \"budget\": {\"effective_tokens\": {\"basis\": \"measured\", \"unit\": \"tokens\", \"value\": 1000}, \"enforced_tokens\": \"effective\", \"limits\": [{\"limit\": {\"basis\": \"configured\", \"unit\": \"tokens\", \"value\": 100000}, \"name\": \"effective_tokens\", \"used\": {\"basis\": \"measured\", \"unit\": \"tokens\", \"value\": 1000}}, {\"limit\": {\"basis\": \"configured\", \"unit\": \"usd\", \"value\": 5.0}, \"name\": \"cost_usd\", \"used\": {\"basis\": \"estimated\", \"unit\": \"usd\", \"value\": 0.02}}, {\"limit\": {\"basis\": \"configured\", \"unit\": \"seconds\", \"value\": 3600.0}, \"name\": \"wall_clock_seconds\", \"used\": {\"basis\": \"measured\", \"unit\": \"seconds\", \"value\": 90.0}}, {\"limit\": {\"basis\": \"configured\", \"unit\": \"count\", \"value\": 200}, \"name\": \"tool_calls\", \"used\": {\"basis\": \"measured\", \"unit\": \"count\", \"value\": 7}}], \"raw_tokens\": {\"basis\": \"measured\", \"unit\": \"tokens\", \"value\": 1200}}, \"by_origin\": {\"handoff\": {\"cost_usd\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"usd\", \"value\": null}, \"effective_tokens\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"tokens\", \"value\": null}, \"responses\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}, \"responses_without_usage\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}, \"tokens\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"tokens\", \"value\": null}, \"tool_calls\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}}, \"main\": {\"cost_usd\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"usd\", \"value\": null}, \"effective_tokens\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"tokens\", \"value\": null}, \"responses\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}, \"responses_without_usage\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}, \"tokens\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"tokens\", \"value\": null}, \"tool_calls\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}}, \"subagent\": {\"cost_usd\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"usd\", \"value\": null}, \"effective_tokens\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"tokens\", \"value\": null}, \"responses\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}, \"responses_without_usage\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}, \"tokens\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"tokens\", \"value\": null}, \"tool_calls\": {\"basis\": \"unavailable\", \"reason\": \"not recorded\", \"unit\": \"count\", \"value\": null}}}, \"figures\": {\"cache_hit_rate\": {\"basis\": \"unavailable\", \"reason\": \"main-conversation cache usage is not recorded in the latest usage flush\", \"unit\": \"ratio\", \"value\": null}, \"context_peak\": {\"basis\": \"unavailable\", \"reason\": \"no main-conversation response with usage has settled yet\", \"unit\": \"tokens\", \"value\": null}, \"context_used\": {\"basis\": \"unavailable\", \"reason\": \"the main conversation has not settled a response carrying usage yet\", \"unit\": \"tokens\", \"value\": null}, \"context_window\": {\"basis\": \"unavailable\", \"reason\": \"no pinned context window\", \"unit\": \"tokens\", \"value\": null}, \"continuations\": {\"basis\": \"measured\", \"unit\": \"count\", \"value\": 0}, \"cost_usd\": {\"basis\": \"estimated\", \"unit\": \"usd\", \"value\": 0.02}, \"elapsed_seconds\": {\"basis\": \"measured\", \"unit\": \"seconds\", \"value\": 90.0}, \"native.context_estimate_tokens\": {\"basis\": \"unavailable\", \"reason\": \"the step loop has not estimated the main conversation yet\", \"unit\": \"tokens\", \"value\": null}, \"review_seats_running\": {\"basis\": \"measured\", \"unit\": \"count\", \"value\": 0}, \"tool_calls\": {\"basis\": \"measured\", \"unit\": \"count\", \"value\": 7}, \"turns\": {\"basis\": \"not_applicable\", \"unit\": \"count\", \"value\": null}}, \"issue\": {\"number\": 7, \"title\": \"Fixture issue\"}, \"last_activity_at\": \"2026-11-10T08:00:00Z\", \"native_phase\": \"implement#1\", \"observed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}, \"generated_at\": \"2026-11-10T08:00:00Z\", \"identity\": {\"agent_version\": \"0.0.0-test\", \"backend\": \"coding-agent\", \"capabilities\": [\"next-issue\", \"stop-after\"], \"contract\": {\"major\": 1, \"minor\": 0}, \"mode\": \"online\", \"process_started_at\": \"2026-11-09T08:00:00Z\", \"repository\": \"example-owner/example-repo\"}, \"instance\": {\"last_halt\": {\"at\": \"2026-11-09T09:00:00Z\", \"detail\": \"halt detail\", \"reason\": \"test-halt\"}, \"observed_at\": \"2026-11-10T08:00:00Z\", \"startup_check\": null, \"state\": \"active\"}, \"intake\": {\"observed_at\": \"2026-11-10T08:00:00Z\", \"pending_stop\": null, \"priority\": null, \"state\": \"open\", \"stop_plan\": null}, \"last_finished\": {\"attempt_id\": \"implement#4/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": null}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 4, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": null, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}, \"provider_capability\": null, \"recent_commands\": [], \"recent_events\": [{\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#7/1\", \"event\": \"claim_opened\", \"issue\": 7, \"level\": \"info\", \"text\": \"claim opened implement#7/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#6/1\", \"event\": \"claim_opened\", \"issue\": 6, \"level\": \"info\", \"text\": \"claim opened implement#6/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#6/1\", \"event\": \"outcome_recorded\", \"level\": \"info\", \"outcome\": \"completed\", \"reason\": \"all done\", \"text\": \"outcome recorded implement#6/1 completed: all done\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#6/1\", \"event\": \"attempt_finished\", \"level\": \"info\", \"text\": \"attempt finished implement#6/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#6/1\", \"event\": \"sealed\", \"level\": \"info\", \"text\": \"sealed implement#6/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#5/1\", \"event\": \"claim_opened\", \"issue\": 5, \"level\": \"info\", \"text\": \"claim opened implement#5/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#5/1\", \"event\": \"attempt_released\", \"level\": \"info\", \"text\": \"attempt released implement#5/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#5/1\", \"event\": \"sealed\", \"level\": \"info\", \"text\": \"sealed implement#5/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#1/1\", \"event\": \"claim_opened\", \"issue\": 1, \"level\": \"info\", \"text\": \"claim opened implement#1/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#1/1\", \"event\": \"attempt_finished\", \"level\": \"info\", \"text\": \"attempt finished implement#1/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#1/1\", \"event\": \"sealed\", \"level\": \"info\", \"text\": \"sealed implement#1/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#2/1\", \"event\": \"claim_opened\", \"issue\": 2, \"level\": \"info\", \"text\": \"claim opened implement#2/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#2/1\", \"event\": \"attempt_finished\", \"level\": \"info\", \"text\": \"attempt finished implement#2/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#2/1\", \"event\": \"sealed\", \"level\": \"info\", \"text\": \"sealed implement#2/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#3/1\", \"event\": \"claim_opened\", \"issue\": 3, \"level\": \"info\", \"text\": \"claim opened implement#3/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#3/1\", \"event\": \"attempt_finished\", \"level\": \"info\", \"text\": \"attempt finished implement#3/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#3/1\", \"event\": \"sealed\", \"level\": \"info\", \"text\": \"sealed implement#3/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#4/1\", \"event\": \"claim_opened\", \"issue\": 4, \"level\": \"info\", \"text\": \"claim opened implement#4/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#4/1\", \"event\": \"attempt_finished\", \"level\": \"info\", \"text\": \"attempt finished implement#4/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"attempt_id\": \"implement#4/1\", \"event\": \"sealed\", \"level\": \"info\", \"text\": \"sealed implement#4/1\"}, {\"at\": \"2026-11-10T08:00:00Z\", \"event\": \"halted\", \"level\": \"error\", \"reason\": \"test-halt\", \"text\": \"halted test-halt: halt detail\"}], \"truncated\": false}"

GOLDEN_STATUS_OFFLINE = "{\"current_attempt\": null, \"generated_at\": \"2026-11-10T08:00:00Z\", \"identity\": {\"agent_version\": \"0.0.0-test\", \"backend\": \"coding-agent\", \"capabilities\": [\"next-issue\", \"stop-after\"], \"contract\": {\"major\": 1, \"minor\": 0}, \"mode\": \"offline\", \"process_started_at\": null, \"repository\": \"example-owner/example-repo\"}, \"instance\": {\"last_halt\": {\"at\": \"2026-11-09T09:00:00Z\", \"detail\": \"halt detail\", \"reason\": \"test-halt\"}, \"observed_at\": \"2026-11-10T08:00:00Z\", \"startup_check\": null, \"state\": \"stopped\"}, \"intake\": {\"observed_at\": \"2026-11-10T08:00:00Z\", \"pending_stop\": null, \"priority\": null, \"state\": \"open\", \"stop_plan\": null}, \"last_finished\": {\"attempt_id\": \"implement#4/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": null}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 4, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": null, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}, \"provider_capability\": null, \"recent_commands\": [], \"recent_events\": [], \"truncated\": false}"
GOLDEN_IDENTITY_LIVE = "{\"agent_version\": \"0.0.0-test\", \"backend\": \"coding-agent\", \"capabilities\": [\"next-issue\", \"stop-after\"], \"contract\": {\"major\": 1, \"minor\": 0}, \"mode\": \"online\", \"process_started_at\": \"2026-11-09T08:00:00Z\", \"repository\": \"example-owner/example-repo\"}"
GOLDEN_IDENTITY_OFFLINE = "{\"agent_version\": \"0.0.0-test\", \"backend\": \"coding-agent\", \"capabilities\": [\"next-issue\", \"stop-after\"], \"contract\": {\"major\": 1, \"minor\": 0}, \"mode\": \"offline\", \"process_started_at\": null, \"repository\": \"example-owner/example-repo\"}"
GOLDEN_ATTEMPTS_FIRST = "{\"cursor\": \"eyJ2IjoxLCJsYXN0Ijo2fQ==\", \"page\": [{\"attempt_id\": \"implement#4/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": null}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 4, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": null, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}, {\"attempt_id\": \"implement#3/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": null}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 3, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": null, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}]}"
GOLDEN_ATTEMPTS_BEFORE = "{\"cursor\": \"eyJ2IjoxLCJsYXN0Ijo0fQ==\", \"page\": [{\"attempt_id\": \"implement#2/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": null}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 2, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": null, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}, {\"attempt_id\": \"implement#1/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": null}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 1, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": null, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}]}"
GOLDEN_ATTEMPTS_SHOW = "{\"attempt\": {\"attempt_id\": \"implement#6/1\", \"attention_required\": false, \"delivery\": {\"pull_request\": {\"number\": 44, \"url\": \"https://example.invalid/pr/44\"}}, \"finished_at\": \"2026-11-10T08:00:00Z\", \"issue\": {\"number\": 6, \"title\": null}, \"lifecycle_state\": \"finished\", \"outcome\": {\"label\": \"completed (all done)\", \"native\": \"completed\", \"reason\": \"all done\"}, \"sealed\": true, \"sealed_at\": \"2026-11-10T08:00:00Z\", \"started_at\": \"2026-11-09T08:00:00Z\"}, \"manifest\": [{\"kind\": \"events\", \"media_type\": \"application/x-ndjson\", \"path\": \"events.jsonl\", \"sha256\": \"5037d27474deb4825beb6a2b9d9506d6c3b26c1dc6cf77f89935666d916a8798\", \"size\": 834}]}"


def _effective_config() -> dict[str, Any]:
    return {
        "workflow": {
            "ceiling": {
                "cost_usd": 5.0,
                "tokens": 100_000,
                "tool_calls": 200,
                "wall_clock_minutes": 60,
            }
        }
    }


def _usage() -> BudgetSnapshot:
    return BudgetSnapshot(
        elapsed_seconds=90.0,
        usage=UsageTotals(
            tokens=1200,
            effective_tokens=1000,
            cost_usd=0.02,
            tool_calls=7,
            input_tokens=800,
            output_tokens=200,
            cache_read_tokens=200,
            cache_write_tokens=0,
            cost_input_usd=0.01,
            cost_output_usd=0.005,
            cost_cache_read_usd=0.001,
            cost_cache_write_usd=0.0,
        ),
    )


def build_fixture(state_dir: Path) -> tuple[Path, LiveView]:
    """Build the characterization fixture ledger under ``state_dir``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = state_dir / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    try:
        started = datetime(2026, 11, 9, 8, 0, 0, tzinfo=timezone.utc)
        store.write_instance_metadata(
            target_repository=REPO,
            version=FIXED_VERSION,
            process_started_at=started,
        )
        # Current attempt with step executions.
        current_id = "implement#7/1"
        ledger.open_attempt(current_id, effective_configuration=_effective_config())
        ledger.open_step_execution(current_id, "plan", 1, entry_from_step=None, entry_result=None)
        ledger.end_step_execution(
            current_id, "plan", 1, result="done",
            payload={"pull_url": "https://example.invalid/pr/1", "pull_number": 1},
            facts={"pull_url": "https://example.invalid/pr/1", "pull_number": 1},
            usage={"cost_usd": 0.01, "effective_tokens": 500, "tokens": 600, "tool_calls": 3},
        )
        ledger.open_step_execution(current_id, "implement", 1, entry_from_step="plan", entry_result="done")
        ledger.record_step_transition(current_id, kind="route", from_step="plan", result="done", to_step="implement")
        ledger.record_usage(current_id, _usage())
        # Finished attempt that delivered a pull request.
        finished_id = "implement#6/1"
        ledger.open_attempt(finished_id, effective_configuration=_effective_config())
        ledger.open_step_execution(finished_id, "plan", 1)
        ledger.end_step_execution(
            finished_id, "plan", 1, result="opened",
            payload={"pull_url": "https://example.invalid/pr/44", "pull_number": 44},
            facts={"pull_url": "https://example.invalid/pr/44", "pull_number": 44},
            usage={"cost_usd": 0.02, "effective_tokens": 800, "tokens": 900, "tool_calls": 5},
        )
        ledger.record_step_effect_intent(
            finished_id, "plan", 1, 0,
            rendered_payload="pr body", expected_target={"base": "main"},
        )
        ledger.record_step_effect_receipt(finished_id, "plan", 1, 0, remote_id="44")
        ledger.record_step_transition(finished_id, kind="route", from_step="plan", result="opened", to_step="done")
        ledger.record_step_transition(finished_id, kind="end", from_step="done", result="opened", to_end="merged")
        ledger.record_terminal_outcome(finished_id, outcome="completed", reason="all done")
        ledger.set_attempt_state(finished_id, "finished")
        # Released attempt.
        released_id = "implement#5/1"
        ledger.open_attempt(released_id, effective_configuration=_effective_config())
        ledger.set_attempt_state(released_id, "released")
        # Extra attempts for paging (oldest first).
        for n in (1, 2, 3, 4):
            aid = f"implement#{(n)}/1"
            if aid in (current_id, finished_id, released_id):
                continue
            try:
                ledger.open_attempt(aid, effective_configuration=_effective_config())
                ledger.set_attempt_state(aid, "finished")
            except Exception:
                pass
        # Recorded halt.
        store.record_halt(
            reason="test-halt", detail="halt detail",
            occurred_at=datetime(2026, 11, 9, 9, 0, 0, tzinfo=timezone.utc),
        )
        # Pin deterministic created_at values.
        connection = sqlite3.connect(str(ledger_path))
        try:
            connection.execute("UPDATE attempts SET created_at = ?", (FIXED_STARTED,))
            connection.commit()
        finally:
            connection.close()
    finally:
        store.close()
        ledger.close()
    live = LiveView(process_started_at=datetime(2026, 11, 9, 8, 0, 0, tzinfo=timezone.utc))
    live.set_ready()
    live.set_claim(current_id, 7, "Fixture issue")
    live.set_phase("implement#1")
    return ledger_path, live


def _freeze_time(monkeypatch: Any) -> None:
    import sys

    from coding_agent.operator import contract as contract_mod
    from coding_agent.operator import listener as listener_mod

    def _fixed_now() -> str:
        return FIXED_NOW

    original = contract_mod.utc_now_z
    monkeypatch.setattr(contract_mod, "utc_now_z", _fixed_now)
    # Several modules bind ``utc_now_z`` at import time (``events``,
    # ``listener``, ``commands``). Whether they see the freeze depends on
    # import order: a module imported before the freeze keeps the real
    # clock (e.g. when another test module already imported it during
    # collection). Patch every loaded binding so the freeze holds
    # regardless of collection order. Function-level imports read the
    # contract attribute at call time and are already frozen.
    for module in list(sys.modules.values()):
        if module is None:
            continue
        name = getattr(module, "__name__", "")
        if name != "coding_agent" and not name.startswith("coding_agent."):
            continue
        if module.__dict__.get("utc_now_z") is original:
            monkeypatch.setattr(module, "utc_now_z", _fixed_now)
    monkeypatch.setattr(listener_mod, "_utc_now_iso", _fixed_now)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def test_live_status_characterization(tmp_path: Path, monkeypatch: Any) -> None:
    _freeze_time(monkeypatch)
    from coding_agent.operator import listener as listener_mod

    state_dir = tmp_path / "state"
    ledger_path, live = build_fixture(state_dir)
    result = listener_mod.build_status_result(ledger_path, live)
    if not GOLDEN_STATUS_LIVE:
        raise AssertionError("golden not recorded:\n" + _dump(result))
    assert _dump(result) == GOLDEN_STATUS_LIVE


def test_offline_status_characterization(tmp_path: Path, monkeypatch: Any) -> None:
    _freeze_time(monkeypatch)
    from coding_agent.operator import offline as offline_mod

    state_dir = tmp_path / "state"
    ledger_path, _ = build_fixture(state_dir)
    result = offline_mod.load_offline_status(ledger_path, state_dir)
    if not GOLDEN_STATUS_OFFLINE:
        raise AssertionError("golden not recorded:\n" + _dump(result))
    assert _dump(result) == GOLDEN_STATUS_OFFLINE


def test_identity_characterization(tmp_path: Path, monkeypatch: Any) -> None:
    _freeze_time(monkeypatch)
    from coding_agent.operator import listener as listener_mod
    from coding_agent.operator import offline as offline_mod

    state_dir = tmp_path / "state"
    ledger_path, live = build_fixture(state_dir)
    live_result = listener_mod.build_identity_result(ledger_path, live)
    offline_result = offline_mod.load_offline_identity(ledger_path)
    if not GOLDEN_IDENTITY_LIVE:
        raise AssertionError("live golden not recorded:\n" + _dump(live_result))
    if not GOLDEN_IDENTITY_OFFLINE:
        raise AssertionError("offline golden not recorded:\n" + _dump(offline_result))
    assert _dump(live_result) == GOLDEN_IDENTITY_LIVE
    assert _dump(offline_result) == GOLDEN_IDENTITY_OFFLINE


def test_attempts_list_characterization(tmp_path: Path, monkeypatch: Any) -> None:
    _freeze_time(monkeypatch)
    from coding_agent.operator import attempts as attempts_mod

    state_dir = tmp_path / "state"
    ledger_path, _ = build_fixture(state_dir)
    page, cursor = attempts_mod.list_attempts_page(ledger_path, limit=2)
    if not GOLDEN_ATTEMPTS_FIRST:
        raise AssertionError(
            "first-page golden not recorded:\n" + _dump({"page": page, "cursor": cursor})
        )
    assert _dump({"page": page, "cursor": cursor}) == GOLDEN_ATTEMPTS_FIRST
    assert cursor is not None
    page2, cursor2 = attempts_mod.list_attempts_page(ledger_path, limit=2, before=cursor)
    if not GOLDEN_ATTEMPTS_BEFORE:
        raise AssertionError(
            "before-page golden not recorded:\n" + _dump({"page": page2, "cursor": cursor2})
        )
    assert _dump({"page": page2, "cursor": cursor2}) == GOLDEN_ATTEMPTS_BEFORE


def test_attempts_show_characterization(tmp_path: Path, monkeypatch: Any) -> None:
    _freeze_time(monkeypatch)
    from coding_agent.operator import attempts as attempts_mod

    state_dir = tmp_path / "state"
    ledger_path, _ = build_fixture(state_dir)
    shown = attempts_mod.show_attempt(
        ledger_path=ledger_path, state_dir=state_dir, attempt_id="implement#6/1"
    )
    if not GOLDEN_ATTEMPTS_SHOW:
        raise AssertionError("show golden not recorded:\n" + _dump(shown))
    assert _dump(shown) == GOLDEN_ATTEMPTS_SHOW
