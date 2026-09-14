from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.client import GitHubClient
from coding_agent.implement import skeleton
from coding_agent.implement.fingerprint import compute_fingerprint
from coding_agent.implement.git import GitFailure, checkout_workspace, ensure_mirror
from coding_agent.implement.seam import UnconfirmedSeam, confirm_seam, derive_seam_set
from conftest import init_origin_repo, run_git

TEST_BASE_URL = "https://api.github.test"


def _commit_more(path: Path, filename: str) -> str:
    (path / filename).write_text("more\n", encoding="utf-8")
    run_git(["add", "."], path)
    run_git(["commit", "-q", "-m", f"add {filename}"], path)
    return run_git(["rev-parse", "HEAD"], path)


# --- compute_fingerprint -----------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    assert compute_fingerprint("t", "b") == compute_fingerprint("t", "b")


def test_fingerprint_changes_with_title() -> None:
    assert compute_fingerprint("t1", "b") != compute_fingerprint("t2", "b")


def test_fingerprint_changes_with_body() -> None:
    assert compute_fingerprint("t", "b1") != compute_fingerprint("t", "b2")


def test_fingerprint_does_not_collide_across_the_title_body_boundary() -> None:
    assert compute_fingerprint("a", "bc") != compute_fingerprint("ab", "c")


# --- derive_seam_set / confirm_seam (ADR 0008, issue #32) ---------------------

# Real bodies from this repository's own issue tracker (lbacik/coding-agent),
# fetched verbatim, per the acceptance criteria's "range of real issue-body
# shapes" requirement.

ISSUE_29_TITLE = "Build S3: implement agent implement --issue <n>"
ISSUE_29_BODY = (
    "## Scope\n\n"
    "Build `agent implement --issue <n>`, the S3 slice of [the v1 implementation "
    "plan](https://github.com/lbacik/coding-agent/blob/main/docs/plan/v1-implementation-plan.md"
    "#s3--skill-execution-on-an-explicit-issue). Currently the CLI "
    "(`src/coding_agent/cli.py`) only wires `preflight`, `startup-check`, "
    "`verify-skill-bundle`, `validate` — `implement` does not exist yet, so no pilot run "
    "against the sandbox is possible until this lands.\n\n"
    "Per the plan, S3 covers: mirror and workspace, Base Revision pin, Fingerprint, the "
    "Validation Contract read at the base, `/implement` with `/tdd` nested, the Attempt "
    "Header carrying the Seam Set and the `confirm_seam` assertion in front of it, the "
    "injection of `tdd`'s Companion Files with no tool to fetch any other, the "
    "explicit-activation registry, the bounded tool loop, the per-tool-result cap, the "
    "model/provider contract through `init_chat_model`, the preflight capability assertion, "
    "usage counted after every response. Ends with a Delivery Snapshot committed and pushed. "
    "**Not in this slice:** review, pull requests, labels, comments.\n\n"
    "## Build constraint decided ahead of the design (from [#27]"
    "(https://github.com/lbacik/coding-agent/issues/27))\n\n"
    "The `run_tests` subprocess given to the implementer **must not inherit the Worker's "
    'GitHub credential**. `L3-IMP-9` ("the model attempts a GitHub mutation — no such tool '
    'exists") is re-founded on the work node\'s environment as well as its toolset — '
    "[#25](https://github.com/lbacik/coding-agent/issues/25) observed a test file mutate "
    "source code as a side effect of collection, so the guarantee that matters is the "
    "environment the subprocess runs in, not just what tools are named.\n\n"
    "## Acceptance rows this slice owes\n\n"
    "Per [the acceptance matrix](https://github.com/lbacik/coding-agent/blob/main/docs/plan/"
    "v1-acceptance-matrix.md): `L1-1, L1-3, L1-4, L1-4a, L1-5, L1-6` (`L1-2` is retired — no "
    "model-driven classifier exists for it, per [#24](https://github.com/lbacik/coding-agent/"
    "issues/24)) and `L3-IMP-1` through `L3-IMP-15`.\n\n"
    "## Done when\n\n"
    "A small real task on the sandbox repository (`lbacik/coding-agent-sandbox`) produces a "
    "pushed branch whose tree changes what the issue asked for, validated by S2's harness. "
    "Both providers pass the contract checks, and at least one small real implementation run "
    "has been made against each. Skill instructions are never compacted or summarised. A "
    "ceiling crossed mid-loop stops the loop rather than waiting for the next node.\n\n"
    "Once this lands, the pilot run itself is what [#26]"
    "(https://github.com/lbacik/coding-agent/issues/26) and [#27]"
    "(https://github.com/lbacik/coding-agent/issues/27) are parked waiting on.\n"
)

