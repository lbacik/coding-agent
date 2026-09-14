from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from coding_agent.github.client import GitHubClient
from coding_agent.implement import skeleton
from coding_agent.implement.fingerprint import compute_fingerprint
from coding_agent.implement.git import GitFailure, checkout_workspace, ensure_mirror
from coding_agent.implement.pinned_prefix import (
    AttemptFacts,
    PinnedPrefixTooLarge,
    SkillPathResolverPresent,
    assert_no_skill_path_resolver,
    assert_within_compaction_threshold,
    compose_attempt_header,
    compose_pinned_prefix,
    estimate_tokens,
)
from coding_agent.implement.seam import UnconfirmedSeam, confirm_seam, derive_seam_set
from coding_agent.provider.config import DEFAULT_COMPACTION_THRESHOLDS, PINNED_MODELS
from coding_agent.skillbundle.verify import BUNDLE_SPEC
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
        skills_dir=_write_skills_dir(tmp_path),
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
    )

    assert report.ok is True
    assert [r.name for r in report.results] == [
        "issue fetched",
        "mirror updated",
        "workspace checked out",
        "fingerprint computed",
        "seam set confirmed",
        "pinned prefix composed",
    ]
    assert all(r.passed for r in report.results)
    assert report.issue is not None
    assert report.issue.title == "a title"
    assert report.workspace is not None
    assert report.workspace.base_revision == sha
    assert report.fingerprint == compute_fingerprint("a title", body)
    assert report.seam_set == ("a_thing",)
    assert report.pinned_prefix is not None
    assert len(report.pinned_prefix.injected_files) == 5


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
        skills_dir=tmp_path / "skills",
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
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
        skills_dir=tmp_path / "skills",
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
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
        skills_dir=tmp_path / "skills",
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
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


def test_run_implement_skeleton_stops_after_a_missing_skill_bundle(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    origin_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, _ = origin_repo
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
        skills_dir=tmp_path / "no-such-skills-dir",
        target_language="python",
        pin=_PIN,
        compaction_thresholds=DEFAULT_COMPACTION_THRESHOLDS,
    )

    assert report.ok is False
    assert [r.name for r in report.results] == [
        "issue fetched",
        "mirror updated",
        "workspace checked out",
        "fingerprint computed",
        "seam set confirmed",
        "pinned prefix composed",
    ]
    assert report.results[-1].passed is False
    assert "could not read the Skill Bundle" in report.results[-1].detail
    assert report.pinned_prefix is None


def test_run_implement_skeleton_stops_after_an_oversized_pinned_prefix(
    client: GitHubClient,
    requests_mock: Any,
    tmp_path: Path,
    origin_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, _ = origin_repo
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
        skills_dir=_write_skills_dir(tmp_path),
        target_language="python",
        pin=_PIN,
        compaction_thresholds={_PIN.key: 1},
    )

    assert report.ok is False
    assert report.results[-1].name == "pinned prefix composed"
    assert report.results[-1].passed is False
    assert "compaction threshold" in report.results[-1].detail
    assert report.pinned_prefix is None


# --- compose_pinned_prefix / compose_attempt_header (S3.4, issue #33) --------

# A small fixture tree standing in for the installed Skill Bundle
# (ADR 0012's committed manifest), rather than requiring the built Docker
# image at test time. Distinct, greppable bodies per file so "which file
# ended up where, verbatim" is checkable by content, not just by label.
_IMPLEMENT_SKILL_MD = "# implement\n\nUse /tdd where possible.\n"
_TDD_SKILL_MD = "# tdd\n\nConfirm seams with the user. See tests.md and mocking.md.\n"
_CODEBASE_DESIGN_SKILL_MD = "# codebase-design\n\nA reference to consult, not a session to run.\n"
_TDD_TESTS_MD = "# tests.md\n\nWorked examples, all TypeScript and jest.\n"
_TDD_MOCKING_MD = "# mocking.md\n\nMock at system boundaries only.\n"
_CODEBASE_DESIGN_DEEPENING_MD = "# DEEPENING.md\n\nDelete the old shallow tests.\n"
_CODEBASE_DESIGN_DESIGN_IT_TWICE_MD = "# DESIGN-IT-TWICE.md\n\nSpawn 3+ sub-agents in parallel.\n"


