from __future__ import annotations

from pathlib import Path


def resolve_within(root: Path, raw_path: str) -> Path | None:
    """`root / raw_path`, resolved, or `None` where it escapes `root` --
    shared by every caller that turns an untrusted, caller-supplied path
    segment (a tool's `path` argument, a capped result's `artifact_id`)
    into a real filesystem path. Callers choose how to react to `None`:
    `toolset._resolve_within` raises, `result_capping.FilesystemArtifactStore`
    treats it as not-found."""
    candidate = (root / raw_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        return None
    return candidate
