"""The redaction registry: the one choke point for every secret.

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (credentials pass
through one choke point: loaded into a redaction registry at startup,
filtered out of both log streams and out of tool arguments and results,
never present in the environment of child processes, and used for git
through a credential helper rather than a remote URL; registered secrets
are replaced by ``«redacted:<name>»``; the environment is never logged; git
remote URLs never carry the token).

Every output surface funnels through :class:`SecretRegistry`: the
transcript writer, the targeted-diagnostics excerpts, tool arguments and
results, ledger records, exception messages and tracebacks, and both log
streams (via :func:`install_stream_redaction`). There is no second
redaction path: the former per-module replacements (``str.replace`` with
``"***"``, per-module loops) delegate here.

MVP row ids: L3-ERR-11, L3-ERR-12.
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, overload


def redaction_marker(name: str) -> str:
    """The replacement text for a registered secret (§18, L3-ERR-11)."""
    return f"«redacted:{name}»"


class SecretRegistry:
    """The registered secrets and the one replacement rule applied to all of them.

    Maps each secret's registered name (an environment-variable name such as
    ``GITHUB_TOKEN``) to its value. :meth:`redact` replaces every non-empty
    value with ``«redacted:<name>»``; values are applied longest-first so
    overlapping secrets redact deterministically. ``"credential"`` is the
    conventional name for a bare value that arrives without one.
    """

    CREDENTIAL_NAME = "credential"

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        ordered: dict[str, str] = {}
        for name, value in (secrets or {}).items():
            if name and value:
                ordered[name] = value
        self._secrets = tuple(
            sorted(ordered.items(), key=lambda item: (-len(item[1]), item[0]))
        )

    @classmethod
    def from_mapping(cls, secrets: Mapping[str, str] | None) -> SecretRegistry:
        """Build a registry from a name-to-value mapping (§18)."""
        if isinstance(secrets, SecretRegistry):
            return secrets
        return cls(dict(secrets or {}))

    @classmethod
    def from_environment(
        cls,
        names: Iterable[str] | SecretRegistry | None,
        env: Mapping[str, str] | None = None,
    ) -> SecretRegistry:
        """Build a registry by reading secret values from the environment.

        ``names`` is the registered secret names (or a registry, returned
        unchanged); only names that are set and non-empty in ``env`` (the
        live environment by default) enter the registry. The environment
        itself is never logged or copied anywhere by this helper — values
        land only in the registry.
        """
        if isinstance(names, SecretRegistry):
            return names
        if names is None:
            return cls({})
        source = os.environ if env is None else env
        found: dict[str, str] = {}
        for name in names:
            value = source.get(str(name))
            if value:
                found[str(name)] = value
        return cls(found)

    @property
    def names(self) -> tuple[str, ...]:
        """The registered secret names, longest value first."""
        return tuple(name for name, _ in self._secrets)

    @property
    def secrets(self) -> Mapping[str, str]:
        """The registered name-to-value mapping (a copy)."""
        return dict(self._secrets)

    def as_mapping(self) -> dict[str, str]:
        """The registry as a plain name-to-value mapping."""
        return dict(self._secrets)

    def __len__(self) -> int:
        return len(self._secrets)

    def __bool__(self) -> bool:
        return bool(self._secrets)

    def _replacements(self) -> Iterator[tuple[str, str]]:
        """Each secret value and its replacement marker (§18, L3-ERR-11)."""
        for name, secret in self._secrets:
            marker = redaction_marker(name)
            yield secret, marker
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            if escaped != secret:
                yield escaped, marker

    def redact(self, text: str) -> str:
        """Replace every registered secret in ``text`` with its marker.

        The one string redaction every output surface uses (§18,
        L3-ERR-11): both the raw value and its JSON-escaped form (for text
        already serialized to JSON) are replaced. Non-string input is
        returned unchanged so callers can map this over mixed payloads.
        """
        if not isinstance(text, str) or not self._secrets:
            return text
        redacted = text
        for secret, marker in self._replacements():
            if secret and secret in redacted:
                redacted = redacted.replace(secret, marker)
        return redacted

    @overload
    def redact_json(self, value: str) -> str: ...
    @overload
    def redact_json(self, value: Mapping[str, object]) -> dict[str, object]: ...
    @overload
    def redact_json(self, value: object) -> object: ...
    def redact_json(self, value: object) -> object:
        """Replace every registered secret in a JSON-shaped value.

        Walks mappings (keys and values), sequences and strings; anything
        else passes through. Ledger payloads and Continuation Snapshots are
        redacted in structured form before the post-redaction size cap is
        enforced, so a marker is never cut in half.
        """
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {
                self.redact(str(key)): self.redact_json(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact_json(item) for item in value]
        return value

    def scrub_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """A child-process environment with no registered secret in it.

        Drops every variable whose name is registered and every variable
        whose value contains a registered secret value (§18, L3-ERR-12).
        The input mapping is never mutated.
        """
        if not self._secrets:
            return dict(env)
        cleaned: dict[str, str] = {}
        for key, value in env.items():
            if key in self.as_mapping():
                continue
            if any(secret in value for _, secret in self._secrets):
                continue
            cleaned[key] = value
        return cleaned

    def contains_secret(self, text: str) -> str | None:
        """The first registered secret value found in ``text``, if any."""
        for _, secret in self._secrets:
            if secret and secret in text:
                return secret
        return None


Redactable = str | Mapping[str, str] | SecretRegistry | None
"""Anything a ``redact=``/``redactions=`` parameter accepts (§18).

