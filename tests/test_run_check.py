"""The implementing Step's diagnostic in-loop check runs (``run_check``).

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (``run_check(name)``
runs one Validation Contract check exactly as pinned: its schema is an enum
generated per Attempt from the Validation Contract's ``checks`` — under
``checks: none`` the tool is not bound at all; it runs the pinned command
with ``{evidence_dir}`` substituted under its own per-Attempt subdirectory,
credential-free (§18), with a process-group kill on timeout; it returns the
exit code and output, redacted and capped like any tool result; where the
check declares ``evidence`` it also returns the parsed JUnit summary) and §9
(each call records an Execution Footprint and sits behind the Ref Guard like
every code-executing tool; its result is recorded in the Run Ledger and is an
input to nothing: not the Completion Claim checks, not the Implementation
Outcome, not the Publication Gate, which ``final_checks`` alone decides §12).

MVP row ids: C-CHK-1, C-CHK-2, C-CHK-3, C-CHK-4, C-CHK-5, C-CHK-6, C-CHK-7,
C-CHK-8, C-CHK-9.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.profile.schema import Check, Evidence, ProjectProfile, Toolchain
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.completion import CriterionEvidence
from coding_agent.runtime.footprint import (
    FOOTPRINT_KIND,
    FootprintEntry,
    record_footprint_event,
)
from coding_agent.runtime.git import (
    attempt_branch_sha,
    head_branch,
    head_revision,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_TOOL_NAMES,
    build_subagent_tools,
)
from coding_agent.runtime.graph.tools import build_implement_step_tools
from coding_agent.runtime.pinned_prefix import compose_mvp_attempt_header
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.runtime.ref_guard import (
    REF_GUARD_KIND,
    RefState,
    record_ref_guard_event,
)
from coding_agent.runtime.run_check import IN_LOOP_CHECK_KIND, record_in_loop_check_event
from coding_agent.runtime.toolset import build_run_check_tool
from coding_agent.runtime.final_checks import final_checks_green, run_final_checks
from coding_agent.validate.harness import CommandContext, run_validation_contract
from coding_agent.validate.junit import JUnitResult
from coding_agent.validate.results import RawCommandResult
from coding_agent.validate.runner import (
    CommandRunner,
    CredentialStrippedCommandRunner,
    ExecutedCommand,
    SubprocessCommandRunner,
)

ATTEMPT_BRANCH = "agent/7/1-greet"


def _profile(*checks: Check) -> ProjectProfile:
    return ProjectProfile(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="true",
        test_all="true",
        test_targeted="run-tests {path}",
        evidence=Evidence(
            format="junit-xml",
            test_all="{evidence_dir}/all.xml",
            test_targeted="{evidence_dir}/targeted.xml",
        ),
        checks=checks,
        services=(),
    )


def _lint_check() -> Check:
    return Check(name="lint", command="echo lint-ok", evidence=None)


def _types_check() -> Check:
    return Check(name="types", command="echo types-ok", evidence=None)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "agent@example.test")
    _git(repo, "config", "user.name", "agent")


def _seed(repo: Path) -> None:
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_app.py").write_text("def test_app(): ...\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")


def _attempt_repo(tmp_path: Path, name: str = "ws") -> Path:
    """A git checkout standing on the Attempt branch with one seed commit."""
    repo = tmp_path / name
    _init_repo(repo)
    _seed(repo)
    _git(repo, "checkout", "-q", "-b", ATTEMPT_BRANCH)
    return repo


def _snapshot() -> BudgetSnapshot:
    return BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())


class _RecordingRunner:
    """A fake runner recording every command it was asked to run."""

    def __init__(self, exit_code: int = 0) -> None:
        self.calls: list[str] = []
        self.exit_code = exit_code

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        self.calls.append(command)
        return ExecutedCommand(
            command=command, exit_code=self.exit_code, stdout="ok\n", stderr="", timed_out=False
        )


def _run_tool(
    repo: Path,
    runner: CommandRunner,
    profile: ProjectProfile,
    evidence_dir: Path,
    ledger: RunLedger | None = None,
    attempt_id: str = "#7/1",
) -> tuple[
    BaseTool,
    list[RawCommandResult],
    list[tuple[str, str, tuple[FootprintEntry, ...]]],
    list[RefState],
]:
    """A ``run_check`` tool with ledger-backed check, footprint and guard sinks."""
    seen_checks: list[RawCommandResult] = []
    footprints: list[tuple[str, str, tuple[FootprintEntry, ...]]] = []
    guards: list[RefState] = []

    def _on_check(result: RawCommandResult) -> None:
        seen_checks.append(result)
        if ledger is not None:
            record_in_loop_check_event(
                ledger,
                attempt_id,
                _snapshot(),
                check_name=result.name,
                command=result.command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                junit=result.junit,
            )

    def _on_footprint(
        tool: str, arg: str, entries: tuple[FootprintEntry, ...]
    ) -> None:
        footprints.append((tool, arg, entries))
        if ledger is not None:
            record_footprint_event(
                ledger, attempt_id, _snapshot(), tool=tool, arg=arg, entries=entries
            )

    def _on_ref_guard(
        tool: str, arg: str, before: RefState, after: RefState
    ) -> None:
        guards.append(before)
        if ledger is not None:
            record_ref_guard_event(
                ledger,
                attempt_id,
                _snapshot(),
                tool=tool,
                arg=arg,
                before=before,
                after=after,
            )

    tool = build_run_check_tool(
        profile,
        repo,
        runner,
        evidence_dir,
        workspace_root=repo,
        attempt_id=attempt_id,
        on_check=_on_check,
        on_footprint=_on_footprint,
        on_ref_guard=_on_ref_guard,
        attempt_branch=ATTEMPT_BRANCH,
    )
    return tool, seen_checks, footprints, guards


def _bound_names(
    repo: Path, skills_dir: Path, profile: ProjectProfile, evidence_dir: Path
) -> set[str]:
    tools = build_implement_step_tools(
        repo,
        skills_dir=skills_dir,
        profile=profile,
        evidence_dir=evidence_dir,
        criterion_evidence=CriterionEvidence(),
        criteria_ids=("AC-1",),
    )
    return {tool.name for tool in tools}


def test_c_chk_1_schema_offers_exactly_declared_checks_and_refuses_unknown(
    tmp_path: Path,
) -> None:
    """C-CHK-1: the schema enum holds exactly the declared check names — never
    ``test_all`` or ``test_targeted`` — and an unknown name is refused with a
    tool result that executes nothing."""
    repo = _attempt_repo(tmp_path)
    evidence_dir = tmp_path / "ev"
    runner = _RecordingRunner()
    tool, seen, _, _ = _run_tool(repo, runner, _profile(_lint_check(), _types_check()), evidence_dir)
    schema = tool.args_schema
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    name_schema = properties["name"]
    assert isinstance(name_schema, dict)
    assert name_schema.get("enum") == ["lint", "types"]
    assert "test_all" not in name_schema.get("enum", [])
    assert "test_targeted" not in name_schema.get("enum", [])

    result = str(tool.invoke({"name": "nope"}))
    assert "unknown check" in result
    assert "Nothing was executed" in result
    assert runner.calls == []
    assert seen == []


def test_c_chk_2_no_checks_means_no_tool_and_subagents_never_bind_it(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-CHK-2: under ``checks: none`` the implementing Step binds no
    ``run_check`` tool — and Sub-agents never bind one either."""
    from coding_agent.runtime.graph.subagent import SubagentRecord

    repo = _attempt_repo(tmp_path)
    evidence_dir = tmp_path / "ev"
    names = _bound_names(repo, skills_dir, _profile(), evidence_dir)
    assert "run_check" not in names
    assert "test_targeted" in names

    checked = _bound_names(repo, skills_dir, _profile(_lint_check()), evidence_dir)
    assert "run_check" in checked

    assert "run_check" not in SUBAGENT_TOOL_NAMES
    subagent_tools = build_subagent_tools(repo, SubagentRecord())
    assert "run_check" not in {tool.name for tool in subagent_tools}


