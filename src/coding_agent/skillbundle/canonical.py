"""Canonical JSON for generated Skill Bundle records.

Contract: the installation resolution
(issue #373, ``requirements.json``/``manifest.json``) and the phased-validation
resolution (issue #375, ``validation.json``): UTF-8 without BOM or trailing
newline, object keys sorted by Unicode code point, no insignificant
whitespace, literal non-ASCII characters, standard JSON string escapes,
integer schema/version numbers and booleans; arrays retain their specified
order. No floats occur. Digest values are lowercase 64-hex SHA-256 strings.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def dumps_canonical(value: object) -> bytes:
    """Encode ``value`` as canonical JSON bytes.

    Keys sorted by Unicode code point, compact separators, literal non-ASCII
    (``ensure_ascii=False``), no trailing newline. Rejects floats (including
    ``bool``-masked ints confusion is safe: ``bool`` is allowed, ``float`` is
    not) so generated records never contain them.
    """
    _reject_floats(value)
    text = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Lowercase hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_canonical(value: object) -> str:
    """SHA-256 of the canonical JSON encoding of ``value``."""
    return sha256_bytes(dumps_canonical(value))


def is_hex64(value: object) -> bool:
    """Whether ``value`` is a lowercase 64-hex SHA-256 string."""
    return isinstance(value, str) and HEX64_RE.match(value) is not None


def is_hex40(value: object) -> bool:
    """Whether ``value`` is a lowercase 40-hex Git commit string."""
    return isinstance(value, str) and HEX40_RE.match(value) is not None


def _reject_floats(value: object) -> None:
    if isinstance(value, float):
        raise ValueError("floats are not allowed in canonical records")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _no_float(value: str) -> Any:
    raise ValueError("floats are not allowed in generated records")


def loads_strict(raw: bytes) -> tuple[Any | None, str | None]:
    """Parse ``raw`` JSON bytes strictly.

    Returns ``(value, None)`` on success or ``(None, error)`` where ``error``
    names the problem without echoing file contents. Rejects invalid UTF-8,
    malformed JSON, duplicate keys and floats.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"is not valid UTF-8: {exc}"
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=_no_float,
        )
    except ValueError as exc:
        return None, f"does not parse: {exc}"
    return value, None


def contains_null(value: object) -> bool:
    """Whether ``value`` contains a JSON null anywhere."""
    if value is None:
        return True
    if isinstance(value, dict):
        return any(contains_null(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_null(item) for item in value)
    return False
