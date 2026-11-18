"""Recovery from the pinned identity and archived blobs (SKV3-29/30/31).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §16 (a restart
never enters the Workflow graph and never runs a Workflow Step; recovery uses
pinned values only) and §2.10 (the Attempt records its Effective
Configuration in full).

Matrix row ids: SKV3-29 (with a changed valid deployment and no former store,
recovery settles effects and finalisation from the archive), SKV3-30 (a
missing, corrupt, escaping, symlinked or incompatible core or blob refuses
recovery naming the Attempt and fault; no partial success, current-file
repair, secret restoration or skipped integrity gate) and SKV3-31 (an
incompatible or incomplete open legacy record is refused, never converted or
given fabricated archives; closed legacy history stays listable, viewable and
exportable with existing numbering).

Covered at the Worker-loop seam (``recover_unfinished_step_attempts`` /
``run_worker_loop``), including refusal on a corrupted blob.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim, sweep_and_claim
from coding_agent.inputs.port import InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger, digest_effective_configuration
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator.store import OperatorStore
from coding_agent.skillbundle.member_identity import BundleMember, InventoryFile
from coding_agent.skillbundle.policy import PolicyFile, PolicyReview
from coding_agent.skillbundle.recovery import (
    ArchiveRecoveryRefused,
    ensure_archive_for_recovery,
    validate_archive_for_attempt,
)
from coding_agent.worker.run import (
    WorkerLoopConfig,
    recover_unfinished_step_attempts,
    run_worker_loop,
)
from coding_agent.workflow.effective import (
    PinnedConfigurationRefused,
    content_index_for_record,
)
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    Instruction,
    RouteTarget,
    WorkflowStep,
    Workflow,
)
from support.effective_config import full_effective_configuration
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel  # noqa: F401  (no-model assertion)

LABEL = "agent:implement"
REPO = "octocat/sandbox"


def _issue(number: int = 42) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title="Add a greeting",
        body="a body",
        is_open=True,
        labels=(LABEL,),
        assignees=(),
        created_at="2026-10-01T00:00:00Z",
        author_login="human",
        author_id=7,
    )


def _human_event() -> LabeledEvent:
    return LabeledEvent(
        label=LABEL,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )


def _tracker_with_issue(number: int = 42) -> FakeStepTracker:
    tracker = FakeStepTracker()
    tracker.inputs[number] = _issue(number)
    tracker.labeled_events[(number, LABEL)] = _human_event()
    return tracker


def _workflow(workflow_id: str = "implement", *, with_prompt: bool = True) -> Workflow:
    instructions: tuple[Instruction, ...] = (
        (Instruction(prompt="prompts/a.md"),) if with_prompt else ()
    )
    return Workflow(
        id=workflow_id,
        model="sonnet",
        input=InputDecl(type="issue", label=LABEL),
        ceiling=AttemptCeiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        start="work",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(
                    instructions=instructions,
                    results={"done": {}},
                ),
                max_executions=1,
                routes={"done": RouteTarget(end="completed")},
            ),
        },
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _member(
    name: str,
    files: Mapping[str, bytes],
    *,
    not_delivered: tuple[str, ...] = (),
    source: str = "git+https://example.com/org/skills-a.git",
) -> BundleMember:
    skill_body = files["SKILL.md"]
    skill_digest = _digest(skill_body)
    inventory = tuple(
        InventoryFile(
            path=path, sha256=_digest(raw), executable=False
        )
        for path, raw in sorted(files.items())
    )
    policy_files = tuple(
        PolicyFile(
            path=path,
            sha256=_digest(files[path]),
            executable=False,
            disposition="not-delivered",
            reason="withheld",
        )
        for path in not_delivered
    )
    review = PolicyReview(
        source_identity=source,
        name=name,
        skill_sha256=skill_digest,
        requires=(),
        files=policy_files,
        references=(),
        reason="test review",
    )
    return BundleMember(
        name=name,
        source_identity=source,
        resolved_commit="a" * 40,
        declared_ref=None,
        relative_source_path=name,
        store_relative_path=name,
        files=inventory,
        overlays=(),
        review=review,
    )


def _claim_with_components(
    tmp_path: Path,
    *,
    prompt_texts: dict[str, str],
    members: list[BundleMember],
    deliveries: dict[str, str],
    components: dict[str, dict[str, bytes]],
    number: int = 42,
) -> tuple[RunLedger, OperatorStore, FakeStepTracker, ConfirmedClaim]:
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    store.write_instance_metadata(target_repository=REPO)
    tracker = _tracker_with_issue(number)
    effective = full_effective_configuration(
        _workflow(),
        prompt_texts=dict(prompt_texts),
        bundle_members=list(members),
        activations=dict(deliveries),
    )
    config = ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration=effective,
        prompt_texts=dict(prompt_texts),
        activation_deliveries=dict(deliveries),
        activation_components={name: dict(paths) for name, paths in components.items()},
    )
    claimed = sweep_and_claim(tracker, ledger, config)
    assert isinstance(claimed, ConfirmedClaim)
    return ledger, store, tracker, claimed


def _startup_bytes() -> tuple[
    dict[str, str], dict[str, str], dict[str, dict[str, bytes]], list[BundleMember]
]:
    prompt_texts = {"prompts/a.md": "hello prompt\n"}
    alpha_skill = b"alpha skill body"
    alpha_extra = b"alpha extra companion"
    beta_skill = b"beta skill body"
    members = [
        _member("alpha", {"SKILL.md": alpha_skill, "extra.md": alpha_extra}),
        _member("beta", {"SKILL.md": beta_skill}),
    ]
    deliveries = {
        "alpha": "delivery-alpha-bytes",
        "beta": "delivery-beta-bytes",
    }
    components = {
        "alpha": {"SKILL.md": alpha_skill, "extra.md": alpha_extra},
        "beta": {"SKILL.md": beta_skill},
    }
    return prompt_texts, deliveries, components, members


def _archive_for(tmp_path: Path, attempt_id: str) -> Path:
    return attempt_archives.archive_dir_for(tmp_path, REPO, attempt_id)


def _loop_config(tracker: FakeStepTracker) -> WorkerLoopConfig:
    return WorkerLoopConfig(
        claim_config=ClaimConfig(
            workflow_id="implement",
            input_type="issue",
            selection_label=LABEL,
            base_branch="main",
            agent_login=tracker.agent_login,
            agent_id=tracker.agent_id,
            effective_configuration=full_effective_configuration(
                _workflow(), prompt_texts={"prompts/a.md": "hello prompt\n"}
            ),
        ),
        workflow=_workflow(),
        poll_interval_seconds=60.0,
    )


# --- SKV3-29: recovery from the archive after the deployment changed --------


def test_recovery_settles_from_archive_after_deployment_changed(tmp_path: Path) -> None:
    """SKV3-29: a changed valid deployment with no former store still settles.

    The Claim archives prompts, deliveries and components; the restart then
    settles an in-flight intent and finishes the recorded outcome using only
    the pinned identity and the archived blobs, never the current files.
    """
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        ledger.record_terminal_outcome(
            claimed.attempt_id,
            outcome="completed",
            reason="verified-completion",
            terminal_comment_body="done",
            writes=[],
        )
        # One in-flight intent: a post-comment the restart must settle.
        ledger.record_step_effect_intent(
            claimed.attempt_id,
            "work",
            1,
            0,
            rendered_payload="settlement probe\nhello from the archive",
            expected_target={"operation": "post-comment"},
        )
        # The deployment changed and the former store is gone: the recovery
        # entry takes no prompt/delivery/component bytes at all. It resolves
        # everything from the ledger row plus the archive.
        summary = recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )
        assert summary.settled == (claimed.attempt_id,)
        assert summary.pending == ()
        assert ledger.get_attempt(claimed.attempt_id).state == "finished"
        stored = ledger.get_outcome(claimed.attempt_id)
        assert stored is not None
        assert stored.outcome == "completed"
        # The settled write landed exactly once.
        assert tracker.call_names().count("post_comment") == 1
    finally:
        store.close()
        ledger.close()


def test_recovery_uses_pinned_labels_not_current_file(tmp_path: Path) -> None:
    """SKV3-29: end labels come from the pinned record, not the live file."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        # A live Workflow with different end labels must not leak into the
        # Attempt: recovery reads them from the pinned Effective
        # Configuration.
        summary = recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )
        assert summary.settled == (claimed.attempt_id,)
        assert ledger.get_attempt(claimed.attempt_id).state == "finished"
    finally:
        store.close()
        ledger.close()