def test_c_chk_3_same_template_as_final_checks_credential_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-CHK-3: ``run_check`` renders its command from the same pinned template
    and code as ``final_checks`` — the rendered commands differ only in
    ``{evidence_dir}`` — and its child environment carries no credential."""
    monkeypatch.setenv("TARGET_TOKEN_TEST", "tok-value")
    monkeypatch.setenv("SNEAKY_CARRIER_TEST", "shared-secret-xyz")
    repo = _attempt_repo(tmp_path)
    evidence_dir = tmp_path / "ev"
    profile = _profile(
        Check(name="lint", command="echo T=${TARGET_TOKEN_TEST:-absent} S=${SNEAKY_CARRIER_TEST:-absent} E={evidence_dir}", evidence=None)
    )
    registry = SecretRegistry(
        {
            "TARGET_TOKEN_TEST": "tok-value",
            "NAMED_SECRET": "shared-secret-xyz",
        }
    )
    tool, seen, _, _ = _run_tool(
        repo, CredentialStrippedCommandRunner(registry), profile, evidence_dir
    )
    result = str(tool.invoke({"name": "lint"}))
    assert "T=absent" in result
    assert "S=absent" in result
    assert "tok-value" not in result
    assert "shared-secret-xyz" not in result
    assert len(seen) == 1
    in_loop_command = seen[0].command
    assert str(evidence_dir / "in-loop-checks" / "1") in in_loop_command

    final_evidence = tmp_path / "final"
    final_results = run_validation_contract(
        profile,
        CommandContext(
            CredentialStrippedCommandRunner(registry), repo, final_evidence, workspace_root=repo
        ),
    )
    final_command = next(item.command for item in final_results if item.name == "lint")
    assert _normalise(
        in_loop_command, str(evidence_dir / "in-loop-checks" / "1")
    ) == _normalise(final_command, str(final_evidence))


def _normalise(command: str, evidence_root: str) -> str:
    return command.replace(evidence_root, "@EVIDENCE@")


def test_c_chk_4_hung_check_killed_at_code_owned_timeout(tmp_path: Path) -> None:
    """C-CHK-4: a hung check's whole process group is killed at the code-owned
    timeout and the call returns a timeout tool result."""
    repo = _attempt_repo(tmp_path)
    tool, seen, _, _ = _run_tool(
        repo,
        SubprocessCommandRunner(timeout_seconds=1),
        _profile(Check(name="lint", command="sleep 30", evidence=None)),
        tmp_path / "ev",
    )
    started = time.monotonic()
    result = str(tool.invoke({"name": "lint"}))
    elapsed = time.monotonic() - started
    assert elapsed < 15
    assert "timed_out: true" in result
    assert "exit_code=124" in result
    assert len(seen) == 1
    assert seen[0].timed_out is True


def test_c_chk_5_evidence_summary_written_under_own_subdirectory(
    tmp_path: Path,
) -> None:
    """C-CHK-5: a check declaring ``evidence`` returns its parsed JUnit
    summary, written under the ``run_check`` evidence subdirectory — never
    ``test_targeted``'s or ``final_checks``'s paths."""
    repo = _attempt_repo(tmp_path)
    evidence_dir = tmp_path / "ev"
    junit_xml = (
        '<testsuite tests="2">'
        '<testcase classname="pkg" name="A"/>'
        '<testcase classname="pkg" name="B"><failure message="bad"/></testcase>'
        "</testsuite>"
    )
    profile = _profile(
        Check(
            name="lint",
            command=f"printf '%s' '{junit_xml}' > {{evidence_dir}}/lint.xml",
            evidence="{evidence_dir}/lint.xml",
        )
    )
    tool, seen, _, _ = _run_tool(repo, SubprocessCommandRunner(), profile, evidence_dir)
    result = str(tool.invoke({"name": "lint"}))
    assert "exit_code=0" in result
    assert "junit: executed=2" in result
    assert "pkg::B" in result
    assert len(seen) == 1
    assert seen[0].junit is not None
    assert seen[0].junit.executed == 2
    assert seen[0].junit.failure_ids == frozenset({"pkg::B"})
    report = evidence_dir / "in-loop-checks" / "1" / "lint.xml"
    assert report.exists()
    assert (evidence_dir / "targeted").exists() is False
    assert (evidence_dir / "final-checks").exists() is False


def test_c_chk_6_in_loop_result_is_an_input_to_nothing(tmp_path: Path) -> None:
    """C-CHK-6: a failing in-loop check records no Criterion Evidence and no
    Validation Evidence — and a green in-loop check followed by a red
    ``final_checks`` still yields a red ``final_checks``."""
    repo = _attempt_repo(tmp_path)
    evidence_dir = tmp_path / "ev"
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        evidence = CriterionEvidence()
        profile = _profile(Check(name="lint", command="echo boom >&2 && exit 3", evidence=None))
        tool, _, _, _ = _run_tool(repo, SubprocessCommandRunner(), profile, evidence_dir, ledger=ledger)
        result = str(tool.invoke({"name": "lint"}))
        assert "exit_code=3" in result
        assert evidence.history() == ()
        kinds = [record.kind for record in ledger.records("#7/1")]
        assert kinds == [IN_LOOP_CHECK_KIND, FOOTPRINT_KIND]
    finally:
        ledger.close()

    gate_profile = _profile(Check(name="lint", command="test ! -f .redflag", evidence=None))
    gate_profile = ProjectProfile(
        schema=gate_profile.schema,
        language=gate_profile.language,
        working_directory=gate_profile.working_directory,
        toolchain=gate_profile.toolchain,
        bootstrap=gate_profile.bootstrap,
        test_all=(
            "printf '%s' '<testsuite tests=\"1\">"
            "<testcase classname=\"t\" name=\"all\"/></testsuite>'"
            " > {evidence_dir}/test_all.xml"
        ),
        test_targeted=gate_profile.test_targeted,
        evidence=gate_profile.evidence,
        checks=gate_profile.checks,
        services=gate_profile.services,
    )
    gate_tool, _, _, _ = _run_tool(
        repo, SubprocessCommandRunner(), gate_profile, tmp_path / "ev2"
    )
    in_loop = str(gate_tool.invoke({"name": "lint"}))
    assert "exit_code=0" in in_loop
    (repo / ".redflag").write_text("red\n", encoding="utf-8")
    results = run_final_checks(
        gate_profile, SubprocessCommandRunner(), repo, tmp_path / "final"
    )
    assert any(item.name == "lint" and item.exit_code != 0 for item in results)
    assert final_checks_green(results) is False


def test_c_chk_7_each_call_writes_one_ledger_event(tmp_path: Path) -> None:
    """C-CHK-7: each ``run_check`` call writes one Run Ledger event carrying
    the check name, the rendered command, the exit code and the timeout flag."""
    repo = _attempt_repo(tmp_path)
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        evidence_dir = tmp_path / "ev"
        tool, _, _, _ = _run_tool(
            repo,
            SubprocessCommandRunner(),
            _profile(_lint_check(), _types_check()),
            evidence_dir,
            ledger=ledger,
        )
        str(tool.invoke({"name": "lint"}))
        str(tool.invoke({"name": "types"}))
        records = [
            record for record in ledger.records("#7/1") if record.kind == IN_LOOP_CHECK_KIND
        ]
        assert len(records) == 2
        first, second = records
        assert first.kind == IN_LOOP_CHECK_KIND
        assert second.kind == IN_LOOP_CHECK_KIND
        payload: dict[str, Any] = first.payload
        assert payload["name"] == "lint"
        assert payload["exit_code"] == 0
        assert payload["timed_out"] is False
        assert "echo lint-ok" in payload["command"]
        assert payload["junit"] is None
        assert second.payload["name"] == "types"
    finally:
        ledger.close()


def test_c_chk_8_footprint_recorded_and_ref_guard_restores(tmp_path: Path) -> None:
    """C-CHK-8: a ``run_check`` that modifies a workspace file records an
    Execution Footprint naming the path; one that moves the branch or HEAD
    triggers the Ref Guard restoration."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        repo = _attempt_repo(tmp_path)
        evidence_dir = tmp_path / "ev"
        profile = _profile(
            Check(name="lint", command="echo 'VALUE = 2' > src/app.py && echo done", evidence=None)
        )
        tool, seen, footprints, _ = _run_tool(
            repo, SubprocessCommandRunner(), profile, evidence_dir, ledger=ledger
        )
        result = str(tool.invoke({"name": "lint"}))
        assert "done" in result
        assert "Execution Footprint" in result
        assert "src/app.py" in result
        assert len(seen) == 1
        assert len(footprints) == 1
        tool_name, arg, entries = footprints[0]
        assert tool_name == "run_check"
        assert arg == "lint"
        assert [(entry.path, entry.change) for entry in entries] == [
            ("src/app.py", "modified")
        ]
        kinds = [record.kind for record in ledger.records("#7/1")]
        assert kinds == [IN_LOOP_CHECK_KIND, FOOTPRINT_KIND]
        assert ledger.records("#7/1")[0].payload["name"] == "lint"

        before = RefState(
            branch=head_branch(repo),
            head_sha=head_revision(repo),
            branch_sha=attempt_branch_sha(repo, ATTEMPT_BRANCH),
        )
        mover = _profile(
            Check(
                name="lint",
                command="git commit -qm model-commit --allow-empty && echo moved",
                evidence=None,
            )
        )
        mover_tool, _, mover_guards, _ = _run_tool(
            repo, SubprocessCommandRunner(), mover, tmp_path / "ev3", ledger=ledger
        )
        mover_result = str(mover_tool.invoke({"name": "lint"}))
        after = RefState(
            branch=head_branch(repo),
            head_sha=head_revision(repo),
            branch_sha=attempt_branch_sha(repo, ATTEMPT_BRANCH),
        )
        assert after == before
        assert len(mover_guards) == 1
        assert "Ref Guard" in mover_result
        assert "restored" in mover_result
        guard_records = [
            record
            for record in ledger.records("#7/1")
            if record.kind == REF_GUARD_KIND
        ]
        assert len(guard_records) == 1
        assert guard_records[0].payload["tool"] == "run_check"
    finally:
        ledger.close()


