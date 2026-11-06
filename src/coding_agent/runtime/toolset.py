from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from coding_agent.validate.junit import JUnitResult

from langchain_core.tools import BaseTool, StructuredTool, tool

from coding_agent.runtime.paths import resolve_within
from coding_agent.runtime.ref_guard import (
    RefGuardFailed,
    RefState,
    format_ref_guard_suffix,
    read_ref_state,
    restore_ref_state,
)
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
    InMemoryArtifactStore,
)
from coding_agent.validate.diagnostics import TargetedTestAdapter
from coding_agent.profile.schema import ProjectProfile
from coding_agent.runtime.footprint import (
    FootprintEntry,
    capture_footprint,
    collect_dirty_digests,
    format_footprint_suffix,
)
from coding_agent.runtime.redaction import Redactable, registry_from
from coding_agent.runtime.run_check import IN_LOOP_CHECKS_EVIDENCE_SUBDIR
from coding_agent.validate.harness import CommandContext, run_named_check
from coding_agent.validate.results import RawCommandResult
from coding_agent.validate.runner import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    CommandRunner,
    ExecutedCommand,
    clamp_timeout,
)


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
    own failure never ends the step (`runtime.graph.step.run_implement_step`)."""
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


def _emit_footprint(
    on_footprint: Callable[..., None] | None,
    tool: str,
    arg: str,
    entries: tuple[FootprintEntry, ...],
) -> None:
    """Deliver one call's footprint to ``on_footprint`` without failing the call.

    The current shape is ``(tool, arg, entries)``; a ``TypeError`` falls back
    to the retired ``(arg, entries)`` shape so callers bound before
    ``run_command`` existed keep working.
    """
    if on_footprint is None:
        return
    try:
        on_footprint(tool, arg, entries)
    except TypeError:
        try:
            on_footprint(arg, entries)
        except Exception:
            pass
    except Exception:
        pass


def _run_guarded_call(
    *,
    workspace: Path,
    attempt_branch: str | None,
    footprint_root: Path,
    tool: str,
    arg: str,
    on_footprint: Callable[..., None] | None,
    on_ref_guard: Callable[[str, str, RefState, RefState], None] | None,
    call: Callable[[], str],
) -> str:
    """Run one code-executing tool call under the footprint and Ref Guard.

    The shared wrapper ``test_targeted``, ``run_command`` and ``run_check``
    run through: the Execution Footprint is captured
    exactly as ``test_targeted`` always captured it, and the Ref Guard
    compares the Attempt's branch and HEAD before and after the call,
    restoring both with ref plumbing only where either moved. A call that
    moved nothing records no guard event and its result is byte-for-byte
    what ``call`` returned plus any footprint part. A restoration that fails
    raises :class:`RefGuardFailed`, which the step loop lets propagate so the
    Attempt ends ``failed`` / ``infrastructure-failure`` with class ``git``.

    MVP row ids: C-FP-1, C-FP-2, C-FP-3, C-FP-7, C-FP-8,
    C-RG-1, C-RG-2, C-RG-3, C-RG-4, C-RG-5.
    """
    before_ref = (
        read_ref_state(workspace, attempt_branch)
        if attempt_branch is not None
        else None
    )
    try:
        before_fp = collect_dirty_digests(footprint_root)
    except Exception:
        before_fp = {}
    failure: BaseException | None = None
    try:
        result = call()
    except BaseException as exc:
        failure = exc
        result = ""
    try:
        entries = capture_footprint(footprint_root, before_fp)
    except Exception:
        entries = ()
    _emit_footprint(on_footprint, tool, arg, entries)
    suffix = format_footprint_suffix(entries)
    if suffix and result:
        result = result + suffix
    elif suffix:
        result = suffix.lstrip("\n")
    if before_ref is not None:
        after_ref = read_ref_state(workspace, attempt_branch or "")
        if after_ref != before_ref:
            try:
                restore_ref_state(
                    workspace,
                    attempt_branch=attempt_branch or "",
                    before=before_ref,
                )
            except RefGuardFailed:
                raise
            except Exception as exc:
                raise RefGuardFailed(
                    "ref guard could not restore the Attempt refs: "
                    f"{exc}"
                ) from exc
            if on_ref_guard is not None:
                try:
                    on_ref_guard(tool, arg, before_ref, after_ref)
                except Exception:
                    pass
            result = result + format_ref_guard_suffix(tool, before_ref, after_ref)
    if failure is not None:
        raise failure
    return result


def build_test_targeted_tool(
    profile: ProjectProfile,
    context: CommandContext,
    *,
    artifact_store: ArtifactStore | None = None,
    inline_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    attempt_id: str = "attempt",
    redactions: Redactable = None,
    redact: Redactable = None,
    on_junit: Callable[[JUnitResult], None] | None = None,
    on_footprint: Callable[..., None] | None = None,
    on_ref_guard: Callable[[str, str, RefState, RefState], None] | None = None,
    attempt_branch: str | None = None,
) -> BaseTool:
    """`test_targeted`, run through `context.runner` (`L3-IMP-9` owns the
    credential-stripping runner in production; this tool only uses the runner
    it is given).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (the
    runtime-owned targeted-test tool runs the Validation Contract's
    ``test_targeted`` command with ``{path}`` substituted, is the sole
    writer among tools of ``{evidence_dir}``, parses the JUnit result and
    records Criterion Evidence; after the call the runtime records the
    call's Execution Footprint and the Ref Guard restores a moved branch or
    HEAD with ref plumbing only) and §11 (the command runs in its own process
    group with a per-command timeout; a timeout kills the whole group and
    returns as a timeout tool result the model can react to, never as a
    process death).

    ``on_footprint`` receives the tool name, the call's path argument and its
    footprint entries (empty where the call changed nothing) after every call,
    including calls that time out or fail. Where the footprint is non-empty
    the tool result gains one extra part naming the changed paths and their
    change kinds; where it is empty the result is byte-for-byte what the
    diagnostic adapter returned. Footprint capture never fails the call and
    never changes the diagnostic result: it is not an input to the
    Implementation Outcome, the Completion Claim checks or the Publication
    Gate. ``on_ref_guard`` receives the tool name, the path argument and the
    before/after ref states where the guard restored a move.

    MVP row ids: L2-2, L3-ERR-14, P-CMD-1, C-FP-1, C-FP-2, C-FP-3, C-FP-7,
    C-RG-3, C-RG-4.
    """

    base = registry_from(redactions)
    extra = registry_from(redact)
    merged = {**base.as_mapping(), **extra.as_mapping()}
    effective_redactions: Redactable = merged
    diagnostic_adapter = TargetedTestAdapter(
        profile,
        context,
        artifact_store=artifact_store or InMemoryArtifactStore(),
        inline_limit=inline_limit,
        attempt_id=attempt_id,
        redactions=effective_redactions,
        on_junit=on_junit,
    )

    footprint_root = (
        context.workspace_root if context.workspace_root is not None else context.working_directory
    )
    guard_workspace = footprint_root

    @tool
    def test_targeted(path: str) -> str:
        """Run the Validation Contract's targeted test command against one
        named test file and report the outcome. `path` is relative to the
        working directory the Validation Contract's commands run in."""
        return _run_guarded_call(
            workspace=guard_workspace,
            attempt_branch=attempt_branch,
            footprint_root=footprint_root,
            tool="test_targeted",
            arg=path,
            on_footprint=on_footprint,
            on_ref_guard=on_ref_guard,
            call=lambda: diagnostic_adapter.run(path),
        )

    return test_targeted


