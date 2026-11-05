"""Oracle-access detection for the declared-completion experiment (#69).

Scans a harness run's Transcript for signs the run reached this repository
or a hidden oracle, so the harness can void the run instead of scoring it.

The scan reads only what the model issued and what came back:

- every tool-call argument in every model response;
- every tool result (``type == "tool"`` messages carried by requests).

It never reads text the runtime supplied: opening prompts, the Target Issue
body, the Skill Bundle, or runtime messages (all ``HumanMessage`` content in
requests is skipped, including silence continuations).

Signal set:

- ``lbacik/coding-agent``, case-insensitive, never matching
  ``lbacik/coding-agent-sandbox`` (negative lookahead on ``-sandbox``);
- ``fixtures/exp-69`` (literal);
- the file name (basename) of every oracle file in the protocol.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_SIGNAL = "lbacik/coding-agent"
"""The repository signal: matched case-insensitively, never with ``-sandbox``."""

FIXTURES_SIGNAL = "fixtures/exp-69"
"""The fixtures-tree signal: a literal substring."""

_REPO_PATTERN = re.compile(r"lbacik/coding-agent(?!-sandbox)", re.IGNORECASE)
"""``REPO_SIGNAL`` case-insensitively, not followed by ``-sandbox``."""

_EXCERPT_WINDOW = 60
"""Chars of context kept on each side of a match in its excerpt."""


def oracle_file_signal_names(oracle_files: Mapping[str, str]) -> list[str]:
    """Basename signals for one fixture's ``oracle_files`` mapping."""
    names: list[str] = []
    for published in oracle_files:
        base = Path(published).name
        if base and base not in names:
            names.append(base)
    return names


def oracle_access_signals(protocol: Any) -> list[str]:
    """The full signal set for ``protocol``: repo, fixtures tree, oracle names.

    Takes the experiment protocol (duck-typed: needs only ``.fixtures`` with
    ``.oracle_files`` mappings) so this stays importable without the protocol
    module at type-check time.
    """
    signals: list[str] = [REPO_SIGNAL, FIXTURES_SIGNAL]
    for fixture in protocol.fixtures:
        for name in oracle_file_signal_names(fixture.oracle_files):
            if name not in signals:
                signals.append(name)
    return signals


def transcript_sha256(path: Path) -> str:
    """SHA-256 hex digest of the Transcript file at ``path``."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _excerpt(text: str, start: int, end: int) -> str:
    before = max(0, start - _EXCERPT_WINDOW)
    after = min(len(text), end + _EXCERPT_WINDOW)
    snippet = text[before:after].replace("\n", " ")
    if len(snippet) > 2 * _EXCERPT_WINDOW + 80:
        snippet = snippet[: 2 * _EXCERPT_WINDOW + 80] + "…"
    prefix = "…" if before > 0 else ""
    suffix = "…" if after < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _match_positions(text: str, signal: str) -> list[tuple[int, int]]:
    if signal == REPO_SIGNAL:
        return [(m.start(), m.end()) for m in _REPO_PATTERN.finditer(text)]
    index = text.find(signal)
    positions: list[tuple[int, int]] = []
    while index != -1:
        positions.append((index, index + len(signal)))
        index = text.find(signal, index + 1)
    return positions


def _text_of(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def scan_transcript(path: Path, signals: Sequence[str]) -> list[dict[str, Any]]:
    """Scan the Transcript at ``path`` for ``signals``.

    Returns one match per (record, signal, field): the signal, the
    conversation number, the record ``seq`` (its position), whether the hit
    was in a tool-call argument or a tool result, and a short excerpt.
    Raises ``OSError`` where the file cannot be read and ``ValueError``
    where a line is not JSON.
    """
    matches: list[dict[str, Any]] = []
    conversation = 0
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        seq = record.get("seq")
        position = seq if isinstance(seq, int) else None
        if kind == "request":
            new_messages = record.get("new_messages")
            message_count = record.get("message_count")
            if (
                isinstance(message_count, int)
                and isinstance(new_messages, list)
                and message_count == len(new_messages)
            ):
                conversation += 1
            elif conversation == 0:
                conversation = 1
            if not isinstance(new_messages, list):
                continue
            for message in new_messages:
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "tool":
                    continue
                content = _text_of(message.get("content"))
                if content is None:
                    continue
                for signal in signals:
                    for start, end in _match_positions(content, signal):
                        matches.append(
                            {
                                "signal": signal,
                                "conversation": conversation,
                                "seq": position,
                                "kind": "tool_result",
                                "excerpt": _excerpt(content, start, end),
                            }
                        )
        elif kind == "response":
            if conversation == 0:
                conversation = 1
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                args_text = _text_of(call.get("args"))
                if args_text is None:
                    continue
                for signal in signals:
                    for start, end in _match_positions(args_text, signal):
                        matches.append(
                            {
                                "signal": signal,
                                "conversation": conversation,
                                "seq": position,
                                "kind": "tool_call",
                                "excerpt": _excerpt(args_text, start, end),
                            }
                        )
        else:
            continue
    return matches


def build_oracle_access_check(
    transcript_path: Path, signals: Sequence[str]
) -> dict[str, Any]:
    """The per-run ``oracle_access_check`` block for ``transcript_path``.

    ``{"checked": True, "transcript_digest": sha256, "matches": [...]}``
    where the file scans cleanly; ``{"checked": False,
    "transcript_digest": None, "matches": []}`` where it is missing or
    unreadable, so a run that cannot be shown clean is never scored.
    """
    try:
        digest = transcript_sha256(transcript_path)
        matches = scan_transcript(transcript_path, signals)
    except (OSError, ValueError, UnicodeDecodeError):
        return {"checked": False, "transcript_digest": None, "matches": []}
    return {
        "checked": True,
        "transcript_digest": digest,
        "matches": matches,
    }
