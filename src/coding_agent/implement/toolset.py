from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from coding_agent.implement.paths import resolve_within
from coding_agent.implement.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
    InMemoryArtifactStore,
)
from coding_agent.validate.diagnostics import TargetedTestAdapter
from coding_agent.profile.schema import ProjectProfile
from coding_agent.validate.harness import CommandContext


NAVIGATION_RESULT_LIMIT = 100
"""The fixed page size for workspace discovery and text search."""

READ_LINES_LINE_LIMIT = 200
READ_LINES_BYTE_LIMIT = 16 * 1024


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root.resolve()).as_posix()


def _read_utf8_text(path: Path) -> str | None:
    """Return UTF-8 text, or ``None`` for binary and undecodable files."""
    try:
        content = path.read_bytes()
    except OSError:
        return None
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_ignored(root: Path, path: Path) -> bool:
    """Apply the workspace's concise `.gitignore` rules without granting a
    tool any Git or shell capability.

    The Target Project's workspace is the only tree traversed here. Nested
    ignore files deliberately work relative to the directory that owns them;
    negated patterns are supported in their usual last-match-wins order.
    """
    candidate = path.resolve()
    root_resolved = root.resolve()
    ignored = False
    directories = [
        directory
        for directory in reversed(candidate.parents)
        if directory == root_resolved or root_resolved in directory.parents
    ]
    for directory in directories:
        ignore_file = directory / ".gitignore"
        try:
            rules = ignore_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        relative = candidate.relative_to(directory).as_posix()
        for rule in rules:
            rule = rule.strip()
            if not rule or rule.startswith("#"):
                continue
            negated = rule.startswith("!")
            pattern = rule[1:] if negated else rule
            directory_only = pattern.endswith("/")
            pattern = pattern.rstrip("/")
            if not pattern:
                continue
            relative_path = Path(relative)
            matched = relative_path.match(pattern) or (
                "/" not in pattern and relative_path.match(f"**/{pattern}")
            )
            if directory_only:
                matched = any(
                    Path(*relative_path.parts[:index]).match(pattern)
                    for index in range(1, len(relative_path.parts))
                ) or (candidate.is_dir() and matched)
            if matched:
                ignored = not negated
    return ignored


def _workspace_files(root: Path, pattern: str) -> list[Path] | str:
    if Path(pattern).is_absolute():
        return "Error: glob pattern must be relative to the workspace root"
    try:
        candidates = root.glob(pattern)
        files = []
        for candidate in candidates:
            resolved = resolve_within(root, str(candidate.relative_to(root)))
            if resolved is None or not resolved.is_file() or ".git" in candidate.parts:
                continue
            files.append(resolved)
    except (OSError, ValueError) as exc:
        return f"Error: invalid glob pattern {pattern!r}: {exc}"
    return sorted(set(files), key=lambda path: _relative_path(root, path))


def _encode_cursor(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str, key_size: int) -> tuple[Any, ...] | str:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError):
        return "Error: invalid cursor"
    if not isinstance(value, list) or len(value) != key_size:
        return "Error: invalid cursor"
    if key_size == 1 and not isinstance(value[0], str):
        return "Error: invalid cursor"
    if key_size == 2 and (
        not isinstance(value[0], str) or isinstance(value[1], bool) or not isinstance(value[1], int)
    ):
        return "Error: invalid cursor"
    return tuple(value)


def _page(items: list[tuple[tuple[Any, ...], str]], cursor: str | None, key_size: int) -> str:
    start_after: tuple[Any, ...] | None = None
    if cursor is not None:
        decoded = _decode_cursor(cursor, key_size)
        if isinstance(decoded, str):
            return decoded
        start_after = decoded
    selected = [item for item in items if start_after is None or item[0] > start_after]
    page = selected[:NAVIGATION_RESULT_LIMIT]
    rendered = [item[1] for item in page]
    if len(selected) > len(page):
        rendered.append(f"next_cursor={_encode_cursor(list(page[-1][0]))}")
    return "\n".join(rendered) if rendered else "(no matches)"


class PathEscapesWorkspace(Exception):
    """A tool argument names a path outside the workspace root."""