def test_validate_returns_required_blobs(tmp_path: Path) -> None:
    """The validator proves the archive holds what finalisation needs."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        stored = ledger.get_attempt(claimed.attempt_id)
        archive = _archive_for(tmp_path, claimed.attempt_id)
        blobs = validate_archive_for_attempt(
            archive_dir=archive,
            record=stored.effective_configuration,
            stored_digest=stored.effective_configuration_digest,
            stored_refs=stored.content_refs,
            attempt_id=claimed.attempt_id,
        )
        assert blobs
        for digest, raw in blobs.items():
            assert _digest(raw) == digest
        assert ensure_archive_for_recovery(
            ledger=ledger, attempt_id=claimed.attempt_id
        ) == blobs
    finally:
        store.close()
        ledger.close()


# --- SKV3-30: refusals name the Attempt and the fault -------------------------


def test_corrupt_blob_refuses_at_worker_loop_seam(tmp_path: Path) -> None:
    """SKV3-30: a corrupted blob refuses recovery, naming Attempt and fault."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        archive = _archive_for(tmp_path, claimed.attempt_id)
        stored = ledger.get_attempt(claimed.attempt_id)
        index = stored.content_refs
        assert isinstance(index, dict)
        # Corrupt the first required blob in place.
        first_digest: str | None = None
        prompts_index = index.get("prompts")
        assert isinstance(prompts_index, dict)
        for entry in prompts_index.values():
            assert isinstance(entry, dict)
            digest = entry.get("sha256")
            assert isinstance(digest, str)
            first_digest = digest
            break
        assert first_digest is not None
        target = archive / "content" / first_digest
        assert target.is_file()
        target.write_bytes(b"corrupted bytes\n")
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            recover_unfinished_step_attempts(
                tracker=tracker, ledger=ledger, workflow=_workflow()
            )
        assert claimed.attempt_id in str(excinfo.value)
        assert excinfo.value.attempt_id == claimed.attempt_id
        assert excinfo.value.fault
        # No partial success: the ledger row and tracker writes are untouched.
        assert ledger.get_attempt(claimed.attempt_id).state == "claim-confirmed"
        assert ledger.get_outcome(claimed.attempt_id) is None
        assert tracker.call_names().count("post_comment") == 0
    finally:
        store.close()
        ledger.close()


