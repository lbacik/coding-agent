"""Live Round Handoff preparation from the Attempt run (contract §8).

Issue #612, acceptance bullet 3 (live wiring). The live finaliser builds
its preparation from the typed Workflow (enablement plus the taken Route's
annotation), the declared note document or the ledger's Execution
Summaries, the workspace HEAD, the Claim pin, the push receipt and the
remote read-back — then decides the offer or diagnostic through
:func:`decide_handoff` and settles it through :func:`finish_attempt`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.ledger.store import AttemptPin, RunLedger
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    build_live_preparation,
)
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.worker.attempt_run import (
    _decide_live_handoff,
    _live_push_evidence,
    _read_live_remote_evidence,
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
ATTEMPT = "implement#7/1"
NUMBER = 7
PINNED_LABEL = "agent:implement"
PICKUP_LABEL = "round-finished"
WORK = "a" * 40
BASE = "b" * 40
REF = "agent/implement-7-1-work"


def _workflow(
    *,
    enable_handoff: bool = True,
    kind: str | None = "hard",
    note: str | None = None,
) -> Workflow:
    annotation = (
        RoundHandoffAnnotation(kind=kind, note=note) if kind is not None else None
    )
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label=PINNED_LABEL),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        start="publish",
        defaults_routes={
            "error": RouteTarget(end="failed"),
        },
        steps={
            "publish": WorkflowStep(
                id="publish",
                agent=None,
                operation="commit-and-push",
                max_executions=1,
                routes={
                    "pushed": RouteTarget(end="unfinished", round_handoff=annotation),
                },
            ),
        },
        outcomes={"unfinished": ("stale-unfinished",)},
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
        "workflow_id": "implement",
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


def _record_execution(
    ledger: RunLedger,
    *,
    step_id: str = "work",
    seq: int = 1,
    result: str = "done",
    payload: dict[str, object] | None = None,
    facts: dict[str, object] | None = None,
) -> None:
    ledger.open_step_execution(ATTEMPT, step_id, seq)
    ledger.end_step_execution(
        ATTEMPT,
        step_id,
        seq,
        result=result,
        payload=payload,
        facts=facts if facts is not None else {"changed_files": ["src/a.py"]},
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
    monkeypatch: pytest.MonkeyPatch, *, work_sha: str | None = WORK, ancestor: bool = True
) -> None:
    import coding_agent.worker.attempt_run as attempt_run

    def _fake(**kwargs: Any) -> tuple[str | None, str | None, bool]:
        return WORK, work_sha, ancestor

    monkeypatch.setattr(attempt_run, "_read_live_remote_evidence", _fake)


# --- the pure preparation constructor --------------------------------------------------


def test_live_preparation_needs_the_publishing_path() -> None:
    """Off the path there is no preparation and no diagnostic section."""
    base: dict[str, Any] = {
        "workflow_id": "implement",
        "issue": NUMBER,
        "attempt": ATTEMPT,
        "outcome": "unfinished",
        "ending_step": "publish",
        "step_result": "pushed",
        "pickup_label": PICKUP_LABEL,
        "limit": 2,
        "annotation_kind": "hard",
        "note_text": "did the thing",
        "work_commit": WORK,
        "ref": REF,
        "base_revision": BASE,
        "push_landed": True,
        "remote_verified": True,
    }

    assert build_live_preparation(**base) is not None
    assert build_live_preparation(**{**base, "outcome": "completed"}) is None
    assert build_live_preparation(**{**base, "pickup_label": None}) is None
    assert build_live_preparation(**{**base, "limit": None}) is None
    assert build_live_preparation(**{**base, "annotation_kind": None}) is None
    assert build_live_preparation(**{**base, "annotation_kind": "soft"}) is None


# --- the note resolution ------------------------------------------------------------------


def test_ai_assisted_note_resolves_the_declared_document(ledger: RunLedger) -> None:
    """The published note is the declared Step Artifact, bounded with a pointer."""
    _open(ledger)
    _record_execution(
        ledger, step_id="note", result="ready", payload={"document": "the plan"}
    )

    note = _resolve_live_handoff_note(
        ledger=ledger,
        attempt_id=ATTEMPT,
        annotation_kind="ai-assisted",
        annotation_note="note.ready.document",
    )

    assert note == "the plan"


def test_ai_assisted_note_without_a_document_is_missing(ledger: RunLedger) -> None:
    """An unresolvable note reference is a missing note, never an empty offer."""
    _open(ledger)

    assert (
        _resolve_live_handoff_note(
            ledger=ledger,
            attempt_id=ATTEMPT,
            annotation_kind="ai-assisted",
            annotation_note="note.ready.document",
        )
        is None
    )
    assert (
        _resolve_live_handoff_note(
            ledger=ledger,
            attempt_id=ATTEMPT,
            annotation_kind="ai-assisted",
            annotation_note=None,
        )
        is None
    )


def test_hard_note_composes_execution_summaries(ledger: RunLedger) -> None:
    """The model-free note renders the ledger's available summaries."""
    _open(ledger)
    _record_execution(ledger)

    note = _resolve_live_handoff_note(
        ledger=ledger,
        attempt_id=ATTEMPT,
        annotation_kind="hard",
        annotation_note=None,
        context=["crossed hard at 90000 tokens"],
    )

    assert note is not None
    assert "src/a.py" in note
    assert "crossed hard" in note


