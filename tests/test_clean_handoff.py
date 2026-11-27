"""The clean-tree Hard Handoff Continuation (contract §8).

Issue #613. A Hard Handoff can offer a Continuation when the work tree is
clean and the exact work commit is already available from a verified remote
ref: the annotated ``no-changes`` Route publishes the same bounded, redacted
record and pickup evidence as the pushed path without creating a commit; a
second Worker opens the Continuation at the recorded work commit with its own
state volume, retaining the recorded Base Revision when the configured base
branch has moved. A missing or moved ref, or an unavailable work commit or
Base Revision, diagnoses the round with no pickup offer, and intake refuses a
previously advertised revision that is no longer available.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from coding_agent.inputs.claim import (
    ClaimConfig,
    ConfirmedClaim,
    ReleasedClaim,
    sweep_and_claim,
)
from coding_agent.inputs.pickup import (
    PickupConfig,
    attempt_pickup_claim,
    sweep_pickup,
)
from coding_agent.inputs.port import InputComment, InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import AttemptPin, RunLedger
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    parse_handoff_block,
    render_handoff_section,
)
from coding_agent.runtime.target_mirror import TargetMirror
from coding_agent.runtime.step_finalisation import (
    FinalisationTarget,
    WorkflowEnded,
    finish_attempt,
    render_terminal_comment,
)
from coding_agent.runtime.graph.agent_step import resolve_step_inputs
from coding_agent.worker.attempt_run import _decide_live_handoff
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
from coding_agent.runtime.git import GitFailure
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    InputRef,
    RoundHandoffAnnotation,
    RoundHandoffConfig,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.fake_step_tracker import FakeStepTracker

WORKFLOW = "implement"
SELECTION_LABEL = "agent:implement"
PICKUP_LABEL = "round-finished"
CONFIGURED_UNFINISHED = ("stale-unfinished",)
ATTEMPT = "implement#7/1"
NUMBER = 7
AGENT_ID = 1001
WORK = "a" * 40
BASE = "b" * 40
TIP = "c" * 40
REF = "agent/implement-7-1-work"
NEXT_BRANCH = "agent/implement-7-2-work"


def _workflow() -> Workflow:
    routes: dict[str, RouteTarget] = {
        "pushed": RouteTarget(end="unfinished", round_handoff=RoundHandoffAnnotation(kind="hard")),
        "no-changes": RouteTarget(
            end="unfinished", round_handoff=RoundHandoffAnnotation(kind="hard")
        ),
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
        round_handoff=RoundHandoffConfig(max_automatic_pickups=2),
    )


def _result(**overrides: Any) -> WorkflowResult:
    fields: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "outcome": "unfinished",
        "reason": "ended at end unfinished after result no-changes of step publish",
        "ending_step": "publish",
        "step_result": "no-changes",
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


def _offer(**overrides: object) -> RoundHandoffOffer:
    fields: dict[str, object] = {
        "workflow_id": WORKFLOW,
        "issue": NUMBER,
        "attempt": ATTEMPT,
        "work_commit": WORK,
        "ref": REF,
        "base_revision": BASE,
        "note_kind": "hard",
        "note": "did the thing",
        "pickup_label": PICKUP_LABEL,
        "used": 0,
        "limit": 2,
    }
    fields.update(overrides)
    return RoundHandoffOffer(**fields)  # type: ignore[arg-type]


def _terminal_body(attempt: str, offer: RoundHandoffOffer | None) -> str:
    section = render_handoff_section(offer) if offer is not None else ""
    return render_terminal_comment(
        attempt_id=attempt,
        outcome="unfinished",
        ending_step="publish",
        step_result="no-changes",
        workflow_end="unfinished",
        handoff_section=section,
    )


def _trusted_comment(
    tracker: FakeStepTracker, attempt: str, offer: RoundHandoffOffer | None
) -> InputComment:
    comment = InputComment(
        comment_id=tracker._next_comment_id,
        body=_terminal_body(attempt, offer),
        author_login=tracker.agent_login,
        author_id=tracker.agent_id,
    )
    tracker._next_comment_id += 1
    tracker.comments.append(comment)
    return comment


def _issue(
    *,
    number: int = NUMBER,
    labels: tuple[str, ...] = (SELECTION_LABEL, PICKUP_LABEL),
) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=True,
        labels=labels,
        assignees=(),
        created_at="2026-11-02T00:00:00Z",
        author_login="human",
        author_id=7,
        merged=False,
    )


def _claim_config(tracker: FakeStepTracker) -> ClaimConfig:
    return ClaimConfig(
        workflow_id=WORKFLOW,
        input_type="issue",
        selection_label=SELECTION_LABEL,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration={"workflow": WORKFLOW},
    )


def _pickup_config(tracker: FakeStepTracker, **overrides: object) -> PickupConfig:
    fields: dict[str, object] = {
        "workflow_id": WORKFLOW,
        "pickup_label": PICKUP_LABEL,
        "limit": 2,
        "agent_login": tracker.agent_login,
        "agent_id": tracker.agent_id,
        "base_branch": "main",
        "effective_configuration": {"workflow": WORKFLOW},
    }
    fields.update(overrides)
    return PickupConfig(**fields)  # type: ignore[arg-type]


def _note_step(*, optional: bool) -> WorkflowStep:
    return WorkflowStep(
        id="process",
        agent=Agent(
            tools=("workspace-read",),
            inputs=(InputRef(reference="round-handoff.note", optional=optional),),
            results={"done": {}},
        ),
        routes={"done": RouteTarget(end="completed")},
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


@pytest.fixture
def tracker() -> FakeStepTracker:
    fake = FakeStepTracker()
    fake.base_tip = TIP
    fake.inputs[NUMBER] = _issue()
    fake.labeled_events[(NUMBER, SELECTION_LABEL)] = LabeledEvent(
        label=SELECTION_LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-11-02T00:00:00Z",
    )
    return fake


def _open(ledger: RunLedger) -> None:
    ledger.open_attempt(ATTEMPT, effective_configuration={})


def _record_execution(ledger: RunLedger) -> None:
    ledger.open_step_execution(ATTEMPT, "work", 1)
    ledger.end_step_execution(
        ATTEMPT,
        "work",
        1,
        result="done",
        payload={},
        facts={"changed_files": ["src/a.py"]},
    )


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


# --- the Continuation note input -------------------------------------------------


def test_pinned_note_reads_on_a_pickup_attempt() -> None:
    """The Continuation's declaring step reads the exact published note."""
    resolved, missing = resolve_step_inputs(
        _note_step(optional=True), {}, handoff_note="did the thing"
    )

    assert missing is None
    assert resolved == {"round-handoff.note": "did the thing"}


