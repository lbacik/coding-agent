"""M4 tail, second half: the Publication Gate verdict and the Delivery Intent.

Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the Publication
Gate re-reads the Target Issue and requires, in order, the Implementation
Outcome ``verified completion``, no Drift, the review gate, green final
checks and a clean tree; the first unmet condition names the reason and
every unmet condition is recorded for the terminal comment; base-branch
movement is compared and recorded for disclosure but never blocks, and the
Worker never rebases or merges; ``delivery_intent`` records ``delivered``
as a Delivery Intent before any delivery write), §13 (the write sequence
keyed by ``(attempt_id, write_kind)``; a pull request opens only once),
§14 (the Implementation Outcome → Terminal Outcome table) and §15 (the
pull-request and terminal comment contents, with the Attempt Marker), and
[ADR 0003](../adr/0003-delivery-intent.md) (the Intent is written before
the first delivery write and a resumption finishes it rather than
re-deciding).

MVP row ids: P-DEL-1, P-DEL-2, P-DEL-6, P-DEL-7, P-DEL-8, P-DEL-9,
P-DEL-10, C-PUB-1, C-PUB-2, C-OUT-1, C-ERR-2, L3-DEL-5, L3-DEL-14,
L3-DEL-21, G-TDD-1, L3-REV-10.

Every push here goes to a local bare remote; no test touches the network.

§14 row → scripted run map. Every Implementation Outcome row is reached by
at least one scripted run in this suite (this file unless named): ``verified
completion`` with every gate condition met → ``delivered`` (``test_p_del_7``
below and the M1 happy path); ``verified completion`` with an unmet
condition → ``failed`` / ``drift`` (``test_p_del_6``), ``review-missing``
(the M3 review-return runs, plus ``test_c_pub_2``), ``failing-validation``
(``test_p_del_2``) and ``dirty-tree`` (``test_p_del_8``); ``implemented but
unverified`` → ``failed`` / ``unverified`` and ``already satisfied`` →
``failed`` / ``already-satisfied`` (``test_p_del_9``); ``saved partial
work`` from a ceiling cut pushes the branch with no pull request
(``test_saved_partial_work``); ``completion not established`` passes
through with ``criteria-uncovered``, ``claims-unverified`` and
``undeclared`` end-to-end (``test_p_del_9`` and the step tests) and maps
``no-completion-evidence`` where no claim was made
(``test_no_completion_evidence_mapping``). A crossed ceiling ends the
Attempt ``failed-limit`` naming the limit (contract §11, C-LIM-1, P-LIM-1,
L3-IMP-7): the deterministic finalization commits and pushes the branch
with no final checks, the terminal comment carries the limit, the spend
and the Continuation Snapshot, ``ready-for-human`` is added and no pull
request opens (``test_saved_partial_work_ceiling_cut_pushes_branch_without_pr``;
the four-ceiling scripted proof lives in ``tests/test_mvp_limits.py``).
``soft-stall`` and ``verification-reserve`` as terminal reasons belong to
the M7 Gates slice.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import LedgerRecord, RunLedger
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.completion import CriterionEvidence, decide_implementation_outcome
from coding_agent.runtime.graph.attempt import (
    ATTEMPT_NODE_ORDER,
    AttemptResult,
    run_attempt,
)
from coding_agent.runtime.publication import (
    DELIVERY_INTENT_KIND,
    DIRTY_TREE_REASON,
    DRIFT_REASON,
    FAILING_VALIDATION_REASON,
    PUBLICATION_GATE_KIND,
    PUBLISH_WRITE_KIND,
    REVIEW_MISSING_REASON,
    evaluate_publication_gate,
)
from coding_agent.github.errors import GitHubFailure
from coding_agent.tracker.port import PullRequest, TrackedIssue
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import REVIEW_SKILL_TEXT

IDENTITY = Identity(account_id=999, login="coding-agent")

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

EXPECTED_BRANCH = "agent/7/1-add-a-greeting"


def _profile(
    test_all: str,
    *,
    working_directory: str = ".",
    checks: str = "checks: none",
) -> str:
    return f"""\
