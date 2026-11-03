"""The process environment never leaks between tests.

``cli.main(["--app-env-file", ...])`` loads env files straight into
``os.environ`` via ``setdefault``. ``monkeypatch.delenv(name,
raising=False)`` on an unset variable records no undo entry, so without the
autouse ``_isolated_environ`` fixture in ``tests/conftest.py`` the loaded
value survives into every later test module. The first test below exercises
that exact leak path; the second runs after it in the same session and
proves nothing survived.
"""

import os
from pathlib import Path

import pytest

from coding_agent import cli


def test_app_env_file_values_are_visible_within_the_loading_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First half of the pair: run the real leak path.

    With ``GITHUB_TOKEN`` unset (as in the minimal gate environment),
    loading an app env file through ``cli.main`` writes the file's values
    into ``os.environ``. This test asserts they arrived, so a future change
    that stops loading env files cannot silently turn the second test
    vacuous.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "GITHUB_TOKEN=from-custom-file\nAGENT_TEST_ISOLATION_PROBE=leaked-value\n"
    )

    def fake_run_preflight_command(owner: str, repo: str, token: str) -> int:
        assert token == "from-custom-file"
        return 0

    monkeypatch.setattr(cli, "run_preflight_command", fake_run_preflight_command)

    assert (
        cli.main(
            ["--app-env-file", str(env_file), "preflight", "--repo", "octocat/sandbox"]
        )
        == 0
    )
    assert os.environ["GITHUB_TOKEN"] == "from-custom-file"
    assert os.environ["AGENT_TEST_ISOLATION_PROBE"] == "leaked-value"


def test_app_env_file_values_do_not_survive_into_the_next_test() -> None:
    """Second half of the pair: nothing the previous test loaded remains.

    The ``GITHUB_TOKEN`` assertion compares against the leaked value rather
    than asserting absence, so this also passes in a developer shell that
    carries a real token: whatever was there before the previous test is
    what is here now, never ``from-custom-file``.
    """
    assert os.environ.get("GITHUB_TOKEN") != "from-custom-file"
    assert "AGENT_TEST_ISOLATION_PROBE" not in os.environ