def _write_skills_dir(base: Path) -> Path:
    skills_dir = base / ".agents" / "skills"
    (skills_dir / "implement").mkdir(parents=True)
    (skills_dir / "implement" / "SKILL.md").write_text(_IMPLEMENT_SKILL_MD, encoding="utf-8")
    (skills_dir / "tdd").mkdir(parents=True)
    (skills_dir / "tdd" / "SKILL.md").write_text(_TDD_SKILL_MD, encoding="utf-8")
    (skills_dir / "tdd" / "tests.md").write_text(_TDD_TESTS_MD, encoding="utf-8")
    (skills_dir / "tdd" / "mocking.md").write_text(_TDD_MOCKING_MD, encoding="utf-8")
    (skills_dir / "codebase-design").mkdir(parents=True)
    (skills_dir / "codebase-design" / "SKILL.md").write_text(
        _CODEBASE_DESIGN_SKILL_MD, encoding="utf-8"
    )
    (skills_dir / "codebase-design" / "DEEPENING.md").write_text(
        _CODEBASE_DESIGN_DEEPENING_MD, encoding="utf-8"
    )
    (skills_dir / "codebase-design" / "DESIGN-IT-TWICE.md").write_text(
        _CODEBASE_DESIGN_DESIGN_IT_TWICE_MD, encoding="utf-8"
    )
    return skills_dir


_FACTS = AttemptFacts(
    issue_number=33,
    issue_title="S3.4: Pinned Prefix composition",
    fingerprint="deadbeef" * 8,
    base_revision="cafef00d" * 5,
    seam_set=("compose_pinned_prefix", "compose_attempt_header"),
    target_language="python",
)


def test_compose_pinned_prefix_injects_exactly_the_five_files_in_order(tmp_path: Path) -> None:
    skills_dir = _write_skills_dir(tmp_path)

    prefix = compose_pinned_prefix(skills_dir, _FACTS)

    assert [f.label for f in prefix.injected_files] == [
        "implement/SKILL.md",
        "tdd/SKILL.md",
        "codebase-design/SKILL.md",
        "tdd/tests.md",
        "tdd/mocking.md",
    ]


def test_compose_pinned_prefix_injects_each_file_byte_for_byte_verbatim(tmp_path: Path) -> None:
    skills_dir = _write_skills_dir(tmp_path)

    prefix = compose_pinned_prefix(skills_dir, _FACTS)

    by_label = {f.label: f.text for f in prefix.injected_files}
    assert by_label["implement/SKILL.md"] == _IMPLEMENT_SKILL_MD
    assert by_label["tdd/SKILL.md"] == _TDD_SKILL_MD
    assert by_label["codebase-design/SKILL.md"] == _CODEBASE_DESIGN_SKILL_MD
    assert by_label["tdd/tests.md"] == _TDD_TESTS_MD
    assert by_label["tdd/mocking.md"] == _TDD_MOCKING_MD


def test_compose_pinned_prefix_never_injects_codebase_designs_own_companions(
    tmp_path: Path,
) -> None:
    skills_dir = _write_skills_dir(tmp_path)

    prefix = compose_pinned_prefix(skills_dir, _FACTS)

    labels = [f.label for f in prefix.injected_files]
    assert "codebase-design/DEEPENING.md" not in labels
    assert "codebase-design/DESIGN-IT-TWICE.md" not in labels
    assert _CODEBASE_DESIGN_DEEPENING_MD not in prefix.rendered
    assert _CODEBASE_DESIGN_DESIGN_IT_TWICE_MD not in prefix.rendered


def test_compose_pinned_prefixs_injected_manifest_is_tdds_bundle_spec_companions() -> None:
    # The manifest is read from BUNDLE_SPEC rather than repeated, so a Skill
    # Bundle pin bump that adds or renames a tdd companion is one place to
    # update, not two.
    assert BUNDLE_SPEC.companion_files["tdd"] == ("tests.md", "mocking.md")


def test_compose_attempt_header_carries_the_seam_set() -> None:
    header = compose_attempt_header(_FACTS)
    assert "`compose_pinned_prefix`" in header
    assert "`compose_attempt_header`" in header


