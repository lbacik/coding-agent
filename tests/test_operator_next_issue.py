"""A ``next issue N`` priority chooses the Worker's next Claim (issue #252).

Contract: operator contract major 1 (parent #245). An operator records
which Eligible Issue the Worker takes next with ``next issue N``; the
priority never bypasses any human authorisation.

Engine paths are exercised through ``listener._handle_one`` (the same
function the socket thread calls) with a listener-owned ``FakeTracker``
that is never the Worker's; the Claim boundary through
``worker.claim.select_and_claim`` with the Worker's own ``FakeTracker``;
and ``agentctl`` mapping through the entry point with a stubbed engine.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import commands as operator_commands
from coding_agent.operator import listener as operator_listener
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import OperatorStore
from coding_agent.tracker.port import CandidateIssue
from coding_agent.worker.claim import ClaimConfig, select_and_claim

SELECTION_LABEL = "ready-for-agent"


def _started_at() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore]:
    """A ledger plus the instance row mutations need for the repo guard."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(
        target_repository="example-owner/example-repo",
        process_started_at=_started_at(),
    )
    return ledger, store


def _live() -> LiveView:
    live = LiveView(process_started_at=_started_at())
    live.set_ready()
    return live


def _claim_config(**overrides: Any) -> ClaimConfig:
    fields: dict[str, Any] = {
        "selection_label": SELECTION_LABEL,
        "agent_login": "coding-agent",
    }
    fields.update(overrides)
    return ClaimConfig(**fields)


def _candidate(
    number: int,
    *,
    day: int = 1,
    is_open: bool = True,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = (SELECTION_LABEL,),
    body: str = "## Acceptance criteria\n- [ ] done\n",
) -> CandidateIssue:
    return CandidateIssue(
        number=number,
        title=f"Issue {number}",
        body=body,
        is_open=is_open,
        assignees=assignees,
        labels=labels,
        created_at=f"2026-10-{day:02d}T00:00:00Z",
    )


def _tracker_with(
    *candidates: CandidateIssue, agent_login: str = "coding-agent"
) -> Any:
    from support.fake_tracker import FakeTracker

    tracker = FakeTracker(agent_login=agent_login)
    for candidate in candidates:
        tracker.candidates[candidate.number] = candidate
    return tracker


def _request(op: str, **fields: Any) -> bytes:
    body: dict[str, Any] = {"op": op}
    body.update(fields)
    return (json.dumps(body) + "\n").encode("utf-8")


