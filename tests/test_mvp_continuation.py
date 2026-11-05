"""M8 Continuation: a new Attempt starting from the previous pushed work.

Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (the Continuation
Snapshot round-trips through the terminal comment as a versioned block
carrying its SHA-256 digest; a tampered snapshot is treated as absent) and
§16 (a Continuation runs on its own new branch, created at the previous head
and rebased onto the current Base Revision during ``readiness``; a rebase
conflict ends the Attempt ``failed`` / ``continuation-conflict`` with
``ready-for-human`` and no model call; a deleted previous branch is a fresh
start from the Base Revision; the Acceptance Criteria come from the current
body and the Effective Configuration is the current one; the baseline is
evaluated at the new Base Revision; where no verified snapshot exists the
Attempt continues from the branch and its diff against the base).

MVP row ids: P-HND-1, P-HND-2, P-HND-3, P-HND-4, W-PIN-4, REC-WRK-3.

Each test runs Attempt ``#7/1`` to a failed outcome with pushed work (so its
terminal comment carries a Continuation Snapshot block), then runs ``#7/2``
and asserts how the Continuation used that work. Every push goes to a local
bare remote; no test touches the network and no test calls a real model.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import RunLedger, digest_effective_configuration
from coding_agent.runtime.context_handoff import ContinuationSnapshot
from coding_agent.runtime.continuation import parse_snapshot_block
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.publication import TERMINAL_COMMENT_KIND, render_snapshot_block
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

IDENTITY = Identity(account_id=999, login="coding-agent")

CONFIG_1: dict[str, object] = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

CONFIG_2: dict[str, object] = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
    "workflow": {"id": "survey", "selector": {"label": "ready-for-agent"}},
}

BODY_1 = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"

BODY_2 = "## Acceptance criteria\n- [ ] `greet` returns a warm greeting\n"

PREV_BRANCH = "agent/7/1-add-a-greeting"
NEW_BRANCH = "agent/7/2-add-a-greeting"

GREEN_PROFILE = """\
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


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare remote whose base tree holds the green profile."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(GREEN_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    """A workspace cloned from the local bare remote."""
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _activate(call_id: str = "call-act-1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[_call("activate_implement", {}, call_id)],
    )


def _write_and_test(
    write_id: str = "call-write-1", test_id: str = "call-test-1"
) -> AIMessage:
    return AIMessage(
        content="writing then testing",
        tool_calls=[
            _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id),
            _call("test_targeted", {"path": "tests/test_greet.py"}, test_id),
        ],
    )


def _completion(call_id: str, covering: tuple[str, ...] = ("pkg::test_ok",)) -> AIMessage:
    return AIMessage(
        content="the change is complete",
        tool_calls=[_call("report_completion", {"AC-1": list(covering)}, call_id)],
    )


def _review(call_id: str = "call-rev-1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[_call("activate_code_review", {}, call_id)],
    )


def _seat(call_id: str, findings: str = "no findings") -> AIMessage:
    return AIMessage(
        content="review seat",
        tool_calls=[_call("report_review", {"findings": findings}, call_id)],
    )


def _unverified_model() -> ScriptedModel:
    """A scripted model ending ``failed`` / ``unverified`` with a pushed diff.

    The unbacked identifiers are returned twice; the third call is
    force-accepted with the diff standing, so the Delivery Snapshot pushes
    the branch and the terminal comment carries a Continuation Snapshot.
    """
    return ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _completion("call-rep-1", ("pkg::nope",)),
            _completion("call-rep-2", ("pkg::nope",)),
            _completion("call-rep-3", ("pkg::nope",)),
        ]
    )


def _verified_model() -> ScriptedModel:
    """A scripted model ending ``delivered`` / ``verified-completion``."""
    return ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _seat("seat-std-1"),
            _seat("seat-spec-1"),
            _completion("call-rep-1"),
        ]
    )


def _run(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    tracker: FakeTracker,
    model: ScriptedModel,
    bare_remote: Path,
    attempt_number: int,
    effective_configuration: dict[str, object],
) -> AttemptResult:
    return run_attempt(
        issue_number=7,
        attempt_number=attempt_number,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=effective_configuration,
        skills_dir=skills_dir,
        remote_url=str(bare_remote),
        agent_identity=IDENTITY,
    )


def _remote_branches(bare_remote: Path) -> list[str]:
    output = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], bare_remote
    )
    return sorted(line for line in output.splitlines() if line.strip())


def _first_model_text(model: ScriptedModel) -> str:
    """Every message text of the scripted model's first call, joined."""
    assert model.invocations, "the model was never called"
    return "\n".join(str(message.content) for message in model.invocations[0])