def test_compose_attempt_header_answers_the_codebase_design_companion_gap() -> None:
    header = compose_attempt_header(_FACTS)
    assert "DEEPENING.md" in header
    assert "DESIGN-IT-TWICE.md" in header
    assert "unavailable" in header


def test_compose_attempt_header_answers_the_tdd_companion_language_gap() -> None:
    header = compose_attempt_header(_FACTS)
    assert "TypeScript" in header
    assert "python" in header


def test_compose_attempt_header_is_authored_text_not_a_slice_of_any_injected_file() -> None:
    # ADR 0007: the header is composed prose the Worker authors, never an
    # edit to upstream text -- so it must not simply echo an injected file's
    # own bytes back as itself.
    header = compose_attempt_header(_FACTS)
    assert header != _IMPLEMENT_SKILL_MD
    assert header != _TDD_SKILL_MD
    assert header != _CODEBASE_DESIGN_SKILL_MD


# --- assert_within_compaction_threshold (L3-IMP-13) --------------------------

_PIN = PINNED_MODELS["anthropic"]


def test_a_real_composed_prefix_is_comfortably_within_the_default_threshold(
    tmp_path: Path,
) -> None:
    skills_dir = _write_skills_dir(tmp_path)
    prefix = compose_pinned_prefix(skills_dir, _FACTS)

    assert_within_compaction_threshold(prefix, _PIN, DEFAULT_COMPACTION_THRESHOLDS)
    assert prefix.estimated_tokens < DEFAULT_COMPACTION_THRESHOLDS[_PIN.key]


def test_assert_within_compaction_threshold_refuses_an_oversized_prefix(tmp_path: Path) -> None:
    skills_dir = _write_skills_dir(tmp_path)
    prefix = compose_pinned_prefix(skills_dir, _FACTS)

    with pytest.raises(PinnedPrefixTooLarge):
        assert_within_compaction_threshold(prefix, _PIN, {_PIN.key: 1})


def test_assert_within_compaction_threshold_refuses_a_pin_missing_from_the_table(
    tmp_path: Path,
) -> None:
    skills_dir = _write_skills_dir(tmp_path)
    prefix = compose_pinned_prefix(skills_dir, _FACTS)

    with pytest.raises(PinnedPrefixTooLarge):
        assert_within_compaction_threshold(prefix, _PIN, {})


def test_estimate_tokens_grows_with_text_length() -> None:
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


# --- assert_no_skill_path_resolver (L3-IMP-14) -------------------------------


def test_assert_no_skill_path_resolver_passes_an_empty_toolset() -> None:
    assert_no_skill_path_resolver(())


def test_assert_no_skill_path_resolver_passes_tools_with_unrelated_arguments() -> None:
    @tool
    def run_tests(path: str) -> str:
        """A tool that takes a path but cannot resolve one under a skill's
        directory -- it has no notion of "skill" at all."""
        return ""

    assert_no_skill_path_resolver((run_tests,))


def test_assert_no_skill_path_resolver_refuses_the_retired_read_skill_resource_shape() -> None:
    # ADR 0012: read_skill_resource(skill, path) is the tool the decision
    # retired. Nothing in this codebase has this shape today -- the
    # assertion documents the invariant going forward, so it is exercised
    # here against a fake standing in for the shape it must never let back in.
    @tool
    def read_skill_resource(skill: str, path: str) -> str:
        """The retired shape: resolves an arbitrary path under a skill's
        own directory."""
        return ""

    with pytest.raises(SkillPathResolverPresent) as excinfo:
        assert_no_skill_path_resolver((read_skill_resource,))
    assert "read_skill_resource" in str(excinfo.value)


def test_assert_no_skill_path_resolver_catches_a_renamed_variant_of_the_shape() -> None:
    # The heuristic matches on substrings, not exact names, precisely so a
    # tool differently named from the retired `read_skill_resource(skill,
    # path)` -- but with the same "which skill, which file" shape -- still
    # trips it.
    @tool
    def fetch_skill_file(skill_id: str, file: str) -> str:
        """Differently named, same shape as the retired tool."""
        return ""

    with pytest.raises(SkillPathResolverPresent):
        assert_no_skill_path_resolver((fetch_skill_file,))
