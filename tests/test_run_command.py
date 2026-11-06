"""The implementing Step's general shell and the Ref Guard (ADR 0025, #196).

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (``run_command`` runs
a command string through ``sh -c`` in the Project Profile's
``working_directory`` with the same credential-free environment
``test_targeted`` gets, a process-group kill on a code-owned timeout the
model may shorten but never lengthen, and network access as a stated limit;
its result carries the exit code, stdout and stderr, redacted and capped;
nothing it runs counts as evidence) and §9 (every call records an Execution
Footprint exactly as ``test_targeted`` does, and the Ref Guard compares the
Attempt's branch and HEAD before and after the call, restoring both with
ref plumbing only where either moved).

MVP row ids: C-RUN-1, C-RUN-2, C-RUN-3, C-RUN-4, C-RUN-5, C-RUN-6, C-FP-8,
C-RG-1, C-RG-2, C-RG-3, C-RG-4, C-RG-5, C-SUB-38.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.profile.schema import Evidence, ProjectProfile, Toolchain
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.completion import CriterionEvidence
from coding_agent.runtime.footprint import (
    FOOTPRINT_KIND,
    FootprintEntry,
    record_footprint_event,
)
from coding_agent.runtime.git import (
    GitFailure,
    attempt_branch_sha,
    head_branch,
    head_revision,
)
from coding_agent.runtime.graph.subagent import (
    SUBAGENT_TOOL_NAMES,
    SubagentRecord,
    build_subagent_tools,
)
from coding_agent.runtime.pinned_prefix import compose_mvp_attempt_header
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.runtime.ref_guard import (
    REF_GUARD_KIND,
    RefGuardFailed,
    RefState,
    record_ref_guard_event,
)
from coding_agent.runtime.toolset import (
    build_run_command_tool,
    build_test_targeted_tool,
)
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.runner import (
    CommandRunner,
    CredentialStrippedCommandRunner,
    ExecutedCommand,
    SubprocessCommandRunner,
)

ATTEMPT_BRANCH = "agent/7/1-greet"


def _profile() -> ProjectProfile:
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
        checks=(),
        services=(),
    )


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
    """A fake runner capturing the per-call timeout it was given."""

    timeout_seconds: float = 60.0

    def __init__(self) -> None:
        self.seen: list[float] = []

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        assert timeout_seconds is not None
        self.seen.append(timeout_seconds)
        return ExecutedCommand(
            command=command, exit_code=0, stdout="ok\n", stderr="", timed_out=False
        )


def _run_tool(
    repo: Path,
    runner: CommandRunner,
    ledger: RunLedger | None = None,
    attempt_id: str = "#7/1",
) -> tuple[BaseTool, list[tuple[str, str, tuple[FootprintEntry, ...]]], list[RefState]]:
    """A ``run_command`` tool with ledger-backed footprint and guard sinks."""
    footprints: list[tuple[str, str, tuple[FootprintEntry, ...]]] = []
    guards: list[RefState] = []

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

    tool = build_run_command_tool(
        repo,
        runner,
        workspace_root=repo,
        attempt_id=attempt_id,
        on_footprint=_on_footprint,
        on_ref_guard=_on_ref_guard,
        attempt_branch=ATTEMPT_BRANCH,
    )
    return tool, footprints, guards


def test_c_run_1_returns_exit_code_stdout_and_stderr_redacted(
    tmp_path: Path,
) -> None:
    """C-RUN-1: ``run_command`` runs in the working directory and returns the
    exit code, stdout and stderr, redacted like any other tool result."""
    repo = _attempt_repo(tmp_path)
    secret_tool = build_run_command_tool(
        repo,
        SubprocessCommandRunner(),
        workspace_root=repo,
        redactions={"LEAKED": "s3cr3t-value"},
        attempt_branch=ATTEMPT_BRANCH,
    )
    result = str(
        secret_tool.invoke(
            {"command": "pwd && echo s3cr3t-value && echo boom >&2 && exit 3"}
        )
    )
    assert "exit_code=3" in result
    assert "timed_out: false" in result
    assert str(repo) in result
    assert "boom" in result
    assert "s3cr3t-value" not in result
    assert "redacted" in result.lower()


def test_c_run_2_environment_carries_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-RUN-2: neither ``target.token_env`` nor ``api_key_env`` nor a
    registered secret under another name reaches the command."""
    monkeypatch.setenv("TARGET_TOKEN_TEST", "tok-value")
    monkeypatch.setenv("PROVIDER_KEY_TEST", "key-value")
    monkeypatch.setenv("SNEAKY_CARRIER_TEST", "shared-secret-xyz")
    repo = _attempt_repo(tmp_path)
    registry = SecretRegistry(
        {
            "TARGET_TOKEN_TEST": "tok-value",
            "PROVIDER_KEY_TEST": "key-value",
            "NAMED_SECRET": "shared-secret-xyz",
        }
    )
    runner = CredentialStrippedCommandRunner(registry)
    tool, _, _ = _run_tool(repo, runner)
    result = str(
        tool.invoke(
            {
                "command": (
                    "echo T=${TARGET_TOKEN_TEST:-absent} "
                    "P=${PROVIDER_KEY_TEST:-absent} "
                    "S=${SNEAKY_CARRIER_TEST:-absent}"
                )
            }
        )
    )
    assert "T=absent" in result
    assert "P=absent" in result
    assert "S=absent" in result
    assert "tok-value" not in result
    assert "key-value" not in result
    assert "shared-secret-xyz" not in result