def build_run_command_tool(
    working_directory: Path,
    runner: CommandRunner,
    *,
    workspace_root: Path | None = None,
    attempt_id: str = "attempt",
    redactions: Redactable = None,
    redact: Redactable = None,
    on_footprint: Callable[..., None] | None = None,
    on_ref_guard: Callable[[str, str, RefState, RefState], None] | None = None,
    attempt_branch: str | None = None,
) -> BaseTool:
    """`run_command`: a general shell in the working directory (§8, ADR 0025).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (the command runs
    through ``sh -c`` in the Project Profile's ``working_directory`` with the
    same credential-free environment ``test_targeted`` gets; the timeout is
    code-owned — a requested ``timeout_seconds`` shorter than the runner's own
    is honoured, a longer or non-positive one is clamped; the result carries
    the exit code, stdout and stderr, redacted and capped like any tool
    result; nothing it runs counts as evidence) and §9 (every call records an
    Execution Footprint exactly as ``test_targeted`` does, and the Ref Guard
    restores a moved branch or HEAD with ref plumbing only).

    The tool never writes ``{evidence_dir}`` and never parses JUnit output:
    a test executed through ``run_command`` produces no Criterion Evidence
    and no Validation Evidence. Network access is allowed, as a stated limit.

    MVP row ids: C-RUN-1, C-RUN-2, C-RUN-3, C-RUN-4, C-RUN-5, C-FP-8,
    C-RG-1, C-RG-2, C-RG-4.
    """
    base = registry_from(redactions)
    extra = registry_from(redact)
    merged = {**base.as_mapping(), **extra.as_mapping()}
    effective: Redactable = merged
    registry = registry_from(effective)

    footprint_root = workspace_root if workspace_root is not None else working_directory
    guard_workspace = footprint_root
    code_owned = getattr(runner, "timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)
    try:
        code_owned_timeout = float(code_owned)
    except (TypeError, ValueError):
        code_owned_timeout = DEFAULT_COMMAND_TIMEOUT_SECONDS

    def _execute(command: str, timeout_seconds: float | None) -> ExecutedCommand:
        effective = clamp_timeout(timeout_seconds, code_owned_timeout)
        try:
            return runner.run(
                command, cwd=working_directory, timeout_seconds=effective
            )
        except TypeError:
            return runner.run(command, cwd=working_directory)

    @tool
    def run_command(command: str, timeout_seconds: float | None = None) -> str:
        """Run a shell command in the working directory and report its outcome.

        `command` runs through `sh -c` in the Project Profile's working
        directory with no credential in its environment and the code-owned
        per-command timeout; `timeout_seconds` may ask for a shorter timeout,
        never a longer one. The result carries the exit code, stdout and
        stderr. Nothing run through this tool counts as evidence, and commits
        are made by the runtime: the branch and HEAD are restored where a
        call moved them.
        """
        effective = clamp_timeout(timeout_seconds, code_owned_timeout)

        def _call() -> str:
            executed = _execute(command, effective)
            redacted_command = registry.redact(command)
            stdout = registry.redact(executed.stdout)
            stderr = registry.redact(executed.stderr)
            lines = [
                f"run_command exit_code={executed.exit_code}",
                f"command: {redacted_command}",
                f"timed_out: {'true' if executed.timed_out else 'false'}",
                "stdout:",
                stdout,
                "stderr:",
                stderr,
            ]
            return "\n".join(lines)

        return _run_guarded_call(
            workspace=guard_workspace,
            attempt_branch=attempt_branch,
            footprint_root=footprint_root,
            tool="run_command",
            arg=command,
            on_footprint=on_footprint,
            on_ref_guard=on_ref_guard,
            call=_call,
        )

    return run_command


def build_run_check_tool(
    profile: ProjectProfile,
    working_directory: Path,
    runner: CommandRunner,
    evidence_dir: Path,
    *,
    workspace_root: Path | None = None,
    attempt_id: str = "attempt",
    redactions: Redactable = None,
    redact: Redactable = None,
    on_check: Callable[[RawCommandResult], None] | None = None,
    on_footprint: Callable[..., None] | None = None,
    on_ref_guard: Callable[[str, str, RefState, RefState], None] | None = None,
    attempt_branch: str | None = None,
) -> BaseTool:
    """`run_check`: run one Validation Contract check exactly as pinned (§8).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (the tool takes one
    argument, ``name``; its schema is an enum generated per Attempt from the
    Validation Contract's ``checks`` — ``test_all`` and ``test_targeted`` are
    never offered; under ``checks: none`` the tool is not bound at all; it
    runs exactly the pinned command, with ``{evidence_dir}`` substituted under
    its own per-Attempt subdirectory, credential-free (§18), with a
    process-group kill on timeout; it returns the exit code and output,
    redacted and capped like any tool result; where the check declares
    ``evidence`` it also returns the parsed JUnit failures) and §9 (each call
    records an Execution Footprint and sits behind the Ref Guard like every
    code-executing tool; its result is recorded in the Run Ledger and is an
    input to nothing).

    The tool reuses the harness's :func:`run_named_check` entry point — the
    same code ``final_checks`` renders through — so the in-loop check and
    ``final_checks`` render from the same pinned template; the only permitted
    difference in the rendered command is the ``{evidence_dir}`` substitution.
    A name outside the declared checks is refused with a tool result and
    executes nothing. ``on_check`` receives each completed run's
    :class:`RawCommandResult` so the caller can record the Run Ledger event;
    like the footprint and guard sinks it never fails the call. The result is
    diagnostic only: it records no Criterion Evidence and no Validation
    Evidence, and the Implementation Outcome, the Completion Claim checks and
    the Publication Gate never read it.

    MVP row ids: C-CHK-1, C-CHK-3, C-CHK-4, C-CHK-5, C-CHK-6, C-CHK-7.
    """

    allowed = tuple(check.name for check in profile.checks)
    allowed_set = frozenset(allowed)
    base = registry_from(redactions)
    extra = registry_from(redact)
    merged = {**base.as_mapping(), **extra.as_mapping()}
    registry = registry_from(merged)

    footprint_root = workspace_root if workspace_root is not None else working_directory
    guard_workspace = footprint_root
    invocation_number = 0

    def _check_result_text(result: RawCommandResult) -> str:
        lines = [
            f"run_check {result.name} exit_code={result.exit_code}",
            f"command: {registry.redact(result.command)}",
            f"timed_out: {'true' if result.timed_out else 'false'}",
            "stdout:",
            registry.redact(result.stdout),
            "stderr:",
            registry.redact(result.stderr),
        ]
        if result.evidence_declared:
            if result.junit is not None:
                failures = sorted(result.junit.failure_ids)
                lines.append(
                    f"junit: executed={result.junit.executed} "
                    f"failure_ids={failures}"
                )
            else:
                lines.append(
                    "junit: evidence declared but no report could be parsed"
                )
        return "\n".join(lines)

    def _run_check(name: str) -> str:
        if name not in allowed_set:
            known = (
                ", ".join(f"`{check}`" for check in allowed)
                if allowed
                else "(none declared)"
            )
            return (
                f"Error: unknown check {name!r}: the pinned Validation "
                f"Contract declares {known}. Nothing was executed."
            )
        nonlocal invocation_number
        invocation_number += 1
        invocation_evidence_dir = (
            evidence_dir / IN_LOOP_CHECKS_EVIDENCE_SUBDIR / str(invocation_number)
        )
        invocation_evidence_dir.mkdir(parents=True, exist_ok=True)
        check_context = CommandContext(
            runner,
            working_directory,
            invocation_evidence_dir,
            workspace_root=workspace_root,
        )

        def _call() -> str:
            result = run_named_check(profile, check_context, name)
            if on_check is not None:
                try:
                    on_check(result)
                except Exception:
                    pass
            return _check_result_text(result)

        return _run_guarded_call(
            workspace=guard_workspace,
            attempt_branch=attempt_branch,
            footprint_root=footprint_root,
            tool="run_check",
            arg=name,
            on_footprint=on_footprint,
            on_ref_guard=on_ref_guard,
            call=_call,
        )

    _run_check.__name__ = "run_check"
    name_property: dict[str, object] = {
        "description": "The Validation Contract check to run, exactly as pinned.",
        "type": "string",
    }
    if allowed:
        name_property["enum"] = list(allowed)
    return StructuredTool.from_function(
        func=_run_check,
        name="run_check",
        description=(
            "Run one Validation Contract check exactly as pinned and report "
            "its outcome. Diagnostic only: the result is not Criterion "
            "Evidence and changes no outcome; `final_checks` runs every check "
            "once against the Delivery Snapshot and decides, with no "
            "remediation round."
        ),
        args_schema={
            "type": "object",
            "properties": {"name": name_property},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
