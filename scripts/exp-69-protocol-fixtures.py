"""Render the experiment protocol's ``fixtures`` section for #69.

Reads ``fixtures/exp-69/manifest.yaml`` and prints, as YAML on stdout, the
``fixtures`` list the experiment harness's protocol parser expects: each
fixture's Target Issue number, its frozen issue-body digest, the Base
Revision commit, its oracle command and its oracle files with their content
inlined. Paste the output into the protocol document (#69).

Refuses, with exit status 2, while the manifest still lacks a value that is
only known once the Base Revision tag and the Target Issues exist in the
sandbox Target Repository.

Usage: uv run python scripts/exp-69-protocol-fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

EXP_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "exp-69"


class _LiteralDumper(yaml.SafeDumper):
    """Dumps multi-line strings as literal blocks, so oracle files stay readable."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_LiteralDumper.add_representer(str, _represent_str)


class NotYetRecorded(ValueError):
    """The manifest still lacks a value only the sandbox can supply."""


def render(manifest: dict[str, Any]) -> dict[str, Any]:
    """The protocol ``fixtures`` section for ``manifest``."""
    base_commit = manifest["base"]["commit"]
    missing: list[str] = []
    if not base_commit:
        missing.append("base.commit")
    fixtures: list[dict[str, Any]] = []
    for fixture in manifest["fixtures"]:
        for key in ("issue_number", "issue_body_digest"):
            if not fixture.get(key):
                missing.append(f"{fixture['id']}.{key}")
        fixtures.append(
            {
                "id": fixture["id"],
                "issue_number": fixture["issue_number"],
                "issue_body_digest": fixture["issue_body_digest"],
                "base_revision": base_commit,
                "oracle": list(fixture["oracle"]["command"]),
                "oracle_files": {
                    published: (EXP_DIR / source).read_text(encoding="utf-8")
                    for published, source in fixture["oracle"]["files"].items()
                },
            }
        )
    if missing:
        raise NotYetRecorded(f"manifest not yet recorded: {', '.join(missing)}")
    return {"fixtures": fixtures}


def main() -> int:
    manifest = yaml.safe_load((EXP_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    try:
        section = render(manifest)
    except NotYetRecorded as exc:
        print(exc, file=sys.stderr)
        return 2
    sys.stdout.write(
        yaml.dump(section, Dumper=_LiteralDumper, sort_keys=False, width=100)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