schema: 2
language: python
working_directory: {working_directory}
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "{test_all}"
  test_targeted: "mkdir -p {{evidence_dir}} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {{evidence_dir}}/test_targeted.xml && true {{path}}"
evidence:
  format: junit-xml
  test_all: "{{evidence_dir}}/test_all.xml"
  test_targeted: "{{evidence_dir}}/test_targeted.xml"
{checks}
services: []
"""


def _passing_test_all(marker: str = "test_ok") -> str:
    return (
        "mkdir -p {evidence_dir} && printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"1\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"" + marker + "\\\"/>"
        "</testsuite>' > {evidence_dir}/test_all.xml"
    )


def _red_test_all() -> str:
    """Green at the Base Revision, red in the authoritative final run.

    The baseline runs the same pinned contract from its own evidence dir,
    so only the ``.../final-checks`` run fails: the Attempt reaches
    ``verified completion`` and the Publication Gate decides on red final
    checks (P-DEL-2).
    """
    return (
        "mkdir -p {evidence_dir} && case \\\"{evidence_dir}\\\" in "
        "*final-checks*) printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"2\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_ok\\\"/>"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_bad\\\">"
        "<failure message=\\\"boom\\\"/></testcase></testsuite>' "
        "> {evidence_dir}/test_all.xml && false;; "
        "*) printf '%s' "
        "'<testsuite name=\\\"suite\\\" tests=\\\"1\\\">"
        "<testcase classname=\\\"pkg\\\" name=\\\"test_ok\\\"/></testsuite>' "
        "> {evidence_dir}/test_all.xml;; esac"
    )


def _dirtying_test_all() -> str:
    """Green, but leaves a leftover file in the authoritative final run."""
    return (
        _passing_test_all() + " && case \\\"{evidence_dir}\\\" in "
        "*final-checks*) touch leftover.txt;; *) true;; esac"
    )


GREEN_PROFILE = _profile(_passing_test_all())


def _init_remote(
    tmp_path: Path,
    profile_text: str,
    *,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """A local bare remote whose base tree holds the profile (and extras)."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    for relpath, content in (extra_files or {}).items():
        target = src / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(profile_text, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _advance_remote_main(bare: Path, tmp_path: Path) -> str:
    """Push one new commit to the bare remote's ``main`` (§12, P-DEL-7).

    The workspace was cloned before this runs, so its ``HEAD`` — the Base
    Revision ``readiness`` pins — stays behind while the live base head
    moves. The Worker never fetches, rebases or merges it back.
    """
    advancer = tmp_path / "advancer"
    run_git(["clone", "-q", str(bare), str(advancer)], cwd=tmp_path)
    (advancer / "base-note.txt").write_text("the base moved\n", encoding="utf-8")
    run_git(["add", "."], advancer)
    run_git(["commit", "-q", "-m", "advance the base branch"], advancer)
    run_git(["push", "-q", "origin", "HEAD:main"], advancer)
    return run_git(["rev-parse", "HEAD"], advancer)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    return _init_remote(tmp_path, GREEN_PROFILE)


@pytest.fixture
def workspace(tmp_path: Path, bare_remote: Path) -> Path:
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare_remote), str(workdir)], cwd=tmp_path)
    return workdir


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