def test_c_chk_9_header_names_checks_and_diagnostic_rule() -> None:
    """C-CHK-9: the Attempt Header names the declared checks and states the
    diagnostic rule — and says ``run_check`` is unavailable under
    ``checks: none``."""
    header = compose_mvp_attempt_header(
        attempt_id="#7/1",
        base_revision="abc123",
        branch=ATTEMPT_BRANCH,
        acceptance_criteria=(("AC-1", "greet"),),
        seam_set=("greet",),
        target_language="python",
        checks=("lint", "types"),
    )
    assert "lint" in header
    assert "types" in header
    assert "run_check" in header
    assert "diagnostic" in header
    assert "final_checks" in header
    assert "no remediation" in header

    none_header = compose_mvp_attempt_header(
        attempt_id="#7/1",
        base_revision="abc123",
        branch=ATTEMPT_BRANCH,
        acceptance_criteria=(("AC-1", "greet"),),
        seam_set=("greet",),
        target_language="python",
    )
    assert "run_check" in none_header
    assert "unavailable" in none_header


def test_run_check_result_carries_junit_summary_for_evidence_checks(
    tmp_path: Path,
) -> None:
    """The tool result for an evidenced check carries the parsed JUnit summary
    (executed count and failure identifiers), like any tool result carries
    its outcome."""
    repo = _attempt_repo(tmp_path)
    junit_xml = (
        '<testsuite tests="1">'
        '<testcase classname="pkg" name="only"><failure message="bad"/></testcase>'
        "</testsuite>"
    )
    profile = _profile(
        Check(
            name="lint",
            command=f"printf '%s' '{junit_xml}' > {{evidence_dir}}/lint.xml",
            evidence="{evidence_dir}/lint.xml",
        )
    )
    tool, _, _, _ = _run_tool(repo, SubprocessCommandRunner(), profile, tmp_path / "ev")
    result = str(tool.invoke({"name": "lint"}))
    assert "junit: executed=1" in result
    assert "pkg::only" in result


