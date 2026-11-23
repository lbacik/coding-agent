"""The AI-assisted Round Handoff publication and pickup (contract §8).

Issue #614. A successful AI-assisted Handoff publishes the receiver's note
— the bounded, redacted ``document`` Step Artifact from the latest eligible
Step Execution — with the verified work as a Round Handoff. A second Worker
sees that note as declared input to a fresh Continuation. The enforced
reference proof and the terminal record and pickup rules match the Hard
Handoff; a failed push, an unverifiable revision or a missing note leaves a
diagnostic with no offer. When note production itself fails, the configured
Hard Handoff publishes its Execution Summary account with no further model
requests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.ledger.store import AttemptPin, RunLedger
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    parse_handoff_block,
    render_handoff_section,
)
from coding_agent.worker.attempt_run import (
    _decide_live_handoff,
    _resolve_live_handoff_note,
)
from coding_agent.workflow.schema import (
    AttemptCeiling,
    InputDecl,
    RouteTarget,
    RoundHandoffAnnotation,
    RoundHandoffConfig,
    Workflow,
    WorkflowStep,
)

WORKFLOW = "implement"
SELECTION_LABEL = "agent:implement"
PICKUP_LABEL = "round-finished"
CONFIGURED_UNFINISHED = ("stale-unfinished",)
ATTEMPT = "implement#7/1"
NUMBER = 7
WORK = "a" * 40
BASE = "b" * 40
REF = "agent/implement-7-1-work"
NOTE_REF = "scribe.handoff.note"


def _workflow(
    *,
    kind: str | None = "ai-assisted",
    note: str | None = NOTE_REF,
    enable_handoff: bool = True,
) -> Workflow:
    annotation = (
        RoundHandoffAnnotation(kind=kind, note=note) if kind is not None else None
    )
    routes: dict[str, RouteTarget] = {
        "pushed": RouteTarget(end="unfinished", round_handoff=annotation),
        "no-changes": RouteTarget(end="unfinished", round_handoff=annotation),
    }
    return Workflow(
        id=WORKFLOW,
        model="sonnet",
        input=InputDecl(type="issue", label=SELECTION_LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="publish",
        defaults_routes={"error": RouteTarget(end="failed")},
        steps={
            "publish": WorkflowStep(
                id="publish",
                agent=None,
                operation="commit-and-push",
                max_executions=1,
                routes=routes,
            ),
        },
        outcomes={"unfinished": CONFIGURED_UNFINISHED},
        round_handoff=(
            RoundHandoffConfig(max_automatic_pickups=2) if enable_handoff else None
        ),
    )


def _result(**overrides: Any) -> WorkflowResult:
    fields: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "outcome": "unfinished",
        "reason": "ended at end unfinished after result pushed of step publish",
        "ending_step": "publish",
        "step_result": "pushed",
        "workflow_end": "unfinished",
        "ended": True,
    }
    fields.update(overrides)
    return WorkflowResult(**fields)


def _pin(**overrides: Any) -> AttemptPin:
    fields: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "workflow_id": WORKFLOW,
        "input_type": "issue",
        "input_number": NUMBER,
        "title": "Add a greeting",
        "body": "a body",
        "fingerprint": "fp",
        "base_revision": BASE,
        "base_tip": BASE,
        "input_head": None,
        "head_branch": None,
        "base_branch": "main",
        "selection_actor": "alice",
        "selection_actor_id": 123,
        "selection_actor_type": "User",
        "selection_actor_at": "2026-11-23T16:00:00+00:00",
        "identity_mode": "org-member",
    }
    fields.update(overrides)
    return AttemptPin(**fields)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _open(ledger: RunLedger) -> None:
    ledger.open_attempt(ATTEMPT, effective_configuration={})


def _record_note(
    ledger: RunLedger,
    *,
    step_id: str = "scribe",
    result: str = "handoff",
    document: str = "the handoff note",
) -> None:
    ledger.open_step_execution(ATTEMPT, step_id, 1)
    ledger.end_step_execution(
        ATTEMPT,
        step_id,
        1,
        result=result,
        payload={"note": document},
        facts={"changed_files": ["src/a.py"]},
    )


def _record_push_receipt(ledger: RunLedger, *, ref: str = REF) -> None:
    ledger.record_step_effect_intent(
        ATTEMPT,
        "publish",
        1,
        0,
        rendered_payload=f"commit-and-push {ATTEMPT}: push {WORK} to {ref}",
        expected_target={
            "operation": "commit-and-push",
            "branch": ref,
            "revision": WORK,
        },
    )
    ledger.record_step_effect_receipt(ATTEMPT, "publish", 1, 0, remote_id=WORK)


def _remote(
    monkeypatch: pytest.MonkeyPatch,
    *,
    work_sha: str | None = WORK,
    ancestor: bool = True,
) -> None:
    import coding_agent.worker.attempt_run as attempt_run

    def _fake(**kwargs: Any) -> tuple[str | None, str | None, bool]:
        return WORK, work_sha, ancestor

    monkeypatch.setattr(attempt_run, "_read_live_remote_evidence", _fake)


# --- slice 1: the live decision publishes the declared document ----------------


def test_ai_assisted_pushed_round_publishes_the_declared_document(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An annotated pushed Route publishes the receiver's note document."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_note(ledger)
    _record_push_receipt(ledger)
    _remote(monkeypatch)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, RoundHandoffOffer)
    assert decided.note_kind == "ai-assisted"
    assert decided.note == "the handoff note"
    assert decided.work_commit == WORK
    assert decided.ref == REF
    assert decided.base_revision == BASE
    assert decided.pickup_label == PICKUP_LABEL
    # The same terminal record and pickup rules as the Hard Handoff.
    assert parse_handoff_block(render_handoff_section(decided)) == decided


def test_ai_assisted_no_changes_round_publishes_without_a_push(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An annotated no-changes Route publishes the same note with no receipt."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_note(ledger)
    _remote(monkeypatch)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(
            step_result="no-changes",
            reason="ended at end unfinished after result no-changes of step publish",
        ),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, RoundHandoffOffer)
    assert decided.note_kind == "ai-assisted"
    assert decided.note == "the handoff note"
    assert decided.work_commit == WORK
    assert parse_handoff_block(render_handoff_section(decided)) == decided


def test_ai_assisted_note_resolves_the_latest_eligible_execution(
    ledger: RunLedger,
) -> None:
    """The published note is the latest eligible Step Execution's document."""
    _open(ledger)
    _record_note(ledger, document="first note")
    ledger.open_step_execution(ATTEMPT, "scribe", 2)
    ledger.end_step_execution(
        ATTEMPT,
        "scribe",
        2,
        result="handoff",
        payload={"note": "second note"},
        facts={},
    )

    note = _resolve_live_handoff_note(
        ledger=ledger,
        attempt_id=ATTEMPT,
        annotation_kind="ai-assisted",
        annotation_note=NOTE_REF,
    )

    assert note == "second note"