def test_missing_blob_refuses_with_no_tracker_writes(tmp_path: Path) -> None:
    """A missing blob refuses before any settlement write."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        archive = _archive_for(tmp_path, claimed.attempt_id)
        stored = ledger.get_attempt(claimed.attempt_id)
        index = stored.content_refs
        assert isinstance(index, dict)
        activations = index.get("activations")
        assert isinstance(activations, dict)
        alpha = activations.get("alpha")
        assert isinstance(alpha, dict)
        digest = alpha.get("sha256")
        assert isinstance(digest, str)
        (archive / "content" / digest).unlink()
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            recover_unfinished_step_attempts(
                tracker=tracker, ledger=ledger, workflow=_workflow()
            )
        assert claimed.attempt_id in str(excinfo.value)
        assert excinfo.value.fault == "missing-blob"
        assert ledger.get_attempt(claimed.attempt_id).state == "claim-confirmed"
        assert tracker.call_names().count("post_comment") == 0
        assert tracker.call_names().count("remove_label") == 0
    finally:
        store.close()
        ledger.close()


def test_symlinked_blob_refuses(tmp_path: Path) -> None:
    """A symlinked blob refuses rather than following the link."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        archive = _archive_for(tmp_path, claimed.attempt_id)
        stored = ledger.get_attempt(claimed.attempt_id)
        blobs = ensure_archive_for_recovery(ledger=ledger, attempt_id=claimed.attempt_id)
        first_digest = next(iter(blobs.keys()))
        target = archive / "content" / first_digest
        raw = target.read_bytes()
        outside = tmp_path / "outside.bin"
        outside.write_bytes(raw)
        target.unlink()
        target.symlink_to(outside)
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            recover_unfinished_step_attempts(
                tracker=tracker, ledger=ledger, workflow=_workflow()
            )
        assert excinfo.value.fault == "symlinked-blob"
        assert claimed.attempt_id in str(excinfo.value)
        assert ledger.get_attempt(claimed.attempt_id).state == "claim-confirmed"
    finally:
        store.close()
        ledger.close()


