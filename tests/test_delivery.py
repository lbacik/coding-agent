from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime import delivery
from coding_agent.runtime.delivery import (
    COMMITTED_AND_PUSHED,
    NO_CHANGE_PRODUCED,
    REMOTE_BRANCH_COLLISION,
    deliver_snapshot,
    publish_delivery_snapshot,
)
from coding_agent.runtime.git import (
    Workspace,
    checkout_workspace,
    ensure_mirror,
    push_branch,
    remote_ref_sha,
)
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.runtime.review_snapshot import publish_review_snapshot
from conftest import init_origin_repo, run_git

IDENTITY = Identity(account_id=999, login="coding-agent")


def _checked_out_workspace(tmp_path: Path) -> tuple[Path, Workspace]:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace = checkout_workspace(mirror, tmp_path / "workspace")
    return origin, workspace


def test_deliver_snapshot_reports_no_change_produced_and_pushes_nothing(tmp_path: Path) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)

    outcome = deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Do the thing",
        attempt_number=1,
    )

    assert outcome.kind == NO_CHANGE_PRODUCED
    assert outcome.branch_name is None
    assert outcome.commit_sha is None
    assert run_git(["branch", "-a"], origin) == "* main"


def test_deliver_snapshot_commits_and_pushes_when_the_tree_changed(tmp_path: Path) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")

    outcome = deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Fix the loop!",
        attempt_number=2,
    )

    assert outcome.kind == COMMITTED_AND_PUSHED
    assert outcome.branch_name == "agent/34/2-fix-the-loop"
    assert outcome.commit_sha is not None

    remote_sha = run_git(["rev-parse", "agent/34/2-fix-the-loop"], origin)
    assert remote_sha == outcome.commit_sha
    assert run_git(["log", "-1", "--format=%an", remote_sha], origin) == "coding-agent"
    assert (
        run_git(["log", "-1", "--format=%ae", remote_sha], origin)
        == "999+coding-agent@users.noreply.github.com"
    )
    assert run_git(["log", "-1", "--format=%B", remote_sha], origin).strip().endswith(
        "Attempt: #34/2"
    )


def test_deliver_snapshot_reports_an_existing_remote_attempt_branch_without_committing(
    tmp_path: Path,
) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)
    branch_name = "agent/34/2-fix-the-loop"
    run_git(["branch", branch_name], origin)
    remote_sha = run_git(["rev-parse", branch_name], origin)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")

    outcome = deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Fix the loop!",
        attempt_number=2,
    )

    assert outcome.kind == REMOTE_BRANCH_COLLISION
    assert outcome.branch_name == branch_name
    assert outcome.commit_sha is None
    assert outcome.next_action == "choose a new attempt number or inspect the existing remote branch"
    assert run_git(["rev-parse", branch_name], origin) == remote_sha
    assert run_git(["rev-parse", "HEAD"], workspace.path) == workspace.base_revision
    assert run_git(["status", "--porcelain"], workspace.path) == "?? changed.txt"


def test_deliver_snapshot_reports_a_branch_created_while_pushing_as_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, workspace = _checked_out_workspace(tmp_path)
    branch_name = "agent/34/2-fix-the-loop"
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    def competing_push(
        active_workspace: Workspace,
        remote_url: str,
        active_branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        # A genuinely divergent competitor: a commit on the same branch name
        # that our snapshot is not a descendant of. The push is fast-forward
        # only (no force flag is ever passed), so it fails and the existing
        # remote branch is preserved as the collision.
        run_git(["checkout", "-q", "-b", active_branch_name], origin)
        (origin / "competitor.txt").write_text("competitor\n", encoding="utf-8")
        run_git(["add", "."], origin)
        run_git(["commit", "-q", "-m", "competitor"], origin)
        run_git(["checkout", "-q", "main"], origin)
        push_branch(
            active_workspace, remote_url, active_branch_name, redact=redact, helper=helper
        )

    monkeypatch.setattr("coding_agent.runtime.delivery.push_branch", competing_push)

    outcome = delivery.deliver_snapshot(
        workspace,
        remote_url=str(origin),
        identity=IDENTITY,
        issue_number=34,
        issue_title="Fix the loop!",
        attempt_number=2,
    )

    assert outcome.kind == REMOTE_BRANCH_COLLISION
    assert outcome.branch_name == branch_name
    assert outcome.commit_sha is not None
    competitor_sha = run_git(["rev-parse", branch_name], origin)
    assert competitor_sha != outcome.commit_sha
    assert run_git(["show", f"{competitor_sha}:competitor.txt"], origin).strip() == "competitor"


def _open_ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "ledger.sqlite3")


