"""M3 review-snapshot push behind ``activate_code_review``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (when the model
calls ``activate_code_review`` the runtime commits the current tree and
pushes the Attempt's branch before any reviewer starts), §13 (the write
sequence: pass the in-tool Gate, record the intent keyed by
``(attempt_id, write_kind)``, look the remote ref up with ``ls-remote``,
push only when it is absent or behind, record the receipt carrying the
remote sha; a kind that can repeat carries its ordinal; push refusals are
classified by write kind and target path, never by error string) and §18
(one branch per Attempt; author and committer are the id-based noreply
address; every commit carries the ``Attempt: #<issue>/<n>`` trailer;
unsigned and never squashed; no branch is ever deleted).

MVP row ids: L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10, P-DEL-4, C-REV-1.

Every push here goes to a local bare remote; no test touches the network.
Until the reviewers land, the activation result returns the pushed sha and
states that no review ran, and the review status stays unmet.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import EffectRecord, RunLedger
from coding_agent.runtime import review_snapshot
from coding_agent.runtime.git import (
    GitFailure,
    Workspace,
    checkout_workspace,
    ensure_mirror,
)
from coding_agent.runtime.git import push_branch as git_push_branch
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.graph.tools import _with_review_snapshot
from coding_agent.runtime.review_snapshot import (
    MAX_REVIEW_INVOCATIONS,
    REVIEW_SNAPSHOT_KIND_PREFIX,
    WORKFLOWS_FAILURE_REASON,
    ReviewSnapshotRefused,
    ReviewSnapshotState,
    next_review_snapshot_ordinal,
    publish_review_snapshot,
)
from coding_agent.skillbundle.activation import build_activation_tools
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")
ATTEMPT_ID = "#34/2"
ISSUE_NUMBER = 34
ATTEMPT_NUMBER = 2
ISSUE_TITLE = "Fix the loop!"
BRANCH_NAME = "agent/34/2-fix-the-loop"

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

HAPPY_PATH_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare remote standing in for the Target Repository."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    return bare


@pytest.fixture
def checked_out_workspace(tmp_path: Path, bare_remote: Path) -> Workspace:
    """A workspace checked out from the local bare remote via its mirror."""
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(bare_remote))
    return checkout_workspace(mirror, tmp_path / "workspace")


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _publish(
    workspace: Workspace,
    ledger: RunLedger,
    bare_remote: Path,
    *,
    ordinal: int | None = None,
) -> Any:
    return publish_review_snapshot(
        workspace=workspace,
        ledger=ledger,
        attempt_id=ATTEMPT_ID,
        issue_number=ISSUE_NUMBER,
        attempt_number=ATTEMPT_NUMBER,
        issue_title=ISSUE_TITLE,
        identity=IDENTITY,
        remote_url=str(bare_remote),
        ordinal=ordinal,
    )


def _push_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    """Count ``push_branch`` calls in the review-snapshot module."""
    calls: list[int] = []
    real_push = git_push_branch

    def counting_push(
        workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: str | None = None,
    ) -> None:
        calls.append(1)
        real_push(workspace, remote_url, branch_name, redact=redact)

    monkeypatch.setattr(review_snapshot, "push_branch", counting_push)
    return calls


def _git_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Record every ``subprocess.run`` argv issued while active."""
    import subprocess as subprocess_module

    real_run = subprocess_module.run
    commands: list[list[str]] = []

    def spy(
        args: Sequence[str] | str, **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        if isinstance(args, (list, tuple)):
            commands.append([str(part) for part in args])
        else:
            commands.append([str(args)])
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess_module, "run", spy)
    return commands


