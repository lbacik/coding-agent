"""Claim-boundary content archiving: blobs before the ledger transaction (#398).

Contract: at the Claim boundary the Attempt Archive holds digest-addressed
blobs for every prompt the Workflow needs and every available activation's
components and delivery bytes — including on-demand-only members — written
and flushed before the ledger transaction the Claim depends on.
``not-delivered`` bodies have no blob; their digests stay pinned in the
content-reference index only. A crash before or after archive durability
never leaves a confirmed Claim referencing missing or unflushed content;
orphaned unreferenced files are permitted. Existing Claim intent/assignment
ordering is unchanged.

Covered at the Worker-loop seam (``sweep_and_claim``/``attempt_claim``),
including the crash windows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from coding_agent.inputs.claim import ClaimConfig, ConfirmedClaim, sweep_and_claim
from coding_agent.inputs.port import InputType, LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator.store import OperatorStore
from coding_agent.workflow.effective import (
    collect_claim_blobs,
    content_index_for_record,
)
from support.fake_step_tracker import FakeStepTracker

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


def _startup_bytes() -> tuple[
    dict[str, str], dict[str, str], dict[str, dict[str, bytes]]
]:
    """Prompt texts, deliveries and components with an on-demand-only member."""
    prompt_texts = {"prompts/a.md": "hello prompt\n"}
    deliveries = {
        "alpha": "delivery-alpha-bytes",
        "beta": "delivery-beta-on-demand-bytes",
    }
    components: dict[str, dict[str, bytes]] = {
        "alpha": {
            "SKILL.md": b"alpha skill body",
            "extra.md": b"alpha extra companion",
        },
        "beta": {"SKILL.md": b"beta skill body"},
    }
    return prompt_texts, deliveries, components


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _effective_with_index(
    prompt_texts: Mapping[str, str],
    deliveries: Mapping[str, str],
    components: Mapping[str, Mapping[str, bytes]],
) -> dict[str, object]:
    """A synthetic record carrying the prompts/activations/components sections."""
    prompts: dict[str, object] = {}
    for locator, text in prompt_texts.items():
        raw = text.encode("utf-8")
        prompts[locator] = {"sha256": _digest(raw), "size_bytes": len(raw)}
    activations: dict[str, object] = {}
    for name, delivery in deliveries.items():
        raw = delivery.encode("utf-8")
        activations[name] = {
            "framing_version": 1,
            "sha256": _digest(raw),
            "size_bytes": len(raw),
        }
    members: dict[str, object] = {}
    for name, paths in components.items():
        files = [
            {"path": path, "sha256": _digest(raw), "executable": False}
            for path, raw in sorted(paths.items())
        ]
        skill_digest = _digest(paths["SKILL.md"])
        members[name] = {
            "sourceIdentity": "test-source",
            "resolvedCommit": "a" * 40,
            "declaredRef": None,
            "relativeSourcePath": name,
            "storeRelativePath": name,
            "files": files,
            "overlays": [],
            "review": {
                "sourceIdentity": "test-source",
                "name": name,
                "skillSha256": skill_digest,
                "requires": [],
                "files": [],
                "references": [],
                "reason": "test",
            },
        }
    return {
        "version": 2,
        "prompts": prompts,
        "activations": activations,
        "skill_bundle": {"members": members},
    }


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore]:
    ledger = RunLedger(tmp_path / "run-ledger.sqlite3")
    store = OperatorStore(tmp_path / "run-ledger.sqlite3")
    store.write_instance_metadata(target_repository=REPO)
    return ledger, store


def _tracker_with_issue() -> FakeStepTracker:
    fake = FakeStepTracker()
    fake.inputs[42] = _issue()
    fake.labeled_events[(42, LABEL)] = _human_event()
    return fake


def _config(
    tracker: FakeStepTracker,
    effective: Mapping[str, object],
    prompt_texts: Mapping[str, str],
    deliveries: Mapping[str, str],
    components: Mapping[str, Mapping[str, bytes]],
) -> ClaimConfig:
    return ClaimConfig(
        workflow_id="implement",
        input_type="issue",
        selection_label=LABEL,
        base_branch="main",
        agent_login=tracker.agent_login,
        agent_id=tracker.agent_id,
        effective_configuration=effective,
        prompt_texts=dict(prompt_texts),
        activation_deliveries=dict(deliveries),
        activation_components={n: dict(p) for n, p in components.items()},
    )


def _archive_for(tmp_path: Path, attempt_id: str) -> Path:
    return attempt_archives.archive_dir_for(tmp_path, REPO, attempt_id)


def test_blobs_flushed_before_dependent_ledger_transaction(tmp_path: Path) -> None:
    """Archive blobs land on disk before the Claim's ledger row opens."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with_issue()
        prompt_texts, deliveries, components = _startup_bytes()
        effective = _effective_with_index(prompt_texts, deliveries, components)
        config = _config(tracker, effective, prompt_texts, deliveries, components)

        blobs_at_archive_time: dict[str, Any] = {}
        real_store = attempt_archives.store_content_blobs
        seen_ledger_row = {"saw": False}

        def _recording(
            archive_dir: Path, blobs: dict[str, bytes] | Mapping[str, bytes]
        ) -> list[Path]:
            # The dependent ledger transaction has not run yet: no row.
            seen_ledger_row["saw"] = ledger.has_attempt("implement#42/1")
            blobs_at_archive_time.update(dict(blobs))
            stored = real_store(archive_dir, blobs)
            # Flushed before return: every blob readable with exact bytes.
            for digest, raw in blobs.items():
                candidate = archive_dir / "content" / digest
                assert candidate.is_file()
                assert candidate.read_bytes() == bytes(raw)
            return stored

        monkeypatch = pytest.MonkeyPatch()
        with monkeypatch.context() as patched:
            patched.setattr(
                attempt_archives, "store_content_blobs", _recording
            )
            claimed = sweep_and_claim(tracker, ledger, config)
        assert isinstance(claimed, ConfirmedClaim)
        assert seen_ledger_row["saw"] is False
        assert blobs_at_archive_time

        archive = _archive_for(tmp_path, claimed.attempt_id)
        expected = collect_claim_blobs(
            prompt_texts=prompt_texts,
            activation_deliveries=deliveries,
            activation_components=components,
        )
        for digest, raw in expected.items():
            assert (archive / "content" / digest).read_bytes() == raw
        # Every available activation is archived, including on-demand-only beta.
        for name in ("alpha", "beta"):
            raw = deliveries[name].encode("utf-8")
            assert (archive / "content" / _digest(raw)).is_file()
        record = ledger.get_attempt(claimed.attempt_id)
        index = record.content_refs
        assert isinstance(index, dict)
    finally:
        store.close()
        ledger.close()


