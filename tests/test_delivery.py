from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime import delivery
from coding_agent.runtime.delivery import (
    COMMITTED_AND_PUSHED,
    DELIVERY_SNAPSHOT_COLLISION_KIND,
    DELIVERY_SNAPSHOT_WRITE_KIND,
    NO_CHANGE_PRODUCED,
    REMOTE_BRANCH_COLLISION,
    REMOTE_BRANCH_COLLISION_NEXT_ACTION,
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
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.publication import PUBLISH_WRITE_KIND
from coding_agent.runtime.redaction import GitCredentialHelper, Redactable
from coding_agent.runtime.review_snapshot import publish_review_snapshot
from conftest import init_origin_repo, run_git
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

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


_GRAPH_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

_GRAPH_EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

_GRAPH_BRANCH = "agent/7/1-add-a-greeting"


@pytest.fixture
def graph_ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _init_graph_remote(tmp_path: Path) -> Path:
    """A local bare remote whose base tree holds the project profile."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(_GRAPH_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _seed_competitor_branch(bare: Path, tmp_path: Path, *, base: str, dirname: str) -> str:
    """Push a foreign commit on the Attempt branch from a side clone."""
    seeder = tmp_path / dirname
    run_git(["clone", "-q", str(bare), str(seeder)], cwd=tmp_path)
    run_git(["fetch", "-q", "origin"], seeder)
    run_git(["checkout", "-q", "-b", _GRAPH_BRANCH, base], seeder)
    (seeder / "competitor.txt").write_text("competitor\n", encoding="utf-8")
    run_git(["add", "."], seeder)
    run_git(["commit", "-q", "-m", "competitor"], seeder)
    run_git(["push", "-q", "origin", _GRAPH_BRANCH], seeder)
    return run_git(["rev-parse", _GRAPH_BRANCH], seeder)


def _graph_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _graph_model(*, with_review: bool) -> ScriptedModel:
    """Activate, write, test, and complete; review subagents only where asked.

    The targeted run reports ``pkg::test_ok``, which the completion claim
    covers, so ``run_step`` reaches the Delivery Snapshot with a green tree.
    A first claim without a completed review is returned once by design, so
    the review-less flow claims twice; the reviewed flow writes once more
    after the review so the Delivery Snapshot has a commit of its own to
    push (otherwise the tree is already clean at the review sha and no push
    — and no race — can happen).
    """
    messages: list[AIMessage] = [
        AIMessage(
            content="activating",
            tool_calls=[_graph_call("activate_implement", {}, "call-act-1")],
        ),
        AIMessage(
            content="writing then testing",
            tool_calls=[
                _graph_call(
                    "write_file",
                    {"path": "greeting.txt", "content": "hello\n"},
                    "call-write-1",
                ),
                _graph_call(
                    "test_targeted", {"path": "tests/test_greet.py"}, "call-test-1"
                ),
            ],
        ),
    ]
    if with_review:
        messages.append(
            AIMessage(
                content="reviewing",
                tool_calls=[_graph_call("activate_code_review", {}, "call-rev-1")],
            )
        )
        messages.append(
            AIMessage(
                content="reviewing with subagents",
                tool_calls=[
                    _graph_call("spawn_subagent", {"description": "review standards", "instructions": "review the change against the Target Issue"}, "call-spawn-1"),
                    _graph_call("spawn_subagent", {"description": "review spec", "instructions": "review the change against the Target Issue"}, "call-spawn-2"),
                ],
            )
        )
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    _graph_call("report_subagent", {"report": "no findings"}, "sub-report-1")
                ],
            )
        )
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    _graph_call("report_subagent", {"report": "no findings"}, "sub-report-2")
                ],
            )
        )
        messages.append(
            AIMessage(
                content="polishing after review",
                tool_calls=[
                    _graph_call(
                        "write_file",
                        {"path": "notes.txt", "content": "polish\n"},
                        "call-write-2",
                    ),
                ],
            )
        )
    messages.append(
        AIMessage(
            content="the change is complete",
            tool_calls=[_graph_call("report_completion", {"AC-1": ["pkg::test_ok"]}, "call-rep-1")],
        )
    )
    if not with_review:
        messages.append(
            AIMessage(
                content="the change is complete",
                tool_calls=[
                    _graph_call("report_completion", {"AC-1": ["pkg::test_ok"]}, "call-rep-2")
                ],
            )
        )
    return ScriptedModel(messages)


def _run_graph_attempt(
    *,
    tmp_path: Path,
    skills_dir: Path,
    ledger: RunLedger,
    bare: Path,
    model: ScriptedModel,
) -> tuple[AttemptResult, FakeTracker]:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workdir)], cwd=tmp_path)
    tracker = FakeTracker()
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workdir,
        effective_configuration=_GRAPH_EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
        remote_url=str(bare),
        agent_identity=IDENTITY,
    )
    return result, tracker


def _assert_foreign_collision_end_to_end(
    *,
    result: AttemptResult,
    tracker: FakeTracker,
    ledger: RunLedger,
    bare: Path,
    foreign_sha: str,
) -> None:
    """§13/§14/§15: the collision ends the Attempt with an observable note."""
    assert (result.outcome, result.reason) == ("failed", "interrupted")
    effect = ledger.get_effect("#7/1", DELIVERY_SNAPSHOT_WRITE_KIND)
    assert effect is not None
    assert effect.status == "intent"
    collisions = [
        record
        for record in ledger.records("#7/1")
        if record.kind == DELIVERY_SNAPSHOT_COLLISION_KIND
    ]
    assert len(collisions) == 1
    payload = dict(collisions[0].payload)
    assert payload["branch"] == _GRAPH_BRANCH
    assert payload["remote_sha"] == foreign_sha
    assert payload["foreign"] is True
    assert payload["next_action"] == REMOTE_BRANCH_COLLISION_NEXT_ACTION
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert "open_pull_request" not in tracker.call_names()
    assert "add_ready_for_human" in tracker.call_names()
    comment = next(
        args[1] for name, args in tracker.calls if name == "post_terminal_comment"
    )
    assert _GRAPH_BRANCH in str(comment)
    assert REMOTE_BRANCH_COLLISION_NEXT_ACTION in str(comment)
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("failed", "interrupted")
    assert run_git(["rev-parse", _GRAPH_BRANCH], bare) == foreign_sha
    assert (
        run_git(["show", f"{foreign_sha}:competitor.txt"], bare).strip()
        == "competitor"
    )


def test_graph_attempt_with_preexisting_foreign_branch_ends_interrupted(
    tmp_path: Path, skills_dir: Path, graph_ledger: RunLedger
) -> None:
    """§13 end to end: a foreign remote Attempt branch means no push.

    The Delivery Snapshot commit lands locally, the push never happens, the
    Attempt ends ``failed`` / ``interrupted`` with a ``delivery-snapshot-
    collision`` ledger event, the terminal comment names the branch and its
    human action, no pull request opens and the foreign branch is preserved.
    """
    bare = _init_graph_remote(tmp_path)
    foreign_sha = _seed_competitor_branch(
        bare, tmp_path, base="origin/main", dirname="seeder"
    )

    result, tracker = _run_graph_attempt(
        tmp_path=tmp_path,
        skills_dir=skills_dir,
        ledger=graph_ledger,
        bare=bare,
        model=_graph_model(with_review=False),
    )

    _assert_foreign_collision_end_to_end(
        result=result,
        tracker=tracker,
        ledger=graph_ledger,
        bare=bare,
        foreign_sha=foreign_sha,
    )


def test_graph_attempt_with_race_during_delivery_push_ends_interrupted(
    tmp_path: Path, skills_dir: Path, graph_ledger: RunLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§13 end to end: a branch created during the push is a race, not an overwrite.

    The review snapshot pushes first (its sha is ours), then a foreign
    commit wins the delivery push: the Attempt ends ``failed`` /
    ``interrupted`` with the re-read foreign sha, no pull request opens and
    the foreign branch is preserved.
    """
    bare = _init_graph_remote(tmp_path)
    seeder = tmp_path / "seeder"
    run_git(["clone", "-q", str(bare), str(seeder)], cwd=tmp_path)
    raced: list[str] = []

    def competing_push(
        active_workspace: Workspace,
        remote_url: str,
        active_branch_name: str,
        *,
        redact: Redactable = None,
        helper: GitCredentialHelper | None = None,
    ) -> None:
        run_git(["fetch", "-q", "origin"], seeder)
        run_git(
            ["checkout", "-q", "-b", active_branch_name, f"origin/{active_branch_name}"],
            seeder,
        )
        (seeder / "competitor.txt").write_text("competitor\n", encoding="utf-8")
        run_git(["add", "."], seeder)
        run_git(["commit", "-q", "-m", "competitor"], seeder)
        run_git(["push", "-q", "origin", active_branch_name], seeder)
        raced.append(run_git(["rev-parse", active_branch_name], seeder))
        push_branch(
            active_workspace, remote_url, active_branch_name, redact=redact, helper=helper
        )

    monkeypatch.setattr("coding_agent.runtime.delivery.push_branch", competing_push)

    result, tracker = _run_graph_attempt(
        tmp_path=tmp_path,
        skills_dir=skills_dir,
        ledger=graph_ledger,
        bare=bare,
        model=_graph_model(with_review=True),
    )

    assert len(raced) == 1
    _assert_foreign_collision_end_to_end(
        result=result,
        tracker=tracker,
        ledger=graph_ledger,
        bare=bare,
        foreign_sha=raced[0],
    )