def _answer(
    ledger_path: Path,
    raw: bytes,
    *,
    claim_config: ClaimConfig | None = None,
    tracker_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    return operator_listener._handle_one(
        raw,
        ledger_path=ledger_path,
        live=_live(),
        claim_config=claim_config,
        tracker_factory=tracker_factory,
    )


def _commit_next(
    ledger_path: Path,
    request_id: str,
    issue: int,
    listener_tracker: Any,
    claim_config: ClaimConfig | None = None,
) -> dict[str, Any]:
    """Commit a priority through the listener; assert it was accepted."""
    factory_calls: list[str] = []
    inner = listener_tracker

    def _factory() -> Any:
        factory_calls.append("built")
        return inner

    reply = _answer(
        ledger_path,
        _request(
            "next",
            request_id=request_id,
            expect_repo="example-owner/example-repo",
            issue=issue,
        ),
        claim_config=claim_config or _claim_config(),
        tracker_factory=_factory,
    )
    assert reply is not None
    assert "command" in reply, reply
    command = reply["command"]
    assert isinstance(command, dict)
    assert command["acknowledgement"] == "accepted"
    assert command["kind"] == "next"
    assert command["arguments"] == {"issue": issue}
    assert factory_calls, "the commit must build its own tracker instance"
    return reply


def _stored(ledger_path: Path, request_id: str) -> operator_commands.CommandRecord | None:
    connection = operator_commands.open_ledger_connection(
        ledger_path, timeout_seconds=2.0
    )
    try:
        return operator_commands.get_command(connection, request_id)
    finally:
        connection.close()


def test_priority_claims_later_issue_first_and_completes(
    tmp_path: Path,
) -> None:
    """With two Eligible Issues, the later one wins and the command completes."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        config = _claim_config()
        reply = _commit_next(
            ledger.path, "req-next-1", 41, listener_tracker, config
        )
        assert reply["replayed"] is False
        claimed = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert claimed is not None
        assert claimed.issue_number == 41
        stored = _stored(ledger.path, "req-next-1")
        assert stored is not None
        assert stored.acknowledgement == "completed"
        # The earlier issue is still there for the next sweep.
        ledger.set_attempt_state(claimed.attempt_id, "finished")
        second = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert second is not None
        assert second.issue_number == 40
    finally:
        store.close()
        ledger.close()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        ("closed", "closed"),
        ("no-label", "selection-label"),
        ("assigned", "assigned"),
        ("ready", "ready-for-human"),
        ("wontfix", "wontfix"),
        ("empty-body", "empty-body"),
        ("require-all", "require_all"),
        ("exclude", "exclude"),
        ("blocker", "blocker"),
        ("attempt-pr", "attempt-pr"),
    ],
)
def test_ineligible_issue_rejected_at_submit(
    tmp_path: Path, mutate: str, reason: str
) -> None:
    """A definitely ineligible issue is ``rejected`` at submit, with the reason."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        labels: tuple[str, ...] = (SELECTION_LABEL,)
        body = "## Acceptance criteria\n- [ ] done\n"
        assignees: tuple[str, ...] = ()
        is_open = True
        blocker = False
        attempt_pr = False
        extra: dict[str, Any] = {}
        if mutate == "closed":
            is_open = False
        elif mutate == "no-label":
            labels = ("other-label",)
        elif mutate == "assigned":
            assignees = ("human",)
        elif mutate == "ready":
            labels = (SELECTION_LABEL, "ready-for-human")
        elif mutate == "wontfix":
            labels = (SELECTION_LABEL, "wontfix")
        elif mutate == "empty-body":
            body = "   \n"
        elif mutate == "require-all":
            extra = {"require_all": ("needs-triage",)}
        elif mutate == "exclude":
            labels = (SELECTION_LABEL, "paused")
            extra = {"exclude": ("paused",)}
        elif mutate == "blocker":
            blocker = True
        elif mutate == "attempt-pr":
            attempt_pr = True
        listener_tracker = _tracker_with(
            _candidate(
                41, is_open=is_open, assignees=assignees, labels=labels, body=body
            )
        )
        if blocker:
            listener_tracker.blockers_open[41] = True
        if attempt_pr:
            listener_tracker.attempt_prs_open.add(41)
        config = _claim_config(**extra)
        reply = _answer(
            ledger.path,
            _request(
                "next",
                request_id="req-next-guard",
                expect_repo="example-owner/example-repo",
                issue=41,
            ),
            claim_config=config,
            tracker_factory=lambda: listener_tracker,
        )
        assert reply is not None
        assert reply["error_code"] == "rejected"
        assert reply["details"] == {"issue": 41, "reason": reason}
        assert _stored(ledger.path, "req-next-guard") is None
        assert operator_commands.get_pending_next_command(
            operator_commands.open_ledger_connection(
                ledger.path, timeout_seconds=2.0
            )
        ) is None
    finally:
        store.close()
        ledger.close()


def test_commit_read_failure_commits_and_boundary_decides(
    tmp_path: Path,
) -> None:
    """A failed commit-time read commits; the Claim boundary resolves it."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        from support.fake_tracker import FakeTracker

        failing = FakeTracker(agent_login="coding-agent")
        failing.fail_on = "fetch_candidate"
        config = _claim_config()
        reply = _answer(
            ledger.path,
            _request(
                "next",
                request_id="req-next-flaky",
                expect_repo="example-owner/example-repo",
                issue=41,
            ),
            claim_config=config,
            tracker_factory=lambda: failing,
        )
        assert reply is not None
        assert reply["command"]["acknowledgement"] == "accepted"
        # The Worker's own reads succeed: the priority completes.
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        claimed = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert claimed is not None
        assert claimed.issue_number == 41
        assert _stored(ledger.path, "req-next-flaky") is not None
        stored = _stored(ledger.path, "req-next-flaky")
        assert stored is not None
        assert stored.acknowledgement == "completed"
    finally:
        store.close()
        ledger.close()


def test_issue_ineligible_after_commit_becomes_not_fulfilled(
    tmp_path: Path,
) -> None:
    """N guarded at the boundary resolves ``not_fulfilled``; selection goes on."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        config = _claim_config()
        _commit_next(ledger.path, "req-next-stale", 41, listener_tracker, config)
        # A human adds ``ready-for-human`` before the next sweep.
        stale = worker_tracker.candidates[41]
        from dataclasses import replace

        worker_tracker.candidates[41] = replace(
            stale, labels=(*stale.labels, "ready-for-human")
        )
        claimed = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert claimed is not None
        assert claimed.issue_number == 40
        stored = _stored(ledger.path, "req-next-stale")
        assert stored is not None
        assert stored.acknowledgement == "not_fulfilled"
    finally:
        store.close()
        ledger.close()


def test_absent_issue_becomes_not_fulfilled(tmp_path: Path) -> None:
    """N missing from the candidates resolves ``not_fulfilled``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(_candidate(40, day=1))
        config = _claim_config()
        _commit_next(ledger.path, "req-next-gone", 41, listener_tracker, config)
        claimed = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert claimed is not None
        assert claimed.issue_number == 40
        stored = _stored(ledger.path, "req-next-gone")
        assert stored is not None
        assert stored.acknowledgement == "not_fulfilled"
    finally:
        store.close()
        ledger.close()


def test_readback_release_becomes_not_fulfilled(tmp_path: Path) -> None:
    """N released by its read-back resolves ``not_fulfilled`` in that release."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )

        def _race(issue_number: int) -> None:
            from dataclasses import replace

            if issue_number == 41:
                current = worker_tracker.candidates[41]
                worker_tracker.candidates[41] = replace(
                    current, assignees=(*current.assignees, "human")
                )

        worker_tracker.on_assign = _race
        config = _claim_config()
        _commit_next(ledger.path, "req-next-race", 41, listener_tracker, config)
        claimed = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert claimed is not None
        assert claimed.issue_number == 40
        stored = _stored(ledger.path, "req-next-race")
        assert stored is not None
        assert stored.acknowledgement == "not_fulfilled"
        # The failed priority Attempt released exactly once, numbered once.
        assert ledger.next_attempt_number(41) == 2
    finally:
        store.close()
        ledger.close()


def test_failed_readback_resolves_in_finalization(tmp_path: Path) -> None:
    """A read-back failure leaves the priority pending until finalization."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        from support.fake_tracker import FakeTracker

        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker.fail_on = "fetch_candidate"
        config = _claim_config()
        _commit_next(
            ledger.path, "req-next-broken", 41, listener_tracker, config
        )
        with pytest.raises(RuntimeError, match="fake tracker failure"):
            select_and_claim(
                tracker=worker_tracker, ledger=ledger, config=config
            )
        assert ledger.has_unfinished_attempt() is True
        stored = _stored(ledger.path, "req-next-broken")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
        ledger.set_attempt_state("#41/1", "released")
        stored = _stored(ledger.path, "req-next-broken")
        assert stored is not None
        assert stored.acknowledgement == "not_fulfilled"
    finally:
        store.close()
        ledger.close()


def test_newer_priority_supersedes_older(tmp_path: Path) -> None:
    """A newer priority supersedes the older one; the sweep tries the newer."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1),
            _candidate(41, day=2),
            _candidate(42, day=3),
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1),
            _candidate(41, day=2),
            _candidate(42, day=3),
        )
        config = _claim_config()
        _commit_next(ledger.path, "req-next-old", 41, listener_tracker, config)
        _commit_next(ledger.path, "req-next-new", 42, listener_tracker, config)
        old = _stored(ledger.path, "req-next-old")
        assert old is not None
        assert old.acknowledgement == "superseded"
        claimed = select_and_claim(
            tracker=worker_tracker, ledger=ledger, config=config
        )
        assert claimed is not None
        assert claimed.issue_number == 42
        new = _stored(ledger.path, "req-next-new")
        assert new is not None
        assert new.acknowledgement == "completed"
    finally:
        store.close()
        ledger.close()


