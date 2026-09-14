from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool, tool

from coding_agent.profile.schema import ProjectProfile
from coding_agent.validate.harness import CommandContext, run_targeted_test
from coding_agent.validate.results import classify


class PathEscapesWorkspace(Exception):
    """A tool argument names a path outside the workspace root."""


def _resolve_within(root: Path, raw_path: str) -> Path:
    candidate = (root / raw_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathEscapesWorkspace(f"{raw_path!r} resolves outside the workspace")
    return candidate


def _resolve_or_error(root: Path, raw_path: str) -> Path | str:
    """`_resolve_within`, with its refusal turned into the `"Error: ..."`
    string every tool here returns on failure rather than raising — a tool's
    own failure never ends the loop (`implement.loop.run_tool_loop`)."""
    try:
        return _resolve_within(root, raw_path)
    except PathEscapesWorkspace as exc:
        return f"Error: {exc}"


def build_file_tools(workspace_root: Path) -> tuple[BaseTool, ...]:
    """Read, write and edit tools scoped to `workspace_root` — the whole of
    what the model's toolset can touch on disk (contract §1: no GitHub
    mutation of any kind is ever in this set)."""

    @tool
    def read_file(path: str) -> str:
        """Read a text file's whole content. `path` is relative to the
        workspace root."""
        resolved = _resolve_or_error(workspace_root, path)
        if isinstance(resolved, str):
            return resolved
        try:
            return resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: could not read {path!r}: {exc}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write `content` to a file, creating it (and any parent
        directories) or overwriting it if it already exists. `path` is
        relative to the workspace root."""
        resolved = _resolve_or_error(workspace_root, path)
        if isinstance(resolved, str):
            return resolved
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"Error: could not write {path!r}: {exc}"
        return f"wrote {path!r}"

    @tool
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace one exact occurrence of `old_string` with `new_string` in
        a file. `old_string` must occur in the file exactly once; if it
        occurs zero or more than once, no edit is made and an error is
        returned instead. `path` is relative to the workspace root."""
        resolved = _resolve_or_error(workspace_root, path)
        if isinstance(resolved, str):
            return resolved
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: could not read {path!r}: {exc}"
        occurrences = text.count(old_string)
        if occurrences == 0:
            return f"Error: old_string not found in {path!r}"
        if occurrences > 1:
            return (
                f"Error: old_string occurs {occurrences} times in {path!r}; "
                "it must be unique"
            )
        resolved.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        return f"edited {path!r}"

    return (read_file, write_file, edit_file)


def build_test_targeted_tool(profile: ProjectProfile, context: CommandContext) -> BaseTool:
    """`test_targeted`, run through `context.runner` — the caller supplies a
    `CredentialStrippedCommandRunner` so the subprocess never inherits the
    GitHub credential (`L3-IMP-9`)."""

    @tool
    def test_targeted(path: str) -> str:
        """Run the Validation Contract's targeted test command against one
        named test file and report the outcome. `path` is relative to the
        working directory the Validation Contract's commands run in."""
        result = run_targeted_test(profile, context, path)
        outcome = classify(result)
        executed = result.junit.executed if result.junit is not None else None
        failures = sorted(result.junit.failure_ids) if result.junit is not None else []
        return (
            f"command={result.command!r} exit={result.exit_code} outcome={outcome} "
            f"executed={executed} failures={failures}"
        )

    return test_targeted
