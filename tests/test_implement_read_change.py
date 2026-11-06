"""The implementing Step binds the Sub-agents' ``read_change`` reader.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (every implementing
Step binds ``read_change`` — the same tool, with the same behaviour, as the
Sub-agent ``read_change`` — so the implementer reads its own change without
delegating to a Sub-agent) and
[ADR 0025](../../docs/adr/0025-withholding-a-shell-only-disguises-execution.md).

MVP row ids: C-IMP-2, C-IMP-3, C-IMP-4.
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import BaseTool

from conftest import init_origin_repo, run_git

from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.profile.schema import ProjectProfile
from coding_agent.runtime.completion import CriterionEvidence, StepReportRecord
from coding_agent.runtime.graph.subagent import build_read_change_tool
from coding_agent.runtime.graph.tools import build_implement_step_tools
from coding_agent.runtime.result_capping import InMemoryArtifactStore

_HAPPY_PATH_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


def _parse_profile() -> ProjectProfile:
    parsed = parse_profile_yaml(_HAPPY_PATH_PROFILE)
    assert isinstance(parsed, ProjectProfile)
    return parsed


def _base_revision(workspace: Path) -> str:
    return run_git(["rev-parse", "HEAD"], workspace)


def _implement_tools(
    workspace: Path,
    tmp_path: Path,
    skills_dir: Path,
    *,
    base_revision: str,
    is_final: bool,
    store: InMemoryArtifactStore,
) -> tuple[BaseTool, ...]:
    if is_final:
        tools = build_implement_step_tools(
            workspace,
            skills_dir=skills_dir,
            profile=_parse_profile(),
            evidence_dir=tmp_path / "evidence-final",
            criterion_evidence=CriterionEvidence(),
            criteria_ids=("AC-1",),
            artifact_store=store,
            review_base_revision=base_revision,
        )
    else:
        tools = build_implement_step_tools(
            workspace,
            skills_dir=skills_dir,
            profile=_parse_profile(),
            evidence_dir=tmp_path / "evidence-nonfinal",
            criterion_evidence=CriterionEvidence(),
            criteria_ids=("AC-1",),
            artifact_store=store,
            is_final=False,
            step_report_record=StepReportRecord(),
            review_base_revision=base_revision,
        )
    return tools


def _by_name(tools: tuple[BaseTool, ...]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


def test_implement_step_binds_read_change_final_and_nonfinal(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-IMP-2: the bound set contains ``read_change`` in both positions."""
    workspace = tmp_path / "repo"
    init_origin_repo(workspace)
    base = _base_revision(workspace)

    final_store = InMemoryArtifactStore()
    final_tools = _implement_tools(
        workspace, tmp_path, skills_dir, base_revision=base, is_final=True, store=final_store
    )
    final_names = {tool.name for tool in final_tools}
    assert "read_change" in final_names

    nonfinal_store = InMemoryArtifactStore()
    nonfinal_tools = _implement_tools(
        workspace, tmp_path, skills_dir, base_revision=base, is_final=False, store=nonfinal_store
    )
    nonfinal_names = {tool.name for tool in nonfinal_tools}
    assert "read_change" in nonfinal_names


def test_implement_read_change_matches_subagent_on_same_tree(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-IMP-3: same tree, same result; untracked included, ignored excluded."""
    workspace = tmp_path / "repo"
    init_origin_repo(workspace)
    base = _base_revision(workspace)

    (workspace / "README.md").write_text("hello world\n", encoding="utf-8")
    (workspace / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("untracked notes\n", encoding="utf-8")
    (workspace / "debug.log").write_text("noise\n", encoding="utf-8")

    store = InMemoryArtifactStore()
    tools = _implement_tools(
        workspace, tmp_path, skills_dir, base_revision=base, is_final=True, store=store
    )
    by_name = _by_name(tools)
    implement_result = str(by_name["read_change"].invoke({}))

    subagent_tool = build_read_change_tool(
        workspace,
        base,
        artifact_store=store,
        artifact_id="read-change-test-subagent",
    )
    subagent_result = str(subagent_tool.invoke({}))

    assert implement_result == subagent_result
    assert "notes.txt" in implement_result
    assert "untracked notes" in implement_result
    assert "hello world" in implement_result
    assert ".gitignore" in implement_result
    assert "debug.log" not in implement_result


def test_implement_read_change_pointer_stable_across_calls(
    tmp_path: Path, skills_dir: Path
) -> None:
    """C-IMP-4: an oversized change is bounded with a resolving pointer.

    After a second, different ``read_change`` call, the first pointer still
    returns the first call's full diff.
    """
    workspace = tmp_path / "repo"
    init_origin_repo(workspace)
    base = _base_revision(workspace)

    (workspace / "big.txt").write_text("x" * 20_000 + "\n", encoding="utf-8")

    store = InMemoryArtifactStore()
    tools = _implement_tools(
        workspace, tmp_path, skills_dir, base_revision=base, is_final=True, store=store
    )
    by_name = _by_name(tools)
    first = str(by_name["read_change"].invoke({}))
    assert "characters total" in first
    assert "read_result_slice" in first
    first_match = re.search(r"artifact_id='([^']+)'", first)
    assert first_match is not None
    first_id = first_match.group(1)
    assert "implement-read-change-" in first_id

    read_slice = by_name["read_result_slice"]
    full_first = str(read_slice.invoke({"artifact_id": first_id, "start": 0, "end": 10_000_000}))  # type: ignore[union-attr]
    assert "big.txt" in full_first
    assert "x" * 100 in full_first

    (workspace / "big.txt").write_text("y" * 20_000 + "\n", encoding="utf-8")
    second = str(by_name["read_change"].invoke({}))  # type: ignore[union-attr]
    second_match = re.search(r"artifact_id='([^']+)'", second)
    assert second_match is not None
    second_id = second_match.group(1)
    assert second_id != first_id

    full_first_again = str(read_slice.invoke({"artifact_id": first_id, "start": 0, "end": 10_000_000}))  # type: ignore[union-attr]
    assert full_first_again == full_first
    assert "x" * 100 in full_first_again
    assert "y" * 100 not in full_first_again

    full_second = str(read_slice.invoke({"artifact_id": second_id, "start": 0, "end": 10_000_000}))  # type: ignore[union-attr]
    assert "y" * 100 in full_second
