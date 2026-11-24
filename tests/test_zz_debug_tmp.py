"""Temporary debug for the two-worker e2e (deleted before finishing)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_debug_two_worker_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_ai_assisted_handoff import (  # noqa: E402
        BASE,
        PICKUP_LABEL,
        SELECTION_LABEL,
        _e2e_ai,
        _e2e_config_clean,
        _e2e_config_pushed,
        _e2e_git,
        _e2e_matrix,
        _e2e_offer,
        _e2e_remote,
        _e2e_report_call,
        _e2e_run_worker,
        _e2e_seed_tracker,
    )
    from dataclasses import replace as _replace
    from support.scripted_model import ScriptedModel as _ScriptedModel

    note_7 = "handoff note for issue 7"
    note_8 = "handoff note for issue 8"

    tracker = _e2e_seed_tracker()
    seeded = _e2e_remote(tmp_path)
    base = seeded["base"]
    tracker.base_tip = base
    w1 = tmp_path / "w1"

    greeting = "hello from worker 1\n"
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

    src = Path(seeded["src"])
    (src / "moved.txt").write_text("moved\n", encoding="utf-8")
    _e2e_git(src, "add", "-A")
    _e2e_git(src, "commit", "-qm", "moved-base")
    _e2e_git(src, "push", "-q", seeded["remote"], "main")

    from coding_agent.inputs.claim import in_force_claim_comments
    from coding_agent.inputs.pickup import (
        newest_pickup_offer,
        pickup_exclusion_reason,
    )
    from coding_agent.ledger.store import RunLedger
    from coding_agent.worker.attempt_run import verify_offer_remote

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    ledger = RunLedger(fresh / "ledger.sqlite3")
    try:
        for number in (7, 8):
            held = tracker.inputs[number]
            comments = tracker.list_comments(number, "issue")
            found = newest_pickup_offer(
                comments,
                agent_id=tracker.agent_id,
                workflow_id="implement",
                issue_number=number,
            )
            in_force = in_force_claim_comments(comments, ledger, tracker.agent_id)
            print(f"\n=== issue {number} ===")
            print(f"labels={held.labels} assignees={held.assignees} open={held.is_open}")
            print(f"n_comments={len(comments)}")
            for comment in comments:
                print(
                    f"  id={comment.comment_id} author={comment.author_login}/"
                    f"{comment.author_id} marker={comment.marker_present} "
                    f"len={len(comment.body)} head={comment.body[:120]!r}"
                )
            print(f"offer_found={found is not None}")
            if found is not None:
                offer = found[0]
                print(
                    f"offer kind={offer.note_kind} pickup_label={offer.pickup_label} "
                    f"used={offer.used} limit={offer.limit} exhausted={offer.exhausted} "
                    f"ref={offer.ref} work={offer.work_commit} base={offer.base_revision}"
                )
            print(f"in_force_claims={[c.comment_id for c in in_force]}")
            reason = pickup_exclusion_reason(
                held,
                offer=found[0] if found is not None else None,
                pickup_label="round-finished",
                blocker_open=False,
                claim_in_force=bool(in_force),
                agent_pr_open=False,
            )
            print(f"pickup_exclusion={reason}")
    finally:
        ledger.close()

    import subprocess as _sp

    mirror_parent = tmp_path / "mirrorparent"
    mirror_parent.mkdir()
    _sp.run(
        ["git", "clone", "-q", "--bare", seeded["remote"], str(mirror_parent / "m.git")],
        check=True,
    )
    for number in (7, 8):
        comments = tracker.list_comments(number, "issue")
        found = newest_pickup_offer(
            comments,
            agent_id=tracker.agent_id,
            workflow_id="implement",
            issue_number=number,
        )
        assert found is not None
        print(
            f"verify_remote issue {number}: "
            f"{verify_offer_remote(mirror_dir=mirror_parent / 'm.git', canonical_remote=seeded['remote'], offer=found[0])}"
        )