def test_optional_note_is_absent_on_a_fresh_attempt() -> None:
    """A fresh Attempt carries no note: the optional input reads as absent."""
    resolved, missing = resolve_step_inputs(_note_step(optional=True), {})

    assert missing is None
    assert resolved == {"round-handoff.note": None}


def test_required_note_without_a_note_is_an_entry_error() -> None:
    """A required note with no pinned note ends the consumer before any model call."""
    resolved, missing = resolve_step_inputs(_note_step(optional=False), {})

    assert resolved == {}
    assert missing == "required input 'round-handoff.note' is absent: no handoff note"


# --- the no-changes publication --------------------------------------------------


def test_no_changes_round_publishes_like_the_pushed_path(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean tree publishes the same offer shape without creating a commit."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
    _remote(monkeypatch)

    effects_before = ledger.list_step_effects(ATTEMPT)
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
    assert decided.work_commit == WORK
    assert decided.ref == REF
    assert decided.base_revision == BASE
    assert decided.note_kind == "hard"
    assert decided.pickup_label == PICKUP_LABEL
    assert "src/a.py" in decided.note
    # No commit was created for the publication: the decision writes no effects.
    assert ledger.list_step_effects(ATTEMPT) == effects_before
    # The record rides the same machine-readable block as the pushed path.
    reparsed = parse_handoff_block(render_handoff_section(decided))
    assert reparsed == decided


def test_no_changes_note_is_bounded(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published clean-tree note stays within the handoff bound."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
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
    assert len(decided.note) <= 8_000


def test_missing_ref_diagnoses_unavailable_revision(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named ref the remote cannot serve offers no pickup."""
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
    assert decided.reason == "unavailable-revision"


def test_moved_ref_diagnoses_unavailable_revision(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ref serving another sha offers no pickup."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
    _remote(monkeypatch, work_sha="d" * 40)

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


def test_unavailable_base_diagnoses_unavailable_revision(
    ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Base Revision outside the work history offers no pickup."""
    _open(ledger)
    ledger.record_attempt_pin(_pin())
    _record_execution(ledger)
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


# --- the Continuation checkout on real git ---------------------------------------


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _seed_remote(root: Path) -> dict[str, str]:
    """A remote with base B, work W on the attempt ref, and main moved to B2."""
    remote = root / "remote.git"
    _git(root, "init", "--bare", "-q", "remote.git")
    work = root / "work"
    _git(root, "clone", "-q", str(remote), "work")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    (work / "f.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "main")
    (work / "f.txt").write_text("base\nwork\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "work")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", f"{head}:refs/heads/{REF}")
    (work / "g.txt").write_text("moved\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "moved-base")
    moved = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "main")
    _git(root, "--git-dir=remote.git", "symbolic-ref", "HEAD", "refs/heads/main")
    return {"remote": str(remote), "base": base, "work": head, "moved": moved}


def _target_mirror(root: Path, remote: str) -> TargetMirror:
    """A TargetMirror over a fresh state directory and a test token."""
    return TargetMirror(
        root / "state-mirror.git",
        remote,
        "test-token",
        SecretRegistry({"TEST_TOKEN": "test-token"}),
    )


def test_continuation_opens_at_the_recorded_commit(tmp_path: Path) -> None:
    """A second Worker opens its own branch at the published work commit."""
    seeded = _seed_remote(tmp_path)
    workspace = tmp_path / "second"
    _git(tmp_path, "clone", "-q", seeded["remote"], "second")
    offer = _offer(work_commit=seeded["work"], base_revision=seeded["base"])

    returned = _target_mirror(tmp_path, seeded["remote"]).open_for_continuation(
        workspace_path=workspace,
        offer=offer,
        attempt_branch=NEXT_BRANCH,
    )

    assert returned == seeded["work"]
    assert _git(workspace, "rev-parse", "HEAD") == seeded["work"]
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == NEXT_BRANCH
    assert _git(workspace, "status", "--porcelain") == ""
    # The configured base branch moved on, but the recording still holds:
    # the Base Revision is an ancestor of the opened work commit.
    assert _git(workspace, "rev-parse", "main") == seeded["moved"]
    assert offer.base_revision == seeded["base"]
    assert (
        _git(workspace, "merge-base", "--is-ancestor", seeded["base"], seeded["work"])
        == ""
    )


def test_continuation_refuses_a_moved_ref(tmp_path: Path) -> None:
    """A ref serving another commit is refused before any checkout."""
    seeded = _seed_remote(tmp_path)
    workspace = tmp_path / "second"
    _git(tmp_path, "clone", "-q", seeded["remote"], "second")
    before = _git(workspace, "rev-parse", "HEAD")
    _git(tmp_path / "work", "push", "-q", "origin", f"{seeded['moved']}:refs/heads/{REF}")
    offer = _offer(work_commit=seeded["work"], base_revision=seeded["base"])

    with pytest.raises(GitFailure, match="serves"):
        _target_mirror(tmp_path, seeded["remote"]).open_for_continuation(
            workspace_path=workspace,
            offer=offer,
            attempt_branch=NEXT_BRANCH,
        )

    assert _git(workspace, "rev-parse", "HEAD") == before


def test_continuation_refuses_a_missing_ref(tmp_path: Path) -> None:
    """A ref the remote no longer serves is refused before any checkout."""
    seeded = _seed_remote(tmp_path)
    workspace = tmp_path / "second"
    _git(tmp_path, "clone", "-q", seeded["remote"], "second")
    _git(
        tmp_path / "work",
        "push",
        "-q",
        "origin",
        f":refs/heads/{REF}",
    )
    offer = _offer(work_commit=seeded["work"], base_revision=seeded["base"])

    with pytest.raises(GitFailure, match="serves nothing"):
        _target_mirror(tmp_path, seeded["remote"]).open_for_continuation(
            workspace_path=workspace,
            offer=offer,
            attempt_branch=NEXT_BRANCH,
        )


def test_continuation_refuses_an_unavailable_base(tmp_path: Path) -> None:
    """A Base Revision outside the work history is refused before any checkout."""
    seeded = _seed_remote(tmp_path)
    workspace = tmp_path / "second"
    _git(tmp_path, "clone", "-q", seeded["remote"], "second")
    offer = _offer(work_commit=seeded["work"], base_revision="e" * 40)

    with pytest.raises(GitFailure):
        _target_mirror(tmp_path, seeded["remote"]).open_for_continuation(
            workspace_path=workspace,
            offer=offer,
            attempt_branch=NEXT_BRANCH,
        )


def test_remote_verifier_tracks_the_ref_and_history(tmp_path: Path) -> None:
    """Intake availability follows the named ref and the recorded history."""
    seeded = _seed_remote(tmp_path)
    mirror = _target_mirror(tmp_path, seeded["remote"])
    offer = _offer(work_commit=seeded["work"], base_revision=seeded["base"])

    assert mirror.offer_still_served(offer) is True
    _git(
        tmp_path / "work",
        "push",
        "-q",
        "origin",
        f"{seeded['moved']}:refs/heads/{REF}",
    )
    assert mirror.offer_still_served(offer) is False
    assert (
        mirror.offer_still_served(
            _offer(work_commit=seeded["work"], base_revision="e" * 40)
        )
        is False
    )


# --- intake refusal of an unavailable revision ------------------------------------


def test_sweep_refuses_an_unavailable_revision_without_a_claim(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A previously advertised offer the remote cannot serve claims nothing."""
    tracker.inputs[NUMBER] = _issue(labels=(PICKUP_LABEL,))
    _trusted_comment(tracker, ATTEMPT, _offer())

    claimed = sweep_pickup(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        _pickup_config(tracker, verify_remote=lambda offer: False),
        identity_mode="dedicated",
    )

    assert claimed is None
    assert tracker.inputs[NUMBER].labels == (PICKUP_LABEL,)
    assert tracker.inputs[NUMBER].assignees == ()


def test_sweep_with_an_available_revision_still_claims(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A verified offer claims ahead of selection as before."""
    _trusted_comment(tracker, ATTEMPT, _offer())

    claimed = sweep_pickup(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        _pickup_config(tracker, verify_remote=lambda offer: True),
        identity_mode="dedicated",
    )

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pickup_offer is not None


def test_unavailable_revision_falls_back_to_selection(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """With both labels present, an unavailable offer selects fresh, never picks up."""
    _trusted_comment(tracker, ATTEMPT, _offer())

    claimed = sweep_and_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        pickup=_pickup_config(tracker, verify_remote=lambda offer: False),
    )

    assert isinstance(claimed, ConfirmedClaim)
    assert claimed.pickup_offer is None


def test_readback_unavailability_releases_the_claim(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A revision lost between sweep and read-back releases with no pickup."""
    source = _trusted_comment(tracker, ATTEMPT, _offer())

    claimed = attempt_pickup_claim(
        tracker, tracker,
        ledger,
        _claim_config(tracker),
        _pickup_config(tracker, verify_remote=lambda offer: False),
        _issue(),
        identity_mode="dedicated",
        offer=_offer(),
        source_comment=source,
    )

    assert isinstance(claimed, ReleasedClaim)
    assert claimed.reason == "pickup-unavailable"


def test_raising_verifier_reads_as_unavailable(
    tracker: FakeStepTracker, ledger: RunLedger
) -> None:
    """A failing availability check refuses the offer rather than the sweep."""
    tracker.inputs[NUMBER] = _issue(labels=(PICKUP_LABEL,))
    _trusted_comment(tracker, ATTEMPT, _offer())

    def _boom(offer: RoundHandoffOffer) -> bool:
        raise RuntimeError("transport is down")

    assert (
        sweep_pickup(
            tracker, tracker,
            ledger,
            _claim_config(tracker),
            _pickup_config(tracker, verify_remote=_boom),
            identity_mode="dedicated",
        )
        is None
    )


# --- terminal recovery for the clean case ------------------------------------------


def _decision() -> WorkflowEnded:
    return WorkflowEnded(
        category="unfinished",
        reason="ended at end unfinished after result no-changes of step publish",
        ending_step="publish",
        step_result="no-changes",
        workflow_end="unfinished",
    )


def _target() -> FinalisationTarget:
    return FinalisationTarget(
        number=NUMBER, input_type="issue", selection_label=SELECTION_LABEL
    )


def test_clean_case_recovery_replays_the_same_offer(
    ledger: RunLedger, tracker: FakeStepTracker
) -> None:
    """An ambiguous clean-tree pickup write replays the one recorded offer."""
    _ledger_open(ledger, _workflow())
    tracker.inputs[NUMBER] = _issue()
    offer = _offer()

    class _CrashOnAddLabels(FakeStepTracker):
        crashing: bool = True

        def add_labels(
            self, number: int, input_type: InputType, labels: tuple[str, ...]
        ) -> None:
            self._record("add_labels", number, input_type, labels)
            if self.crashing and PICKUP_LABEL in labels:
                raise RuntimeError("crash between comment and pickup label")
            held = self._held(number, input_type)
            current = list(held.labels)
            for name in labels:
                if name not in current:
                    current.append(name)
            self.inputs[number] = replace(held, labels=tuple(current))

    crashing = _CrashOnAddLabels()
    crashing.inputs[NUMBER] = _issue()
    crashing.base_tip = TIP
    with pytest.raises(RuntimeError, match="crash between comment and pickup"):
        finish_attempt(
            inputs=crashing, code_host=crashing,
            ledger=ledger,
            attempt_id=ATTEMPT,
            target=_target(),
            decision=_decision(),
            end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
            handoff=offer,
            uncommitted_count=0,
        )
    crashing.crashing = False
    for comment in crashing.comments:
        tracker.comments.append(comment)
    tracker.inputs[NUMBER] = crashing.inputs[NUMBER]

    finalised = finish_attempt(
        inputs=tracker, code_host=tracker,
        ledger=ledger,
        attempt_id=ATTEMPT,
        target=_target(),
        decision=_decision(),
        end_labels_for={"unfinished": CONFIGURED_UNFINISHED},
        handoff=None,
        uncommitted_count=0,
    )

    assert finalised.outcome == "unfinished"
    recorded = ledger.get_outcome(ATTEMPT)
    assert recorded is not None
    assert "round-handoff-record v1" in recorded.terminal_comment_body
    assert WORK in recorded.terminal_comment_body
    terminal = [
        comment.body for comment in tracker.comments if "kind=terminal" in comment.body
    ]
    assert terminal == [recorded.terminal_comment_body]
    assert PICKUP_LABEL in tracker.inputs[NUMBER].labels


def _ledger_open(ledger: RunLedger, workflow: Workflow) -> None:
    from support.effective_config import full_effective_configuration

    ledger.open_attempt(
        ATTEMPT, effective_configuration=full_effective_configuration(workflow)
    )