ISSUE_32_TITLE = "S3.2: Seam Set derivation and confirm_seam, wired into agent implement"
ISSUE_32_BODY = (
    "## Parent\n\nPart of #29.\n\n## What to build\n\n"
    "The minimal, model-free Readiness Fact ADR 0008 describes for the Seam Set, wired into "
    "the\n`agent implement` skeleton from #30: a text test over the Target Issue (does it "
    "name a public\nsymbol, path or endpoint?) that derives the Seam Set when it can, and "
    "`confirm_seam`, which\nperforms no model call and asserts a Seam Set is present before "
    "anything downstream may proceed.\nWhere the issue names no seam, the command ends "
    "cleanly — a non-transient failure with a clear\nexplanation, never a model call, never "
    "a guess.\n\nRun against an issue that names its seam (e.g. a backtick-quoted symbol or "
    "path in the body) and\nwatch the command confirm it and continue; run against one that "
    "doesn't and watch it refuse before\nanything model-facing happens.\n\n"
    "## Acceptance criteria\n\n"
    "- [ ] `derive_seam_set` is a text test only — decidable from the issue's title and body "
    "without\n      any model call — that returns the named symbols/paths/endpoints when the "
    "issue names at\n      least one, and nothing when it doesn't.\n"
    "- [ ] `confirm_seam` performs no model call and takes no branch a correct run can reach "
    "when a\n      Seam Set is present; entering it with none present is an internal "
    "invariant violation, not\n      a request for clarification (`L3-IMP-11`, `ADR 0008`).\n"
    "- [ ] `agent implement` wires this in right after the workspace from #30 is ready: a "
    "confirmed\n      Seam Set is reported and the command continues (to whatever the "
    "next-available ticket\n      implements); an unconfirmed one ends the command with a "
    "clear, non-zero-exit explanation.\n"
    "- [ ] Tested against a range of real issue-body shapes, including this repository's own "
    "issues\n      (e.g. #29 itself, which does not name a seam for itself — a useful edge "
    "case) and at least\n      one issue that does.\n"
    "- [ ] `uv run mypy src tests` and `uv run pytest -q` pass.\n\n"
    "## Blocked by\n\n- #30 (S3.1 — needs the `agent implement` skeleton and issue fetch to "
    "wire into)."
)


def test_derive_seam_set_finds_nothing_in_a_scoping_issue_with_no_checklist() -> None:
    # #29: a real issue, thick with backtick-quoted identifiers, paths and row
    # ids — none of them the subject of an acceptance-criteria item, since #29
    # has no checklist at all. The useful edge case the acceptance criteria calls
    # out: plenty of backticks, zero derivable seam.
    assert derive_seam_set(ISSUE_29_TITLE, ISSUE_29_BODY) == ()


def test_derive_seam_set_finds_the_symbols_a_checklist_names() -> None:
    # #32: this very ticket. Its acceptance criteria name the two functions
    # under test and the CLI command they're wired into; its `uv run mypy
    # src tests` criterion is not a symbol and must not be picked up.
    seams = derive_seam_set(ISSUE_32_TITLE, ISSUE_32_BODY)
    assert seams == ("derive_seam_set", "confirm_seam", "agent implement")


def test_derive_seam_set_reads_a_bare_backtick_from_the_title() -> None:
    # A title is one short line a human wrote to name the task, not free-form
    # prose accumulating incidental references the way a body is (#29's body
    # is exactly that prose) -- so a title's own backtick-quoted symbol is
    # trusted without the checklist/verb shape the body is held to.
    assert derive_seam_set("Fix `parse_receipt` off-by-one", "") == ("parse_receipt",)


def test_derive_seam_set_ignores_a_bare_mention_outside_a_checklist_item() -> None:
    body = "This touches `parse_receipt` somewhere in the middle of a sentence."
    assert derive_seam_set("a title", body) == ()


def test_derive_seam_set_deduplicates_preserving_first_occurrence_order() -> None:
    body = (
        "- [ ] `foo` is added\n"
        "- [ ] `bar` returns early\n"
        "- [ ] `foo` is also covered by a regression test\n"
    )
    assert derive_seam_set("a title", body) == ("foo", "bar")


def test_confirm_seam_hands_back_a_present_seam_set_unchanged() -> None:
    assert confirm_seam(("foo", "bar")) == ("foo", "bar")


def test_confirm_seam_refuses_an_empty_seam_set() -> None:
    with pytest.raises(UnconfirmedSeam):
        confirm_seam(())


# --- ensure_mirror ------------------------------------------------------------