def test_hard_note_without_content_is_missing(ledger: RunLedger) -> None:
    """No summaries and no context is a missing note."""
    _open(ledger)
    ledger.open_step_execution(ATTEMPT, "work", 1)
    ledger.end_step_execution(ATTEMPT, "work", 1, result="done", facts={})

    assert (
        _resolve_live_handoff_note(
            ledger=ledger,
            attempt_id=ATTEMPT,
            annotation_kind="hard",
            annotation_note=None,
        )
        is None
    )


# --- the push evidence ----------------------------------------------------------------------


def test_push_receipt_lands_the_revision(ledger: RunLedger) -> None:
    """The latest receipt for the Attempt branch is the landed revision."""
    _open(ledger)
    _record_push_receipt(ledger)

    assert _live_push_evidence(ledger=ledger, attempt_id=ATTEMPT, ref=REF) == (
        True,
        WORK,
    )


def test_push_evidence_ignores_other_branches(ledger: RunLedger) -> None:
    """A receipt for another ref is not this round's push."""
    _open(ledger)
    _record_push_receipt(ledger, ref="agent/other")

    assert _live_push_evidence(ledger=ledger, attempt_id=ATTEMPT, ref=REF) == (
        False,
        None,
    )


def test_no_receipt_is_no_push_evidence(ledger: RunLedger) -> None:
    """A clean-tree no-changes round records no receipt of its own."""
    _open(ledger)

    assert _live_push_evidence(ledger=ledger, attempt_id=ATTEMPT, ref=REF) == (
        False,
        None,
    )


# --- the live decision ---------------------------------------------------------------------------


def test_live_decision_offers_a_verified_hard_round(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path carries the exact label and the pinned evidence."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
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
    assert decided.pickup_label == PICKUP_LABEL
    assert (decided.work_commit, decided.base_revision) == (WORK, BASE)
    assert decided.selection_actor_id == 123


def test_live_decision_stays_ordinary_off_the_path(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled, unannotated and non-unfinished rounds stay ordinary."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
    _record_push_receipt(ledger)
    _remote(monkeypatch)

    assert (
        _decide_live_handoff(
            ledger=ledger,
            workflow=_workflow(enable_handoff=False),
            result=_result(),
            number=NUMBER,
            workspace_path=Path("/nonexistent"),
            attempt_branch=REF,
            canonical_remote="https://example.com/o/r.git",
        )
        is None
    )
    assert (
        _decide_live_handoff(
            ledger=ledger,
            workflow=_workflow(kind=None),
            result=_result(),
            number=NUMBER,
            workspace_path=Path("/nonexistent"),
            attempt_branch=REF,
            canonical_remote="https://example.com/o/r.git",
        )
        is None
    )
    assert (
        _decide_live_handoff(
            ledger=ledger,
            workflow=_workflow(),
            result=_result(outcome="completed", ending_step="work", step_result="done"),
            number=NUMBER,
            workspace_path=Path("/nonexistent"),
            attempt_branch=REF,
            canonical_remote="https://example.com/o/r.git",
        )
        is None
    )


def test_live_decision_diagnoses_a_failed_push(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No receipt and no remote commit is a failed push with no pickup."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
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


def test_live_decision_serving_remote_covers_a_missing_receipt(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-changes round the remote already serves still publishes."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
    _remote(monkeypatch)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(),
        result=_result(step_result="pushed"),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, RoundHandoffOffer)


def test_live_decision_diagnoses_a_missing_note(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable note document is a missing-note diagnostic."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_push_receipt(ledger)
    _remote(monkeypatch)

    decided = _decide_live_handoff(
        ledger=ledger,
        workflow=_workflow(kind="ai-assisted", note="note.ready.document"),
        result=_result(),
        number=NUMBER,
        workspace_path=Path("/nonexistent"),
        attempt_branch=REF,
        canonical_remote="https://example.com/o/r.git",
    )

    assert isinstance(decided, HandoffDiagnostic)
    assert decided.reason == "missing-note"


def test_live_decision_diagnoses_an_unavailable_revision(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote serving another sha leaves an unavailable-revision diagnostic."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
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


def test_live_decision_without_a_pin_has_no_authorisation(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Claim pin is missing authorisation, with ordinary labels."""
    _open(ledger)
    _record_execution(ledger)
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
    assert decided.reason == "missing-authorisation"


def test_live_decision_refuses_a_rebased_base(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Base Revision outside the work history is unavailable."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
    _record_push_receipt(ledger)
    _remote(monkeypatch, ancestor=False)

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


# --- the git read-back -------------------------------------------------------------------------------


def _git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=path, check=True)
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_remote_evidence_reads_the_workspace_head(tmp_path: Path) -> None:
    """The reader reports HEAD, a missing remote ref and a true ancestry."""
    repo = tmp_path / "ws"
    head = _git_repo(repo)

    work, remote_sha, ancestor = _read_live_remote_evidence(
        workspace_path=repo,
        canonical_remote="https://example.com/o/r.git",
        ref="agent/implement-7-1-work",
        base_revision=head,
        helper=None,
        redact=None,
    )

    assert work == head
    assert remote_sha is None
    assert ancestor is True


def test_remote_evidence_degrades_on_an_unreadable_tree(tmp_path: Path) -> None:
    """A missing checkout diagnoses the round instead of failing finalisation."""
    work, remote_sha, ancestor = _read_live_remote_evidence(
        workspace_path=tmp_path / "absent",
        canonical_remote="https://example.com/o/r.git",
        ref="agent/implement-7-1-work",
        base_revision=BASE,
        helper=None,
        redact=None,
    )

    assert (work, remote_sha, ancestor) == (None, None, False)