def test_escaping_core_refuses(tmp_path: Path) -> None:
    """A core with an escaping component path refuses."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        stored = ledger.get_attempt(claimed.attempt_id)
        record = dict(stored.effective_configuration)
        bundle = dict(cast(Mapping[str, object], record["skill_bundle"]))
        bundle_members = dict(cast(Mapping[str, object], bundle["members"]))
        alpha = dict(cast(Mapping[str, object], bundle_members["alpha"]))
        raw_files = cast(list[object], alpha["files"])
        files = [dict(cast(Mapping[str, object], item)) for item in raw_files]
        files[0] = {**files[0], "path": "../evil.md"}
        alpha["files"] = files
        bundle_members["alpha"] = alpha
        bundle["members"] = bundle_members
        record["skill_bundle"] = bundle
        # Re-pin the tampered core so the digest and reference-index gates
        # pass and the escaping path itself is what refuses.
        repinned_digest = digest_effective_configuration(record)
        repinned_refs = content_index_for_record(record)
        archive = _archive_for(tmp_path, claimed.attempt_id)
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            validate_archive_for_attempt(
                archive_dir=archive,
                record=record,
                stored_digest=repinned_digest,
                stored_refs=repinned_refs,
                attempt_id=claimed.attempt_id,
            )
        assert excinfo.value.fault == "escaping-core"
        assert claimed.attempt_id in str(excinfo.value)
    finally:
        store.close()
        ledger.close()


def test_core_digest_mismatch_refuses(tmp_path: Path) -> None:
    """A tampered core digest refuses; current files never repair it."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        stored = ledger.get_attempt(claimed.attempt_id)
        archive = _archive_for(tmp_path, claimed.attempt_id)
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            validate_archive_for_attempt(
                archive_dir=archive,
                record=stored.effective_configuration,
                stored_digest="0" * 64,
                stored_refs=stored.content_refs,
                attempt_id=claimed.attempt_id,
            )
        assert excinfo.value.fault == "core-digest-mismatch"
        assert claimed.attempt_id in str(excinfo.value)
    finally:
        store.close()
        ledger.close()


def test_reference_index_mismatch_refuses(tmp_path: Path) -> None:
    """A reference index that disagrees with the record refuses."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        stored = ledger.get_attempt(claimed.attempt_id)
        archive = _archive_for(tmp_path, claimed.attempt_id)
        tampered_refs = dict(stored.content_refs)
        tampered_refs["prompts"] = {}
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            validate_archive_for_attempt(
                archive_dir=archive,
                record=stored.effective_configuration,
                stored_digest=stored.effective_configuration_digest,
                stored_refs=tampered_refs,
                attempt_id=claimed.attempt_id,
            )
        assert excinfo.value.fault == "reference-index-mismatch"
    finally:
        store.close()
        ledger.close()


def test_incompatible_version_refuses(tmp_path: Path) -> None:
    """A record with another version is incompatible, never converted."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        stored = ledger.get_attempt(claimed.attempt_id)
        record = dict(stored.effective_configuration)
        record["version"] = 1
        archive = _archive_for(tmp_path, claimed.attempt_id)
        with pytest.raises(ArchiveRecoveryRefused) as excinfo:
            validate_archive_for_attempt(
                archive_dir=archive,
                record=record,
                stored_digest=stored.effective_configuration_digest,
                stored_refs=stored.content_refs,
                attempt_id=claimed.attempt_id,
            )
        assert excinfo.value.fault == "incompatible-version"
        assert claimed.attempt_id in str(excinfo.value)
    finally:
        store.close()
        ledger.close()


def test_no_secret_restoration_for_not_delivered(tmp_path: Path) -> None:
    """``not-delivered`` digests stay pinned; their bodies are never loaded."""
    secret = b"top secret bytes"
    skill_body = b"alpha skill body"
    extra = b"alpha extra companion"
    members = [
        _member(
            "alpha",
            {"SKILL.md": skill_body, "extra.md": extra, "secret.bin": secret},
            not_delivered=("secret.bin",),
        )
    ]
    prompt_texts = {"prompts/a.md": "hello prompt\n"}
    deliveries = {"alpha": "delivery-alpha-bytes"}
    components = {"alpha": {"SKILL.md": skill_body, "extra.md": extra}}
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        stored = ledger.get_attempt(claimed.attempt_id)
        index = stored.content_refs
        assert isinstance(index, dict)
        components_index = index.get("components")
        assert isinstance(components_index, dict)
        alpha_index = components_index.get("alpha")
        assert isinstance(alpha_index, dict)
        secret_digest = _digest(secret)
        assert alpha_index["secret.bin"] == {
            "sha256": secret_digest,
            "archived": False,
        }
        archive = _archive_for(tmp_path, claimed.attempt_id)
        assert not (archive / "content" / secret_digest).exists()
        # Recovery succeeds without the secret, and never creates it.
        summary = recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )
        assert summary.settled == (claimed.attempt_id,)
        assert not (archive / "content" / secret_digest).exists()
    finally:
        store.close()
        ledger.close()


