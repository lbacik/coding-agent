"""The Target Repository mirror owns credential lifetime, freshness and
redaction (§7.2, §8, §18).

The mirror is constructed from a directory, the canonical credential-free
remote URL, a token string and a redaction registry — never from the startup
artifact and never through a private function. Every public method refreshes
the mirror first (clone on first use, fetch-and-update otherwise) under its
own short-lived credential helper, so forgetting to refresh is
unrepresentable: a fresh state directory resolves and verifies against a
valid remote. All tests run against a real bare remote under a temp dir.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from coding_agent.runtime.git import GitFailure
from coding_agent.runtime.redaction import SecretRegistry
from coding_agent.runtime.round_handoff import RoundHandoffOffer
from coding_agent.runtime.target_mirror import TargetMirror

TOKEN = "github_pat_target_mirror_testtoken"
REF = "agent/implement-7-1-work"
NEXT_BRANCH = "agent/implement-7-2-work"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _seed_remote(root: Path) -> dict[str, str]:
    """A remote with base B, work W on the attempt ref, and main moved to B2."""
    remote = root / "remote.git"
    _git(root, "init", "--bare", "-q", "remote.git")
    work = root / "work"
    _git(root, "clone", "-q", str(remote), "work")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    (work / "f.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "f.txt").write_text("base\nwork\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "work")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", f"{head}:refs/heads/{REF}")
    _git(work, "checkout", "-q", "main")
    (work / "g.txt").write_text("moved\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "moved-base")
    moved = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "main")
    _git(root, "--git-dir=remote.git", "symbolic-ref", "HEAD", "refs/heads/main")
    return {"remote": str(remote), "base": base, "work": head, "moved": moved}


def _offer(**overrides: object) -> RoundHandoffOffer:
    fields: dict[str, object] = {
        "workflow_id": "implement",
        "issue": 7,
        "attempt": "implement#7/1",
        "work_commit": "a" * 40,
        "ref": REF,
        "base_revision": "b" * 40,
        "note_kind": "hard",
        "note": "did the thing",
        "pickup_label": "round-finished",
        "used": 0,
        "limit": 2,
    }
    fields.update(overrides)
    return RoundHandoffOffer(**fields)  # type: ignore[arg-type]


def _mirror(root: Path, remote: str) -> TargetMirror:
    """A TargetMirror over a fresh state directory and a test token."""
    return TargetMirror(
        root / "state-mirror.git",
        remote,
        TOKEN,
        SecretRegistry({"GITHUB_TOKEN": TOKEN}),
    )


def test_resolve_base_revision_clones_a_fresh_state_volume(
    tmp_path: Path,
) -> None:
    """No mirror yet still resolves: the call clones first (§7.2).

    The regression test for the stale-mirror halt class: the caller has no
    refresh step to forget, so a fresh state directory plus a valid remote
    resolves the merge base instead of failing.
    """
    seeded = _seed_remote(tmp_path)
    mirror_dir = tmp_path / "state-mirror.git"
    assert not mirror_dir.exists()

    resolved = _mirror(tmp_path, seeded["remote"]).resolve_base_revision(
        seeded["work"], seeded["moved"]
    )

    assert resolved == seeded["base"]
    assert mirror_dir.exists()


def test_offer_probe_served_moved_missing_and_unavailable(
    tmp_path: Path,
) -> None:
    """The probe answers the domain question with no exceptions (§8)."""
    seeded = _seed_remote(tmp_path)
    mirror = _mirror(tmp_path, seeded["remote"])
    offer = _offer(
        work_commit=seeded["work"], base_revision=seeded["base"]
    )

    assert mirror.offer_still_served(offer) is True

    # A ref serving another commit is moved: not served.
    _git(
        tmp_path / "work",
        "push",
        "-q",
        "origin",
        f"+{seeded['moved']}:refs/heads/{REF}",
    )
    assert mirror.offer_still_served(offer) is False

    # A ref the remote no longer serves is missing: not served.
    _git(tmp_path / "work", "push", "-q", "origin", f":refs/heads/{REF}")
    assert mirror.offer_still_served(offer) is False

    # A Base Revision outside the work history is unavailable: not served.
    _git(
        tmp_path / "work",
        "push",
        "-q",
        "origin",
        f"{seeded['work']}:refs/heads/{REF}",
    )
    assert (
        mirror.offer_still_served(
            _offer(work_commit=seeded["work"], base_revision="e" * 40)
        )
        is False
    )


def test_offer_probe_maps_transport_failure_to_false(tmp_path: Path) -> None:
    """An unreachable remote reads ``False``, never an exception (§8)."""
    mirror = _mirror(tmp_path, str(tmp_path / "no-such-remote.git"))
    offer = _offer()

    assert mirror.offer_still_served(offer) is False


def test_open_workspace_checks_out_the_configured_branch(
    tmp_path: Path,
) -> None:
    """The workspace opens at the branch head from a fresh mirror (§7)."""
    seeded = _seed_remote(tmp_path)
    workspace_dir = tmp_path / "workspace"

    workspace = _mirror(tmp_path, seeded["remote"]).open_workspace(
        workspace_dir, branch="main"
    )

    assert workspace.path == workspace_dir
    assert workspace.base_branch == "main"
    assert workspace.base_revision == seeded["moved"]
    assert _git(workspace_dir, "rev-parse", "HEAD") == seeded["moved"]


def test_pull_request_open_refuses_a_missing_branch_or_head(
    tmp_path: Path,
) -> None:
    """Missing pins fail before any remote read (§7.2, §9.10)."""
    seeded = _seed_remote(tmp_path)
    mirror = _mirror(tmp_path, seeded["remote"])
    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", "-q", seeded["remote"], str(workspace))

    with pytest.raises(GitFailure, match="head branch is missing"):
        mirror.open_for_pull_request(
            workspace_path=workspace, input_head="d" * 40, head_branch=""
        )
    with pytest.raises(GitFailure, match="Input Head is missing"):
        mirror.open_for_pull_request(
            workspace_path=workspace, input_head="", head_branch="feature"
        )


def test_continuation_failure_carries_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed continuation raises a redacted ``GitFailure`` (§8, §18).

    The error carries no token, and the short-lived credential helper is
    removed.
    """
    created_helpers: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _recording_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, helper_path = real_mkstemp(*args, **kwargs)
        created_helpers.append(helper_path)
        return descriptor, helper_path

    monkeypatch.setattr(tempfile, "mkstemp", _recording_mkstemp)
    mirror = _mirror(tmp_path, str(tmp_path / "no-such-remote.git"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(GitFailure) as excinfo:
        mirror.open_for_continuation(
            workspace_path=workspace,
            offer=_offer(),
            attempt_branch=NEXT_BRANCH,
        )

    assert TOKEN not in str(excinfo.value)
    assert created_helpers != []
    for helper_path in created_helpers:
        assert not Path(helper_path).exists()