def _last_opening_text(model: ScriptedModel) -> str:
    """The final message of the first call: the Continuation's extra opening.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §16 (the first step
    opens with the Pinned Prefix and the previous snapshot — or, where none
    verifies, the branch-and-diff note). The extra opening rides last, after
    the Attempt Header and the Target Issue.
    """
    assert model.invocations, "the model was never called"
    opening = model.invocations[0]
    assert len(opening) == 3, f"expected header + issue + continuation extra, got {len(opening)}"
    return str(opening[-1].content)


def _snapshot_json_from_comment(body: str) -> str:
    """The snapshot JSON out of a terminal comment's snapshot block (§15)."""
    assert "## Continuation Snapshot" in body
    fenced = body.split("```json", 1)[1].split("```", 1)[0]
    return fenced.strip()


def _run_first_attempt(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    tracker: FakeTracker,
    bare_remote: Path,
) -> str:
    """Run ``#7/1`` to ``failed`` / ``unverified``; return its comment body."""
    result = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=_unverified_model(),
        bare_remote=bare_remote,
        attempt_number=1,
        effective_configuration=CONFIG_1,
    )
    assert (result.outcome, result.reason) == ("failed", "unverified")
    assert PREV_BRANCH in _remote_branches(bare_remote)
    body = tracker.comments[-1].body
    assert TERMINAL_COMMENT_KIND in body or "Attempt #7/1" in body
    assert "## Continuation Snapshot" in body
    return body


def _advance_main(workspace: Path, files: dict[str, str], message: str) -> str:
    """Commit ``files`` on ``main`` and push it; return the new base sha."""
    run_git(["checkout", "-q", "main"], workspace)
    for relpath, content in files.items():
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", message], workspace)
    run_git(["push", "-q", "origin", "main"], workspace)
    return run_git(["rev-parse", "HEAD"], workspace)


def _sample_snapshot() -> ContinuationSnapshot:
    return ContinuationSnapshot.from_payload(
        {
            "version": 1,
            "attempt_id": "#7/1",
            "base_revision": "a" * 40,
            "branch": PREV_BRANCH,
            "commit": "b" * 40,
            "changed_files": ["greeting.txt"],
            "commands": [{"command": "test_targeted tests/test_greet.py", "result": "passed"}],
            "validation_evidence": [],
            "unresolved_questions": [],
            "next_intended_action": "keep working",
        }
    )


def _render_sample() -> tuple[str, str, str, int]:
    snapshot = _sample_snapshot()
    encoded = snapshot.to_json()
    block = render_snapshot_block(
        snapshot_json=encoded, digest=snapshot.digest, version=snapshot.version
    )
    return block, encoded, snapshot.digest, snapshot.version


