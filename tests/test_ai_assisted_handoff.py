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
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.graph.agent_step import (
    MAX_REPORT_CORRECTIONS,
    REPORT_TOOL_NAME,
    StepAttemptFacts,
)
from coding_agent.runtime.graph.step_executor import (
    ProductionStepExecutor,
    StepOutcome,
)
from coding_agent.runtime.graph.workflow_graph import WorkflowResult, run_workflow
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
    Agent,
    AttemptCeiling,
    ContextThresholds,
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


# --- slice 5: two workers, separate state volumes, real graph -------------------

_E2E_BASE_YAML = """\
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

_E2E_CEILING = (
    "    limits: {cost-usd: 6, effective-tokens: 1500000,"
    " tool-calls: 300, wall-clock-minutes: 30}\n"
)

_E2E_DEFAULTS = """\
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
"""

_E2E_UNFINISHED_OUTCOMES = """\
    outcomes:
      unfinished: {labels: [stale-unfinished]}
"""

_E2E_SCRIBE_AGENT = """\
      scribe:
        agent:
          tools: [workspace-read, workspace-write]
          inputs: [{optional: round-handoff.note}]
          results:
            handoff:
              note: document
"""


def _e2e_config_pushed() -> str:
    return (
        _E2E_BASE_YAML
        + "workflows:\n"
        + "  implement:\n"
        + "    model: sonnet\n"
        + "    input: {type: issue, label: agent:implement}\n"
        + _E2E_CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: scribe\n"
        + _E2E_DEFAULTS
        + _E2E_UNFINISHED_OUTCOMES
        + "    steps:\n"
        + _E2E_SCRIBE_AGENT
        + "        routes: {handoff: handoff-push}\n"
        + "      handoff-push:\n"
        + "        operation: commit-and-push\n"
        + "        routes:\n"
        + "          pushed: {end: unfinished,\n"
        + "            round-handoff: {kind: ai-assisted, note: scribe.handoff.note}}\n"
        + "          no-changes: {end: unfinished,\n"
        + "            round-handoff: {kind: ai-assisted, note: scribe.handoff.note}}\n"
    )


def _e2e_config_clean() -> str:
    return (
        _E2E_BASE_YAML
        + "workflows:\n"
        + "  implement:\n"
        + "    model: sonnet\n"
        + "    input: {type: issue, label: agent:implement}\n"
        + _E2E_CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: scribe\n"
        + _E2E_DEFAULTS
        + _E2E_UNFINISHED_OUTCOMES
        + "    steps:\n"
        + _E2E_SCRIBE_AGENT
        + "        routes: {handoff: push-1}\n"
        + "      push-1:\n"
        + "        operation: commit-and-push\n"
        + "        routes: {pushed: push-2, no-changes: push-2}\n"
        + "      push-2:\n"
        + "        operation: commit-and-push\n"
        + "        routes:\n"
        + "          pushed: {end: unfinished,\n"
        + "            round-handoff: {kind: ai-assisted, note: scribe.handoff.note}}\n"
        + "          no-changes: {end: unfinished,\n"
        + "            round-handoff: {kind: ai-assisted, note: scribe.handoff.note}}\n"
    )


def _e2e_config_continue() -> str:
    return (
        _E2E_BASE_YAML
        + "workflows:\n"
        + "  implement:\n"
        + "    model: sonnet\n"
        + "    input: {type: issue, label: agent:implement}\n"
        + _E2E_CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: preflight\n"
        + _E2E_DEFAULTS
        + "    outcomes:\n"
        + "      completed: {labels: [did-it]}\n"
        + "      blocked: {labels: [ready-for-human]}\n"
        + "      failed: {labels: [ready-for-human]}\n"
        + "    steps:\n"
        + "      preflight:\n"
        + "        operation: check-readiness\n"
        + "        routes: {ready: scribe, not-ready: {end: blocked}}\n"
        + "      scribe:\n"
        + "        agent:\n"
        + "          tools: [workspace-read]\n"
        + "          inputs: [{optional: round-handoff.note}]\n"
        + "          results: {done: {}}\n"
        + "        routes: {done: {end: completed}, limit-exceeded: handoff-push}\n"
        + "      handoff-push:\n"
        + "        operation: commit-and-push\n"
        + "        routes:\n"
        + "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
        + "          no-changes: {end: unfinished, round-handoff: {kind: hard}}\n"
    )


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


# --- slice 4: the Hard Handoff fallback when note production fails ---------------


def _fallback_report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    import json as _json

    return {
        "name": REPORT_TOOL_NAME,
        "args": {"result": result, "payload_json": _json.dumps(payload)},
        "id": call_id,
        "type": "tool_call",
    }


def _fallback_ai(
    calls: list[dict[str, object]] | None = None,
    content: str = "",
    usage: dict[str, object] | None = None,
) -> Any:
    from langchain_core.messages import AIMessage as _AIMessage

    return _AIMessage(
        content=content,
        tool_calls=list(calls or []),
        usage_metadata=usage,
    )


def _fallback_usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _fallback_workflow() -> Workflow:
    """Source soft-crosses into a Reserved receiver that cannot report.

    The receiver's ``error`` (and hard-crossing/limit) Routes stay
    model-free through the configured Hard Handoff operation, which
    publishes the same ``pushed``/``no-changes`` record shape with a
    kind-``hard`` annotation.
    """
    hard_annotation = RoundHandoffAnnotation(kind="hard")
    ai_annotation = RoundHandoffAnnotation(kind="ai-assisted", note=NOTE_REF)
    process = WorkflowStep(
        id="process",
        agent=Agent(tools=["workspace-read"], results={"done": {}}),
        max_executions=1,
        context_thresholds=ContextThresholds(hard_tokens=40000, soft_tokens=20000),
        routes={
            "done": RouteTarget(end="completed"),
            "hard-context-threshold": RouteTarget(step="hard-handoff"),
            "soft-context-threshold": RouteTarget(
                step="scribe", inherit_conversation=True
            ),
        },
    )
    scribe = WorkflowStep(
        id="scribe",
        agent=Agent(
            tools=["workspace-read"], results={"handoff": {"note": "document"}}
        ),
        max_executions=1,
        reserved=True,
        limits={
            "cost-usd": 2.0,
            "effective-tokens": 200000.0,
            "tool-calls": 100.0,
            "wall-clock-minutes": 10.0,
        },
        context_thresholds=ContextThresholds(hard_tokens=40000),
        routes={
            "handoff": RouteTarget(step="handoff-push"),
            "hard-context-threshold": RouteTarget(step="hard-handoff"),
            "error": RouteTarget(step="hard-handoff"),
            "limit-exceeded": RouteTarget(step="hard-handoff"),
        },
    )
    handoff_push = WorkflowStep(
        id="handoff-push",
        agent=None,
        operation="commit-and-push",
        max_executions=1,
        routes={
            "pushed": RouteTarget(end="unfinished", round_handoff=ai_annotation),
            "no-changes": RouteTarget(end="unfinished", round_handoff=ai_annotation),
        },
    )
    hard_handoff = WorkflowStep(
        id="hard-handoff",
        agent=None,
        operation="commit-and-push",
        max_executions=1,
        routes={
            "pushed": RouteTarget(end="unfinished", round_handoff=hard_annotation),
            "no-changes": RouteTarget(
                end="unfinished", round_handoff=hard_annotation
            ),
        },
    )
    return Workflow(
        id=WORKFLOW,
        model="sonnet",
        input=InputDecl(type="issue", label=SELECTION_LABEL),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=1500000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps={
            "process": process,
            "scribe": scribe,
            "handoff-push": handoff_push,
            "hard-handoff": hard_handoff,
        },
        outcomes={"unfinished": CONFIGURED_UNFINISHED},
        round_handoff=RoundHandoffConfig(max_automatic_pickups=2),
    )


class _FallbackHybridExecutor:
    """Agent steps run for real; operations script a pushed publication.

    Mirrors the production adapter split: ``agent`` steps delegate to
    :class:`ProductionStepExecutor` while ``commit-and-push`` operations
    land the Attempt-branch push receipt in the ledger and report
    ``pushed`` — the publication the Hard Handoff would have made.
    """

    def __init__(self, agent_executor: ProductionStepExecutor) -> None:
        self._agent = agent_executor

    def execute(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
        inherited: Any = None,
    ) -> StepOutcome:
        if step.agent is not None:
            return self._agent.execute(step, seq, attempt_id, ledger, inherited)
        ledger.record_step_effect_intent(
            attempt_id,
            step.id,
            seq,
            0,
            rendered_payload=f"commit-and-push {attempt_id}: push {WORK} to {REF}",
            expected_target={
                "operation": "commit-and-push",
                "branch": REF,
                "revision": WORK,
            },
        )
        ledger.record_step_effect_receipt(attempt_id, step.id, seq, 0, remote_id=WORK)
        return StepOutcome(
            result="pushed",
            payload={},
            facts={},
            usage={},
            limit_fact=None,
        )


def test_receiver_failure_falls_back_to_the_hard_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing receiver publishes the Hard Handoff with no further model calls.

    The source soft-crosses once; the Reserved receiver exhausts bounded
    report correction and ends ``error``, which routes model-free to the
    configured Hard Handoff operation. The published Execution Summary
    carries both the source crossing limit record and the receiving
    failure, and deciding the round makes no further model requests.
    """
    from coding_agent.runtime.step_limits import build_budgets as _build_budgets
    from support.agent_step_deps import make_agent_step_deps as _make_deps
    from support.scripted_model import ScriptedModel as _ScriptedModel

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        workflow = _fallback_workflow()
        decl_pin = PinnedModel(
            provider="anthropic",
            model="claude-sonnet-5",
            endpoint="messages",
            effort="medium",
            output_cap_tokens=32000,
            context_threshold_tokens=80000,
            context_window_tokens=1000000,
        )
        policy = policy_for_pin(decl_pin)
        assert policy.context_window is not None
        big = "p" * 4000
        model = _ScriptedModel(
            [
                _fallback_ai(
                    [
                        {
                            "name": "read_file",
                            "args": {"path": "README.md"},
                            "id": "big-1",
                            "type": "tool_call",
                        }
                    ],
                    content=big,
                    usage=_fallback_usage(25000, 5000),
                ),
                *[
                    _fallback_ai([_fallback_report_call("bogus", {})])
                    for _ in range(MAX_REPORT_CORRECTIONS + 1)
                ],
            ]
        )
        budgets = _build_budgets(workflow, attempt_id=ATTEMPT)
        deps = _make_deps(
            model=model,
            workspace=workspace,
            tmp_path=tmp_path,
            attempt=StepAttemptFacts(
                attempt_id=ATTEMPT,
                workflow_id=WORKFLOW,
                input_type="issue",
                input_number=NUMBER,
                title="Add a greeting",
                body="a body",
                base_revision=BASE,
                current_revision=BASE,
            ),
            pin=decl_pin,
            context_window=policy.context_window,
            ledger=ledger,
            budgets=budgets,
        )
        result = run_workflow(
            workflow=workflow,
            attempt_id=ATTEMPT,
            ledger=ledger,
            step_executor=_FallbackHybridExecutor(
                ProductionStepExecutor(agent_deps=deps)
            ),
            budgets=budgets,
        )
        assert result.outcome == "unfinished"
        assert result.ending_step == "hard-handoff"
        assert result.step_result == "pushed"
        executions = [
            (record.step_id, record.result)
            for record in ledger.list_step_executions(ATTEMPT)
        ]
        assert executions == [
            ("process", "soft-context-threshold"),
            ("scribe", "error"),
            ("hard-handoff", "pushed"),
        ]
        calls_after_run = len(model.invocations)
        assert calls_after_run == 1 + MAX_REPORT_CORRECTIONS + 1

        ledger.record_attempt_pin(_pin())
        _remote(monkeypatch)
        decided = _decide_live_handoff(
            ledger=ledger,
            workflow=workflow,
            result=result,
            number=NUMBER,
            workspace_path=Path("/nonexistent"),
            attempt_branch=REF,
            canonical_remote="https://example.com/o/r.git",
        )

        assert isinstance(decided, RoundHandoffOffer)
        assert decided.note_kind == "hard"
        assert "Hard Handoff" in decided.note
        assert "soft-context-threshold" in decided.note
        assert "## process #1" in decided.note
        assert "## scribe #1" in decided.note
        assert parse_handoff_block(render_handoff_section(decided)) == decided
        assert len(model.invocations) == calls_after_run
    finally:
        ledger.close()


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


