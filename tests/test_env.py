import os
from pathlib import Path

import pytest

from coding_agent.env import (
    EnvFileError,
    load_application_environment,
    load_dotenv,
    load_required_env_file,
)


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


def test_load_required_env_file_sets_unset_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REQUIRED_ENV_FILE_TEST_VAR", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text("REQUIRED_ENV_FILE_TEST_VAR=from-file\n")

    load_required_env_file(env_file)

    assert os.environ["REQUIRED_ENV_FILE_TEST_VAR"] == "from-file"
    del os.environ["REQUIRED_ENV_FILE_TEST_VAR"]


def test_load_required_env_file_never_overrides_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REQUIRED_ENV_FILE_TEST_VAR", "already-set")
    env_file = tmp_path / "custom.env"
    env_file.write_text("REQUIRED_ENV_FILE_TEST_VAR=from-file\n")

    load_required_env_file(env_file)

    assert os.environ["REQUIRED_ENV_FILE_TEST_VAR"] == "already-set"


def test_load_required_env_file_raises_on_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError, match="not found"):
        load_required_env_file(tmp_path / "does-not-exist.env")


def test_load_required_env_file_raises_on_a_directory(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError, match="not found"):
        load_required_env_file(tmp_path)


def test_load_application_environment_supplements_dotenv_with_app_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BASE_ONLY", raising=False)
    monkeypatch.delenv("APP_ONLY", raising=False)
    monkeypatch.delenv("SHARED", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BASE_ONLY=base\nSHARED=base\n")
    app_file = tmp_path / "app.env"
    app_file.write_text("APP_ONLY=app\nSHARED=app\n")

    load_application_environment(app_file)

    assert os.environ["BASE_ONLY"] == "base"
    assert os.environ["APP_ONLY"] == "app"
    assert os.environ["SHARED"] == "base"
    del os.environ["BASE_ONLY"]
    del os.environ["APP_ONLY"]
    del os.environ["SHARED"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="permission bits are not enforced")
def test_load_required_env_file_raises_on_an_unreadable_file(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("SOME_VAR=1\n")
    env_file.chmod(0o000)
    try:
        with pytest.raises(EnvFileError, match="cannot read"):
            load_required_env_file(env_file)
    finally:
        env_file.chmod(0o644)