def test_priority_stays_pending_with_intake_closed(tmp_path: Path) -> None:
    """With Intake closed the priority stays pending and never reopens it."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        stop_reply = _answer(
            ledger.path,
            _request(
                "stop",
                request_id="req-stop-closed",
                expect_repo="example-owner/example-repo",
            ),
        )
        assert stop_reply is not None
        assert stop_reply["command"]["acknowledgement"] == "completed"
        assert ledger.intake_is_open() is False
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        config = _claim_config()
        reply = _commit_next(
            ledger.path, "req-next-drain", 41, listener_tracker, config
        )
        assert reply["command"]["acknowledgement"] == "accepted"
        assert ledger.intake_is_open() is False
        assert (
            select_and_claim(
                tracker=worker_tracker, ledger=ledger, config=config
            )
            is None
        )
        assert ledger.intake_is_open() is False
        stored = _stored(ledger.path, "req-next-drain")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
    finally:
        store.close()
        ledger.close()


def test_listener_never_uses_worker_tracker(tmp_path: Path) -> None:
    """The commit-time read runs on the listener's instance, not the Worker's."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        worker_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        before = list(worker_tracker.calls)
        builds: list[str] = []

        def _factory() -> Any:
            builds.append("built")
            return listener_tracker

        reply = _answer(
            ledger.path,
            _request(
                "next",
                request_id="req-next-sep",
                expect_repo="example-owner/example-repo",
                issue=41,
            ),
            claim_config=_claim_config(),
            tracker_factory=_factory,
        )
        assert reply is not None
        assert "command" in reply
        assert list(worker_tracker.calls) == before
        assert builds, "the listener must build its own tracker instance"
        assert any(
            name == "fetch_candidate" for name, _ in listener_tracker.calls
        )
    finally:
        store.close()
        ledger.close()