def _resolve_within(root: Path, raw_path: str) -> Path:
    resolved = resolve_within(root, raw_path)
    if resolved is None:
        raise PathEscapesWorkspace(f"{raw_path!r} resolves outside the workspace")
    return resolved


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

    @tool
    def list_directory(path: str = ".") -> str:
        """List a directory's immediate entries -- one per line, `d ` or
        `f ` prefixed for a subdirectory or a file. `path` is relative to
        the workspace root; defaults to the root itself. Use this before
        guessing a path to `read_file`, which errors on a directory."""
        resolved = _resolve_or_error(workspace_root, path)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_dir():
            return f"Error: {path!r} is not a directory"
        try:
            entries = sorted(resolved.iterdir(), key=lambda entry: entry.name)
        except OSError as exc:
            return f"Error: could not list {path!r}: {exc}"
        if not entries:
            return "(empty directory)"
        return "\n".join(f"{'d' if entry.is_dir() else 'f'} {entry.name}" for entry in entries)

    @tool
    def find_files(
        root: str = ".",
        pattern: str = "**/*",
        include_ignored: bool = False,
        cursor: str | None = None,
    ) -> str:
        """Find UTF-8 text files under a relative workspace directory.
        Results are sorted workspace-relative paths, capped deterministically,
        and include `next_cursor` when another page is available. Use this as
        an evidence-oriented discovery step before broad directory walking."""
        resolved_root = _resolve_or_error(workspace_root, root)
        if isinstance(resolved_root, str):
            return resolved_root
        if not resolved_root.is_dir():
            return f"Error: {root!r} is not a directory"
        files = _workspace_files(resolved_root, pattern)
        if isinstance(files, str):
            return files
        paths = [
            path
            for path in files
            if (include_ignored or not _is_ignored(workspace_root, path))
            and _read_utf8_text(path) is not None
        ]
        return _page(
            [((_relative_path(workspace_root, path),), _relative_path(workspace_root, path)) for path in paths],
            cursor,
            1,
        )

    @tool
    def search_text(
        query: str,
        root: str = ".",
        file_globs: list[str] | None = None,
        include_ignored: bool = False,
        cursor: str | None = None,
    ) -> str:
        """Find literal UTF-8 text matches under a relative workspace
        directory. Each result is a stable workspace-relative `path:line:`
        reference for `read_lines`; results are sorted and paged."""
        resolved_root = _resolve_or_error(workspace_root, root)
        if isinstance(resolved_root, str):
            return resolved_root
        if not resolved_root.is_dir():
            return f"Error: {root!r} is not a directory"
        patterns = file_globs or ["**/*"]
        files_by_path: dict[str, Path] = {}
        for pattern in patterns:
            files = _workspace_files(resolved_root, pattern)
            if isinstance(files, str):
                return files
            files_by_path.update({_relative_path(workspace_root, path): path for path in files})
        matches: list[tuple[tuple[Any, ...], str]] = []
        for relative, path in sorted(files_by_path.items()):
            if not include_ignored and _is_ignored(workspace_root, path):
                continue
            text = _read_utf8_text(path)
            if text is None:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(((relative, line_number), f"{relative}:{line_number}: {line}"))
        return _page(matches, cursor, 2)

    @tool
    def read_lines(path: str, start_line: object, end_line: object | None = None) -> str:
        """Read an inclusive, one-based line range from one UTF-8 workspace
        file. The response names its total line count and `next_start_line`
        whenever a fixed line or byte cap stops the requested range."""
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line <= 0
            or (end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int)))
            or (end_line is not None and end_line <= 0)
            or (end_line is not None and end_line < start_line)
        ):
            return "Error: invalid line range; use positive one-based inclusive integers"
        resolved = _resolve_or_error(workspace_root, path)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"Error: {path!r} is not a file"
        text = _read_utf8_text(resolved)
        if text is None:
            return f"Error: {path!r} is not a UTF-8 text file"
        lines = text.splitlines()
        total_lines = len(lines)
        if start_line > total_lines:
            return f"Error: start_line {start_line} exceeds total_lines {total_lines}"
        final_line = min(end_line if end_line is not None else total_lines, total_lines)
        numbered: list[str] = []
        delivered_bytes = len(f"total_lines={total_lines}\n".encode("utf-8"))
        next_start_line: int | None = None
        for line_number in range(start_line, final_line + 1):
            rendered = f"{line_number}: {lines[line_number - 1]}"
            rendered_bytes = len((rendered + "\n").encode("utf-8"))
            if not numbered and delivered_bytes + rendered_bytes > READ_LINES_BYTE_LIMIT:
                return f"Error: line {line_number} exceeds the {READ_LINES_BYTE_LIMIT}-byte read limit"
            if numbered and (
                len(numbered) >= READ_LINES_LINE_LIMIT
                or delivered_bytes + rendered_bytes > READ_LINES_BYTE_LIMIT
            ):
                next_start_line = line_number
                break
            numbered.append(rendered)
            delivered_bytes += rendered_bytes
            if len(numbered) >= READ_LINES_LINE_LIMIT and line_number < final_line:
                next_start_line = line_number + 1
                break
        result = [f"total_lines={total_lines}", *numbered]
        if next_start_line is not None:
            result.append(f"next_start_line={next_start_line}")
        return "\n".join(result)

    return (
        read_file,
        write_file,
        edit_file,
        list_directory,
        find_files,
        search_text,
        read_lines,
    )


def build_test_targeted_tool(
    profile: ProjectProfile,
    context: CommandContext,
    *,
    artifact_store: ArtifactStore | None = None,
    inline_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    attempt_id: str = "attempt",
    redactions: Mapping[str, str] | None = None,
    redact: str | None = None,
) -> BaseTool:
    """`test_targeted`, run through `context.runner` — the caller supplies a
    `CredentialStrippedCommandRunner` so the subprocess never inherits the
    GitHub credential (`L3-IMP-9`)."""

    effective_redactions = dict(redactions or {})
    if redact is not None:
        effective_redactions.setdefault("credential", redact)
    diagnostic_adapter = TargetedTestAdapter(
        profile,
        context,
        artifact_store=artifact_store or InMemoryArtifactStore(),
        inline_limit=inline_limit,
        attempt_id=attempt_id,
        redactions=effective_redactions,
    )

    @tool
    def test_targeted(path: str) -> str:
        """Run the Validation Contract's targeted test command against one
        named test file and report the outcome. `path` is relative to the
        working directory the Validation Contract's commands run in."""
        return diagnostic_adapter.run(path)

    return test_targeted