def test_not_delivered_excluded_but_digest_pinned(tmp_path: Path) -> None:
    """``not-delivered`` bodies have no blob; their digests stay in the index."""
    secret = b"top secret bytes"
    skill_body = b"alpha skill body"
    extra = b"alpha extra companion"
    prompt_texts = {"prompts/a.md": "hello prompt\n"}
    deliveries = {"alpha": "delivery-alpha-bytes"}
    components: dict[str, dict[str, bytes]] = {
        "alpha": {"SKILL.md": skill_body, "extra.md": extra},
    }
    blobs = collect_claim_blobs(
        prompt_texts=prompt_texts,
        activation_deliveries=deliveries,
        activation_components=components,
    )
    assert _digest(secret) not in blobs

    secret_digest = _digest(secret)
    skill_digest = _digest(skill_body)
    extra_digest = _digest(extra)
    record: dict[str, object] = {
        "prompts": {
            "prompts/a.md": {
                "sha256": _digest(prompt_texts["prompts/a.md"].encode("utf-8")),
                "size_bytes": len(prompt_texts["prompts/a.md"].encode("utf-8")),
            }
        },
        "activations": {
            "alpha": {
                "framing_version": 1,
                "sha256": _digest(deliveries["alpha"].encode("utf-8")),
                "size_bytes": len(deliveries["alpha"].encode("utf-8")),
            }
        },
        "skill_bundle": {
            "members": {
                "alpha": {
                    "sourceIdentity": "s",
                    "resolvedCommit": "b" * 40,
                    "files": [
                        {"path": "SKILL.md", "sha256": skill_digest, "executable": False},
                        {"path": "extra.md", "sha256": extra_digest, "executable": False},
                        {
                            "path": "secret.bin",
                            "sha256": secret_digest,
                            "executable": False,
                        },
                    ],
                    "overlays": [],
                    "review": {
                        "sourceIdentity": "s",
                        "name": "alpha",
                        "skillSha256": skill_digest,
                        "requires": [],
                        "files": [
                            {
                                "path": "secret.bin",
                                "sha256": secret_digest,
                                "executable": False,
                                "disposition": "not-delivered",
                                "reason": "withheld",
                            }
                        ],
                        "references": [],
                        "reason": "test",
                    },
                }
            }
        },
    }
    index = content_index_for_record(record)
    assert isinstance(index, dict)
    components_index = index["components"]
    assert isinstance(components_index, dict)
    alpha_index = components_index["alpha"]
    assert isinstance(alpha_index, dict)
    assert alpha_index["secret.bin"] == {
        "sha256": secret_digest,
        "archived": False,
    }
    assert alpha_index["SKILL.md"] == {"sha256": skill_digest, "archived": True}

    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with_issue()
        config = _config(tracker, record, prompt_texts, deliveries, components)
        claimed = sweep_and_claim(tracker, ledger, config)
        assert isinstance(claimed, ConfirmedClaim)
        archive = _archive_for(tmp_path, claimed.attempt_id)
        assert not (archive / "content" / secret_digest).exists()
        assert (archive / "content" / skill_digest).is_file()
        assert (archive / "content" / extra_digest).is_file()
        stored = ledger.get_attempt(claimed.attempt_id)
        stored_components = stored.content_refs.get("components")
        assert isinstance(stored_components, dict)
        stored_alpha = stored_components["alpha"]
        assert isinstance(stored_alpha, dict)
        entry = stored_alpha["secret.bin"]
        assert isinstance(entry, dict)
        assert entry["archived"] is False
        assert entry["sha256"] == secret_digest
    finally:
        store.close()
        ledger.close()