def test_identity_advertises_next_issue(tmp_path: Path) -> None:
    """``identity`` advertises the priority capability."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        result = operator_listener.build_identity_result(
            ledger.path, _live()
        )
        assert result["capabilities"] == ["next-issue", "stop-after"]
    finally:
        store.close()
        ledger.close()


def test_next_replay_and_payload_mismatch(tmp_path: Path) -> None:
    """Same ID and issue replays; same ID with another issue mismatches."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(
            _candidate(40, day=1), _candidate(41, day=2)
        )
        config = _claim_config()
        first = _commit_next(
            ledger.path, "req-next-idem", 41, listener_tracker, config
        )
        assert first["replayed"] is False
        second = _answer(
            ledger.path,
            _request(
                "next",
                request_id="req-next-idem",
                expect_repo="example-owner/example-repo",
                issue=41,
            ),
            claim_config=config,
            tracker_factory=lambda: listener_tracker,
        )
        assert second is not None
        assert second["replayed"] is True
        assert second["command"] == first["command"]
        clash = _answer(
            ledger.path,
            _request(
                "next",
                request_id="req-next-idem",
                expect_repo="example-owner/example-repo",
                issue=40,
            ),
            claim_config=config,
            tracker_factory=lambda: listener_tracker,
        )
        assert clash is not None
        assert clash["error_code"] == "payload_mismatch"
        across = _answer(
            ledger.path,
            _request(
                "stop",
                request_id="req-next-idem",
                expect_repo="example-owner/example-repo",
            ),
        )
        assert across is not None
        assert across["error_code"] == "payload_mismatch"
    finally:
        store.close()
        ledger.close()


def test_next_repository_mismatch_stores_nothing(tmp_path: Path) -> None:
    """A wrong ``--expect-repo`` answers mismatch and stores nothing."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(_candidate(41))
        reply = _answer(
            ledger.path,
            _request(
                "next",
                request_id="req-next-repo",
                expect_repo="example-owner/other-repo",
                issue=41,
            ),
            claim_config=_claim_config(),
            tracker_factory=lambda: listener_tracker,
        )
        assert reply is not None
        assert reply["error_code"] == "repository_mismatch"
        assert reply["repository"] == "example-owner/example-repo"
        assert _stored(ledger.path, "req-next-repo") is None
        assert listener_tracker.calls == []
    finally:
        store.close()
        ledger.close()


def test_next_invalid_issue_is_invalid_argument(tmp_path: Path) -> None:
    """A non-positive issue number answers ``invalid_argument``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(_candidate(41))
        for bad in (0, -3, "41", 4.5, True, None):
            reply = _answer(
                ledger.path,
                _request(
                    "next",
                    request_id="req-next-bad",
                    expect_repo="example-owner/example-repo",
                    issue=bad,
                ),
                claim_config=_claim_config(),
                tracker_factory=lambda: listener_tracker,
            )
            assert reply is not None, bad
            assert reply["error_code"] == "invalid_argument", bad
        assert _stored(ledger.path, "req-next-bad") is None
    finally:
        store.close()
        ledger.close()