def test_c_run_3_hung_command_killed_at_code_owned_timeout(
    tmp_path: Path,
) -> None:
    """C-RUN-3: a hung command's whole process group is killed at the
    code-owned timeout and the call returns a timeout result."""
    repo = _attempt_repo(tmp_path)
    tool, _, _ = _run_tool(repo, SubprocessCommandRunner(timeout_seconds=1))
    started = time.monotonic()
    result = str(tool.invoke({"command": "sleep 30"}))
    elapsed = time.monotonic() - started
    assert elapsed < 15
    assert "timed_out: true" in result
    assert "exit_code=124" in result


def test_c_run_4_requested_timeout_clamped_and_honoured(tmp_path: Path) -> None:
    """C-RUN-4: a longer request is clamped to the code-owned timeout; a
    shorter one is honoured."""
    repo = _attempt_repo(tmp_path)
    runner = _RecordingRunner()
    tool, _, _ = _run_tool(repo, runner)
    assert runner.timeout_seconds == 60.0
    str(tool.invoke({"command": "true"}))
    str(tool.invoke({"command": "true", "timeout_seconds": 9999}))
    str(tool.invoke({"command": "true", "timeout_seconds": 5}))
    str(tool.invoke({"command": "true", "timeout_seconds": 0}))
    str(tool.invoke({"command": "true", "timeout_seconds": -3}))
    assert runner.seen == [60.0, 60.0, 5.0, 60.0, 60.0]


def test_c_run_5_test_through_run_command_produces_no_evidence(
    tmp_path: Path,
) -> None:
    """C-RUN-5: a test executed through ``run_command`` produces no Criterion
    Evidence, even where it writes JUnit XML."""
    repo = _attempt_repo(tmp_path)
    evidence_dir = tmp_path / "ev"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = CriterionEvidence()
    tool, _, _ = _run_tool(repo, SubprocessCommandRunner())
    junit = "<testsuite tests=\"1\"><testcase classname=\"a\" name=\"t1\"/></testsuite>"
    result = str(
        tool.invoke(
            {"command": f"printf '{junit}' > {evidence_dir}/targeted.xml && echo ran"}
        )
    )
    assert "ran" in result
    assert (evidence_dir / "targeted.xml").exists()
    assert evidence.history() == ()


def test_c_run_6_attempt_header_states_run_command_and_commit_rules() -> None:
    """C-RUN-6: the Attempt Header states the ``run_command`` role, that
    nothing run through it counts as evidence, and the commit rules."""
    header = compose_mvp_attempt_header(
        attempt_id="#7/1",
        base_revision="abc123",
        branch=ATTEMPT_BRANCH,
        acceptance_criteria=(("AC-1", "greet"),),
        seam_set=("greet",),
        target_language="python",
    )
    assert "run_command" in header
    assert "credential-free" in header or "no credential" in header
    assert "code-owned" in header
    assert "counts as evidence" in header or "counts for nothing" in header
    assert "never commits" in header
    assert "restored" in header