def test_review_snapshot_records_intent_pushes_and_receipt_in_order(
    checked_out_workspace: Workspace,
    ledger: RunLedger,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§13: intent, then push, then receipt carrying the remote sha."""
    events: list[str] = []
    real_intent = ledger.record_effect_intent
    real_receipt = ledger.record_effect_receipt

    def logging_intent(
        attempt_id: str,
        write_kind: str,
        *,
        remote_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> EffectRecord:
        events.append("intent")
        return real_intent(
            attempt_id, write_kind, remote_id=remote_id, payload=payload
        )

    def logging_receipt(
        attempt_id: str,
        write_kind: str,
        *,
        remote_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> EffectRecord:
        events.append("receipt")
        return real_receipt(
            attempt_id, write_kind, remote_id=remote_id, payload=payload
        )

    monkeypatch.setattr(ledger, "record_effect_intent", logging_intent)
    monkeypatch.setattr(ledger, "record_effect_receipt", logging_receipt)

    real_push = git_push_branch

    def logging_push(
        workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: str | None = None,
    ) -> None:
        events.append("push")
        real_push(workspace, remote_url, branch_name, redact=redact)

    monkeypatch.setattr(review_snapshot, "push_branch", logging_push)

    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    outcome = _publish(checked_out_workspace, ledger, bare_remote)

    assert events == ["intent", "push", "receipt"]
    assert outcome.pushed is True
    assert outcome.write_kind == f"{REVIEW_SNAPSHOT_KIND_PREFIX}1"
    assert outcome.branch_name == BRANCH_NAME
    effect = ledger.get_effect(ATTEMPT_ID, outcome.write_kind)
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.remote_id == outcome.remote_sha
    assert run_git(["rev-parse", BRANCH_NAME], bare_remote) == outcome.remote_sha
    assert outcome.remote_sha == outcome.commit_sha


def test_two_invocations_produce_distinct_ordinals_and_receipt_reentry_pushes_nothing(
    checked_out_workspace: Workspace,
    ledger: RunLedger,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§13: the review-snapshot kind carries its ordinal; a receipt re-entry
    pushes nothing."""
    pushes = _push_counter(monkeypatch)

    (checked_out_workspace.path / "first.txt").write_text("first\n", encoding="utf-8")
    first = _publish(checked_out_workspace, ledger, bare_remote)
    (checked_out_workspace.path / "second.txt").write_text("second\n", encoding="utf-8")
    second = _publish(checked_out_workspace, ledger, bare_remote)

    assert first.write_kind != second.write_kind
    assert first.write_kind == f"{REVIEW_SNAPSHOT_KIND_PREFIX}1"
    assert second.write_kind == f"{REVIEW_SNAPSHOT_KIND_PREFIX}2"
    assert next_review_snapshot_ordinal(ledger, ATTEMPT_ID) == 3
    effects = ledger.list_effects(ATTEMPT_ID)
    assert [effect.write_kind for effect in effects] == [
        f"{REVIEW_SNAPSHOT_KIND_PREFIX}1",
        f"{REVIEW_SNAPSHOT_KIND_PREFIX}2",
    ]
    assert all(effect.status == "receipt" for effect in effects)
    assert len(pushes) == 2

    again = _publish(checked_out_workspace, ledger, bare_remote, ordinal=1)

    assert again.write_kind == first.write_kind
    assert again.pushed is False
    assert again.remote_sha == first.remote_sha
    assert len(pushes) == 2


def test_branch_identity_without_force_or_deletion(
    checked_out_workspace: Workspace,
    ledger: RunLedger,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L3-DEL-19: ``agent/<issue>/<n>-<slug>``, fast-forward only, nothing
    deleted."""
    commands = _git_spy(monkeypatch)

    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    outcome = _publish(checked_out_workspace, ledger, bare_remote)

    assert outcome.branch_name == "agent/34/2-fix-the-loop"
    tokens = [token for command in commands for token in command]
    assert not any(token.startswith("--force") for token in tokens)
    assert "-D" not in tokens
    assert "--delete" not in tokens
    branches = {
        line.strip().lstrip("* ").strip()
        for line in run_git(["branch"], bare_remote).splitlines()
        if line.strip()
    }
    assert BRANCH_NAME in branches
    assert "main" in branches


def test_commit_identity_unsigned_and_not_squashed(
    checked_out_workspace: Workspace, ledger: RunLedger, bare_remote: Path
) -> None:
    """L3-DEL-20: id-based noreply author and committer, the Attempt
    trailer, unsigned and never squashed."""
    (checked_out_workspace.path / "first.txt").write_text("first\n", encoding="utf-8")
    _publish(checked_out_workspace, ledger, bare_remote)
    (checked_out_workspace.path / "second.txt").write_text("second\n", encoding="utf-8")
    _publish(checked_out_workspace, ledger, bare_remote)

    base_count = int(run_git(["rev-list", "--count", "main"], bare_remote))
    assert int(run_git(["rev-list", "--count", BRANCH_NAME], bare_remote)) == base_count + 2
    for revision in (BRANCH_NAME, f"{BRANCH_NAME}~1"):
        assert (
            run_git(["log", "-1", "--format=%an", revision], bare_remote)
            == "coding-agent"
        )
        assert (
            run_git(["log", "-1", "--format=%ae", revision], bare_remote)
            == "999+coding-agent@users.noreply.github.com"
        )
        assert (
            run_git(["log", "-1", "--format=%cn", revision], bare_remote)
            == "coding-agent"
        )
        assert (
            run_git(["log", "-1", "--format=%ce", revision], bare_remote)
            == "999+coding-agent@users.noreply.github.com"
        )
        assert (
            run_git(["log", "-1", "--format=%B", revision], bare_remote)
            .strip()
            .endswith("Attempt: #34/2")
        )
        assert run_git(["log", "-1", "--format=%G?", revision], bare_remote) == "N"


def test_workflows_tree_is_refused_without_push(
    checked_out_workspace: Workspace, ledger: RunLedger, bare_remote: Path
) -> None:
    """L3-ERR-9: a tree touching ``.github/workflows/`` is refused by the
    Worker itself — classified by write kind and path, never pushed."""
    workflows_dir = checked_out_workspace.path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "ci.yml").write_text("on: push\n", encoding="utf-8")

    with pytest.raises(ReviewSnapshotRefused) as excinfo:
        _publish(checked_out_workspace, ledger, bare_remote)

    assert excinfo.value.reason == WORKFLOWS_FAILURE_REASON
    assert excinfo.value.write_kind == f"{REVIEW_SNAPSHOT_KIND_PREFIX}1"
    assert excinfo.value.target_path == ".github/workflows/"
    assert ledger.get_effect(ATTEMPT_ID, excinfo.value.write_kind) is None
    assert BRANCH_NAME not in run_git(["branch"], bare_remote)


def test_push_rejection_for_missing_signatures_ends_in_the_same_permanent_class(
    checked_out_workspace: Workspace,
    ledger: RunLedger,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L3-ERR-10: a rejected push ends in the same permanent class,
    classified by write kind and target path, never by error string."""
    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")

    def rejecting_push(
        workspace: Workspace,
        remote_url: str,
        branch_name: str,
        *,
        redact: str | None = None,
    ) -> None:
        raise GitFailure(
            f"'git push {remote_url} HEAD:refs/heads/{branch_name}' failed: "
            "remote rejected: missing signatures"
        )

    monkeypatch.setattr(review_snapshot, "push_branch", rejecting_push)

    with pytest.raises(ReviewSnapshotRefused) as excinfo:
        _publish(checked_out_workspace, ledger, bare_remote)

    assert excinfo.value.reason == WORKFLOWS_FAILURE_REASON
    assert excinfo.value.write_kind == f"{REVIEW_SNAPSHOT_KIND_PREFIX}1"
    assert excinfo.value.target_path == BRANCH_NAME
    assert BRANCH_NAME not in run_git(["branch"], bare_remote)


def test_effects_table_created_if_not_exists_without_disturbing_rows(
    tmp_path: Path,
) -> None:
    """§17: reopening an existing ledger file keeps every row."""
    path = tmp_path / "state" / "run-ledger.sqlite3"
    first = RunLedger(path)
    first.open_attempt(ATTEMPT_ID, effective_configuration={"repo": "x"})
    first.record_effect_intent(
        ATTEMPT_ID, f"{REVIEW_SNAPSHOT_KIND_PREFIX}1", remote_id="abc"
    )
    first.close()

    second = RunLedger(path)
    try:
        assert second.get_attempt(ATTEMPT_ID).attempt_id == ATTEMPT_ID
        effect = second.get_effect(ATTEMPT_ID, f"{REVIEW_SNAPSHOT_KIND_PREFIX}1")
        assert effect is not None
        assert effect.status == "intent"
        second.record_effect_intent(
            ATTEMPT_ID, f"{REVIEW_SNAPSHOT_KIND_PREFIX}2", remote_id="def"
        )
        assert (
            second.get_effect(ATTEMPT_ID, f"{REVIEW_SNAPSHOT_KIND_PREFIX}2")
            is not None
        )
    finally:
        second.close()


def test_activate_code_review_runs_snapshot_then_reports_no_review(
    checked_out_workspace: Workspace,
    ledger: RunLedger,
    bare_remote: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10: the activation pushes the branch first; until the reviewers land
    the result carries the pushed sha, states that no review ran, and the
    review status stays unmet."""
    pushes = _push_counter(monkeypatch)
    state = ReviewSnapshotState()
    assert state.status == "not-invoked"

    activation_tools = build_activation_tools(skills_dir)
    (wrapped,) = tuple(
        tool
        for tool in _with_review_snapshot(
            activation_tools,
            skills_dir=skills_dir,
            ledger=ledger,
            review_workspace=checked_out_workspace,
            attempt_id=ATTEMPT_ID,
            issue_number=ISSUE_NUMBER,
            attempt_number=ATTEMPT_NUMBER,
            issue_title=ISSUE_TITLE,
            identity=IDENTITY,
            remote_url=str(bare_remote),
            state=state,
        )
        if tool.name == "activate_code_review"
    )

    (checked_out_workspace.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    result = str(wrapped.invoke({}))

    assert f"Review snapshot {REVIEW_SNAPSHOT_KIND_PREFIX}1 pushed" in result
    assert "No review ran." in result
    assert state.invocations == 1
    assert state.status == "incomplete"
    assert state.snapshots == [run_git(["rev-parse", BRANCH_NAME], bare_remote)]
    effect = ledger.get_effect(ATTEMPT_ID, f"{REVIEW_SNAPSHOT_KIND_PREFIX}1")
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.remote_id == state.snapshots[0]

    for _ in range(MAX_REVIEW_INVOCATIONS - 1):
        str(wrapped.invoke({}))
    limited = str(wrapped.invoke({}))

    assert "invocation limit" in limited
    assert state.invocations == MAX_REVIEW_INVOCATIONS
    # The later invocations found the remote already holding the pushed sha,
    # so only the first call pushed.
    assert len(pushes) == 1


def _completion(call_id: str) -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"AC-1": ["pkg::test_ok"]},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_attempt_with_workflows_tree_ends_failed_workflows_path(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-ERR-9 end to end: the Attempt ends ``failed`` / ``workflows-path``
    and pushes nothing."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PATH_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    workspace = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workspace)], cwd=tmp_path)
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    try:
        model = ScriptedModel(
            [
                AIMessage(
                    content="activating",
                    tool_calls=[
                        {
                            "name": "activate_implement",
                            "args": {},
                            "id": "call_activate_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="working",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "path": ".github/workflows/ci.yml",
                                "content": "on: push\n",
                            },
                            "id": "call_write_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="reviewing",
                    tool_calls=[
                        {
                            "name": "activate_code_review",
                            "args": {},
                            "id": "call_review_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                _completion("call_completion_1"),
                _completion("call_completion_2"),
                _completion("call_completion_3"),
            ]
        )
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=model,
            workspace=workspace,
            effective_configuration=EFFECTIVE_CONFIGURATION,
            skills_dir=skills_dir,
            remote_url=str(bare),
            agent_identity=IDENTITY,
        )
    finally:
        ledger.close()

    assert result.outcome == "failed"
    assert result.reason == WORKFLOWS_FAILURE_REASON
    branches = {
        line.strip().lstrip("* ").strip()
        for line in run_git(["branch"], bare).splitlines()
        if line.strip()
    }
    assert branches == {"main"}
