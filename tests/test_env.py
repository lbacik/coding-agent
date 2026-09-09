import os
from pathlib import Path

from coding_agent.env import load_dotenv


def test_load_dotenv_sets_unset_variables(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.delenv("DOTENV_TEST_VAR", raising=False)  # type: ignore[attr-defined]
    env_file = tmp_path / ".env"
    env_file.write_text('# a comment\nDOTENV_TEST_VAR="hello world"\n\nBARE=1\n')

    load_dotenv(env_file)

    assert os.environ["DOTENV_TEST_VAR"] == "hello world"
    assert os.environ["BARE"] == "1"
    del os.environ["DOTENV_TEST_VAR"]
    del os.environ["BARE"]


def test_load_dotenv_never_overrides_existing(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("DOTENV_TEST_VAR", "already-set")  # type: ignore[attr-defined]
    env_file = tmp_path / ".env"
    env_file.write_text("DOTENV_TEST_VAR=from-file\n")

    load_dotenv(env_file)

    assert os.environ["DOTENV_TEST_VAR"] == "already-set"


def test_load_dotenv_missing_file_is_a_noop(tmp_path: Path) -> None:
    load_dotenv(tmp_path / "does-not-exist.env")
