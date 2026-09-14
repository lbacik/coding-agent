from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coding_agent.github.client import GitHubClient
from coding_agent.github.issues import IssueFetchFailed, TargetIssue, fetch_issue
from coding_agent.implement.fingerprint import compute_fingerprint
from coding_agent.implement.git import GitFailure, Workspace, checkout_workspace, ensure_mirror
from coding_agent.implement.seam import confirm_seam, derive_seam_set


@dataclass(frozen=True)
class StageResult:
    name: str
    passed: bool
    detail: str


@dataclass
class SkeletonReport:
    results: list[StageResult] = field(default_factory=list)
    issue: TargetIssue | None = None
    workspace: Workspace | None = None
    fingerprint: str | None = None
    seam_set: tuple[str, ...] | None = None

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def add(self, result: StageResult) -> None:
        self.results.append(result)


def remote_url(owner: str, repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def run_implement_skeleton(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    token: str,
    *,
    mirror_dir: Path,
    workspace_dir: Path,
) -> SkeletonReport:
    """S3.1 (issue #30) plus S3.2 (issue #32): fetch the Target Issue,
    maintain the mirror, check out a fresh workspace at the Base Revision,
    compute the Fingerprint, and confirm the Seam Set. Stops there — no
    model, no GitHub write. Stages run in order and stop at the first
    failure, since each one depends on the last having actually
    succeeded."""
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
        workspace = checkout_workspace(mirror_dir, workspace_dir)
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
                "seam set confirmed",
                False,
                "the Target Issue names no public symbol, path or endpoint under test; "
                "no Seam Set derivable (ADR 0008)",
            )
        )
        return report

    seam_set = confirm_seam(seam_candidates)
    report.seam_set = seam_set
    report.add(StageResult("seam set confirmed", True, ", ".join(seam_set)))

    return report
