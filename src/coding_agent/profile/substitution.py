from __future__ import annotations

from pathlib import Path

PATH_PLACEHOLDER = "{path}"
EVIDENCE_DIR_PLACEHOLDER = "{evidence_dir}"


class UnresolvedPlaceholder(ValueError):
    """A command or evidence-path template still names a placeholder after
    substitution — the caller did not supply a value it needed."""


def render_command(
    template: str,
    *,
    path: str | None = None,
    evidence_dir: Path | str | None = None,
) -> str:
    """Substitute `{path}` and `{evidence_dir}` in a profile-declared command
    or evidence-path template (contract §7).

    `{evidence_dir}` names a per-Attempt directory the harness creates and
    owns; the profile only names it, so the caller supplies the concrete
    path. Raises if a placeholder the template uses was not given a value —
    a caller bug, not a profile defect.
    """
    result = template
    if evidence_dir is not None:
        result = result.replace(EVIDENCE_DIR_PLACEHOLDER, str(evidence_dir))
    if path is not None:
        result = result.replace(PATH_PLACEHOLDER, path)

    for placeholder in (PATH_PLACEHOLDER, EVIDENCE_DIR_PLACEHOLDER):
        if placeholder in result:
            raise UnresolvedPlaceholder(f"{template!r} needs {placeholder}, but none was given")
    return result