def test_crash_before_archive_leaves_no_attempt_row(tmp_path: Path) -> None:
    """A blob-write failure never opens the dependent ledger transaction."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with_issue()
        prompt_texts, deliveries, components = _startup_bytes()
        effective = _effective_with_index(prompt_texts, deliveries, components)
        config = _config(tracker, effective, prompt_texts, deliveries, components)

        def _failing(
            archive_dir: Path, blobs: dict[str, bytes] | Mapping[str, bytes]
        ) -> list[Path]:
            raise OSError("simulated volume loss before durability")

        monkeypatch = pytest.MonkeyPatch()
        with monkeypatch.context() as patched:
            patched.setattr(attempt_archives, "store_content_blobs", _failing)
            with pytest.raises(OSError):
                sweep_and_claim(tracker, ledger, config)
        assert not ledger.has_attempt("implement#42/1")
        assert ledger.unfinished_attempts() == ()
    finally:
        store.close()
        ledger.close()


def test_crash_after_archive_leaves_orphans_but_confirmed_claim_is_whole(
    tmp_path: Path,
) -> None:
    """Orphaned blobs are permitted; a confirmed Claim still references whole content."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        # Simulate a crash between durability and the ledger commit: blobs
        # for an Attempt whose row never opens.
        orphan_archive = _archive_for(tmp_path, "implement#42/9")
        orphan_blobs = {_digest(b"orphan bytes"): b"orphan bytes"}
        attempt_archives.store_content_blobs(orphan_archive, orphan_blobs)
        assert (orphan_archive / "content" / _digest(b"orphan bytes")).is_file()

        tracker = _tracker_with_issue()
        prompt_texts, deliveries, components = _startup_bytes()
        effective = _effective_with_index(prompt_texts, deliveries, components)
        config = _config(tracker, effective, prompt_texts, deliveries, components)
        claimed = sweep_and_claim(tracker, ledger, config)
        assert isinstance(claimed, ConfirmedClaim)
        # The orphan is still there and permitted.
        assert (orphan_archive / "content" / _digest(b"orphan bytes")).is_file()
        archive = _archive_for(tmp_path, claimed.attempt_id)
        expected = collect_claim_blobs(
            prompt_texts=prompt_texts,
            activation_deliveries=deliveries,
            activation_components=components,
        )
        for digest, raw in expected.items():
            assert (archive / "content" / digest).read_bytes() == raw
    finally:
        store.close()
        ledger.close()


def test_claim_intent_assignment_ordering_unchanged(tmp_path: Path) -> None:
    """The Claim still records intent, assigns, then posts the Claim Comment."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        tracker = _tracker_with_issue()
        prompt_texts, deliveries, components = _startup_bytes()
        effective = _effective_with_index(prompt_texts, deliveries, components)
        config = _config(tracker, effective, prompt_texts, deliveries, components)
        claimed = sweep_and_claim(tracker, ledger, config)
        assert isinstance(claimed, ConfirmedClaim)
        names = tracker.call_names()
        assert names.index("assign_input") < names.index("post_claim_comment")
        effects = ledger.list_step_effects(claimed.attempt_id)
        kinds = [
            (record.step_id, record.execution_number, record.ordinal, record.status)
            for record in effects
        ]
        assert ("", 0, 0, "receipt") in kinds
        assert ("", 0, 1, "receipt") in kinds
        first_ordinal = [entry[2] for entry in kinds if entry[3] == "receipt"]
        assert first_ordinal == sorted(first_ordinal)
        assert tracker.inputs[42].assignees == (tracker.agent_login,)
    finally:
        store.close()
        ledger.close()


def test_legacy_claim_without_bytes_archives_nothing(tmp_path: Path) -> None:
    """A Claim carrying no startup bytes archives nothing and still confirms."""
    ledger = RunLedger(tmp_path / "ledger.db")
    try:
        tracker = _tracker_with_issue()
        config = ClaimConfig(
            workflow_id="implement",
            input_type="issue",
            selection_label=LABEL,
            base_branch="main",
            agent_login=tracker.agent_login,
            agent_id=tracker.agent_id,
            effective_configuration={"workflow": "implement"},
        )
        claimed = sweep_and_claim(tracker, ledger, config)
        assert isinstance(claimed, ConfirmedClaim)
    finally:
        ledger.close()
