from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from langchain_core.tools import BaseTool, tool


class ArtifactNotFound(Exception):
    """`read_result_slice` was asked for an artifact id no capped tool
    result ever stored — the model inventing or mis-copying a pointer."""


class ArtifactStore(Protocol):
    """Where a capped tool result's full content lives, keyed by the tool
    call id that produced it — opaque and provider-generated, never a
    path a caller supplies, so nothing here resolves an arbitrary path
    (`L3-IMP-14` is about the skill directory, not this store, but the
    same shape is worth keeping)."""

    def store(self, artifact_id: str, content: str) -> None: ...
    def read(self, artifact_id: str) -> str: ...


class InMemoryArtifactStore:
    """The `ArtifactStore` tests use — no disk, no cleanup."""

    def __init__(self) -> None:
        self._by_id: dict[str, str] = {}

    def store(self, artifact_id: str, content: str) -> None:
        self._by_id[artifact_id] = content

    def read(self, artifact_id: str) -> str:
        try:
            return self._by_id[artifact_id]
        except KeyError:
            raise ArtifactNotFound(artifact_id) from None


@dataclass(frozen=True)
class FilesystemArtifactStore:
    """The production `ArtifactStore`: one file per artifact under
    `root`, named by the tool call id that produced it. `read`'s
    `artifact_id` reaches here as a `read_result_slice` argument the model
    itself supplies — untrusted the same way a tool's `path` argument is
    (`toolset._resolve_within`) — so a candidate that resolves outside
    `root` is refused as not-found rather than read."""

    root: Path

    def _resolved(self, artifact_id: str) -> Path | None:
        candidate = (self.root / artifact_id).resolve()
        root_resolved = self.root.resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            return None
        return candidate

    def store(self, artifact_id: str, content: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        candidate = self._resolved(artifact_id)
        if candidate is None:
            raise ArtifactNotFound(artifact_id)
        candidate.write_text(content, encoding="utf-8")

    def read(self, artifact_id: str) -> str:
        candidate = self._resolved(artifact_id)
        if candidate is None:
            raise ArtifactNotFound(artifact_id)
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            raise ArtifactNotFound(artifact_id) from None


# `L3-IMP-5`: a tool result over this many characters is capped to head,
# tail and a pointer. A single deployer-wide constant, unlike the
# per-Pinned-Model tables in `provider.config` -- it bounds one tool
# result's own size, not anything measured against a provider's token
# accounting.
DEFAULT_RESULT_CAP_LIMIT: int = 4_000


def cap_tool_result(content: str, *, tool_call_id: str, limit: int, store: ArtifactStore) -> str:
    """`L3-IMP-5`: a tool result at or under `limit` characters returns
    unchanged. Over it, the full content is stashed in `store` under
    `tool_call_id` and this returns head + tail + a pointer naming the
    artifact and its full size — the pointer is what makes the reading
    happen: given one, both pins read a chosen slice unprompted and
    repeatedly (contract §5)."""
    if len(content) <= limit:
        return content

    store.store(tool_call_id, content)
    full_size = len(content)
    head_chars = max(1, limit * 2 // 3)
    tail_chars = max(1, limit - head_chars)
    head = content[:head_chars]
    tail = content[-tail_chars:] if tail_chars else ""
    omitted = max(0, full_size - head_chars - tail_chars)
    return (
        f"{head}\n"
        f"... [capped: {full_size} characters total, {omitted} omitted] ...\n"
        f"{tail}\n"
        f"[pointer] artifact_id={tool_call_id!r} full_size={full_size} characters. "
        "Call read_result_slice(artifact_id, start, end) to read a chosen range of it."
    )


def build_read_result_slice_tool(store: ArtifactStore) -> BaseTool:
    """The tool a capped result's pointer names: reads characters
    `[start, end)` of the full content `cap_tool_result` stashed away."""

    @tool
    def read_result_slice(artifact_id: str, start: int, end: int) -> str:
        """Read a slice of a capped tool result's full content, by
        character offset. `artifact_id` is the pointer a capped result
        named; `start`/`end` follow Python slice semantics."""
        try:
            full = store.read(artifact_id)
        except ArtifactNotFound as exc:
            return f"Error: no stored tool result artifact {exc}"
        return full[start:end]

    return read_result_slice