def test_p_hnd_1_continuation_rebases_snapshot_and_current_inputs(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-HND-1 + W-PIN-4: the new branch, the snapshot and the current inputs.

    ``#7/2`` runs on its own new branch created from the previous head and
    rebased onto the current Base Revision; its first model input carries the
    verified previous snapshot; the criteria come from the current body; and
    the Attempt pins the current Effective Configuration, not the previous
    Attempt's.
    """
    tracker = FakeTracker()
    comment1 = _run_first_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        bare_remote=bare_remote,
    )
    prev_head = run_git(["rev-parse", PREV_BRANCH], bare_remote)
    snapshot_json = _snapshot_json_from_comment(comment1)

    new_base = _advance_main(workspace, {"human.txt": "human work\n"}, "advance base")
    tracker.issue = replace(tracker.issue, body=BODY_2)

    model2 = _verified_model()
    result2 = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model2,
        bare_remote=bare_remote,
        attempt_number=2,
        effective_configuration=CONFIG_2,
    )
    assert (result2.outcome, result2.reason) == ("delivered", "verified-completion")

    assert NEW_BRANCH in _remote_branches(bare_remote)
    new_head = run_git(["rev-parse", NEW_BRANCH], bare_remote)
    # The new branch is rooted on the current Base Revision ...
    run_git(["merge-base", "--is-ancestor", new_base, new_head], bare_remote)
    # ... and carries the previous Attempt's commit (rebased, so by trailer
    # and content — a rebase rewrites the original sha) beside the new work.
    assert run_git(["rev-parse", prev_head], bare_remote) == prev_head
    new_log = run_git(["log", f"{new_base}..{new_head}", "--format=%B"], bare_remote)
    assert "Attempt: #7/1" in new_log
    assert new_head == run_git(["rev-parse", "HEAD"], workspace)
    assert run_git(["show", f"{new_head}:greeting.txt"], bare_remote) == "hello"
    assert run_git(["show", f"{new_head}:human.txt"], bare_remote) == "human work"

    first_text = _first_model_text(model2)
    assert snapshot_json in first_text
    assert "warm greeting" in first_text
    assert "greet" in first_text
    assert snapshot_json in _last_opening_text(model2)

    expected_digest = digest_effective_configuration(CONFIG_2)
    assert expected_digest != digest_effective_configuration(CONFIG_1)
    assert ledger.get_attempt("#7/2").effective_configuration_digest == expected_digest
    assert f"(digest `{expected_digest}`)" in tracker.comments[-1].body


def test_p_hnd_2_rebase_conflict_ends_without_model_call(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-HND-2: a conflicting base change ends ``failed``/``continuation-conflict``.

    The Attempt gains ``ready-for-human`` and the scripted model — whose
    queue is empty, so any call would raise — is never invoked.
    """
    tracker = FakeTracker()
    _run_first_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        bare_remote=bare_remote,
    )
    prev_head = run_git(["rev-parse", PREV_BRANCH], bare_remote)
    _advance_main(workspace, {"greeting.txt": "human base change\n"}, "conflict")

    model2 = ScriptedModel([])
    result2 = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model2,
        bare_remote=bare_remote,
        attempt_number=2,
        effective_configuration=CONFIG_1,
    )
    assert result2.outcome == "failed"
    assert result2.reason.startswith("continuation-conflict")
    assert model2.invocations == []
    assert ("add_ready_for_human", (7,)) in tracker.calls
    assert "rebase conflict" in tracker.comments[-1].body
    assert run_git(["rev-parse", PREV_BRANCH], bare_remote) == prev_head


def test_p_hnd_3_deleted_previous_branch_starts_from_base(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-HND-3: a deleted previous branch is a fresh start from the Base Revision."""
    tracker = FakeTracker()
    _run_first_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        bare_remote=bare_remote,
    )
    base_revision = run_git(["rev-parse", "main"], bare_remote)
    run_git(["push", "-q", "origin", "--delete", PREV_BRANCH], workspace)
    assert PREV_BRANCH not in _remote_branches(bare_remote)
    run_git(["checkout", "-q", "main"], workspace)

    model2 = _verified_model()
    result2 = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model2,
        bare_remote=bare_remote,
        attempt_number=2,
        effective_configuration=CONFIG_1,
    )
    assert (result2.outcome, result2.reason) == ("delivered", "verified-completion")

    new_head = run_git(["rev-parse", NEW_BRANCH], bare_remote)
    assert base_revision != new_head
    run_git(["merge-base", "--is-ancestor", base_revision, new_head], bare_remote)
    # None of the previous Attempt's commits rode along: the log above the
    # base carries this Attempt's work only.
    new_log = run_git(
        ["log", f"{base_revision}..{new_head}", "--format=%B"], bare_remote
    )
    assert "Attempt: #7/1" not in new_log
    assert "Attempt: #7/2" in new_log

    first_text = _first_model_text(model2)
    assert "no verified snapshot" in first_text
    assert "no verified snapshot" in _last_opening_text(model2)


def test_p_hnd_4_no_snapshot_continues_from_branch_and_diff(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-HND-4: with no snapshot the Attempt continues from the branch and its diff."""
    tracker = FakeTracker()
    comment1 = _run_first_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        bare_remote=bare_remote,
    )
    assert _snapshot_json_from_comment(comment1)
    stripped = comment1.split("## Continuation Snapshot")[0].rstrip() + "\n"
    tracker.comments[-1] = replace(tracker.comments[-1], body=stripped)
    run_git(["checkout", "-q", "main"], workspace)

    model2 = _verified_model()
    result2 = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model2,
        bare_remote=bare_remote,
        attempt_number=2,
        effective_configuration=CONFIG_1,
    )
    assert (result2.outcome, result2.reason) == ("delivered", "verified-completion")

    first_text = _first_model_text(model2)
    assert "no verified snapshot" in first_text
    assert PREV_BRANCH in first_text
    assert "greeting.txt" in first_text
    assert "no verified snapshot" in _last_opening_text(model2)


