from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.implement.toolset import build_file_tools, build_test_targeted_tool
from coding_agent.profile.schema import Evidence, ProjectProfile, Toolchain
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.runner import CredentialStrippedCommandRunner, SubprocessCommandRunner


def _tools(root: Path) -> dict[str, object]:
    read_file, write_file, edit_file = build_file_tools(root)
    return {"read_file": read_file, "write_file": write_file, "edit_file": edit_file}


# --- read/write/edit tools ---------------------------------------------------


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    write_result = tools["write_file"].invoke({"path": "a.txt", "content": "hello\n"})  # type: ignore[attr-defined]
    assert "wrote" in write_result

    read_result = tools["read_file"].invoke({"path": "a.txt"})  # type: ignore[attr-defined]
    assert read_result == "hello\n"


def test_write_file_creates_parent_directories(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools["write_file"].invoke({"path": "nested/dir/a.txt", "content": "x"})  # type: ignore[attr-defined]
    assert (tmp_path / "nested" / "dir" / "a.txt").read_text(encoding="utf-8") == "x"


def test_read_file_missing_returns_an_error_string_not_a_raise(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    result = tools["read_file"].invoke({"path": "missing.txt"})  # type: ignore[attr-defined]
    assert result.startswith("Error:")


def test_edit_file_replaces_a_unique_occurrence(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one two three\n", encoding="utf-8")
    tools = _tools(tmp_path)
    result = tools["edit_file"].invoke(  # type: ignore[attr-defined]
        {"path": "a.txt", "old_string": "two", "new_string": "TWO"}
    )
    assert "edited" in result
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one TWO three\n"


def test_edit_file_refuses_a_non_unique_occurrence(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("two two\n", encoding="utf-8")
    tools = _tools(tmp_path)
    result = tools["edit_file"].invoke(  # type: ignore[attr-defined]
        {"path": "a.txt", "old_string": "two", "new_string": "TWO"}
    )
    assert result.startswith("Error:")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "two two\n"


def test_edit_file_refuses_a_missing_occurrence(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    tools = _tools(tmp_path)
    result = tools["edit_file"].invoke(  # type: ignore[attr-defined]
        {"path": "a.txt", "old_string": "nope", "new_string": "x"}
    )
    assert result.startswith("Error:")


@pytest.mark.parametrize("escaping_path", ["../outside.txt", "/etc/passwd"])
def test_tools_refuse_a_path_that_escapes_the_workspace(tmp_path: Path, escaping_path: str) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    tools = _tools(workspace_root)

    result = tools["read_file"].invoke({"path": escaping_path})  # type: ignore[attr-defined]

    assert "outside the workspace" in result


# --- test_targeted tool -------------------------------------------------------


def _profile(test_targeted_command: str) -> ProjectProfile:
    return ProjectProfile(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="true",
        test_all="true",
        test_targeted=test_targeted_command,
        evidence=Evidence(format="junit-xml", test_all="evidence.xml", test_targeted="evidence.xml"),
        checks=(),
        services=(),
    )


def test_test_targeted_tool_reports_the_outcome(tmp_path: Path) -> None:
    profile = _profile("python3 -c \"import sys; sys.exit(0)\"")
    context = CommandContext(SubprocessCommandRunner(), tmp_path, tmp_path)
    tool = build_test_targeted_tool(profile, context)

    result = tool.invoke({"path": "tests/some_test.py"})

    assert "exit=0" in result


def test_test_targeted_tool_runs_through_a_credential_stripped_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secret")
    profile = _profile(
        "python3 -c \"import os, sys; sys.exit(1 if 'GITHUB_TOKEN' in os.environ else 0)\""
    )
    context = CommandContext(CredentialStrippedCommandRunner(["GITHUB_TOKEN"]), tmp_path, tmp_path)
    tool = build_test_targeted_tool(profile, context)

    result = tool.invoke({"path": "tests/some_test.py"})

    assert "exit=0" in result
