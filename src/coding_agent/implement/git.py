from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitFailure(Exception):
    """A git operation exited non-zero. The message has any credential the
    caller asked to protect already redacted, so it is safe to print or log."""


def _run(args: list[str], *, cwd: Path | None = None, redact: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"{' '.join(args)!r} failed: {detail}"
        if redact:
            message = message.replace(redact, "***")
        raise GitFailure(message)
    return result.stdout.strip()


def ensure_mirror(mirror_dir: Path, remote_url: str, *, redact: str | None = None) -> None:
    """A local mirror of the Target Repository: `git clone --mirror` on
    first use, `git remote update --prune` (never a re-clone) once one is
    already there."""
    if (mirror_dir / "HEAD").exists():
        _run(["git", "remote", "update", "--prune"], cwd=mirror_dir, redact=redact)
        return
    mirror_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--mirror", remote_url, str(mirror_dir)], redact=redact)


@dataclass(frozen=True)
class Workspace:
    path: Path
    base_branch: str
    base_revision: str


def checkout_workspace(mirror_dir: Path, workspace_dir: Path) -> Workspace:
    """A fresh checkout from the mirror, at the mirror's current default
    branch head — the Base Revision."""
    head_ref = _run(["git", "symbolic-ref", "HEAD"], cwd=mirror_dir)
    base_branch = head_ref.removeprefix("refs/heads/")

    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)

    _run(["git", "clone", "--branch", base_branch, "--", str(mirror_dir), str(workspace_dir)])
    base_revision = _run(["git", "rev-parse", "HEAD"], cwd=workspace_dir)
    return Workspace(path=workspace_dir, base_branch=base_branch, base_revision=base_revision)
