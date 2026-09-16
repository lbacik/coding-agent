from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coding_agent.github.client import GitHubClient
from coding_agent.github.issues import IssueFetchFailed, TargetIssue, fetch_issue
from coding_agent.implement.fingerprint import compute_fingerprint
from coding_agent.implement.git import GitFailure, Workspace, checkout_workspace, ensure_mirror
from coding_agent.implement.pinned_prefix import (
    AttemptFacts,
    CompactionThresholdTable,
    ContextWindowTable,
    PinnedPrefix,
    PinnedPrefixTooLarge,
    assert_no_skill_path_resolver,
    assert_within_context_window,
    compose_pinned_prefix,
)
from coding_agent.implement.seam import confirm_seam, derive_seam_set
from coding_agent.provider.pinned_model import PinnedModel


@dataclass(frozen=True)
class StageResult:
    name: str
    passed: bool
    detail: str


SEAM_SET_CONFIRMED_STAGE = "seam set confirmed"
"""The `StageResult.name` this module reports the Seam Set gate under
(ADR 0008) — shared with `coding_agent.implement.attempt.implement_outcome`,
which reads it back to recognise a `seam-not-confirmed` outcome, rather than
each module naming the stage by its own string literal."""


@dataclass
class SkeletonReport:
    results: list[StageResult] = field(default_factory=list)
    issue: TargetIssue | None = None
    workspace: Workspace | None = None
    fingerprint: str | None = None
    seam_set: tuple[str, ...] | None = None
    pinned_prefix: PinnedPrefix | None = None

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def add(self, result: StageResult) -> None:
        self.results.append(result)


def remote_url(owner: str, repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def target_repository_url(owner: str, repo: str) -> str:
    """The credential-free canonical URL retained in a workspace's Git config."""
    return f"https://github.com/{owner}/{repo}.git"


def run_implement_skeleton(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    token: str,
    *,
    mirror_dir: Path,
    workspace_dir: Path,
    skills_dir: Path,
    target_language: str,
    pin: PinnedModel,
    compaction_thresholds: ContextWindowTable | CompactionThresholdTable,
    compose_prefix: bool = True,
) -> SkeletonReport:
    """S3.1 (issue #30), S3.2 (issue #32) and S3.4 (issue #33): fetch the
    Target Issue, maintain the mirror, check out a fresh workspace at the
    Base Revision, compute the Fingerprint, confirm the Seam Set, and
    compose the Pinned Prefix. Stops there — no model, no GitHub write.
    Stages run in order and stop at the first failure, since each one
    depends on the last having actually succeeded.

    `target_language` is supplied by the caller rather than read from the
    checked-out Project Profile: that reading is a later ticket's job
    (S3.7), so this stays an explicit, honest input rather than a guess."""
    report = SkeletonReport()

    try:
        issue = fetch_issue(client, owner, repo, issue_number)
    except IssueFetchFailed as exc:
        report.add(StageResult("issue fetched", False, str(exc)))
        return report
    report.issue = issue
    report.add(StageResult("issue fetched", True, f"#{issue.number} {issue.title!r}"))

    url = remote_url(owner, repo, token)
    try:
        ensure_mirror(mirror_dir, url, redact=token)
    except GitFailure as exc:
        report.add(StageResult("mirror updated", False, str(exc)))
        return report
    report.add(StageResult("mirror updated", True, str(mirror_dir)))

    try:
        workspace = checkout_workspace(
            mirror_dir,
            workspace_dir,
            origin_url=target_repository_url(owner, repo),
        )
    except GitFailure as exc:
        report.add(StageResult("workspace checked out", False, str(exc)))
        return report
    report.workspace = workspace
    report.add(
        StageResult(
            "workspace checked out",
            True,
            f"branch={workspace.base_branch} base_revision={workspace.base_revision}",
        )
    )

    fingerprint = compute_fingerprint(issue.title, issue.body)
    report.fingerprint = fingerprint
    report.add(StageResult("fingerprint computed", True, fingerprint))

    # A missing Seam Set is handled here, as a StageResult, not by calling
    # confirm_seam with nothing and catching its refusal: confirm_seam's own
    # guard is a defensive re-assertion for a bug elsewhere (ADR 0008,
    # L3-IMP-11), not the mechanism this command uses to react to a Target
    # Issue that genuinely names none.
    seam_candidates = derive_seam_set(issue.title, issue.body)
    if not seam_candidates:
        report.add(
            StageResult(
                SEAM_SET_CONFIRMED_STAGE,
                False,
                "the Target Issue names no public symbol, path or endpoint under test; "
                "no Seam Set derivable (ADR 0008)",
            )
        )
        return report

    seam_set = confirm_seam(seam_candidates)
    report.seam_set = seam_set
    report.add(StageResult(SEAM_SET_CONFIRMED_STAGE, True, ", ".join(seam_set)))

    if compose_prefix:
        compose_pinned_prefix_for_report(report, skills_dir, target_language, pin, compaction_thresholds)

    return report


def compose_pinned_prefix_for_report(
    report: SkeletonReport,
    skills_dir: Path,
    target_language: str,
    pin: PinnedModel,
    compaction_thresholds: ContextWindowTable | CompactionThresholdTable,
) -> None:
    """Compose the Pinned Prefix after every pre-model gate has passed.

    The ordinary skeleton remains independently useful to earlier callers,
    while an Attempt that needs additional readiness checks can defer this
    irreversible conversation-opening preparation until those checks pass.
    """
    assert report.issue is not None
    assert report.workspace is not None
    assert report.fingerprint is not None
    assert report.seam_set is not None

    # This function stops before any toolset exists (S3.5's real toolset and
    # its own L3-IMP-14 assertion live in `implement.attempt`); asserted
    # anyway against the empty set so the invariant is checked at every
    # point an Attempt could open, not merely documented.
    assert_no_skill_path_resolver(())

    facts = AttemptFacts(
        issue_number=report.issue.number,
        issue_title=report.issue.title,
        fingerprint=report.fingerprint,
        base_revision=report.workspace.base_revision,
        seam_set=report.seam_set,
        target_language=target_language,
    )
    try:
        prefix = compose_pinned_prefix(skills_dir, facts)
        assert_within_context_window(prefix, pin, compaction_thresholds)
    except OSError as exc:
        report.add(
            StageResult(
                "pinned prefix composed",
                False,
                f"could not read the Skill Bundle from {skills_dir}: {exc}",
            )
        )
        return
    except PinnedPrefixTooLarge as exc:
        report.add(StageResult("pinned prefix composed", False, str(exc)))
        return
    report.pinned_prefix = prefix
    report.add(
        StageResult(
            "pinned prefix composed",
            True,
            f"{len(prefix.injected_files)} files injected; "
            f"~{prefix.estimated_tokens} estimated tokens",
        )
    )
