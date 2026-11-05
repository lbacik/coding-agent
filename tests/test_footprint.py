"""The Execution Footprint and the Transcript guarantee (ADR 0018, #27).

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 / §12 (after every
tool call that executes project code — today ``test_targeted`` only — the
runtime records the call's Execution Footprint: each changed workspace path
with its content digest before and after, as a Run Ledger event named in the
tool result; it gates nothing) and §15 (the pull-request body lists the
deduplicated footprint paths, bounded with an explicit truncation marker)
and §17 (the Transcript records every tool call of the implementing Step
with its arguments).

MVP row ids: C-FP-1, C-FP-2, C-FP-3, C-FP-4, C-FP-5, C-FP-6, C-FP-7, C-TRN-1.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.profile.schema import Evidence, ProjectProfile, Toolchain
from coding_agent.runtime.ceilings import UsageTotals
from coding_agent.runtime.completion import CriterionEvidence, decide_implementation_outcome
from coding_agent.runtime.footprint import (
    FOOTPRINT_KIND,
    FootprintEntry,
    footprint_paths_for_attempt,
    record_footprint_event,
    render_footprint_section,
)
from coding_agent.runtime.publication import evaluate_publication_gate, render_pull_request_body
from coding_agent.runtime.toolset import build_test_targeted_tool
from coding_agent.runtime.transcript import TranscriptModel, TranscriptWriter
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.junit import JUnitResult
from coding_agent.validate.runner import ExecutedCommand
from support.scripted_model import ScriptedModel


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


def _init_repo(repo: Path) -> None:
    """A committed git repository standing in for the Target Project checkout."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "agent@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "agent"], check=True
    )


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _SideEffectRunner:
    """A fake command runner that mutates the workspace, then reports."""

    def __init__(
        self,
        effect: Callable[[Path], None],
        *,
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self._effect = effect
        self._exit_code = exit_code
        self._timed_out = timed_out
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        self.calls.append((command, cwd))
        self._effect(cwd)
        return ExecutedCommand(
            command=command,
            exit_code=self._exit_code,
            stdout="",
            stderr="",
            timed_out=self._timed_out,
        )


def _workspace_and_tool(
    tmp_path: Path,
    effect: Callable[[Path], None],
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    seed: Callable[[Path], None] | None = None,
) -> tuple[Path, BaseTool, list[tuple[str, tuple[FootprintEntry, ...]]]]:
    """A git workspace, a ``test_targeted`` tool over it, and its footprint sink."""
    repo = tmp_path / "ws"
    _init_repo(repo)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_app.py").write_text("def test_app(): ...\n", encoding="utf-8")
    if seed is not None:
        seed(repo)
    _commit_all(repo, "seed")
    evidence_dir = tmp_path / "ev"
    context = CommandContext(
        _SideEffectRunner(effect, exit_code=exit_code, timed_out=timed_out),
        repo,
        evidence_dir,
        workspace_root=repo,
    )
    seen: list[tuple[str, tuple[FootprintEntry, ...]]] = []

    def _on_footprint(path: str, entries: tuple[FootprintEntry, ...]) -> None:
        seen.append((path, entries))

    tool = build_test_targeted_tool(
        _profile(), context, attempt_id="#7/1", on_footprint=_on_footprint
    )
    return repo, tool, seen


def _snapshot() -> BudgetSnapshot:
    return BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())


