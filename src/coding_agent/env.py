from __future__ import annotations

import os
from pathlib import Path


class EnvFileError(Exception):
    """An explicitly named env file could not be loaded."""


def _apply_env_file_text(text: str) -> None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_dotenv(path: Path | str = ".env") -> None:
    """Populate unset environment variables from a simple `KEY=VALUE` file.

    Convenience for local/operator use (the S0a runbook's `.env` step);
    never used by the containerised Worker, which gets its credential from
    the deployment environment directly. Existing environment variables are
    never overridden. A missing default file is a silent no-op; for an
    explicitly named file, use `load_required_env_file` instead, which
    raises rather than proceeding silently.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return
    _apply_env_file_text(file_path.read_text())


def load_required_env_file(path: Path | str) -> None:
    """Populate unset environment variables from an explicitly named file.

    Unlike `load_dotenv`'s implicit default, a file named explicitly (the
    CLI's `--env-file`) is expected to exist and be readable; a missing or
    unreadable file raises `EnvFileError` instead of silently proceeding, so
    a mistyped path in a PyCharm run configuration surfaces as a clear
    error rather than a mysterious later failure.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise EnvFileError(f"env file not found: {file_path}")
    try:
        text = file_path.read_text()
    except OSError as exc:
        raise EnvFileError(f"cannot read env file {file_path}: {exc}") from exc
    _apply_env_file_text(text)


def load_application_environment(app_env_file: Path | str | None = None) -> None:
    """Load local application configuration without overriding process values.

    The implicit ``.env`` supplies shared local configuration. An explicitly
    selected application file then supplements it, which lets an IDE provide
    service-specific settings without losing credentials kept in ``.env``.
    Variables supplied by the launcher, ``.env``, and the application file
    retain their first value in that order.
    """
    load_dotenv()
    if app_env_file is not None:
        load_required_env_file(app_env_file)