class _DriftTracker(FakeTracker):
    """A fake tracker whose issue body is edited mid-Attempt (§12, P-DEL-6).

    The Claim reads the original title and body; the Publication Gate's
    re-read sees the edited body, so the Fingerprint no longer matches and
    the verdict is ``drift``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._fetch_count = 0

    def fetch_issue(self, issue_number: int) -> TrackedIssue:
        self._fetch_count += 1
        if self._fetch_count == 2:
            self.issue = TrackedIssue(
                number=self.issue.number,
                title=self.issue.title,
                body=self.issue.body + "\n- [ ] `greet` handles empty names\n",
            )
        self._record("fetch_issue", issue_number)
        return self.issue


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


def _write_only(write_id: str = "call-write-1") -> AIMessage:
    return AIMessage(
        content="writing only",
        tool_calls=[
            _call("write_file", {"path": "greeting.txt", "content": "hello\n"}, write_id),
        ],
    )


def _test_only(test_id: str = "call-test-1") -> AIMessage:
    return AIMessage(
        content="testing only",
        tool_calls=[_call("test_targeted", {"path": "tests/test_greet.py"}, test_id)],
    )


def _completion(
    call_id: str, covering: tuple[str, ...] = ("pkg::test_ok",)
) -> AIMessage:
    return AIMessage(
        content="the change is complete",
        tool_calls=[_call("report_completion", {"AC-1": list(covering)}, call_id)],
    )


def _review(call_id: str = "call-rev-1") -> AIMessage:
    return AIMessage(
        content="reviewing",
        tool_calls=[_call("activate_code_review", {}, call_id)],
    )


def _spawn_pair(s1: str = "call_spawn_1", s2: str = "call_spawn_2") -> AIMessage:
    return AIMessage(
        content="reviewing with subagents",
        tool_calls=[
            _call(
                "spawn_subagent",
                {
                    "description": "review standards",
                    "instructions": "review the change against the Target Issue",
                },
                s1,
            ),
            _call(
                "spawn_subagent",
                {
                    "description": "review spec",
                    "instructions": "review the change against the Target Issue",
                },
                s2,
            ),
        ],
    )


def _report(call_id: str, report: str = "no findings") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[_call("report_subagent", {"report": report}, call_id)],
    )


def _verified_flow() -> ScriptedModel:
    """Activate, write, test, complete a review, then claim full coverage.

    A completed review meets the review gate, so the claim is accepted at
    once with no P-DEL-3 return and the Attempt reaches the gate as
    ``verified completion``.
    """
    return ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            _review(),
            _spawn_pair(),
            _report("sub-report-1"),
            _report("sub-report-2"),
            _completion("call-rep-1"),
        ]
    )


def _run_publication(
    *,
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    model: ScriptedModel,
    bare_remote: Path,
    tracker: FakeTracker | None = None,
    ceilings: LoopCeilings | None = None,
) -> tuple[AttemptResult, FakeTracker]:
    """Run one Attempt with a remote and identity, returning its tracker.

    The anchored ``code-review`` skill lets both reviewer seats run; the
    remote plus identity bind the review-snapshot push behind
    ``activate_code_review`` and the Delivery Snapshot push behind the tail.
    """
    (skills_dir / "code-review" / "SKILL.md").write_text(
        REVIEW_SKILL_TEXT, encoding="utf-8"
    )
    active = tracker if tracker is not None else FakeTracker()
    kwargs: dict[str, object] = {
        "issue_number": 7,
        "attempt_number": 1,
        "tracker": active,
        "ledger": ledger,
        "model": model,
        "workspace": workspace,
        "effective_configuration": EFFECTIVE_CONFIGURATION,
        "skills_dir": skills_dir,
        "remote_url": str(bare_remote),
        "agent_identity": IDENTITY,
    }
    if ceilings is not None:
        kwargs["ceilings"] = ceilings
    return run_attempt(**kwargs), active  # type: ignore[arg-type]


def _remote_branches(bare_remote: Path) -> list[str]:
    output = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], bare_remote
    )
    return sorted(line for line in output.splitlines() if line.strip())


def _events(ledger: RunLedger, kind: str) -> list[LedgerRecord]:
    return [record for record in ledger.records("#7/1") if record.kind == kind]


def _gate_payload(ledger: RunLedger) -> dict[str, object]:
    events = _events(ledger, PUBLICATION_GATE_KIND)
    assert len(events) == 1
    return dict(events[0].payload)


def _intent_payload(ledger: RunLedger) -> dict[str, object]:
    events = _events(ledger, DELIVERY_INTENT_KIND)
    assert len(events) == 1
    return dict(events[0].payload)


def _payload_strs(payload: dict[str, object], key: str) -> list[str]:
    """The string items of a list-valued ledger payload entry (§12, §15)."""
    raw = payload.get(key)
    assert isinstance(raw, list)
    return [str(item) for item in raw]


def _terminal_comment(tracker: FakeTracker) -> str:
    for name, args in tracker.calls:
        if name == "post_terminal_comment":
            return str(args[1])
    raise AssertionError("no terminal comment was posted")


def test_p_del_2_red_final_checks_push_branch_without_pr(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """P-DEL-2: red final checks push the branch but open no pull request."""
    remote = _init_remote(tmp_path, _profile(_red_test_all()))
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("failed", "failing-validation")
    assert EXPECTED_BRANCH in _remote_branches(remote)
    assert ledger.get_effect("#7/1", "delivery-snapshot-push") is not None
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, DELIVERY_INTENT_KIND) == []
    assert "add_ready_for_human" in tracker.call_names()
    payload = _gate_payload(ledger)
    assert payload["ready"] is False
    assert payload["reason"] == FAILING_VALIDATION_REASON
    assert _payload_strs(payload, "unmet") == [FAILING_VALIDATION_REASON]
    assert ledger.get_outcome("#7/1") is not None
    assert _terminal_comment(tracker).find("failing-validation") != -1


def test_p_del_6_issue_body_edit_is_drift(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-DEL-6: an issue body edited mid-Attempt ends ``failed`` / ``drift``."""
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=bare_remote,
        tracker=_DriftTracker(),
    )
    assert (result.outcome, result.reason) == ("failed", "drift")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, DELIVERY_INTENT_KIND) == []
    assert "add_ready_for_human" in tracker.call_names()
    payload = _gate_payload(ledger)
    assert payload["ready"] is False
    assert payload["fingerprint_match"] is False
    assert payload["reason"] == DRIFT_REASON
    assert _payload_strs(payload, "unmet") == [DRIFT_REASON]