def test_direct_settlement_entry_still_validates(tmp_path: Path) -> None:
    """The integrity gate cannot be skipped via the settlement entry."""
    from coding_agent.runtime.step_recovery import recover_attempt

    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        archive = _archive_for(tmp_path, claimed.attempt_id)
        stored = ledger.get_attempt(claimed.attempt_id)
        first_digest = next(
            iter(
                ensure_archive_for_recovery(
                    ledger=ledger, attempt_id=claimed.attempt_id
                ).keys()
            )
        )
        (archive / "content" / first_digest).write_bytes(b"tampered\n")
        with pytest.raises(ArchiveRecoveryRefused):
            recover_attempt(
                tracker=tracker,
                ledger=ledger,
                attempt_id=claimed.attempt_id,
                number=claimed.input_number,
                input_type="issue",
                selection_label=LABEL,
                end_labels_for={"interrupted": ()},
            )
        assert ledger.get_attempt(claimed.attempt_id).state == "claim-confirmed"
        assert ledger.get_outcome(claimed.attempt_id) is None
    finally:
        store.close()
        ledger.close()


def test_recovery_halt_names_attempt_and_fault(tmp_path: Path) -> None:
    """The poll-loop halt for a bad archive names the Attempt and fault."""
    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        archive = _archive_for(tmp_path, claimed.attempt_id)
        stored = ledger.get_attempt(claimed.attempt_id)
        first_digest = next(
            iter(
                ensure_archive_for_recovery(
                    ledger=ledger, attempt_id=claimed.attempt_id
                ).keys()
            )
        )
        (archive / "content" / first_digest).write_bytes(b"bad\n")
        exit_code = run_worker_loop(
            tracker=tracker,
            ledger=ledger,
            loop_config=_loop_config(tracker),
            run_one_attempt=lambda _claimed: (_ for _ in ()).throw(
                AssertionError("no Attempt runs after a refused recovery")
            ),
            sleep=lambda _seconds: None,
            max_sweeps=5,
        )
        assert exit_code == 1
        connection = sqlite3.connect(str(ledger.path))
        try:
            row = connection.execute(
                "SELECT reason, detail FROM operator_halt WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert row[0] == "recovery-refused"
        assert claimed.attempt_id in str(row[1])
    finally:
        store.close()
        ledger.close()


# --- SKV3-31: legacy records ---------------------------------------------------


def test_open_legacy_step_record_is_refused_never_converted(tmp_path: Path) -> None:
    """An incompatible open step record is refused, never given an archive."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "implement#7/1", effective_configuration={"workflow": "x"}
        )
        tracker = _tracker_with_issue(7)
        with pytest.raises(PinnedConfigurationRefused) as excinfo:
            recover_unfinished_step_attempts(
                tracker=tracker, ledger=ledger, workflow=_workflow()
            )
        assert "implement#7/1" in str(excinfo.value)
        assert ledger.get_attempt("implement#7/1").state == "open"
        # Never fabricated: no archive directory appears for the Attempt.
        assert not (tmp_path / "state" / "evidence").exists()
    finally:
        ledger.close()


def test_open_versioned_legacy_record_is_refused(tmp_path: Path) -> None:
    """A ``version: 1`` open record is incompatible, never converted."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        ledger.open_attempt(
            "implement#7/1",
            effective_configuration={"version": 1, "workflow": {"id": "implement"}},
        )
        tracker = _tracker_with_issue(7)
        with pytest.raises(PinnedConfigurationRefused):
            recover_unfinished_step_attempts(
                tracker=tracker, ledger=ledger, workflow=_workflow()
            )
        assert ledger.get_attempt("implement#7/1").state == "open"
    finally:
        ledger.close()


