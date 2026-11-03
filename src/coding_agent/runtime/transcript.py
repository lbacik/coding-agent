"""The per-Attempt model transcript: every request and response, redacted.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (a claim return names
what failed; the transcript is what lets an operator see which check
returned a live claim) and §18 (registered secrets are replaced by
``«redacted:<name>»`` before anything is written).

The transcript wraps the Pinned Model at its port (``InvokableToolModel``),
so every conversation that goes through it is recorded with no change to
the step or review graphs: the implement step and both reviewer seats. Each
request records the messages that are new since the conversation's last
assistant turn (the whole opening list on a conversation's first request,
then the tool results and runtime messages that follow each response), and
each response is recorded as returned. A tool result reaches the transcript
with the request that carries it, so the result of a conversation's last
call (an accepted exit, which no request follows) is not in it; the ledger
records the outcome that call led to.

The file is JSON Lines, one record per line, under the Attempt's evidence
directory, which the model's file tools cannot reach. The transcript is
evidence for an operator, never input to the Attempt: nothing reads it back.

MVP row ids: L3-ERR-11, C-IMP-1.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.pinned_model import InvokableToolModel

TRANSCRIPT_FILE_NAME = "transcript.jsonl"
"""The transcript's file name inside the Attempt's evidence directory."""


class TranscriptWriter:
    """Appends redacted JSON records to one transcript file.

    ``redactions`` maps a secret's registered name to its value. Each value
    is replaced by ``«redacted:<name>»`` in the serialized line, in both its
    raw and its JSON-escaped form, before the line is written (§18).
    """

    def __init__(self, path: Path, redactions: Mapping[str, str] | None = None) -> None:
        self._path = path
        self._redactions = {name: value for name, value in (redactions or {}).items() if value}
        self._sequence = 0

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: Mapping[str, object]) -> None:
        self._sequence += 1
        stamped = {
            "seq": self._sequence,
            "at": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        line = self._redact(json.dumps(stamped, default=str, ensure_ascii=False))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _redact(self, line: str) -> str:
        for name, secret in self._redactions.items():
            marker = f"«redacted:{name}»"
            line = line.replace(secret, marker)
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            if escaped != secret:
                line = line.replace(escaped, marker)
        return line


def serialize_message(message: BaseMessage) -> dict[str, object]:
    """One message as a JSON-ready record: its type, content and calls."""
    record: dict[str, object] = {"type": message.type, "content": message.content}
    if isinstance(message, AIMessage):
        record["tool_calls"] = [
            {"name": call.get("name"), "id": call.get("id"), "args": call.get("args")}
            for call in message.tool_calls
        ]
        usage = getattr(message, "usage_metadata", None)
        if usage is not None:
            record["usage"] = usage
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id is not None:
        record["tool_call_id"] = tool_call_id
    return record


def _new_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """The messages after the last assistant turn, or all where there is none."""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AIMessage):
            return list(messages[index + 1 :])
    return list(messages)


class TranscriptModel:
    """An ``InvokableToolModel`` that records each request and response.

    ``bind_tools`` binds the wrapped model and returns a recording wrapper
    around the bound one, so every conversation built on it is recorded.
    The response is returned exactly as the wrapped model returned it, never
    rebuilt (ADR 0010).
    """

    def __init__(
        self,
        inner: InvokableToolModel,
        writer: TranscriptWriter,
        *,
        tool_names: tuple[str, ...] = (),
        tool_choice: str | None = None,
    ) -> None:
        self._inner = inner
        self._writer = writer
        self._tool_names = tool_names
        self._tool_choice = tool_choice

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> TranscriptModel:
        if tool_choice is None:
            bound = self._inner.bind_tools(tools)
        else:
            bound = self._inner.bind_tools(tools, tool_choice=tool_choice)
        return TranscriptModel(
            bound,
            self._writer,
            tool_names=tuple(tool.name for tool in tools),
            tool_choice=tool_choice,
        )

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        request: dict[str, Any] = {
            "kind": "request",
            "tools": list(self._tool_names),
            "tool_choice": self._tool_choice,
        }
        if isinstance(input, Sequence) and not isinstance(input, str):
            messages = [m for m in input if isinstance(m, BaseMessage)]
            request["message_count"] = len(messages)
            request["new_messages"] = [serialize_message(m) for m in _new_messages(messages)]
        else:
            request["input"] = input
        self._writer.write(request)
        try:
            response = self._inner.invoke(input)
        except Exception as exc:
            self._writer.write({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
            raise
        self._writer.write({"kind": "response", "message": serialize_message(response)})
        return response