def test_p_del_7_base_advance_still_delivers_with_movement_recorded(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """P-DEL-7: an advanced base branch still yields the ready verdict."""
    remote = _init_remote(tmp_path, GREEN_PROFILE)
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    base_revision = run_git(["rev-parse", "HEAD"], workdir)
    base_head = _advance_remote_main(remote, tmp_path)
    assert base_head != base_revision
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    payload = _gate_payload(ledger)
    assert payload["ready"] is True
    assert payload["base_moved"] is True
    assert payload["base_revision"] == base_revision
    assert payload["base_head"] == base_head
    intent = _intent_payload(ledger)
    assert intent["verdict"] == "verified-completion"
    assert str(intent["pull_request_body"]).find("the base has moved since Claim") != -1
    assert "add_ready_for_human" not in tracker.call_names()


def test_p_del_8_dirty_tree_after_green_checks_opens_no_pr(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """P-DEL-8: a dirty tree after green final checks ends ``failed``."""
    remote = _init_remote(tmp_path, _profile(_dirtying_test_all()))
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=remote,
    )
    assert (result.outcome, result.reason) == ("failed", "dirty-tree")
    assert EXPECTED_BRANCH in _remote_branches(remote)
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, DELIVERY_INTENT_KIND) == []
    assert "add_ready_for_human" in tracker.call_names()
    payload = _gate_payload(ledger)
    assert payload["ready"] is False
    assert payload["reason"] == DIRTY_TREE_REASON
    assert _payload_strs(payload, "unmet") == [DIRTY_TREE_REASON]
    assert _payload_strs(payload, "dirty_files") == ["leftover.txt"]