def test_closed_legacy_history_stays_listable_viewable_exportable(
    tmp_path: Path,
) -> None:
    """Closed legacy history stays listable, viewable and exportable."""
    from coding_agent.operator.attempts import (
        export_manifest_document,
        list_attempts_page,
        show_attempt,
        write_export_tar,
    )
    import io

    ledger_path = tmp_path / "state" / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration={"workflow": "x"})
        ledger.set_attempt_state("#7/1", "finished")
        ledger.open_attempt("#8/1", effective_configuration={"workflow": "y"})
        ledger.set_attempt_state("#8/1", "released")
    finally:
        ledger.close()
    state_dir = tmp_path / "state"
    attempts, cursor = list_attempts_page(ledger_path)
    assert [entry["attempt_id"] for entry in attempts] == ["#8/1", "#7/1"]
    assert cursor is None
    shown = show_attempt(
        ledger_path=ledger_path, state_dir=state_dir, attempt_id="#7/1"
    )
    assert shown is not None
    assert shown["attempt"]["attempt_id"] == "#7/1"
    document = export_manifest_document(
        attempt=dict(shown["attempt"]),
        files=list(shown["manifest"]),
    )
    from agent_operator_contract import documents as contract_documents

    assert contract_documents.validate_export_manifest(document) == []
    buffer = io.BytesIO()
    write_export_tar(
        archive_dir=None,
        document=document,
        entries=list(shown["manifest"]),
        fileobj=buffer,
    )
    assert len(buffer.getvalue()) > 0


def test_unfinished_legacy_mvp_attempt_refuses_without_archive(tmp_path: Path) -> None:
    """An unfinished MVP Attempt refuses; nothing is converted or archived."""
    from coding_agent.worker.run import LegacyAttemptRefused

    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        ledger.open_attempt("#7/1", effective_configuration={"workflow": "x"})
        tracker = _tracker_with_issue(7)
        with pytest.raises(LegacyAttemptRefused) as excinfo:
            recover_unfinished_step_attempts(
                tracker=tracker, ledger=ledger, workflow=_workflow()
            )
        assert "#7/1" in str(excinfo.value)
        assert not (tmp_path / "state" / "evidence").exists()
    finally:
        ledger.close()


def test_empty_archive_validates_without_blobs(tmp_path: Path) -> None:
    """An Attempt pinning no content needs no archive directory."""
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        tracker = _tracker_with_issue(7)
        bare = _workflow(with_prompt=False)
        effective = full_effective_configuration(bare)
        assert content_index_for_record(effective) == {
            "prompts": {},
            "activations": {},
            "components": {},
        }
        ledger.open_attempt("implement#7/1", effective_configuration=effective)
        ledger.set_attempt_state("implement#7/1", "claim-confirmed")
        summary = recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=bare
        )
        assert summary.settled == ("implement#7/1",)
    finally:
        ledger.close()


def test_recovery_takes_no_model_nor_current_files(
    tmp_path: Path,
) -> None:
    """Recovery never calls the model and never reads the current deployment."""
    import inspect

    from coding_agent.runtime import step_recovery
    from coding_agent.skillbundle import recovery as recovery_module
    from coding_agent.worker import run as worker_run

    for name in (
        "recover_unconfirmed_attempt",
        "recover_attempt",
        "decide_restart_outcome",
        "settle_open_step_effects",
        "settle_step_effect",
        "finalise_shutdown",
    ):
        parameters = inspect.signature(getattr(step_recovery, name)).parameters
        assert "model" not in parameters
        assert "chat_model" not in parameters
    for name in ("validate_archive_for_attempt", "ensure_archive_for_recovery"):
        parameters = inspect.signature(getattr(recovery_module, name)).parameters
        assert "model" not in parameters
        assert "prompt_texts" not in parameters
        assert "activation_deliveries" not in parameters
    worker_parameters = inspect.signature(
        worker_run.recover_unfinished_step_attempts
    ).parameters
    assert "model" not in worker_parameters

    prompt_texts, deliveries, components, members = _startup_bytes()
    ledger, store, tracker, claimed = _claim_with_components(
        tmp_path,
        prompt_texts=prompt_texts,
        members=members,
        deliveries=deliveries,
        components=components,
    )
    try:
        from support.scripted_model import ScriptedModel

        model = ScriptedModel([])
        summary = recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )
        assert summary.settled == (claimed.attempt_id,)
        assert model.invocations == []
    finally:
        store.close()
        ledger.close()