def test_publish_delivery_snapshot_reports_a_foreign_preexisting_branch_without_pushing(
    tmp_path: Path,
) -> None:
    """§13: a foreign existing remote ref means no push and no overwrite."""
    origin, workspace = _checked_out_workspace(tmp_path)
    branch_name = "agent/34/2-fix-the-loop"
    run_git(["branch", branch_name], origin)
    foreign_sha = run_git(["rev-parse", branch_name], origin)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    ledger = _open_ledger(tmp_path)

    published = publish_delivery_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(origin),
    )

    assert published.branch_name == branch_name
    assert published.commit_sha is not None
    assert published.pushed is False
    assert published.foreign is True
    assert published.remote_sha == foreign_sha
    assert run_git(["rev-parse", branch_name], origin) == foreign_sha
    assert run_git(["rev-parse", branch_name], origin) != published.commit_sha
    effect = ledger.get_effect("#34/2", "delivery-snapshot-push")
    assert effect is not None
    assert effect.status == "intent"


def test_publish_delivery_snapshot_reports_a_race_during_publication_as_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§13: a branch created during the push is a race, not a silent overwrite."""
    origin, workspace = _checked_out_workspace(tmp_path)
    branch_name = "agent/34/2-fix-the-loop"
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    ledger = _open_ledger(tmp_path)

    def competing_push(
        active_workspace: Workspace,
        remote_url: str,
        active_branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        run_git(["checkout", "-q", "-b", active_branch_name], origin)
        (origin / "competitor.txt").write_text("competitor\n", encoding="utf-8")
        run_git(["add", "."], origin)
        run_git(["commit", "-q", "-m", "competitor"], origin)
        run_git(["checkout", "-q", "main"], origin)
        push_branch(
            active_workspace, remote_url, active_branch_name, redact=redact, helper=helper
        )

    monkeypatch.setattr("coding_agent.runtime.delivery.push_branch", competing_push)

    published = publish_delivery_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(origin),
    )

    assert published.branch_name == branch_name
    assert published.commit_sha is not None
    assert published.pushed is False
    assert published.foreign is True
    competitor_sha = run_git(["rev-parse", branch_name], origin)
    assert competitor_sha != published.commit_sha
    # The re-read carries the current foreign sha, not the stale pre-push None.
    assert published.remote_sha == competitor_sha
    assert run_git(["show", f"{competitor_sha}:competitor.txt"], origin).strip() == "competitor"


def test_publish_delivery_snapshot_recognises_the_attempt_own_branch(
    tmp_path: Path,
) -> None:
    """§13: the Attempt's own recorded branch is ours, not a foreign change."""
    origin, workspace = _checked_out_workspace(tmp_path)
    ledger = _open_ledger(tmp_path)
    (workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")

    first = publish_delivery_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(origin),
    )

    assert first.pushed is True
    assert first.foreign is False
    assert first.remote_sha == first.commit_sha
    assert remote_ref_sha(str(origin), first.branch_name or "") == first.commit_sha

    # Re-entering the receipted write pushes nothing and stays non-foreign.
    second = publish_delivery_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(origin),
    )
    assert second.pushed is False
    assert second.foreign is False
    assert second.remote_sha == first.remote_sha


def test_publish_delivery_snapshot_pushes_past_the_attempt_own_review_snapshot(
    tmp_path: Path,
) -> None:
    """§13: a review-snapshot push is ours, so delivery fast-forwards past it."""
    origin, workspace = _checked_out_workspace(tmp_path)
    ledger = _open_ledger(tmp_path)
    ledger.open_attempt("#34/2", effective_configuration={"repo": "x/y"})
    (workspace.path / "review.txt").write_text("review\n", encoding="utf-8")
    review = publish_review_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(origin),
    )
    assert review.pushed is True

    (workspace.path / "delivery.txt").write_text("delivery\n", encoding="utf-8")
    published = publish_delivery_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id="#34/2",
        issue_number=34,
        attempt_number=2,
        issue_title="Fix the loop!",
        identity=IDENTITY,
        remote_url=str(origin),
    )

    assert published.pushed is True
    assert published.foreign is False
    assert remote_ref_sha(str(origin), published.branch_name or "") == published.commit_sha