def test_p_del_9_implemented_but_unverified_opens_no_pr(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-DEL-9a: a force-accepted claim with a diff ends ``failed``."""
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test("call-write-1", "call-test-1"),
            _completion("call-rep-1", ("pkg::nope",)),
            _completion("call-rep-2", ("pkg::nope",)),
            _completion("call-rep-3", ("pkg::nope",)),
        ]
    )
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "unverified")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, PUBLICATION_GATE_KIND) == []
    assert "add_ready_for_human" in tracker.call_names()


def test_p_del_9_already_satisfied_opens_no_pr(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-DEL-9b: no diff with complete passing evidence is already enough."""
    model = ScriptedModel(
        [
            _activate(),
            _test_only("call-test-1"),
            _completion("call-rep-1"),
        ]
    )
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "already-satisfied")
    assert _remote_branches(bare_remote) == ["main"]
    assert ledger.get_effect("#7/1", "delivery-snapshot-push") is None
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, PUBLICATION_GATE_KIND) == []
    assert "add_ready_for_human" in tracker.call_names()


def test_p_del_9_completion_not_established_opens_no_pr(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-DEL-9c: an explicit ``uncovered`` claim is never a pull request."""
    model = ScriptedModel(
        [
            _activate(),
            _write_only("call-write-1"),
            _completion("call-rep-1", ("uncovered",)),
            _completion("call-rep-2", ("uncovered",)),
        ]
    )
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "criteria-uncovered")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, PUBLICATION_GATE_KIND) == []


def test_p_del_9_force_accepted_claim_without_diff_is_claims_unverified(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-DEL-9d: a force-accepted claim with no diff names every failure."""
    model = ScriptedModel(
        [
            _activate(),
            _completion("call-rep-1", ("pkg::nope",)),
            _completion("call-rep-2", ("pkg::nope",)),
            _completion("call-rep-3", ("pkg::nope",)),
        ]
    )
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
    )
    assert (result.outcome, result.reason) == ("failed", "claims-unverified")
    assert _remote_branches(bare_remote) == ["main"]
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, PUBLICATION_GATE_KIND) == []


def test_no_completion_evidence_mapping() -> None:
    """§14: no claim at all is ``completion not established`` (§9, P-DEL-9)."""
    assert decide_implementation_outcome(None, CriterionEvidence(), has_diff=False) == (
        "completion not established",
        "no-completion-evidence",
    )
    assert decide_implementation_outcome(None, CriterionEvidence(), has_diff=True) == (
        "completion not established",
        "no-completion-evidence",
    )


def test_c_pub_2_first_unmet_condition_names_the_reason(
    ledger: RunLedger, skills_dir: Path, tmp_path: Path
) -> None:
    """C-PUB-2: drift beats review-missing beats red checks; all are listed."""
    remote = _init_remote(tmp_path, _profile(_red_test_all()))
    workdir = tmp_path / "workspace"
    run_git(["clone", "-q", str(remote), str(workdir)], cwd=tmp_path)
    model = ScriptedModel(
        [
            _activate(),
            _write_and_test(),
            # No review is ever invoked: the first claim is returned for
            # the missing review and the second is accepted, so the gate
            # still sees drift, no completed review and red final checks.
            _completion("call-rep-1"),
            _completion("call-rep-2"),
        ]
    )
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workdir,
        skills_dir=skills_dir,
        model=model,
        bare_remote=remote,
        tracker=_DriftTracker(),
    )
    assert (result.outcome, result.reason) == ("failed", "drift")
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    payload = _gate_payload(ledger)
    assert payload["ready"] is False
    assert payload["reason"] == DRIFT_REASON
    assert _payload_strs(payload, "unmet") == [
        DRIFT_REASON,
        REVIEW_MISSING_REASON,
        FAILING_VALIDATION_REASON,
    ]
    comment = _terminal_comment(tracker)
    assert "drift" in comment
    assert "review-missing" in comment
    assert "failing-validation" in comment


