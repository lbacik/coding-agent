import re
from typing import Any

from coding_agent.github.client import GitHubClient
from coding_agent.preflight.probes import (
    WORKFLOWS_PROBE_PATH,
    probe_workflows_write_rejected,
    run_preflight,
)

TEST_BASE_URL = "https://api.github.test"
OWNER, REPO = "octocat", "sandbox"


def _register_common(requests_mock: Any) -> None:
    requests_mock.get(f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}", json={"default_branch": "main"})
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "basesha123"}},
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/issues", json={"number": 42}, status_code=201
    )
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/issues/42/comments", json={"id": 1}, status_code=201
    )
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/labels/coding-agent-preflight", status_code=404
    )
    requests_mock.post(f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/labels", status_code=201, json={})
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/issues/42/labels",
        json=[{"name": "coding-agent-preflight"}],
        status_code=200,
    )
    requests_mock.delete(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/issues/42/labels/coding-agent-preflight",
        status_code=200,
        json=[],
    )
    requests_mock.post(f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/git/refs", status_code=201, json={})
    requests_mock.post(f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/pulls", json={"number": 7}, status_code=201)
    requests_mock.patch(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/pulls/7", json={"state": "closed"}, status_code=200
    )
    requests_mock.patch(f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/issues/42", status_code=200, json={})


def _register_contents(requests_mock: Any, *, reject_workflows: bool) -> None:
    def contents_callback(request: Any, context: Any) -> dict[str, Any]:
        if ".github/workflows/" in request.path_url:
            if reject_workflows:
                context.status_code = 403
                return {"message": "workflow scope required"}
            context.status_code = 201
            return {"commit": {"sha": "shouldnothavelanded"}}
        context.status_code = 201
        return {"commit": {"sha": "deadbeef"}}

    requests_mock.put(
        re.compile(rf"{re.escape(TEST_BASE_URL)}/repos/{OWNER}/{REPO}/contents/.*"),
        json=contents_callback,
    )


def test_run_preflight_happy_path(client: GitHubClient, requests_mock: Any) -> None:
    _register_common(requests_mock)
    _register_contents(requests_mock, reject_workflows=True)

    report = run_preflight(client, OWNER, REPO)

    assert report.ok, report.results
    assert [r.name for r in report.results] == [
        "issue comment posted",
        "label added and removed",
        "branch pushed",
        "pull request opened and closed",
        "workflows write rejected",
    ]


def test_run_preflight_flags_unrejected_workflows_write(client: GitHubClient, requests_mock: Any) -> None:
    _register_common(requests_mock)
    _register_contents(requests_mock, reject_workflows=False)

    report = run_preflight(client, OWNER, REPO)

    assert not report.ok
    workflows_result = next(r for r in report.results if r.name == "workflows write rejected")
    assert workflows_result.passed is False
    assert "SUCCEEDED" in workflows_result.detail


def test_run_preflight_skips_dependents_when_branch_push_fails(
    client: GitHubClient, requests_mock: Any
) -> None:
    _register_common(requests_mock)
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/git/refs", status_code=422, json={"message": "exists"}
    )

    report = run_preflight(client, OWNER, REPO)

    assert not report.ok
    by_name = {r.name: r for r in report.results}
    assert by_name["branch pushed"].passed is False
    assert by_name["pull request opened and closed"].detail == "skipped: no branch"
    assert by_name["workflows write rejected"].detail == "skipped: no branch"


def test_run_preflight_records_connectivity_failure(client: GitHubClient, requests_mock: Any) -> None:
    requests_mock.get(f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}", status_code=404, json={"message": "Not Found"})

    report = run_preflight(client, OWNER, REPO)

    assert not report.ok
    assert report.results[0].name == "connectivity"


def test_probe_workflows_write_rejected_treats_401_as_inconclusive_not_a_pass(
    client: GitHubClient, requests_mock: Any
) -> None:
    requests_mock.put(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/contents/{WORKFLOWS_PROBE_PATH}",
        status_code=401,
        json={"message": "Bad credentials"},
    )

    result = probe_workflows_write_rejected(client, OWNER, REPO, branch="agent-preflight/x", run_id="x")

    assert result.passed is False
    assert "401" in result.detail
    assert "not the Workflows permission boundary" in result.detail


def test_run_preflight_bootstraps_an_empty_repository(client: GitHubClient, requests_mock: Any) -> None:
    _register_common(requests_mock)
    _register_contents(requests_mock, reject_workflows=True)
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/{OWNER}/{REPO}/git/ref/heads/main",
        status_code=409,
        json={"message": "Git Repository is empty."},
    )

    report = run_preflight(client, OWNER, REPO)

    assert report.ok, report.results