A bare string is the unnamed-credential shorthand (named
``"credential"``); a mapping is the name-to-value registry in plain form;
a :class:`SecretRegistry` is used directly; ``None`` means no redaction.
Every form funnels into :func:`registry_from`, so there is exactly one
replacement implementation.
"""


def registry_from(redact: Redactable) -> SecretRegistry:
    """Normalize any ``redact=``/``redactions=`` argument to one registry.

    The single funnel every ad-hoc redaction call site delegates to (§18):
    no caller keeps its own replacement loop.
    """
    if isinstance(redact, SecretRegistry):
        return redact
    if isinstance(redact, str):
        return SecretRegistry(
            {SecretRegistry.CREDENTIAL_NAME: redact} if redact else {}
        )
    return SecretRegistry.from_mapping(redact)


def redact_text(text: str, redact: Redactable = None) -> str:
    """Redact ``text`` through the one registry (§18, L3-ERR-11)."""
    return registry_from(redact).redact(text)


def format_exception_redacted(exc: BaseException, redact: Redactable = None) -> str:
    """One exception as redacted text: no secret in the message (§18).

    Adapter errors embed response bodies (``GitHubTrackerError``,
    ``IssueFetchFailed``), so the message that reaches a log stream, a tool
    result or a ledger reason passes through the registry first.
    """
    return registry_from(redact).redact(f"{type(exc).__name__}: {exc}")


def format_traceback_redacted(exc: BaseException, redact: Redactable = None) -> str:
    """One exception's traceback as redacted text (§18, L3-ERR-11).

    Uncaught tracebacks reach the container log verbatim; this is the form
    the choke point gives them instead.
    """
    rendered = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    return registry_from(redact).redact(rendered)


class RedactingWriter:
    """A text stream that redacts every registered secret before writing.

    Wraps one log stream (stdout or stderr); everything printed through the
    wrapper — model output, tool results echoed by the runtime, exception
    text, tracebacks — passes through the registry (§18, L3-ERR-11). The
    environment is never logged because values, not the mapping, are what
    the wrapper matches.
    """

    def __init__(self, wrapped: TextIO, registry: SecretRegistry) -> None:
        self._wrapped = wrapped
        self._registry = registry

    @property
    def registry(self) -> SecretRegistry:
        """The registry this stream filters through (§18)."""
        return self._registry

    def write(self, text: str) -> int:
        """Write ``text`` with every registered secret replaced (§18)."""
        return self._wrapped.write(self._registry.redact(text))

    def writelines(self, lines: Iterable[str]) -> None:
        """Write each line with every registered secret replaced (§18)."""
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        """Flush the wrapped stream."""
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_wrapped"], name)


def install_stream_redaction(registry: SecretRegistry) -> Callable[[], None]:
    """Filter both log streams through the registry (§18, L3-ERR-11).

    Replaces ``sys.stdout`` and ``sys.stderr`` with redacting wrappers, so
    every ``print`` — to either stream — carries only
    ``«redacted:<name>»`` markers. Returns an uninstall callable restoring
    both streams; producers install once at startup and tests install
    around the scripted Attempt. ``PYTHONUNBUFFERED`` only changes when the
    bytes arrive, never what they carry.
    """
    import sys

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = RedactingWriter(original_stdout, registry)
    sys.stderr = RedactingWriter(original_stderr, registry)

    def uninstall() -> None:
        sys.stdout = original_stdout
        sys.stderr = original_stderr

    return uninstall


@dataclass(frozen=True)
class GitCredentialHelper:
    """Git authentication without a credentialed remote URL (§18).

    The helper owns a ``0600`` credential-store file holding the
    ``x-access-token`` entry; git commands authenticate through
    ``-c credential.helper=store --file <path>`` while the remote URL
    stays the canonical credential-free ``https://github.com/...`` form —
    in logs, in ``.git/config`` and in every failure message. The token
    never appears in an argv list.
    """

    store_file: Path
    """The credential-store file (``0600``), removed with the helper."""

    def config_args(self) -> list[str]:
        """The ``-c credential.helper=...`` argv prefix for git (§18)."""
        return ["-c", f"credential.helper=store --file {self.store_file}"]


@contextmanager
def temporary_git_credential_helper(
    token: str, *, host: str = "github.com"
) -> Iterator[GitCredentialHelper]:
    """Hold a short-lived git credential helper for one Attempt (§18).

    Writes the token to a ``0600`` store file outside the repository and
    yields the helper; the file is removed on exit. A token containing a
    newline is refused rather than written.
    """
    if not token:
        raise ValueError("temporary_git_credential_helper needs a non-empty token")
    if "\n" in token or "\r" in token:
        raise ValueError("token must not contain a newline")
    descriptor, tmp_name = tempfile.mkstemp(prefix="coding-agent-git-credentials-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"https://x-access-token:{token}@{host}\n")
        yield GitCredentialHelper(store_file=Path(tmp_name))
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def describe_stream() -> str:
    """A debug aid: which wrapper, if any, filters the log streams (§18)."""
    import sys

    def label(stream: object) -> str:
        if isinstance(stream, RedactingWriter):
            return f"redacting({','.join(stream.registry.names)})"
        return type(stream).__name__

    return f"stdout={label(sys.stdout)} stderr={label(sys.stderr)}"
