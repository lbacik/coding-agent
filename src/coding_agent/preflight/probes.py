from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field

import requests

from coding_agent.github.client import GitHubClient

WORKFLOWS_PROBE_PATH = ".github/workflows/coding-agent-preflight-probe.yml"
PROBE_LABEL = "coding-agent-preflight"


@dataclass(frozen=True)
class ProbeResult:
    name: str
    passed: bool
    detail: str


@dataclass
class PreflightReport:
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def add(self, result: ProbeResult) -> None:
        self.results.append(result)


class PreflightError(Exception):
    """A probe could not even be attempted (as opposed to failing as its result)."""


def _bootstrap_empty_repository(client: GitHubClient, owner: str, repo: str) -> str:
    """Create the first commit on the default branch via the Contents API.

    A sandbox repository created empty (no initial commit) has no ref to
    branch from at all: `GET .../git/ref/heads/{branch}` 409s. The Contents
    API creates that first commit directly, no `branch` or prior `sha` needed.
    """
    response = client.put(
        f"/repos/{owner}/{repo}/contents/README.md",
        json={
            "message": "coding-agent preflight: bootstrap the empty repository",
            "content": base64.b64encode(
                b"# coding-agent sandbox\n\n"
                b"Owned by the Agent Identity credential (S0a) and written to by "
                b"`agent preflight` (S0b). See docs/contract/v1-runtime-contract.md "
                b"in lbacik/coding-agent.\n"
            ).decode("ascii"),
        },
    )
    if response.status_code != 201:
        raise PreflightError(
            f"bootstrap commit returned {response.status_code}: {response.text}"
        )
    return str(response.json()["commit"]["sha"])


def _default_branch_head(client: GitHubClient, owner: str, repo: str) -> tuple[str, str]:
    repo_response = client.get(f"/repos/{owner}/{repo}")
    if repo_response.status_code != 200:
        raise PreflightError(
            f"GET /repos/{owner}/{repo} returned {repo_response.status_code}: {repo_response.text}"
        )
    default_branch = repo_response.json()["default_branch"]

    ref_response = client.get(f"/repos/{owner}/{repo}/git/ref/heads/{default_branch}")
    if ref_response.status_code == 409:
        return default_branch, _bootstrap_empty_repository(client, owner, repo)
    if ref_response.status_code != 200:
        raise PreflightError(
            f"GET .../git/ref/heads/{default_branch} returned "
            f"{ref_response.status_code}: {ref_response.text}"
        )
    return default_branch, ref_response.json()["object"]["sha"]


def _open_probe_issue(client: GitHubClient, owner: str, repo: str, run_id: str) -> int:
    response = client.post(
        f"/repos/{owner}/{repo}/issues",
        json={
            "title": f"coding-agent preflight probe {run_id}",
            "body": (
                "Opened by `agent preflight` (S0b) to prove the Agent Identity "
                "credential's write capability. Safe to ignore; closed automatically "
                "at the end of the run."
            ),
        },
    )
    if response.status_code != 201:
        raise PreflightError(f"POST .../issues returned {response.status_code}: {response.text}")
    return int(response.json()["number"])


def _close_probe_issue(
    client: GitHubClient, owner: str, repo: str, issue_number: int, report: PreflightReport
) -> None:
    summary = "\n".join(f"- {'✓' if r.passed else '✗'} {r.name}: {r.detail}" for r in report.results)
    client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json={"body": f"Preflight run finished.\n\n{summary}"},
    )
    client.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", json={"state": "closed"})


def probe_issue_comment(client: GitHubClient, owner: str, repo: str, issue_number: int) -> ProbeResult:
    response = client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json={"body": "coding-agent preflight probe: issue comment."},
    )
    if response.status_code == 201:
        return ProbeResult("issue comment posted", True, f"comment id {response.json()['id']}")
    return ProbeResult("issue comment posted", False, f"{response.status_code}: {response.text}")


def probe_label_add_remove(
    client: GitHubClient, owner: str, repo: str, issue_number: int
) -> ProbeResult:
    label_get = client.get(f"/repos/{owner}/{repo}/labels/{PROBE_LABEL}")
    if label_get.status_code == 404:
        create = client.post(
            f"/repos/{owner}/{repo}/labels",
            json={
                "name": PROBE_LABEL,
                "color": "ededed",
                "description": "Applied and removed by agent preflight (S0b)",
            },
        )
        if create.status_code not in (201, 422):  # 422: created by a concurrent run
            return ProbeResult(
                "label added and removed",
                False,
                f"label creation returned {create.status_code}: {create.text}",
            )
    elif label_get.status_code != 200:
        return ProbeResult(
            "label added and removed",
            False,
            f"label lookup returned {label_get.status_code}: {label_get.text}",
        )

    add = client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/labels", json={"labels": [PROBE_LABEL]}
    )
    if add.status_code != 200:
        return ProbeResult(
            "label added and removed", False, f"add returned {add.status_code}: {add.text}"
        )
    added_names = {label["name"] for label in add.json()}
    if PROBE_LABEL not in added_names:
        return ProbeResult("label added and removed", False, "label missing from issue after add")

    remove = client.delete(f"/repos/{owner}/{repo}/issues/{issue_number}/labels/{PROBE_LABEL}")
    if remove.status_code != 200:
        return ProbeResult(
            "label added and removed", False, f"remove returned {remove.status_code}: {remove.text}"
        )
    return ProbeResult("label added and removed", True, "added, verified present, removed")