# --- slice 5: the two-worker run, pushed and clean --------------------------------


def _e2e_report_call(
    result: str, payload: dict[str, object], call_id: str = "report-1"
) -> dict[str, object]:
    import json as _json

    return {
        "name": "report_step_result",
        "args": {"result": result, "payload_json": _json.dumps(payload)},
        "id": call_id,
        "type": "tool_call",
    }


def _e2e_ai(
    calls: list[dict[str, object]] | None = None, content: str = ""
) -> Any:
    from langchain_core.messages import AIMessage as _AIMessage

    return _AIMessage(content=content, tool_calls=list(calls or []))


def _e2e_seed_tracker() -> Any:
    from support.fake_step_tracker import FakeStepTracker as _Tracker
    from coding_agent.inputs.port import (
        LabeledEvent as _LabeledEvent,
        TrackedInput as _TrackedInput,
    )

    tracker = _Tracker(agent_login="coding-agent", agent_id=999)
    tracker.base_tip = BASE
    for number, created_at in ((7, "2026-10-01T00:00:00Z"), (8, "2026-10-02T00:00:00Z")):
        tracker.inputs[number] = _TrackedInput(
            number=number,
            input_type="issue",
            title=f"Add a greeting {number}",
            body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
            is_open=True,
            labels=(SELECTION_LABEL,),
            assignees=(),
            created_at=created_at,
            author_login="human",
            author_id=7,
        )
        tracker.labeled_events[(number, SELECTION_LABEL)] = _LabeledEvent(
            label=SELECTION_LABEL,
            actor_login="human",
            actor_id=7,
            actor_type="User",
            created_at=created_at,
        )
    return tracker