def _modify_app(cwd: Path) -> None:
    (cwd / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")


def test_c_fp_1_modified_workspace_file_is_recorded_and_named(tmp_path: Path) -> None:
    """C-FP-1 (the #25 scenario): executed code modifying a non-test workspace
    file records that path with before/after digests, and names it."""
    before_text = "VALUE = 1\n"
    repo, tool, seen = _workspace_and_tool(tmp_path, _modify_app)
    before_digest = hashlib.sha256(before_text.encode("utf-8")).hexdigest()

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert len(seen) == 1
    call_path, entries = seen[0]
    assert call_path == "tests/test_app.py"
    assert [(entry.path, entry.change) for entry in entries] == [
        ("src/app.py", "modified")
    ]
    assert entries[0].digest_before == before_digest
    assert entries[0].digest_after == _sha256_file(repo / "src" / "app.py")
    assert "Execution Footprint" in result
    assert "src/app.py" in result
    assert "modified" in result


def _modify_app_badly(cwd: Path) -> None:
    (cwd / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")


def test_c_fp_1_failed_run_still_records_its_footprint(tmp_path: Path) -> None:
    """C-FP-1: footprint capture wraps the call, including calls that fail."""
    _, tool, seen = _workspace_and_tool(tmp_path, _modify_app_badly, exit_code=1)

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert len(seen) == 1
    assert [entry.path for entry in seen[0][1]] == ["src/app.py"]
    assert "src/app.py" in result


def _add_and_delete(cwd: Path) -> None:
    (cwd / "src" / "added.py").write_text("NEW = 1\n", encoding="utf-8")
    (cwd / "src" / "app.py").unlink()


def test_c_fp_2_add_and_delete_carry_absent_digests(tmp_path: Path) -> None:
    """C-FP-2: an add has no before digest, a delete has no after digest."""
    (repo, tool, seen) = _workspace_and_tool(tmp_path, _add_and_delete)

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert len(seen) == 1
    by_path = {entry.path: entry for entry in seen[0][1]}
    assert set(by_path) == {"src/added.py", "src/app.py"}
    assert by_path["src/added.py"].change == "added"
    assert by_path["src/added.py"].digest_before is None
    assert by_path["src/added.py"].digest_after == _sha256_file(
        repo / "src" / "added.py"
    )
    assert by_path["src/app.py"].change == "deleted"
    assert by_path["src/app.py"].digest_after is None
    assert by_path["src/app.py"].digest_before is not None
    assert "digest_before" not in by_path["src/added.py"].as_dict()
    assert "digest_after" not in by_path["src/app.py"].as_dict()
    assert "src/added.py" in result
    assert "src/app.py" in result


def test_c_fp_1_timed_out_run_still_records_its_footprint(
    tmp_path: Path,
) -> None:
    """C-FP-1: footprint capture wraps the call, including calls that time
    out."""
    _, tool, seen = _workspace_and_tool(
        tmp_path, _modify_app_badly, timed_out=True
    )

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert len(seen) == 1
    assert [entry.path for entry in seen[0][1]] == ["src/app.py"]
    assert "src/app.py" in result


def test_c_fp_3_empty_footprint_leaves_result_byte_identical(tmp_path: Path) -> None:
    """C-FP-3: a call changing nothing records an empty footprint and gains no
    tool-result part."""
    _, tool, seen = _workspace_and_tool(tmp_path, _no_change)

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert seen[0][1] == ()
    assert "Execution Footprint" not in result


def _no_change(cwd: Path) -> None:
    _ = cwd


def _write_ignored(cwd: Path) -> None:
    (cwd / "__pycache__").mkdir(exist_ok=True)
    (cwd / "__pycache__" / "app.pyc").write_bytes(b"\x00pyc")
    (cwd / ".pytest_cache").mkdir(exist_ok=True)
    (cwd / ".pytest_cache" / "CACHEDIR.TAG").write_text("x\n", encoding="utf-8")


def _seed_gitignore(repo: Path) -> None:
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )


def test_c_fp_4_gitignored_writes_are_invisible(tmp_path: Path) -> None:
    """C-FP-4: writes to gitignored paths never appear in the footprint."""
    repo, tool, seen = _workspace_and_tool(
        tmp_path, _write_ignored, seed=_seed_gitignore
    )

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert seen[0][1] == ()
    assert "Execution Footprint" not in result
    assert (repo / "__pycache__" / "app.pyc").exists()


def test_c_fp_5_dirty_before_paths_count_only_on_further_change(
    tmp_path: Path,
) -> None:
    """C-FP-5: a path dirty before the call counts when rewritten during it,
    not when left alone."""
    repo = tmp_path / "ws"
    _init_repo(repo)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "src" / "untouched.py").write_text("U0\n", encoding="utf-8")
    (repo / "src" / "rewritten.py").write_text("R0\n", encoding="utf-8")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_app.py").write_text("def test_app(): ...\n", encoding="utf-8")
    _commit_all(repo, "seed")
    # Dirty both paths before the call; the call rewrites only one of them.
    (repo / "src" / "untouched.py").write_text("U1\n", encoding="utf-8")
    (repo / "src" / "rewritten.py").write_text("R1\n", encoding="utf-8")
    rewritten_before = hashlib.sha256(b"R1\n").hexdigest()

    def effect(cwd: Path) -> None:
        (cwd / "src" / "untouched.py").write_text("U1\n", encoding="utf-8")
        (cwd / "src" / "rewritten.py").write_text("R2\n", encoding="utf-8")

    evidence_dir = tmp_path / "ev"
    context = CommandContext(
        _SideEffectRunner(effect), repo, evidence_dir, workspace_root=repo
    )
    seen: list[tuple[str, tuple[FootprintEntry, ...]]] = []
    tool = build_test_targeted_tool(
        _profile(),
        context,
        attempt_id="#7/1",
        on_footprint=lambda path, entries: seen.append((path, entries)),
    )

    result = str(tool.invoke({"path": "tests/test_app.py"}))

    assert [entry.path for entry in seen[0][1]] == ["src/rewritten.py"]
    entry = seen[0][1][0]
    assert entry.change == "modified"
    assert entry.digest_before == rewritten_before
    assert entry.digest_after == _sha256_file(repo / "src" / "rewritten.py")
    assert "src/rewritten.py" in result
    assert "untouched" not in result