def test_c_fp_8_run_command_footprint_recorded_and_named(tmp_path: Path) -> None:
    """C-FP-8: a ``run_command`` modifying a workspace file records an
    Execution Footprint event naming the path; a no-op call is empty."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        repo = _attempt_repo(tmp_path)
        tool, seen, _ = _run_tool(repo, SubprocessCommandRunner(), ledger=ledger)
        result = str(
            tool.invoke({"command": "echo 'VALUE = 2' > src/app.py && echo done"})
        )
        assert "done" in result
        assert "Execution Footprint" in result
        assert "src/app.py" in result
        assert "modified" in result
        assert len(seen) == 1
        tool_name, arg, entries = seen[0]
        assert tool_name == "run_command"
        assert "src/app.py" in arg
        assert [(entry.path, entry.change) for entry in entries] == [
            ("src/app.py", "modified")
        ]
        records = ledger.records("#7/1")
        kinds = [record.kind for record in records]
        assert kinds == [FOOTPRINT_KIND]
        assert records[0].payload["tool"] == "run_command"
        assert "src/app.py" in str(records[0].payload["arg"])
        footprint_payload = records[0].payload["footprint"]
        assert isinstance(footprint_payload, list)
        assert footprint_payload == [
            {
                "path": "src/app.py",
                "change": "modified",
                "digest_before": entries[0].digest_before,
                "digest_after": entries[0].digest_after,
            }
        ]

        quiet, qseen, _ = _run_tool(repo, SubprocessCommandRunner())
        quiet_result = str(quiet.invoke({"command": "echo steady"}))
        assert "Execution Footprint" not in quiet_result
        assert qseen[0][2] == ()
    finally:
        ledger.close()


def _ref_state(repo: Path) -> RefState:
    return RefState(
        branch=head_branch(repo),
        head_sha=head_revision(repo),
        branch_sha=attempt_branch_sha(repo, ATTEMPT_BRANCH),
    )


def test_c_rg_1_git_commit_is_restored(tmp_path: Path) -> None:
    """C-RG-1: a ``run_command`` running ``git commit`` is followed by a Ref
    Guard restoration: refs back, tree and index as the call left them, a
    ledger event, and the result says so."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        repo = _attempt_repo(tmp_path)
        before = _ref_state(repo)
        tool, _, guards = _run_tool(repo, SubprocessCommandRunner(), ledger=ledger)
        result = str(
            tool.invoke(
                {"command": "echo 'VALUE = 2' > src/app.py && git add -A && git commit -qm model-commit"}
            )
        )
        after = _ref_state(repo)
        assert after == before
        assert head_revision(repo) == before.head_sha
        assert attempt_branch_sha(repo, ATTEMPT_BRANCH) == before.branch_sha
        # The guard restores refs only: the committed tree effect stays.
        assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        assert _git(repo, "status", "--porcelain") != ""
        assert len(guards) == 1
        records = ledger.records("#7/1")
        guard_records = [
            record for record in records if record.kind == REF_GUARD_KIND
        ]
        assert len(guard_records) == 1
        payload = guard_records[0].payload
        assert payload["tool"] == "run_command"
        before_payload = payload["before"]
        after_payload = payload["after"]
        assert isinstance(before_payload, dict)
        assert isinstance(after_payload, dict)
        assert before_payload["branch_sha"] == before.branch_sha
        assert after_payload["branch_sha"] != before.branch_sha
        assert "Ref Guard" in result
        assert "restored" in result
        assert "never commits" in result
    finally:
        ledger.close()


def test_c_rg_2_checkout_new_branch_is_restored(tmp_path: Path) -> None:
    """C-RG-2a: checking out another branch is restored to the Attempt branch."""
    repo = _attempt_repo(tmp_path)
    before = _ref_state(repo)
    tool, _, guards = _run_tool(repo, SubprocessCommandRunner())
    result = str(tool.invoke({"command": "git checkout -qb other-branch"}))
    assert _ref_state(repo) == before
    assert head_branch(repo) == ATTEMPT_BRANCH
    assert _git(repo, "branch", "--list", "other-branch") == "other-branch"
    assert len(guards) == 1
    assert "Ref Guard" in result