# --- slice 2: diagnostics leave no offer --------------------------------------


def test_ai_assisted_failed_push_with_an_available_note_is_a_diagnostic(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note is ready but the push never landed: no pickup is offered."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_note(ledger)
    _remote(monkeypatch, work_sha=None)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, HandoffDiagnostic)
    assert decided.reason == "failed-push"


def test_ai_assisted_unavailable_revision_with_an_available_note_is_a_diagnostic(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note is ready but the remote serves another sha: no pickup."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_note(ledger)
    _record_push_receipt(ledger)
    _remote(monkeypatch, work_sha="c" * 40)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, HandoffDiagnostic)
    assert decided.reason == "unavailable-revision"


def test_ai_assisted_missing_note_is_a_diagnostic(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No resolvable note document is a missing note, never an empty offer."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_push_receipt(ledger)
    _remote(monkeypatch)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, HandoffDiagnostic)
    assert decided.reason == "missing-note"


def test_ai_assisted_no_changes_without_remote_is_unavailable(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean tree the remote cannot serve offers no pickup."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_note(ledger)
    _remote(monkeypatch, work_sha=None)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(
            step_result="no-changes",
            reason="ended at end unfinished after result no-changes of step publish",
        ),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, HandoffDiagnostic)
    assert decided.reason == "unavailable-revision"


def test_ai_assisted_note_is_bounded_with_an_artifact_pointer(
    ledger: RunLedger,
) -> None:
    """An overlong note is cut with a pointer at its Step Artifact."""
    _open(ledger)
    _record_note(ledger, document="x" * 9000)

    note = _resolve_live_handoff_note(
        ledger=ledger,
        attempt_id=ATTEMPT,
        annotation_kind="ai-assisted",
        annotation_note=NOTE_REF,
    )

    assert note is not None
    assert len(note) <= 8_000 + 500
    assert "scribe.1.note" in note
    assert "truncated" in note
