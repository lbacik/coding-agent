from __future__ import annotations

import base64
from pathlib import Path

import pytest

import coding_agent.implement.toolset as toolset_module
from coding_agent.implement.toolset import build_file_tools, build_test_targeted_tool
from coding_agent.profile.schema import Evidence, ProjectProfile, Toolchain
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.runner import CredentialStrippedCommandRunner, SubprocessCommandRunner


def _tools(root: Path) -> dict[str, object]:
    return {tool.name: tool for tool in build_file_tools(root)}


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


# --- list_directory tool ------------------------------------------------------


def test_list_directory_reports_files_and_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    tools = _tools(tmp_path)

    result = tools["list_directory"].invoke({"path": "."})  # type: ignore[attr-defined]

    assert "f a.txt" in result
    assert "d nested" in result


def test_list_directory_defaults_to_the_workspace_root(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    tools = _tools(tmp_path)

    result = tools["list_directory"].invoke({})  # type: ignore[attr-defined]

    assert "f a.txt" in result


def test_list_directory_reports_an_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    tools = _tools(tmp_path)

    result = tools["list_directory"].invoke({"path": "empty"})  # type: ignore[attr-defined]

    assert result == "(empty directory)"


def test_list_directory_refuses_a_file_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    tools = _tools(tmp_path)

    result = tools["list_directory"].invoke({"path": "a.txt"})  # type: ignore[attr-defined]

    assert result.startswith("Error:")


def test_list_directory_missing_returns_an_error_string_not_a_raise(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    result = tools["list_directory"].invoke({"path": "missing"})  # type: ignore[attr-defined]

    assert result.startswith("Error:")


def test_list_directory_refuses_a_path_that_escapes_the_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    tools = _tools(workspace_root)

    result = tools["list_directory"].invoke({"path": "../outside"})  # type: ignore[attr-defined]

    assert "outside the workspace" in result


# --- workspace navigation tools ---------------------------------------------


def test_find_files_returns_sorted_relative_utf8_files_and_a_cursor(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00\xff")
    tools = _tools(tmp_path)

    result = tools["find_files"].invoke({"pattern": "**/*"})  # type: ignore[attr-defined]

    assert result.splitlines() == ["b.py", "nested/a.py"]


def test_find_files_excludes_gitignored_files_unless_explicitly_requested(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    (tmp_path / "shown.txt").write_text("shown\n", encoding="utf-8")
    (tmp_path / "hidden.secret").write_text("hidden\n", encoding="utf-8")
    tools = _tools(tmp_path)

    default = tools["find_files"].invoke({"pattern": "*"})  # type: ignore[attr-defined]
    included = tools["find_files"].invoke(  # type: ignore[attr-defined]
        {"pattern": "*", "include_ignored": True}
    )

    assert "hidden.secret" not in default
    assert "hidden.secret" in included


def test_find_files_honours_a_nested_gitignore_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text("!keep.secret\n", encoding="utf-8")
    (nested / "keep.secret").write_text("keep\n", encoding="utf-8")
    tools = _tools(tmp_path)

    result = tools["find_files"].invoke({"pattern": "**/*"})  # type: ignore[attr-defined]

    assert "nested/keep.secret" in result


def test_find_files_paginates_from_its_returned_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(toolset_module, "NAVIGATION_RESULT_LIMIT", 2)
    tools = _tools(tmp_path)

    first_page = tools["find_files"].invoke({"pattern": "*.txt"})  # type: ignore[attr-defined]
    cursor = next(line.removeprefix("next_cursor=") for line in first_page.splitlines() if line.startswith("next_cursor="))
    second_page = tools["find_files"].invoke(  # type: ignore[attr-defined]
        {"pattern": "*.txt", "cursor": cursor}
    )

    assert first_page.splitlines()[:2] == ["a.txt", "b.txt"]
    assert second_page == "c.txt"


def test_navigation_tools_reject_forged_cursors_without_raising(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    forged = base64.urlsafe_b64encode(b"[1]").decode("ascii")
    tools = _tools(tmp_path)

    files = tools["find_files"].invoke({"cursor": forged})  # type: ignore[attr-defined]
    matches = tools["search_text"].invoke({"query": "needle", "cursor": forged})  # type: ignore[attr-defined]

    assert files == "Error: invalid cursor"
    assert matches == "Error: invalid cursor"


def test_search_text_returns_stable_one_based_line_references(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("first\nneedle z\nlast\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle a\n", encoding="utf-8")
    tools = _tools(tmp_path)

    result = tools["search_text"].invoke({"query": "needle", "file_globs": ["*.txt"]})  # type: ignore[attr-defined]

    assert result.splitlines() == ["a.txt:1: needle a", "z.txt:2: needle z"]


def test_search_text_treats_the_query_as_a_literal_and_skips_binary_files(tmp_path: Path) -> None:
    (tmp_path / "literal.txt").write_text("a.[b]\n", encoding="utf-8")
    (tmp_path / "binary.txt").write_bytes(b"a.[b]\x00\xff")
    tools = _tools(tmp_path)

    result = tools["search_text"].invoke({"query": ".[", "file_globs": ["*.txt"]})  # type: ignore[attr-defined]

    assert result == "literal.txt:1: a.[b]"


def test_read_lines_returns_the_inclusive_numbered_range_and_total(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    tools = _tools(tmp_path)

    result = tools["read_lines"].invoke(  # type: ignore[attr-defined]
        {"path": "notes.txt", "start_line": 2, "end_line": 3}
    )

    assert result == "total_lines=3\n2: two\n3: three"


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "notes.txt", "start_line": 0},
        {"path": "notes.txt", "start_line": -1},
        {"path": "notes.txt", "start_line": 2, "end_line": 1},
        {"path": "notes.txt", "start_line": 1.5},
    ],
)
def test_read_lines_rejects_invalid_ranges_without_raising(tmp_path: Path, arguments: dict[str, object]) -> None:
    (tmp_path / "notes.txt").write_text("one\n", encoding="utf-8")
    tools = _tools(tmp_path)

    result = tools["read_lines"].invoke(arguments)  # type: ignore[attr-defined]

    assert result.startswith("Error: invalid line range")


def test_read_lines_refuses_binary_and_outside_pointing_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "binary.txt").write_bytes(b"\x00\xff")
    (workspace / "escape.txt").symlink_to(outside)
    tools = _tools(workspace)

    binary = tools["read_lines"].invoke({"path": "binary.txt", "start_line": 1})  # type: ignore[attr-defined]
    escaped = tools["read_lines"].invoke({"path": "escape.txt", "start_line": 1})  # type: ignore[attr-defined]

    assert binary == "Error: 'binary.txt' is not a UTF-8 text file"
    assert "outside the workspace" in escaped


def test_read_lines_returns_a_continuation_when_a_cap_stops_the_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(toolset_module, "READ_LINES_LINE_LIMIT", 2)
    tools = _tools(tmp_path)

    result = tools["read_lines"].invoke({"path": "notes.txt", "start_line": 1})  # type: ignore[attr-defined]

    assert result == "total_lines=3\n1: one\n2: two\nnext_start_line=3"


def test_read_lines_refuses_a_single_line_that_exceeds_the_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "long.txt").write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(toolset_module, "READ_LINES_BYTE_LIMIT", 20)
    tools = _tools(tmp_path)

    result = tools["read_lines"].invoke({"path": "long.txt", "start_line": 1})  # type: ignore[attr-defined]

    assert result == "Error: line 1 exceeds the 20-byte read limit"


@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "edit_file", "list_directory"])
def test_existing_file_tools_refuse_an_outside_pointing_symlink(tmp_path: Path, tool_name: str) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape.txt").symlink_to(outside)
    tools = _tools(workspace)
    arguments: dict[str, dict[str, str]] = {
        "read_file": {"path": "escape.txt"},
        "write_file": {"path": "escape.txt", "content": "changed"},
        "edit_file": {"path": "escape.txt", "old_string": "private", "new_string": "changed"},
        "list_directory": {"path": "escape.txt"},
    }

    result = tools[tool_name].invoke(arguments[tool_name])  # type: ignore[attr-defined]

    assert "outside the workspace" in result
    assert outside.read_text(encoding="utf-8") == "private\n"


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