def test_reconcile_leaves_priority_pending(tmp_path: Path) -> None:
    """Restart reconciliation never completes a pending priority."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(_candidate(41))
        config = _claim_config()
        _commit_next(ledger.path, "req-next-restart", 41, listener_tracker, config)
        ledger.reconcile_accepted_commands()
        stored = _stored(ledger.path, "req-next-restart")
        assert stored is not None
        assert stored.acknowledgement == "accepted"
    finally:
        store.close()
        ledger.close()


def test_command_lookup_returns_next_record(tmp_path: Path) -> None:
    """``command <request-id>`` returns a committed priority."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        listener_tracker = _tracker_with(_candidate(41))
        created = _commit_next(
            ledger.path, "req-next-lookup", 41, listener_tracker
        )
        found = _answer(
            ledger.path, _request("command", request_id="req-next-lookup")
        )
        assert found is not None
        assert found["command"] == created["command"]
    finally:
        store.close()
        ledger.close()


def _run_agentctl(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    code = agentctl.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    return code, json.loads(captured.out)


def _engine_reply(
    reply: dict[str, Any],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _stub(body: dict[str, Any]) -> dict[str, Any]:
        _ = body
        return reply

    return _stub


def test_agentctl_next_success_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 carries ``{"command": ..., "replayed": ...}`` for ``next``."""
    command = {
        "request_id": "req-next-cli-1",
        "sequence": 7,
        "kind": "next",
        "arguments": {"issue": 41},
        "repository": "example-owner/example-repo",
        "acknowledgement": "accepted",
        "detail": "priority recorded, tried first at the next claim boundary",
        "created_at": "2026-11-06T09:12:03Z",
        "updated_at": "2026-11-06T09:12:03Z",
    }
    seen: list[dict[str, Any]] = []

    def _stub(body: dict[str, Any]) -> dict[str, Any]:
        seen.append(body)
        return {"command": command, "replayed": False}

    monkeypatch.setattr(agentctl, "_query_engine", _stub)
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "next",
            "issue",
            "41",
            "--request-id",
            "req-next-cli-1",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 0
    assert envelope == {
        "contract": "1.0",
        "ok": True,
        "result": {"command": command, "replayed": False},
    }
    assert seen == [
        {
            "op": "next",
            "request_id": "req-next-cli-1",
            "expect_repo": "example-owner/example-repo",
            "issue": 41,
        }
    ]


def test_agentctl_next_rejected_carries_guard_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A guarded issue exits 1 with ``rejected`` and the guard reason."""
    monkeypatch.setattr(
        agentctl,
        "_query_engine",
        _engine_reply(
            {
                "error_code": "rejected",
                "details": {"issue": 41, "reason": "blocker"},
            }
        ),
    )
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "next",
            "issue",
            "41",
            "--request-id",
            "req-next-cli-2",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 1
    assert envelope["error"]["code"] == "rejected"
    assert envelope["error"]["request_id"] == "req-next-cli-2"
    assert envelope["error"]["retryable"] is False
    assert envelope["error"]["details"] == {"issue": 41, "reason": "blocker"}


def test_agentctl_next_values_and_grammar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad issue numbers are ``invalid_argument``; bad shapes are ``usage``."""
    for argv in (
        [
            "--contract",
            "1",
            "next",
            "issue",
            "0",
            "--request-id",
            "req-next-cli-3",
            "--expect-repo",
            "o/r",
        ],
        [
            "--contract",
            "1",
            "next",
            "issue",
            "abc",
            "--request-id",
            "req-next-cli-3",
            "--expect-repo",
            "o/r",
        ],
        [
            "--contract",
            "1",
            "next",
            "issue",
            "41",
            "--request-id",
            "bad!",
            "--expect-repo",
            "o/r",
        ],
    ):
        code, envelope = _run_agentctl(list(argv), capsys)
        assert code == 1, argv
        assert envelope["error"]["code"] == "invalid_argument", argv
    for argv in (
        ["--contract", "1", "next"],
        ["--contract", "1", "next", "issue"],
        ["--contract", "1", "next", "issue", "41"],
        ["--contract", "1", "next", "pool", "41"],
    ):
        code, envelope = _run_agentctl(list(argv), capsys)
        assert code == 2, argv
        assert envelope["error"]["code"] == "usage", argv


def test_agentctl_next_offline_is_unsupported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Offline ``next`` answers ``unsupported`` without touching the engine."""
    code, envelope = _run_agentctl(
        [
            "--contract",
            "1",
            "--offline",
            "next",
            "issue",
            "41",
            "--request-id",
            "req-next-cli-4",
            "--expect-repo",
            "example-owner/example-repo",
        ],
        capsys,
    )
    assert code == 3
    assert envelope["error"]["code"] == "unsupported"