def test_rec_wrk_3_snapshot_block_verifies_digest_and_version() -> None:
    """REC-WRK-3: the block round-trips; tampering any part reads as absent."""
    block, encoded, digest, version = _render_sample()
    parsed = parse_snapshot_block(block)
    assert parsed is not None
    assert parsed.snapshot_json == encoded
    assert parsed.digest == digest
    assert parsed.version == version

    assert parse_snapshot_block("no snapshot here at all") is None
    assert parse_snapshot_block("## Continuation Snapshot\nnothing else\n") is None

    tampered_json = encoded.replace("keep working", "keep workinx")
    assert tampered_json != encoded
    tampered_block = block.replace(encoded, tampered_json)
    assert parse_snapshot_block(tampered_block) is None

    bad_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    assert parse_snapshot_block(block.replace(digest, bad_digest)) is None

    assert parse_snapshot_block(block.replace("- version: 1", "- version: 2")) is None
    assert parse_snapshot_block(block.replace(digest, "not-a-digest")) is None


def test_rec_wrk_3_tampered_comment_snapshot_is_absent(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """REC-WRK-3: a tampered snapshot in the terminal comment counts as none."""
    tracker = FakeTracker()
    comment1 = _run_first_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        bare_remote=bare_remote,
    )
    snapshot_json = _snapshot_json_from_comment(comment1)
    tampered = snapshot_json.replace("hello", "hallo")
    if tampered == snapshot_json:
        tampered = snapshot_json[:-2] + "x" + snapshot_json[-1:]
    assert tampered != snapshot_json
    tracker.comments[-1] = replace(
        tracker.comments[-1], body=comment1.replace(snapshot_json, tampered)
    )
    run_git(["checkout", "-q", "main"], workspace)

    model2 = _unverified_model()
    result2 = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model2,
        bare_remote=bare_remote,
        attempt_number=2,
        effective_configuration=CONFIG_1,
    )
    assert result2.outcome == "failed"
    first_text = _first_model_text(model2)
    assert "no verified snapshot" in first_text
    assert tampered not in first_text
    assert "no verified snapshot" in _last_opening_text(model2)


def test_w_pin_4_continuation_pins_current_configuration(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """W-PIN-4: the Continuation pins the current Effective Configuration."""
    tracker = FakeTracker()
    _run_first_attempt(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        bare_remote=bare_remote,
    )
    run_git(["checkout", "-q", "main"], workspace)

    model2 = _unverified_model()
    result2 = _run(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        tracker=tracker,
        model=model2,
        bare_remote=bare_remote,
        attempt_number=2,
        effective_configuration=CONFIG_2,
    )
    assert result2.outcome == "failed"

    expected_digest = digest_effective_configuration(CONFIG_2)
    assert expected_digest != digest_effective_configuration(CONFIG_1)
    assert ledger.get_attempt("#7/2").effective_configuration_digest == expected_digest
    comment2 = tracker.comments[-1].body
    assert "Workflow: survey" in comment2
    assert f"(digest `{expected_digest}`)" in comment2