def test_publication_gate_orders_all_four_unmet_conditions() -> None:
    """§12: with every condition unmet the reason is drift, in gate order."""
    verdict = evaluate_publication_gate(
        is_verified_completion=True,
        fingerprint_match=False,
        review_gate_met=False,
        final_checks_green=False,
        dirty_files=("leftover.txt",),
        base_revision="abc123",
        base_head="def456",
    )
    assert verdict.ready is False
    assert verdict.reason == DRIFT_REASON
    assert verdict.unmet == (
        DRIFT_REASON,
        REVIEW_MISSING_REASON,
        FAILING_VALIDATION_REASON,
        DIRTY_TREE_REASON,
    )
    assert verdict.base_moved is True


def test_saved_partial_work_ceiling_cut_pushes_branch_without_pr(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """§11 + §14: a ceiling cut ends ``failed-limit``; work pushed, no PR.

    The deterministic finalization commits and pushes the branch, no final
    checks run, the terminal comment names the limit with the spend and the
    Continuation Snapshot, and ``ready-for-human`` is added (C-LIM-1,
    P-LIM-1).
    """
    model = ScriptedModel(
        [
            _activate(),
            _write_only("call-write-1"),
        ]
    )
    result, tracker = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        ceilings=LoopCeilings(max_tool_calls=1),
    )
    assert (result.outcome, result.reason) == ("failed-limit", "tool_calls")
    assert EXPECTED_BRANCH in _remote_branches(bare_remote)
    assert ledger.get_effect("#7/1", PUBLISH_WRITE_KIND) is None
    assert _events(ledger, PUBLICATION_GATE_KIND) == []
    assert _events(ledger, "final_checks") == []
    assert "add_ready_for_human" in tracker.call_names()
    stored = ledger.get_outcome("#7/1")
    assert stored is not None
    assert stored.outcome == "failed-limit"
    assert "limit crossed: tool_calls" in stored.terminal_comment_body
    assert "Continuation Snapshot" in stored.terminal_comment_body
    usages = _events(ledger, "usage")
    assert usages, "the ceiling finalization flushes usage first"
    last_usage = usages[-1].payload["usage"]
    assert isinstance(last_usage, dict)
    assert last_usage["tool_calls"] >= 2


