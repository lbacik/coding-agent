"""The Code Host profile: the per-vendor facts runtime code reads.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §10.5 (the
pull-request-to-issue link line), §14.4 (the rendered publication body
never exceeds the host's body limit; only the ``document`` is truncated)
and §10.4 (the workflows path authorship refusal).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.

One value type carries every fact that used to be hard-coded GitHub
literals in runtime code: the host identity and base URL, the max body
length, the workflows path prefix and root, the closing-keyword rendering
and the marker fence format. Each :class:`CodeHostPort` exposes its
profile, so runtime code reads the vendor facts from the port instead of
literals. ``GITHUB_PROFILE`` is today's host; a second vendor is a second
value, verified by the same contract suite.
"""

from __future__ import annotations

from dataclasses import dataclass

WORKFLOWS_PATH_PREFIX = ".github/workflows/"
"""The workflows path prefix both git scanners read: the single definition.

Kept as module constants so existing import sites keep working; the value
is the GitHub profile's, and the scanners below read it through
:data:`GITHUB_PROFILE`.
"""

WORKFLOWS_PATH_ROOT = ".github/workflows"
"""The workflows path root both git scanners read: the single definition."""


@dataclass(frozen=True)
class CodeHostProfile:
    """The per-vendor host facts one Code Host port serves.

    ``host_label`` names the vendor for logs and receipts;
    ``base_url`` is the human repository base URL (no trailing slash);
    ``max_body_chars`` caps every rendered publication body;
    ``workflows_path_prefix`` / ``workflows_path_root`` locate the
    workflows directory the authorship refusal scans;
    ``closing_keyword`` / ``non_closing_keyword`` render the
    pull-request-to-issue link line; ``marker_fence_open`` /
    ``marker_fence_close`` fence the Attempt Marker comments.
    """

    host_label: str
    base_url: str
    max_body_chars: int
    workflows_path_prefix: str
    workflows_path_root: str
    closing_keyword: str = "Closes"
    non_closing_keyword: str = "Part of"
    marker_fence_open: str = "<!--"
    marker_fence_close: str = "-->"

    def delivery_reference(self, ref: str, *, draft: bool = False) -> str:
        """The pull-request-to-issue link line for issue ``ref`` (§10.5).

        A ready pull request carries the closing line; a draft carries the
        non-closing line. ``ref`` is the issue reference without vendor
        syntax (``"30"`` renders ``"Closes #30"`` on GitHub): the caller
        passes semantic fields, and only the profile formats vendor text.
        """
        keyword = self.non_closing_keyword if draft else self.closing_keyword
        return f"{keyword} #{ref}"

    def is_workflows_path(self, path: str) -> bool:
        """Whether ``path`` is the workflows directory or a file under it."""
        normalized = path.strip().strip('"')
        return normalized == self.workflows_path_root or normalized.startswith(
            self.workflows_path_prefix
        )

    def repository_url(self, owner: str, repo: str) -> str:
        """The canonical credential-free clone URL for ``owner/repo`` (§18)."""
        return f"{self.base_url}/{owner}/{repo}.git"


GITHUB_PROFILE = CodeHostProfile(
    host_label="GitHub",
    base_url="https://github.com",
    max_body_chars=65_536,
    workflows_path_prefix=WORKFLOWS_PATH_PREFIX,
    workflows_path_root=WORKFLOWS_PATH_ROOT,
)
"""Today's host facts: every GitHub literal runtime code used to carry."""


__all__ = [
    "GITHUB_PROFILE",
    "WORKFLOWS_PATH_PREFIX",
    "WORKFLOWS_PATH_ROOT",
    "CodeHostProfile",
]
