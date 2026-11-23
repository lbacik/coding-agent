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
from coding_agent.runtime.redaction import SecretRegistry
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
from support.workflow_config import load_config

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


def test_ai_assisted_note_is_redacted_from_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published note carries the redacted Step Artifact, never a secret."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    secret = "github_pat_testtoken"
    handle = RunLedger(
        tmp_path / "state" / "run-ledger.sqlite3",
        registry=SecretRegistry({"GITHUB_TOKEN": secret}),
    )
    try:
        handle.open_attempt(ATTEMPT, effective_configuration={})
        handle.open_step_execution(ATTEMPT, "scribe", 1)
        handle.end_step_execution(
            ATTEMPT,
            "scribe",
            1,
            result="handoff",
            payload={"note": f"rotate {secret} now"},
            facts={},
        )

        note = _resolve_live_handoff_note(
            ledger=handle,
            attempt_id=ATTEMPT,
            annotation_kind="ai-assisted",
            annotation_note=NOTE_REF,
        )
    finally:
        handle.close()

    assert note is not None
    assert secret not in note
    assert "«redacted:GITHUB_TOKEN»" in note


# --- slice 3: the enforced reference proof --------------------------------------

_BASE_YAML = """\
version: 4
target:
  kind: github
  repository: OWNER/REPO
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
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000
"""

_CEILING = (
    "    limits: {cost-usd: 6, effective-tokens: 1500000,"
    " tool-calls: 300, wall-clock-minutes: 30}\n"
)

_DEFAULTS = """\
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
"""

_OUTCOMES = """\
    outcomes:
      unfinished: {labels: [needs-followup]}
"""

_PROVEN_STEPS = """\
    steps:
      preflight:
        operation: check-readiness
        routes: {ready: process, not-ready: {end: blocked}}
      process:
        agent:
          tools: [workspace-read]
          inputs: [{optional: round-handoff.note}]
          results:
            done: {summary: document}
        routes: {done: ai-note}
      ai-note:
        agent:
          tools: [workspace-read]
          inputs: [process.facts]
          results:
            handoff: {note: document}
        routes: {handoff: handoff-push}
      handoff-push:
        operation: commit-and-push
        routes:
          pushed: {end: unfinished,
            round-handoff: {kind: ai-assisted, note: ai-note.handoff.note}}
          no-changes: {end: unfinished, round-handoff: {kind: hard}}
"""


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(_BASE_YAML + body, encoding="utf-8")
    return path


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _faults_text(faults: Any) -> str:
    return "\n".join(f"{fault.path}: {fault.detail}" for fault in faults)


def _proven_body() -> str:
    return (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + _CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: preflight\n"
        + _DEFAULTS
        + _OUTCOMES
        + _PROVEN_STEPS
    )


def test_ai_assisted_note_proven_on_every_path_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared document, proven on every path, annotates the Route."""
    _without_env(monkeypatch)

    config, faults = load_config(_write_yaml(tmp_path, _proven_body()))

    assert config is not None, _faults_text(faults)
    assert not faults
    annotation = config.workflows["implement"].steps["handoff-push"].routes["pushed"].round_handoff
    assert annotation == RoundHandoffAnnotation(
        kind="ai-assisted", note="ai-note.handoff.note"
    )


def test_ai_assisted_note_unproven_on_a_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path bypassing the note step refuses the enforced reference proof."""
    _without_env(monkeypatch)
    steps = _PROVEN_STEPS.replace(
        "routes: {ready: process, not-ready: {end: blocked}}",
        "routes: {ready: process, not-ready: handoff-push}",
    )

    config, faults = load_config(
        _write_yaml(
            tmp_path,
            "workflows:\n"
            "  implement:\n"
            "    model: sonnet\n"
            "    input: {type: issue, label: agent:implement}\n"
            + _CEILING
            + "    round-handoff: {max-automatic-pickups: 2}\n"
            + "    start: preflight\n"
            + _DEFAULTS
            + _OUTCOMES
            + steps,
        )
    )

    assert config is None
    text = _faults_text(faults)
    assert "ai-note.handoff.note" in text
    assert "not proven" in text