def test_run_check_result_names_junit_and_ledger_sink_sees_it(
    tmp_path: Path,
) -> None:
    """The ``on_check`` sink receives the completed run with its parsed JUnit,
    so the caller can record the Run Ledger event from the same object."""
    from coding_agent.runtime.run_check import record_in_loop_check_event

    repo = _attempt_repo(tmp_path)
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        junit_xml = '<testsuite tests="1"><testcase classname="pkg" name="A"/></testsuite>'
        profile = _profile(
            Check(
                name="lint",
                command=f"printf '%s' '{junit_xml}' > {{evidence_dir}}/lint.xml",
                evidence="{evidence_dir}/lint.xml",
            )
        )
        seen: list[JUnitResult | None] = []

        def _on_check(result: RawCommandResult) -> None:
            seen.append(result.junit)
            record_in_loop_check_event(
                ledger,
                "#7/1",
                _snapshot(),
                check_name=result.name,
                command=result.command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                junit=result.junit,
            )

        tool = build_run_check_tool(
            profile,
            repo,
            SubprocessCommandRunner(),
            tmp_path / "ev",
            workspace_root=repo,
            attempt_id="#7/1",
            on_check=_on_check,
            attempt_branch=ATTEMPT_BRANCH,
        )
        str(tool.invoke({"name": "lint"}))
        assert len(seen) == 1
        assert seen[0] is not None
        assert seen[0] is not None and seen[0].executed == 1
        records = ledger.records("#7/1")
        assert len(records) == 1
        junit_payload = records[0].payload["junit"]
        assert isinstance(junit_payload, dict)
        assert junit_payload["executed"] == 1
        assert junit_payload["failure_ids"] == []
    finally:
        ledger.close()
