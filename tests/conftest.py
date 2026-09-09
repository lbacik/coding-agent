import pytest

from coding_agent.github.client import GitHubClient

TEST_BASE_URL = "https://api.github.test"


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL)