def _put_contents(
    client: GitHubClient,
    owner: str,
    repo: str,
    path: str,
    *,
    branch: str,
    message: str,
    content: bytes,
) -> requests.Response:
    return client.put(
        f"/repos/{owner}/{repo}/contents/{path}",
        json={
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        },
    )


def probe_branch_push(
    client: GitHubClient, owner: str, repo: str, *, branch: str, base_sha: str, run_id: str
) -> ProbeResult:
    ref = client.post(
        f"/repos/{owner}/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if ref.status_code != 201:
        return ProbeResult("branch pushed", False, f"ref creation returned {ref.status_code}: {ref.text}")

    put = _put_contents(
        client,
        owner,
        repo,
        f"preflight/{run_id}.txt",
        branch=branch,
        message="coding-agent preflight probe: branch push",
        content=f"preflight run {run_id}\n".encode(),
    )
    if put.status_code != 201:
        return ProbeResult("branch pushed", False, f"push returned {put.status_code}: {put.text}")
    return ProbeResult("branch pushed", True, f"commit {put.json()['commit']['sha']} on {branch}")


def probe_pull_request(
    client: GitHubClient, owner: str, repo: str, *, branch: str, base_branch: str, run_id: str
) -> ProbeResult:
    open_response = client.post(
        f"/repos/{owner}/{repo}/pulls",
        json={
            "title": f"coding-agent preflight probe {run_id}",
            "head": branch,
            "base": base_branch,
            "body": "Opened and closed by `agent preflight` (S0b). Never merged.",
        },
    )
    if open_response.status_code != 201:
        return ProbeResult(
            "pull request opened and closed",
            False,
            f"open returned {open_response.status_code}: {open_response.text}",
        )
    number = open_response.json()["number"]

    close_response = client.patch(f"/repos/{owner}/{repo}/pulls/{number}", json={"state": "closed"})
    if close_response.status_code != 200 or close_response.json().get("state") != "closed":
        return ProbeResult(
            "pull request opened and closed",
            False,
            f"close returned {close_response.status_code}: {close_response.text}",
        )
    return ProbeResult("pull request opened and closed", True, f"PR #{number} opened and closed")


def probe_workflows_write_rejected(
    client: GitHubClient, owner: str, repo: str, *, branch: str, run_id: str
) -> ProbeResult:
    response = _put_contents(
        client,
        owner,
        repo,
        WORKFLOWS_PROBE_PATH,
        branch=branch,
        message="coding-agent preflight probe: workflows write (must be rejected)",
        content=f"# preflight probe {run_id}, never expected to land\n".encode(),
    )
    if response.status_code == 403:
        return ProbeResult("workflows write rejected", True, "rejected with 403")
    if 200 <= response.status_code < 300:
        return ProbeResult(
            "workflows write rejected",
            False,
            "the write SUCCEEDED: the Workflows permission boundary is not enforced "
            "by the platform for this token — see contract §10, 'the one branch worth "
            "planning for'. The Worker's own refusal by write kind and target path is "
            "now the only line of defence.",
        )
    if response.status_code == 401:
        return ProbeResult(
            "workflows write rejected",
            False,
            "rejected with 401 — that is a broken credential, not the Workflows "
            "permission boundary; this probe cannot tell the two apart from a 403, "
            "so treat this as inconclusive and re-run once the credential works "
            f"(response: {response.text})",
        )
    return ProbeResult(
        "workflows write rejected",
        False,
        f"rejected, but with an unexpected status {response.status_code}: {response.text}",
    )


def run_preflight(client: GitHubClient, owner: str, repo: str) -> PreflightReport:
    """Every write probe contract §10's Preflight subsection lists.

    Run against a sandbox Target Repository, never at Worker startup.
    """
    report = PreflightReport()
    run_id = uuid.uuid4().hex[:8]

    try:
        base_branch, base_sha = _default_branch_head(client, owner, repo)
    except PreflightError as exc:
        report.add(ProbeResult("connectivity", False, str(exc)))
        return report

    issue_number: int | None = None
    try:
        issue_number = _open_probe_issue(client, owner, repo, run_id)
    except PreflightError as exc:
        report.add(ProbeResult("issue comment posted", False, str(exc)))
        report.add(ProbeResult("label added and removed", False, "skipped: no probe issue"))
    else:
        report.add(probe_issue_comment(client, owner, repo, issue_number))
        report.add(probe_label_add_remove(client, owner, repo, issue_number))

    branch = f"agent-preflight/{run_id}"
    branch_result = probe_branch_push(client, owner, repo, branch=branch, base_sha=base_sha, run_id=run_id)
    report.add(branch_result)

    if branch_result.passed:
        report.add(
            probe_pull_request(client, owner, repo, branch=branch, base_branch=base_branch, run_id=run_id)
        )
        report.add(probe_workflows_write_rejected(client, owner, repo, branch=branch, run_id=run_id))
    else:
        report.add(ProbeResult("pull request opened and closed", False, "skipped: no branch"))
        report.add(ProbeResult("workflows write rejected", False, "skipped: no branch"))

    if issue_number is not None:
        _close_probe_issue(client, owner, repo, issue_number, report)

    return report
