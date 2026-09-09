from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | str = ".env") -> None:
    """Populate unset environment variables from a simple `KEY=VALUE` file.

    Convenience for local/operator use (the S0a runbook's `.env` step);
    never used by the containerised Worker, which gets its credential from
    the deployment environment directly. Existing environment variables are
    never overridden.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return

    for raw_line in file_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
