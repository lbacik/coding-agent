"""The `readiness` node: Readiness Facts before any model call.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (the workspace is
rebuilt with ``reset --hard`` plus ``clean -fdx``; the Project Profile is
read at the Base Revision and pinned as the Validation Contract; there is
no prose fallback; the Acceptance Criteria come from the single
``## Acceptance criteria`` section; the Seam Set is derived from the Target
Issue alone; a missing or invalid fact ends the Attempt ``failed`` with no
model call) and §14 (the Terminal Outcome is recorded before the terminal
writes).

MVP row ids: P-RDY-1, P-RDY-2, P-RDY-3, C-RDY-1, C-RDY-2, C-WSP-1, L2-12,
L2-13, L2-14, L3-CLR-20, L3-IMP-11.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from conftest import init_origin_repo, run_git
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt, run_step
from coding_agent.runtime.graph.context import AttemptContext
from coding_agent.runtime.readiness import resolve_readiness
from coding_agent.tracker.port import TrackedIssue
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

BASE_PROFILE = """\
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

GOOD_BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


def completion_message() -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"AC-1": ["pkg::test_ok"]},
                "id": "call_completion_1",
                "type": "tool_call",
            }
        ],
    )


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(BASE_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


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


def commit_profile(workspace: Path, text: str) -> None:
    path = workspace / "docs" / "agents" / "project-profile.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run_git(["add", "."], workspace)
    run_git(["commit", "-q", "-m", "update profile"], workspace)


def attempt_with_body(
    ledger: RunLedger,
    workspace: Path,
    skills_dir: Path,
    body: str,
    title: str = "Add a greeting",
) -> tuple[AttemptResult, FakeTracker, ScriptedModel]:
    tracker = FakeTracker(issue=TrackedIssue(number=7, title=title, body=body))
    model = ScriptedModel([completion_message()])
    result = run_attempt(
        issue_number=7,
        attempt_number=1,
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
    )
    return result, tracker, model


def test_p_rdy_1_issue_without_acceptance_criteria_fails_without_a_model_call(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    result, tracker, model = attempt_with_body(ledger, workspace, skills_dir, "No criteria here, just prose.\n")
    assert result.outcome == "failed"
    assert "missing-readiness-fact" in result.reason
    assert "acceptance-criteria" in result.reason
    assert model.invocations == []
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert outcome.outcome == "failed"
    assert "acceptance-criteria" in outcome.reason
    assert tracker.call_names() == [
        "fetch_issue",
        "post_terminal_comment",
        "remove_selection_label",
        "add_ready_for_human",
        "release_assignment",
    ]


def test_p_rdy_2_missing_seam_set_fails_without_a_model_call(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    body = "## Acceptance criteria\n- [ ] greet the user warmly\n"
    result, tracker, model = attempt_with_body(ledger, workspace, skills_dir, body)
    assert result.outcome == "failed"
    assert "missing-readiness-fact" in result.reason
    assert "seam-set" in result.reason
    assert model.invocations == []
    outcome = ledger.get_outcome("#7/1")
    assert outcome is not None
    assert "seam-set" in outcome.reason


def test_p_rdy_3_invalid_profile_fails_without_a_model_call(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    commit_profile(workspace, "key: [unclosed\n")
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "invalid-profile" in result.reason
    assert model.invocations == []


def test_c_rdy_1_profile_without_checks_names_the_missing_key(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    commit_profile(workspace, BASE_PROFILE.replace("checks: none\nservices: []\n", "services: []\n"))
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "missing-readiness-fact" in result.reason
    assert "checks" in result.reason
    assert model.invocations == []


def test_c_rdy_1_profile_without_the_evidence_block_names_the_missing_key(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    profile = BASE_PROFILE.replace(
        'evidence:\n  format: junit-xml\n  test_all: "{evidence_dir}/test_all.xml"\n'
        '  test_targeted: "{evidence_dir}/test_targeted.xml"\n',
        "",
    )
    commit_profile(workspace, profile)
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "missing-readiness-fact" in result.reason
    assert "evidence" in result.reason
    assert model.invocations == []


def test_c_rdy_1_empty_checks_list_is_rejected_as_ambiguous(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    commit_profile(workspace, BASE_PROFILE.replace("checks: none", "checks: []"))
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "missing-readiness-fact" in result.reason
    assert "checks" in result.reason
    assert model.invocations == []


def test_c_rdy_1_checks_none_is_accepted(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    # Readiness passes, so the Attempt reaches its step (contract §7,
    # C-RDY-1). The M1 completion family decides the Terminal Outcome from
    # the claim and Criterion Evidence alone (§9): with no diff and no
    # passing evidence this is `completion not established`, not a
    # readiness refusal.
    assert model.invocations != []
    assert result.outcome == "failed"
    assert "missing-readiness-fact" not in result.reason
    assert "unsupported-environment" not in result.reason
    assert "red-baseline" not in result.reason


def test_c_rdy_2_no_profile_with_complete_prose_docs_fails_and_reads_no_prose(
    ledger: RunLedger, workspace: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    run_git(["rm", "-q", "docs/agents/project-profile.yml"], workspace)
    run_git(["commit", "-q", "-m", "drop profile"], workspace)
    (workspace / "AGENTS.md").write_text("# Agent docs\nComplete prose.\n", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("# Claude docs\nComplete prose.\n", encoding="utf-8")
    (workspace / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (workspace / "docs" / "agents" / "guide.md").write_text("Complete prose.\n", encoding="utf-8")

    read_names: list[str] = []
    original_read_text = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        read_names.append(self.name)
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "missing-readiness-fact" in result.reason
    assert "project-profile" in result.reason
    assert model.invocations == []
    assert "AGENTS.md" not in read_names
    assert "CLAUDE.md" not in read_names
    assert "guide.md" not in read_names


def test_c_wsp_1_dirty_workspace_is_rebuilt_before_readiness(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    run_git(["commit", "-q", "--allow-empty", "-m", "anchor"], workspace)
    (workspace / ".gitignore").write_text("ignored*.tmp\n", encoding="utf-8")
    run_git(["add", ".gitignore"], workspace)
    run_git(["commit", "-q", "-m", "ignore tmp"], workspace)
    (workspace / "README.md").write_text("dirty\n", encoding="utf-8")
    (workspace / "untracked.txt").write_text("leftover\n", encoding="utf-8")
    (workspace / "ignored001.tmp").write_text("ignored leftover\n", encoding="utf-8")

    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    # The dirty tree is rebuilt before readiness (contract §7, C-WSP-1), so
    # the Attempt reaches its step. The Terminal Outcome is the M1
    # completion family's, not a readiness refusal.
    assert model.invocations != []
    assert "missing-readiness-fact" not in result.reason
    assert "unsupported-environment" not in result.reason
    assert "red-baseline" not in result.reason
    status = subprocess.run(
        ["git", "status", "--porcelain", "--ignored"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""
    assert not (workspace / "untracked.txt").exists()
    assert not (workspace / "ignored001.tmp").exists()


def test_l2_12_out_of_matrix_toolchain_is_unsupported_before_implementation(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    commit_profile(workspace, BASE_PROFILE.replace('python: "3.13"', 'python: "3.9"'))
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "unsupported-environment" in result.reason
    assert "3.9" in result.reason
    assert "3.13.9" in result.reason
    assert model.invocations == []


def test_l2_13_unknown_schema_is_unsupported_environment(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    commit_profile(workspace, BASE_PROFILE.replace("schema: 2", "schema: 99"))
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "unsupported-environment" in result.reason
    assert model.invocations == []


def test_l2_14_unreachable_service_is_unsupported_environment(
    ledger: RunLedger, workspace: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    profile = BASE_PROFILE.replace("services: []", "services:\n  - name: db\n    url_env: DATABASE_URL\n")
    commit_profile(workspace, profile)
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    assert result.outcome == "failed"
    assert "unsupported-environment" in result.reason
    assert model.invocations == []


def test_l3_clr_20_issue_naming_the_interface_proceeds(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    result, _tracker, model = attempt_with_body(ledger, workspace, skills_dir, GOOD_BODY)
    # L3-CLR-20: the Target Issue names `greet`, so the Seam Set resolves and
    # the Attempt proceeds straight to implementation with no comment (no
    # readiness refusal, one model call). The M1 completion family then
    # decides the Terminal Outcome from the claim and Criterion Evidence
    # alone (§9): with no diff and no passing evidence this is `completion
    # not established`, never `delivered`.
    assert len(model.invocations) == 1
    assert result.outcome == "failed"
    assert "missing-readiness-fact" not in result.reason
    assert "unsupported-environment" not in result.reason
    assert "red-baseline" not in result.reason
    assert "criteria-uncovered" in result.reason or "no-completion-evidence" in result.reason


def test_l3_imp_11_run_step_without_a_seam_set_refuses_without_a_model_call(
    ledger: RunLedger, workspace: Path, skills_dir: Path
) -> None:
    tracker = FakeTracker()
    model = ScriptedModel([completion_message()])
    context = AttemptContext(
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=EFFECTIVE_CONFIGURATION,
        skills_dir=skills_dir,
    )
    update = run_step(
        {
            "attempt_id": "#7/1",
            "issue_number": 7,
            "attempt_number": 1,
            "issue_title": "Add a greeting",
            "issue_body": GOOD_BODY,
        },
        {"configurable": context.to_configurable()},
    )
    assert update.get("outcome") == "failed"
    assert "missing-readiness-fact" in str(update.get("reason"))
    assert model.invocations == []


class _ClosedProber:
    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        raise AssertionError("no services are declared, so nothing may be probed")


def test_profile_read_is_pinned_at_the_base_revision(workspace: Path) -> None:
    from coding_agent.runtime.readiness import ReadinessReady, default_toolchain_matrix

    matrix = default_toolchain_matrix()
    first = resolve_readiness(workspace, "Add a greeting", GOOD_BODY, matrix, {}, _ClosedProber())
    assert isinstance(first, ReadinessReady)
    (workspace / "docs" / "agents" / "project-profile.yml").write_text(
        BASE_PROFILE.replace('bootstrap: "true"', 'bootstrap: "true --changed"'),
        encoding="utf-8",
    )
    second = resolve_readiness(workspace, "Add a greeting", GOOD_BODY, matrix, {}, _ClosedProber())
    assert isinstance(second, ReadinessReady)
    assert first == second