def test_c_rg_2_detached_head_is_restored(tmp_path: Path) -> None:
    """C-RG-2b: a detached HEAD is restored to the Attempt branch."""
    repo = _attempt_repo(tmp_path)
    (repo / "second.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "second")
    before = _ref_state(repo)
    first = _git(repo, "rev-parse", "HEAD~1")
    tool, _, guards = _run_tool(repo, SubprocessCommandRunner())
    result = str(tool.invoke({"command": f"git checkout -q {first}"}))
    assert _ref_state(repo) == before
    assert head_branch(repo) == ATTEMPT_BRANCH
    assert head_revision(repo) == before.head_sha
    assert len(guards) == 1
    assert "Ref Guard" in result


def test_c_rg_2_branch_force_from_detached_is_restored(tmp_path: Path) -> None:
    """C-RG-2c: ``git branch -f`` moving the Attempt ref from a detached HEAD
    (no (a)/(b) change) is still restored."""
    repo = _attempt_repo(tmp_path)
    (repo / "second.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "second")
    before = _ref_state(repo)
    assert before.branch == ATTEMPT_BRANCH
    first = _git(repo, "rev-parse", "HEAD~1")
    tool, _, guards = _run_tool(repo, SubprocessCommandRunner())
    result = str(
        tool.invoke(
            {"command": f"git checkout -q --detach HEAD && git branch -f {ATTEMPT_BRANCH} {first}"}
        )
    )
    assert _ref_state(repo) == before
    assert len(guards) == 1
    assert "Ref Guard" in result


class _CommittingRunner:
    """A fake runner whose executed code commits, like a misbehaving test."""

    def run(
        self, command: str, *, cwd: Path, timeout_seconds: float | None = None
    ) -> ExecutedCommand:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "model-commit"],
            cwd=cwd,
            check=True,
        )
        return ExecutedCommand(
            command=command, exit_code=0, stdout="pass\n", stderr="", timed_out=False
        )


def test_c_rg_3_test_targeted_commit_is_restored(tmp_path: Path) -> None:
    """C-RG-3: a ``test_targeted`` call whose executed code commits is
    restored the same way."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        repo = _attempt_repo(tmp_path)
        before = _ref_state(repo)
        guards: list[tuple[str, str, RefState, RefState]] = []

        def _on_ref_guard(
            tool: str, arg: str, before_state: RefState, after_state: RefState
        ) -> None:
            guards.append((tool, arg, before_state, after_state))
            record_ref_guard_event(
                ledger,
                "#7/1",
                _snapshot(),
                tool=tool,
                arg=arg,
                before=before_state,
                after=after_state,
            )

        context = CommandContext(
            _CommittingRunner(), repo, tmp_path / "ev", workspace_root=repo
        )
        targeted = build_test_targeted_tool(
            _profile(),
            context,
            attempt_id="#7/1",
            on_ref_guard=_on_ref_guard,
            attempt_branch=ATTEMPT_BRANCH,
        )
        result = str(targeted.invoke({"path": "tests/test_app.py"}))
        assert _ref_state(repo) == before
        assert len(guards) == 1
        assert guards[0][0] == "test_targeted"
        assert guards[0][1] == "tests/test_app.py"
        assert "Ref Guard" in result
        records = ledger.records("#7/1")
        assert [record.kind for record in records if record.kind == REF_GUARD_KIND]
    finally:
        ledger.close()


def test_c_rg_4_quiet_call_records_no_event_and_result_unchanged(
    tmp_path: Path,
) -> None:
    """C-RG-4: a call moving no ref records no event and its result is
    byte-for-byte what the call returned plus any footprint part."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        repo = _attempt_repo(tmp_path)
        guarded, _, guards = _run_tool(
            repo, SubprocessCommandRunner(), ledger=ledger
        )
        plain = build_run_command_tool(repo, SubprocessCommandRunner())
        command = "echo steady"
        assert str(guarded.invoke({"command": command})) == str(
            plain.invoke({"command": command})
        )
        assert guards == []
        assert [
            record for record in ledger.records("#7/1") if record.kind == REF_GUARD_KIND
        ] == []
    finally:
        ledger.close()


def test_c_rg_5_failed_restoration_is_a_git_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-RG-5: a restoration that fails ends as a git infrastructure failure
    and never carries on with an unowned HEAD."""
    from coding_agent.runtime import ref_guard as ref_guard_module

    def _boom(
        workspace: Path, branch: str, sha: str | None
    ) -> None:
        raise GitFailure("git update-ref failed: stale .git/index.lock")

    monkeypatch.setattr(ref_guard_module, "update_branch_ref", _boom)
    repo = _attempt_repo(tmp_path)
    tool, _, guards = _run_tool(repo, SubprocessCommandRunner())
    with pytest.raises(RefGuardFailed):
        tool.invoke({"command": "git commit -q --allow-empty -m model-commit"})
    assert issubclass(RefGuardFailed, GitFailure)
    assert guards == []


def test_c_sub_38_subagent_has_no_run_command(tmp_path: Path) -> None:
    """C-SUB-38: the Sub-agent bound set carries no shell: a ``run_command``
    call gets ``no such tool``."""
    assert "run_command" not in SUBAGENT_TOOL_NAMES
    repo = _attempt_repo(tmp_path)
    tools = build_subagent_tools(repo, SubagentRecord())
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == set(SUBAGENT_TOOL_NAMES)
    assert "run_command" not in by_name