def test_delivery_intent_exists_before_publish_performs_anything(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """§12 + ADR 0003: the Intent row predates every delivery write.

    The gate is ready, so ``delivery_intent`` records ``delivered`` — the
    Attempt id, branch, head sha, verdict and the writes the outcome calls
    for — and ``publish`` only then records its ``open-pull-request``
    effect. At the first tracker write (the pull-request open, after both
    nodes) the outcome and the Intent event already exist and agree on the
    head sha; node order (``delivery_intent`` before ``publish``) is
    asserted structurally by the M1 graph test. The terminal comment later
    links the opened pull request (P-DEL-1).
    """
    seen: dict[str, Any] = {}

    def on_first_write(name: str) -> None:
        seen["call"] = name
        seen["outcome"] = ledger.get_outcome("#7/1")
        seen["intent"] = _events(ledger, DELIVERY_INTENT_KIND)
        seen["publish_effect"] = ledger.get_effect("#7/1", PUBLISH_WRITE_KIND)

    assert ATTEMPT_NODE_ORDER.index("delivery_intent") < ATTEMPT_NODE_ORDER.index(
        "publish"
    )
    tracker = FakeTracker(on_first_write=on_first_write)
    result, _ = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=bare_remote,
        tracker=tracker,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert seen["call"] == "open_pull_request"
    outcome = seen["outcome"]
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("delivered", "verified-completion")
    intents = seen["intent"]
    assert isinstance(intents, list) and len(intents) == 1
    intent_payload = dict(intents[0].payload)
    head_sha = run_git(["rev-parse", "HEAD"], workspace)
    assert intent_payload["attempt_id"] == "#7/1"
    assert intent_payload["branch"] == EXPECTED_BRANCH
    assert intent_payload["head_sha"] == head_sha
    assert intent_payload["verdict"] == "verified-completion"
    assert _payload_strs(intent_payload, "writes") == [
        PUBLISH_WRITE_KIND,
        "terminal-comment",
        "remove-selection-label",
        "release-assignment",
    ]
    assert "Closes #7" in str(intent_payload["pull_request_body"])
    assert "delivered" in str(intent_payload["terminal_comment_body"])
    intent_effect = seen["publish_effect"]
    assert intent_effect is not None
    assert intent_effect.status == "intent"
    assert intent_effect.remote_id == head_sha
    effect = ledger.get_effect("#7/1", PUBLISH_WRITE_KIND)
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.remote_id == "1"
    assert effect.payload["pull_request_number"] == 1
    assert effect.payload["pull_request_url"] == "https://tracker.fake/pulls/1"
    final = ledger.get_outcome("#7/1")
    assert final is not None
    assert "https://tracker.fake/pulls/1" in final.terminal_comment_body


def test_p_del_1_delivered_run_opens_one_non_draft_pull_request(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """P-DEL-1, P-DEL-10: a delivered Attempt opens one non-draft pull request.

    The pull request is never a draft, its title is the issue title, its body
    is the recorded §15 body carrying ``Closes #N`` and the Attempt Marker,
    opened from the Attempt's branch against the base branch. The terminal
    comment links it, the Selection Label is removed and ``ready-for-human``
    is not added.
    """
    from coding_agent.runtime.publication import DELIVERY_COMMENT_KIND, attempt_marker

    tracker = FakeTracker()
    result, _ = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=bare_remote,
        tracker=tracker,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    opens = [args for name, args in tracker.calls if name == "open_pull_request"]
    assert len(opens) == 1
    title, head_branch, base_branch, body = opens[0]
    assert title == "Add a greeting"
    assert head_branch == EXPECTED_BRANCH
    assert base_branch == "main"
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert body == outcome.pull_request_body
    assert "Closes #7" in str(body)
    marker = attempt_marker("#7/1", kind=DELIVERY_COMMENT_KIND)
    assert marker in str(body)
    assert tracker.call_names().index("find_pull_request") < tracker.call_names().index(
        "open_pull_request"
    )
    assert len(tracker.pull_requests) == 1
    opened = tracker.pull_requests[0]
    assert opened.draft is False
    assert opened.state == "open"
    assert opened.marker_present is True
    effect = ledger.get_effect("#7/1", PUBLISH_WRITE_KIND)
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.payload["pull_request_number"] == opened.number
    assert effect.payload["pull_request_url"] == opened.url
    assert opened.url in _terminal_comment(tracker)
    assert "remove_selection_label" in tracker.call_names()
    assert "add_ready_for_human" not in tracker.call_names()


def test_c_err_2_unconfirmed_401_opening_the_pull_request_is_infrastructure(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """C-ERR-2: the pull-request write gets a 401 the identity probe does not
    confirm (standing in for the probe verdict, which the adapter tests pin
    down). The write raises infrastructure-class ``github``, never
    ``CredentialFailure``: the Delivery Intent stands, so the Attempt stays
    ``delivered`` with the write intent left without a receipt for recovery
    to finish, and nothing halts the Worker."""

    class Unconfirmed401Tracker(FakeTracker):
        def open_pull_request(
            self, *, title: str, head_branch: str, base_branch: str, body: str
        ) -> PullRequest:
            raise GitHubFailure(
                "POST",
                "/repos/example-owner/example-repo/pulls",
                401,
                reason="infrastructure-failure",
                failure_class="github",
            )

    result, _ = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=bare_remote,
        tracker=Unconfirmed401Tracker(),
    )

    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert (outcome.outcome, outcome.reason) == ("delivered", "verified-completion")
    effect = ledger.get_effect("#7/1", PUBLISH_WRITE_KIND)
    assert effect is not None
    assert effect.status == "intent"


def test_repeat_publish_after_receipt_opens_no_second_pull_request(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """§13: repeating ``publish`` after its receipt makes no second pull request."""
    from coding_agent.runtime.graph.attempt import publish as publish_node
    from coding_agent.runtime.graph.context import AttemptContext

    tracker = FakeTracker()
    model = _verified_flow()
    result, _ = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=model,
        bare_remote=bare_remote,
        tracker=tracker,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    assert len([name for name in tracker.call_names() if name == "open_pull_request"]) == 1
    context = AttemptContext(
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
    )
    head_sha = run_git(["rev-parse", "HEAD"], workspace)
    repeated = publish_node(
        {
            "attempt_id": "#7/1",
            "issue_number": 7,
            "attempt_number": 1,
            "issue_title": "Add a greeting",
            "base_revision": head_sha,
            "base_branch": "main",
            "outcome": "delivered",
        },
        {"configurable": context.to_configurable()},
    )
    assert repeated == {}
    assert len([name for name in tracker.call_names() if name == "open_pull_request"]) == 1


def test_l3_del_21_unmarked_branch_pull_request_is_ignored(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """L3-DEL-21: an unmarked pull request from the same branch is not ours."""
    from coding_agent.tracker.port import PullRequest

    tracker = FakeTracker()
    tracker.pull_requests.append(
        PullRequest(
            number=99,
            url="https://tracker.fake/pulls/99",
            title="a title",
            head_branch=EXPECTED_BRANCH,
            base_branch="main",
            body="a human's pull request without our marker",
            state="open",
            author_login="coding-agent",
            author_id=None,
            marker_present=False,
            draft=False,
        )
    )
    result, _ = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=bare_remote,
        tracker=tracker,
    )
    assert (result.outcome, result.reason) == ("delivered", "verified-completion")
    opens = [args for name, args in tracker.calls if name == "open_pull_request"]
    assert len(opens) == 1
    effect = ledger.get_effect("#7/1", PUBLISH_WRITE_KIND)
    assert effect is not None
    assert effect.payload["pull_request_number"] == 1
    assert effect.payload["pull_request_url"] == "https://tracker.fake/pulls/1"
    assert len(tracker.pull_requests) == 2


def test_l3_del_14_human_closed_pull_request_ends_abandoned(
    ledger: RunLedger, workspace: Path, skills_dir: Path, bare_remote: Path
) -> None:
    """L3-DEL-14: a human-closed pull request from this branch is a withdrawal.

    The Attempt stops cooperatively and ends ``abandoned`` with one comment
    naming the withdrawal; the pull request is never reopened.
    """
    from coding_agent.runtime.publication import (
        DELIVERY_COMMENT_KIND,
        WITHDRAWAL_REASON,
        attempt_marker,
    )
    from coding_agent.tracker.port import PullRequest

    tracker = FakeTracker()
    marker = attempt_marker("#7/1", kind=DELIVERY_COMMENT_KIND)
    tracker.pull_requests.append(
        PullRequest(
            number=9,
            url="https://tracker.fake/pulls/9",
            title="a title",
            head_branch=EXPECTED_BRANCH,
            base_branch="main",
            body=f"Closes #7\n{marker}\n",
            state="open",
            author_login="coding-agent",
            author_id=None,
            marker_present=True,
            draft=False,
        )
    )
    tracker.human_close_pull_request(9)
    result, _ = _run_publication(
        ledger=ledger,
        workspace=workspace,
        skills_dir=skills_dir,
        model=_verified_flow(),
        bare_remote=bare_remote,
        tracker=tracker,
    )
    assert (result.outcome, result.reason) == ("abandoned", WITHDRAWAL_REASON)
    assert "open_pull_request" not in tracker.call_names()
    comment = _terminal_comment(tracker)
    assert "https://tracker.fake/pulls/9" in comment
    assert "will not be reopened" in comment
    effect = ledger.get_effect("#7/1", PUBLISH_WRITE_KIND)
    assert effect is not None
    assert effect.status == "receipt"
    assert effect.payload["pull_request_number"] == 9
    assert effect.payload.get("withdrawal") is True
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert outcome.outcome == "abandoned"