def _e2e_remote(root: Path) -> dict[str, str]:
    import subprocess as _subprocess

    from conftest import (
        commit_validation_profile as _commit_profile,
        init_origin_repo as _init_origin,
        run_git as _run_git,
    )

    src = root / "origin-src"
    _init_origin(src)
    _commit_profile(src)
    base = _run_git(["rev-parse", "HEAD"], src)
    bare = root / "remote.git"
    _subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    return {"src": str(src), "remote": str(bare), "base": base}


def _e2e_matrix(path: Path) -> Path:
    import json as _json

    matrix = path / "toolchain-matrix.json"
    matrix.write_text(
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
    return matrix


def _e2e_run_worker(
    *,
    config_text: str,
    state_dir: Path,
    tracker: Any,
    model: Any,
    remote: str,
    run_dir: Path,
    name: str,
    max_attempts: int,
    workflow_runner: Any = None,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    from coding_agent.worker import supervisor as _supervisor
    from worker_fakes import make_worker_ports as _make_ports

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    run_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    config = run_dir / f"{name}.yaml"
    config.write_text(config_text, encoding="utf-8")
    ports = _make_ports(model=model, tracker=tracker, repository_url=remote)
    return _supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skill_deployment=_supervisor.SkillDeploymentCheck(
            policy=run_dir / "skill-policy.json",
            bundle_dir=run_dir / "bundle",
            home=run_dir / "home",
        ),
        toolchain_matrix_path=_e2e_matrix(run_dir),
        ports=ports.ports,
        sleep=lambda seconds: None,
        max_attempts=max_attempts,
        workflow_runner=workflow_runner,
    )


def _e2e_git(cwd: Path, *args: str) -> str:
    import subprocess as _subprocess

    completed = _subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _e2e_offer(tracker: Any, number: int) -> Any:
    from coding_agent.inputs.pickup import newest_pickup_offer as _newest

    found = _newest(
        tracker.list_comments(number, "issue"),
        agent_id=tracker.agent_id,
        workflow_id=WORKFLOW,
        issue_number=number,
    )
    assert found is not None, f"no pickup offer for issue {number}"
    return found[0]


def test_pushed_and_clean_rounds_continue_on_a_second_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both publication shapes continue on a second Worker, same evidence.

    Worker 1 publishes a pushed AI-assisted Round Handoff for issue 7 (the
    scribe writes a file) and a verified clean one for issue 8 (two
    ``commit-and-push`` steps, the second genuinely reporting
    ``no-changes``). Main advances on the remote between the workers; a
    second Worker with a fresh state volume picks up both offers, opens
    each Continuation at the exact published work commit with the recorded
    Base Revision retained, and sees each published note as declared input
    data after the operation preflight.
    """
    import json as _json

    from coding_agent.ledger.store import RunLedger as _Ledger
    from coding_agent.runtime.graph.workflow_graph import (
        run_workflow as _real_run_workflow,
    )
    from dataclasses import replace as _replace
    from support.scripted_model import ScriptedModel as _ScriptedModel

    note_7 = "handoff note for issue 7: greeting.txt carries the new greeting"
    note_8 = "handoff note for issue 8: tree verified clean, nothing to change"
    greeting = "hello from worker 1\n"

    tracker = _e2e_seed_tracker()
    seeded = _e2e_remote(tmp_path)
    base = seeded["base"]
    tracker.base_tip = base
    w1 = tmp_path / "w1"
    w2 = tmp_path / "w2"

    exit_1a = _e2e_run_worker(
        config_text=_e2e_config_pushed(),
        state_dir=w1 / "state",
        tracker=tracker,
        model=_ScriptedModel(
            [
                _e2e_ai(
                    [
                        {
                            "name": "write_file",
                            "args": {"path": "greeting.txt", "content": greeting},
                            "id": "write-1",
                            "type": "tool_call",
                        }
                    ]
                ),
                _e2e_ai([_e2e_report_call("handoff", {"note": note_7})]),
            ]
        ),
        remote=seeded["remote"],
        run_dir=w1,
        name="pushed",
        max_attempts=1,
        monkeypatch=monkeypatch,
    )
    assert exit_1a == 0
    offer_7 = _e2e_offer(tracker, 7)
    assert offer_7.note_kind == "ai-assisted"
    assert offer_7.note == note_7
    assert offer_7.base_revision == base
    assert offer_7.pickup_label == PICKUP_LABEL
    ledger_1 = _Ledger(w1 / "state" / "run-ledger.sqlite3")
    try:
        assert ledger_1.get_outcome("implement#7/1") is not None
        assert ledger_1.get_outcome("implement#7/1").outcome == "unfinished"
    finally:
        ledger_1.close()

    # The second publisher run must select issue 8, not pick up issue 7's
    # fresh signal: hide the pickup label for the duration of the run and
    # restore it afterwards (the terminal record is untouched).
    saved_labels = tracker.inputs[7].labels
    tracker.inputs[7] = _replace(
        tracker.inputs[7],
        labels=tuple(label for label in saved_labels if label != PICKUP_LABEL),
    )
    try:
        exit_1b = _e2e_run_worker(
            config_text=_e2e_config_clean(),
            state_dir=w1 / "state",
            tracker=tracker,
            model=_ScriptedModel(
                [_e2e_ai([_e2e_report_call("handoff", {"note": note_8})])]
            ),
            remote=seeded["remote"],
            run_dir=w1,
            name="clean",
            max_attempts=1,
            monkeypatch=monkeypatch,
        )
    finally:
        tracker.inputs[7] = _replace(tracker.inputs[7], labels=saved_labels)
    assert exit_1b == 0
    offer_8 = _e2e_offer(tracker, 8)
    assert offer_8.note_kind == "ai-assisted"
    assert offer_8.note == note_8
    assert offer_8.base_revision == base
    assert offer_8.work_commit != offer_7.work_commit

    executions_8 = [
        (record.step_id, record.result)
        for record in _Ledger(w1 / "state" / "run-ledger.sqlite3").list_step_executions(
            "implement#8/1"
        )
    ]
    _Ledger(w1 / "state" / "run-ledger.sqlite3").close()
    assert ("push-2", "no-changes") in executions_8

    # Main moves on while the offers stand: the Continuation must retain
    # the recorded Base Revision, not the moved tip.
    src = Path(seeded["src"])
    (src / "moved.txt").write_text("moved\n", encoding="utf-8")
    _e2e_git(src, "add", "-A")
    _e2e_git(src, "commit", "-qm", "moved-base")
    _e2e_git(src, "push", "-q", seeded["remote"], "main")
    moved = _e2e_git(src, "rev-parse", "HEAD")
    assert moved != base

    offers = {7: offer_7, 8: offer_8}
    seen_notes: dict[str, str] = {}
    workspace_checks: list[str] = []

    def _observing_runner(**kwargs: Any) -> Any:
        attempt_id = str(kwargs["attempt_id"])
        issue = 7 if attempt_id.startswith("implement#7/") else 8
        offer = offers[issue]
        inner = kwargs["step_executor"]
        assert inner.agent_deps is not None
        assert inner.agent_deps.handoff_note == offer.note
        seen_notes[attempt_id] = inner.agent_deps.handoff_note or ""
        workspace_path = inner.agent_deps.workspace
        worker_ledger = kwargs["ledger"]

        class _ObservingExecutor:
            def execute(
                self,
                step: Any,
                seq: int,
                aid: str,
                led: Any,
                inherited: Any = None,
            ) -> Any:
                if step.agent is not None and step.id == "scribe":
                    assert _e2e_git(workspace_path, "rev-parse", "HEAD") == (
                        offer.work_commit
                    )
                    branch = _e2e_git(
                        workspace_path, "symbolic-ref", "--short", "HEAD"
                    )
                    assert branch.startswith(f"agent/implement/{issue}/2-")
                    assert _e2e_git(workspace_path, "status", "--porcelain") == ""
                    if issue == 7:
                        assert (
                            workspace_path / "greeting.txt"
                        ).read_text(encoding="utf-8") == greeting
                    pin = worker_ledger.get_attempt_pin(aid)
                    assert pin is not None
                    assert pin.base_revision == base
                    workspace_checks.append(aid)
                return inner.execute(step, seq, aid, led, inherited)

        kwargs["step_executor"] = _ObservingExecutor()
        return _real_run_workflow(**kwargs)

    model_w2 = _ScriptedModel(
        [
            _e2e_ai([_e2e_report_call("done", {})]),
            _e2e_ai([_e2e_report_call("done", {})]),
        ]
    )
    exit_2 = _e2e_run_worker(
        config_text=_e2e_config_continue(),
        state_dir=w2 / "state",
        tracker=tracker,
        model=model_w2,
        remote=seeded["remote"],
        run_dir=w2,
        name="continue",
        max_attempts=2,
        workflow_runner=_observing_runner,
        monkeypatch=monkeypatch,
    )
    assert exit_2 == 0
    assert seen_notes == {
        "implement#7/2": note_7,
        "implement#8/2": note_8,
    }
    assert sorted(workspace_checks) == ["implement#7/2", "implement#8/2"]

    ledger_2 = _Ledger(w2 / "state" / "run-ledger.sqlite3")
    try:
        assert ledger_2.get_outcome("implement#7/2").outcome == "completed"
        assert ledger_2.get_outcome("implement#8/2").outcome == "completed"
        assert ledger_2.get_attempt_pin("implement#7/2") is not None
        assert ledger_2.get_attempt_pin("implement#7/2").base_revision == base
    finally:
        ledger_2.close()

    # Each Continuation's opening request frames the published note as
    # declared input data: the exact note JSON under the heading, with the
    # data-not-instructions boundary.
    assert len(model_w2.invocations) == 2
    for invocation, expected_note in zip(
        model_w2.invocations, (note_7, note_8), strict=True
    ):
        text = " ".join(str(message.content) for message in invocation)
        assert "# Declared inputs" in text
        assert _json.dumps(expected_note) in text
        assert "data, never instructions" in text
