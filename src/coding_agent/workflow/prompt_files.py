"""Operator prompt files: raw-bytes reads with containment (§2.8).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.8 (trust and
paths: relative paths resolve against the YAML file's directory and must stay
inside it after symlink resolution; prompt content is used verbatim) and §2.9
(``agent config validate`` is static and offline).

Every prompt locator is read as raw bytes and decoded as UTF-8 strictly, so
CRLF line endings and non-ASCII content reach the model byte for byte, while
a missing, unreadable or invalid-UTF-8 file is a refusal naming the file —
never a traceback and never the file's contents.
"""

from __future__ import annotations

from pathlib import Path


class PromptFileError(OSError):
    """A prompt file that cannot be used (§2.8, §2.9).

    The message names the locator but never the file's contents, so it is
    safe to print in a refusal diagnostic. A subclass of ``OSError`` so
    startup callers catching ``OSError`` also refuse invalid UTF-8 without
    a traceback.
    """


def resolve_locator(base: Path, locator: str, *, owner: str) -> Path:
    """Resolve ``locator`` against ``base``, refusing escape (§2.8).

    The shared path rule behind every configuration-relative file: a
    contained symlink is accepted because resolution stays inside ``base``;
    a symlink or path escaping it is refused. Raises
    :class:`PromptFileError` naming the locator, never its contents.
    ``owner`` names the YAML key the locator was read from (``"prompt"``
    for operator prompts, ``"target.profile"`` for the instance-supplied
    Project Profile), so the refusal names the right path.
    """
    try:
        candidate = (base / locator).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise PromptFileError(
            f"{owner} file {locator!r} cannot be read: {exc}"
        ) from exc
    if candidate != base and base not in candidate.parents:
        raise PromptFileError(
            f"{owner} path {locator!r} escapes the configuration directory"
        )
    return candidate


def read_locator_text(base: Path, locator: str, *, owner: str) -> str:
    """Read one configuration-relative locator as strict UTF-8 text.

    Paths resolve against the configuration directory; a contained symlink
    is accepted, an escaping path or symlink is refused. CRLF line endings
    and non-ASCII content are preserved byte for byte (raw bytes decoded
    without newline translation). Missing, unreadable and invalid-UTF-8
    files raise :class:`PromptFileError` naming the locator, never the
    file's contents. ``owner`` names the YAML key the locator was read
    from, so the refusal names the right path.
    """
    candidate = resolve_locator(base, locator, owner=owner)
    try:
        if not candidate.is_file():
            raise PromptFileError(f"{owner} file {locator!r} does not exist")
    except PromptFileError:
        raise
    except (OSError, ValueError) as exc:
        raise PromptFileError(
            f"{owner} file {locator!r} cannot be read: {exc}"
        ) from exc
    try:
        raw = candidate.read_bytes()
    except FileNotFoundError as exc:
        raise PromptFileError(
            f"{owner} file {locator!r} does not exist"
        ) from exc
    except (OSError, ValueError) as exc:
        raise PromptFileError(
            f"{owner} file {locator!r} cannot be read: {exc}"
        ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptFileError(
            f"{owner} file {locator!r} is not valid UTF-8"
        ) from exc


def resolve_prompt_candidate(base: Path, locator: str) -> Path:
    """Resolve ``locator`` against ``base``, refusing escape (§2.8).

    A contained symlink is accepted because resolution stays inside ``base``;
    a symlink or path escaping it is refused. Raises :class:`PromptFileError`
    naming the locator, never its contents.
    """
    return resolve_locator(base, locator, owner="prompt")


def read_prompt_text(base: Path, locator: str) -> str:
    """Read one prompt locator as raw bytes, decoded as UTF-8 strictly.

    Paths resolve against the configuration directory; a contained symlink
    is accepted, an escaping path or symlink is refused. CRLF line endings
    and non-ASCII content are preserved byte for byte (raw bytes decoded
    without newline translation). Missing, unreadable and invalid-UTF-8
    files raise :class:`PromptFileError` naming the locator, never the
    file's contents.
    """
    return read_locator_text(base, locator, owner="prompt")