def test_footprint_event_is_a_run_ledger_event(tmp_path: Path) -> None:
    """§9 / §12: each call's footprint is recorded as a Run Ledger event."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        record_footprint_event(
            ledger,
            "#7/1",
            _snapshot(),
            test_path="tests/test_app.py",
            entries=(
                FootprintEntry(
                    path="src/app.py",
                    change="modified",
                    digest_before="a" * 64,
                    digest_after="b" * 64,
                ),
            ),
        )
        record_footprint_event(
            ledger, "#7/1", _snapshot(), test_path="tests/test_ok.py", entries=()
        )
        records = ledger.records("#7/1")
    finally:
        ledger.close()

    assert [record.kind for record in records] == [FOOTPRINT_KIND, FOOTPRINT_KIND]
    assert records[0].payload["test_path"] == "tests/test_app.py"
    assert records[0].payload["footprint"] == [
        {
            "path": "src/app.py",
            "change": "modified",
            "digest_before": "a" * 64,
            "digest_after": "b" * 64,
        }
    ]
    assert records[1].payload["footprint"] == []


def test_c_fp_6_pr_body_lists_bounded_deduplicated_footprint() -> None:
    """C-FP-6: the §15 body lists deduplicated footprint paths, at most 50,
    with an explicit truncation marker giving the total."""
    assert render_footprint_section([]) == (
        "## Execution Footprint\n"
        "(no workspace paths changed by executed project code)"
    )
    assert render_footprint_section(["src/app.py"]) == (
        "## Execution Footprint\n- src/app.py"
    )
    many = [f"src/file_{index:02d}.py" for index in range(55)]
    section = render_footprint_section(many)
    assert section.splitlines()[1:51] == [f"- {path}" for path in many[:50]]
    assert "... and 5 more (55 total)" in section
    assert "src/file_54.py" not in section.splitlines()


def test_c_fp_6_pr_body_reads_footprint_union_from_the_ledger(
    tmp_path: Path,
) -> None:
    """C-FP-6: the body section is the deduplicated union of footprint events."""
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        record_footprint_event(
            ledger,
            "#7/1",
            _snapshot(),
            test_path="tests/test_a.py",
            entries=(
                FootprintEntry(
                    path="src/b.py",
                    change="modified",
                    digest_before="a" * 64,
                    digest_after="b" * 64,
                ),
                FootprintEntry(
                    path="src/a.py",
                    change="added",
                    digest_before=None,
                    digest_after="c" * 64,
                ),
            ),
        )
        record_footprint_event(
            ledger,
            "#7/1",
            _snapshot(),
            test_path="tests/test_b.py",
            entries=(
                FootprintEntry(
                    path="src/a.py",
                    change="modified",
                    digest_before="c" * 64,
                    digest_after="d" * 64,
                ),
            ),
        )
        paths = footprint_paths_for_attempt(ledger, "#7/1")
    finally:
        ledger.close()

    assert paths == ["src/a.py", "src/b.py"]
    body = render_pull_request_body(
        attempt_id="#7/1",
        issue_number=7,
        workflow_id="implement",
        configuration_digest="abc123",
        base_revision="base-sha",
        base_head="base-sha",
        base_moved=False,
        acceptance_criteria=[{"id": "AC-1", "text": "greet"}],
        claimed_tests={"AC-1": ["pkg::test_ok"]},
        criterion_results={"AC-1": "pass"},
        ever_failed={"AC-1": False},
        validation_commands=[
            {"name": "test_all", "exit_code": 0, "executed": 1, "failure_ids": []}
        ],
        review_status="reviewed",
        review_invocations=1,
        seam_set=["tdd"],
        working_area=".",
        profile_changed=False,
        footprint_paths=paths,
    )
    assert "## Execution Footprint\n- src/a.py\n- src/b.py" in body
    empty_body = render_pull_request_body(
        attempt_id="#7/1",
        issue_number=7,
        workflow_id="implement",
        configuration_digest="abc123",
        base_revision="base-sha",
        base_head="base-sha",
        base_moved=False,
        acceptance_criteria=[{"id": "AC-1", "text": "greet"}],
        claimed_tests={"AC-1": ["pkg::test_ok"]},
        criterion_results={"AC-1": "pass"},
        ever_failed={"AC-1": False},
        validation_commands=[],
        review_status="reviewed",
        review_invocations=1,
        seam_set=["tdd"],
        working_area=".",
        profile_changed=False,
        footprint_paths=[],
    )
    assert "(no workspace paths changed by executed project code)" in empty_body


def test_c_fp_7_footprint_gates_nothing(tmp_path: Path) -> None:
    """C-FP-7: the footprint is never an input to the Implementation Outcome
    or the Publication Gate."""
    assert "footprint" not in inspect.signature(decide_implementation_outcome).parameters
    assert "footprint" not in inspect.signature(evaluate_publication_gate).parameters

    evidence = CriterionEvidence()
    evidence.record_junit(
        JUnitResult(executed=1, failure_ids=frozenset(), passed_ids=frozenset({"pkg::test_ok"}))
    )
    claim: dict[str, tuple[str, ...] | None] = {"AC-1": ("pkg::test_ok",)}
    verdict_before = (
        decide_implementation_outcome(claim, evidence, has_diff=True),
        evaluate_publication_gate(
            is_verified_completion=True,
            fingerprint_match=True,
            review_gate_met=True,
            final_checks_green=True,
            dirty_files=(),
            base_revision="base",
            base_head="base",
        ),
    )
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        record_footprint_event(
            ledger,
            "#7/1",
            _snapshot(),
            test_path="tests/test_app.py",
            entries=(
                FootprintEntry(
                    path="src/app.py",
                    change="modified",
                    digest_before="a" * 64,
                    digest_after="b" * 64,
                ),
            ),
        )
    finally:
        ledger.close()
    verdict_after = (
        decide_implementation_outcome(claim, evidence, has_diff=True),
        evaluate_publication_gate(
            is_verified_completion=True,
            fingerprint_match=True,
            review_gate_met=True,
            final_checks_green=True,
            dirty_files=(),
            base_revision="base",
            base_head="base",
        ),
    )

    assert verdict_before == verdict_after == (
        ("verified completion", "verified-completion"),
        evaluate_publication_gate(
            is_verified_completion=True,
            fingerprint_match=True,
            review_gate_met=True,
            final_checks_green=True,
            dirty_files=(),
            base_revision="base",
            base_head="base",
        ),
    )
    assert verdict_after[1].ready is True


@tool
def probe(value: str) -> str:
    """A probe tool for binding."""
    return value


def _transcript_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_c_trn_1_transcript_records_every_tool_call_with_its_arguments(
    tmp_path: Path,
) -> None:
    """C-TRN-1 (§17): the Transcript records every tool call of the
    implementing Step with its arguments."""
    path = tmp_path / "transcript.jsonl"
    first = AIMessage(
        content="one",
        tool_calls=[
            {"name": "probe", "args": {"value": "x"}, "id": "call_1", "type": "tool_call"}
        ],
    )
    second = AIMessage(
        content="two",
        tool_calls=[
            {"name": "probe", "args": {"value": "y"}, "id": "call_2", "type": "tool_call"}
        ],
    )
    inner = ScriptedModel([first, second])
    model = TranscriptModel(inner, TranscriptWriter(path)).bind_tools([probe])

    opening: list[BaseMessage] = [HumanMessage(content="brief")]
    model.invoke(opening)
    model.invoke([*opening, first, ToolMessage(content="x", tool_call_id="call_1")])

    arg_lists = [
        call["args"]
        for record in _transcript_records(path)
        if record["kind"] == "response"
        for call in record["message"]["tool_calls"]
    ]
    assert arg_lists == [{"value": "x"}, {"value": "y"}]


def test_c_trn_1_transcript_redacts_tool_call_arguments(tmp_path: Path) -> None:
    """C-TRN-1 (§17, §18): registered secrets in tool call arguments reach
    the Transcript only as redaction markers."""
    secret = "s3cr3t-probe-value"
    path = tmp_path / "transcript.jsonl"
    request = AIMessage(
        content="secret",
        tool_calls=[
            {
                "name": "probe",
                "args": {"value": secret},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    model = TranscriptModel(
        ScriptedModel([request]),
        TranscriptWriter(path, redactions={"probe-secret": secret}),
    ).bind_tools([probe])

    model.invoke([HumanMessage(content="brief")])

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "«redacted:probe-secret»" in raw