def test_ensure_mirror_clones_on_first_use(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    sha = init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"

    ensure_mirror(mirror, str(origin))

    assert (mirror / "HEAD").exists()
    assert run_git(["rev-parse", "refs/heads/main"], mirror) == sha


def test_ensure_mirror_updates_rather_than_re_cloning(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))

    # A sentinel git wouldn't touch: proves the directory survives, rather
    # than being deleted and re-cloned into, on the second call.
    sentinel = mirror / "coding-agent-test-sentinel"
    sentinel.write_text("still here\n", encoding="utf-8")

    sha2 = _commit_more(origin, "second.txt")
    ensure_mirror(mirror, str(origin))

    assert sentinel.exists()
    assert run_git(["rev-parse", "refs/heads/main"], mirror) == sha2


def test_ensure_mirror_follows_the_upstream_default_branch(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin, branch="main")
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    assert run_git(["symbolic-ref", "HEAD"], mirror) == "refs/heads/main"

    run_git(["checkout", "-q", "-b", "develop"], origin)
    _commit_more(origin, "on-develop.txt")
    run_git(["symbolic-ref", "HEAD", "refs/heads/develop"], origin)
    run_git(["branch", "-m", "main", "old-main"], origin)

    ensure_mirror(mirror, str(origin))

    assert run_git(["symbolic-ref", "HEAD"], mirror) == "refs/heads/develop"


def test_ensure_mirror_raises_and_redacts_the_credential_on_failure(tmp_path: Path) -> None:
    secret = "s3cr3t-token"
    bogus_remote = str(tmp_path / f"{secret}-does-not-exist")
    mirror = tmp_path / "mirror.git"

    with pytest.raises(GitFailure) as excinfo:
        ensure_mirror(mirror, bogus_remote, redact=secret)

    assert secret not in str(excinfo.value)


# --- checkout_workspace -------------------------------------------------------


def test_checkout_workspace_checks_out_the_mirrors_base_head(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    sha = init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace_dir = tmp_path / "workspace"

    workspace = checkout_workspace(mirror, workspace_dir)

    assert workspace.base_branch == "main"
    assert workspace.base_revision == sha
    assert (workspace_dir / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_checkout_workspace_replaces_a_stale_workspace(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    mirror = tmp_path / "mirror.git"
    ensure_mirror(mirror, str(origin))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "stray.txt").write_text("leftover from a previous run\n", encoding="utf-8")

    checkout_workspace(mirror, workspace_dir)

    assert not (workspace_dir / "stray.txt").exists()
    assert (workspace_dir / "README.md").exists()


# --- run_implement_skeleton: the full orchestration --------------------------


@pytest.fixture
def origin_repo(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin"
    sha = init_origin_repo(origin)
    return origin, sha


def test_run_implement_skeleton_happy_path(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    origin_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, sha = origin_repo
    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": body},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is True
    assert [r.name for r in report.results] == [
        "issue fetched",
        "mirror updated",
        "workspace checked out",
        "fingerprint computed",
        "seam set confirmed",
    ]
    assert all(r.passed for r in report.results)
    assert report.issue is not None
    assert report.issue.title == "a title"
    assert report.workspace is not None
    assert report.workspace.base_revision == sha
    assert report.fingerprint == compute_fingerprint("a title", body)
    assert report.seam_set == ("a_thing",)


def test_run_implement_skeleton_stops_after_a_missing_issue(
    client: GitHubClient, requests_mock: Any, tmp_path: Path
) -> None:
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        999,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is False
    assert [r.name for r in report.results] == ["issue fetched"]
    assert not (tmp_path / "mirror.git").exists()


def test_run_implement_skeleton_stops_after_an_unreachable_mirror(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        skeleton, "remote_url", lambda owner, repo, token: str(tmp_path / "no-such-repo")
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": "a body"},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is False
    assert [r.name for r in report.results] == ["issue fetched", "mirror updated"]
    assert not (tmp_path / "workspace").exists()


def test_run_implement_skeleton_stops_after_an_unconfirmed_seam(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    origin_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, _ = origin_repo
    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/29",
        json={"number": 29, "title": ISSUE_29_TITLE, "body": ISSUE_29_BODY},
    )

    report = skeleton.run_implement_skeleton(
        client,
        "octocat",
        "sandbox",
        29,
        "github_pat_testtoken",
        mirror_dir=tmp_path / "mirror.git",
        workspace_dir=tmp_path / "workspace",
    )

    assert report.ok is False
    assert [r.name for r in report.results] == [
        "issue fetched",
        "mirror updated",
        "workspace checked out",
        "fingerprint computed",
        "seam set confirmed",
    ]
    seam_result = report.results[-1]
    assert seam_result.passed is False
    assert "no public symbol, path or endpoint" in seam_result.detail
    assert report.seam_set is None
