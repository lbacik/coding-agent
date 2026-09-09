import argparse

import pytest

from coding_agent import cli


def test_split_repo_valid() -> None:
    assert cli.split_repo("octocat/sandbox") == ("octocat", "sandbox")


@pytest.mark.parametrize("value", ["", "no-slash", "/repo", "owner/"])
def test_split_repo_rejects_malformed(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.split_repo(value)


def test_load_token_missing_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_TOKEN_VAR", raising=False)
    with pytest.raises(SystemExit):
        cli.load_token("SOME_TOKEN_VAR")


def test_load_token_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_TOKEN_VAR", "github_pat_abc")
    assert cli.load_token("SOME_TOKEN_VAR") == "github_pat_abc"


def test_main_dispatches_to_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")
    calls: list[tuple[str, str, str]] = []

    def fake_run_preflight_command(owner: str, repo: str, token: str) -> int:
        calls.append((owner, repo, token))
        return 0

    monkeypatch.setattr(cli, "run_preflight_command", fake_run_preflight_command)

    exit_code = cli.main(["preflight", "--repo", "octocat/sandbox"])

    assert exit_code == 0
    assert calls == [("octocat", "sandbox", "github_pat_abc")]


def test_main_dispatches_to_startup_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_TOKEN", "github_pat_abc")
    calls: list[tuple[str, str, str]] = []

    def fake_run_startup_check_command(owner: str, repo: str, token: str) -> int:
        calls.append((owner, repo, token))
        return 1

    monkeypatch.setattr(cli, "run_startup_check_command", fake_run_startup_check_command)

    exit_code = cli.main(
        ["startup-check", "--repo", "octocat/sandbox", "--token-env", "SANDBOX_TOKEN"]
    )

    assert exit_code == 1
    assert calls == [("octocat", "sandbox", "github_pat_abc")]


def test_main_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
